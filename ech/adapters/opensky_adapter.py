"""
ech/adapters/opensky_adapter.py
--------------------------------
ADS-B adapter: polls the OpenSky Network public REST API for aircraft
state vectors within a bounding box.  Complements adsb_adapter.py (which
reads a *local* dump1090/PiAware receiver) with wide-area coverage from
OpenSky's crowd-sourced global network.  No messages are added to the
feed (all packets have msg_type="position") so the inbox stays clean.

Config keys:
    bbox            [[min_lat, min_lon], [max_lat, max_lon]] — required.
                    Keep this reasonably small; OpenSky's anonymous quota
                    is shared across all requests regardless of bbox size.
    client_id       OpenSky OAuth2 client ID (optional — see below)
    client_secret   OpenSky OAuth2 client secret (optional)
    poll_interval   Poll cadence in seconds. Default 30 if authenticated,
                    floored at 300 if anonymous (OpenSky's anonymous quota
                    is ~400 requests/day; polling faster just burns it).
    stale_sec       Seconds without a position update before a node is
                    removed (default 120 — aircraft move fast)

Authentication:
    OpenSky's anonymous API access is heavily rate-limited. For real use,
    register a free account at https://opensky-network.org/ and create an
    API client (Account -> API Client) to get client_id/client_secret —
    this raises the quota substantially. Without credentials the adapter
    still works, just polls far less often.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
_STATES_URL = "https://opensky-network.org/api/states/all"

# Index positions within each element of the OpenSky "states" array —
# see https://openskynetwork.github.io/opensky-api/rest.html
_I_ICAO24     = 0
_I_CALLSIGN   = 1
_I_LON        = 5
_I_LAT        = 6
_I_BARO_ALT   = 7
_I_VELOCITY   = 9
_I_TRUE_TRACK = 10
_I_GEO_ALT    = 13
_I_SQUAWK     = 14


class OpenSkyAdapter(Adapter):
    """Read-only ADS-B receiver via the OpenSky Network REST API."""

    is_mock: bool = False
    send_enabled: bool = False

    def __init__(self, config: dict):
        super().__init__(config)
        bbox = config.get("bbox") or config.get("bounding_box")
        if not bbox:
            raise ValueError("OpenSky adapter requires 'bbox': [[min_lat,min_lon],[max_lat,max_lon]]")
        (self._lat_min, self._lon_min), (self._lat_max, self._lon_max) = bbox

        self._client_id     = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")
        self._authenticated  = bool(self._client_id and self._client_secret)

        default_poll = 30.0 if self._authenticated else 300.0
        poll = float(config.get("poll_interval", default_poll))
        if not self._authenticated and poll < 300.0:
            log.warning(
                "OpenSky %s: anonymous access is rate-limited — flooring poll_interval to 300s "
                "(register a free API client at opensky-network.org for faster polling)",
                self.name,
            )
            poll = 300.0
        self._poll = poll

        self._stale_sec = float(config.get("stale_sec", 120.0))
        self._nodes: dict[str, MeshNode] = {}
        self._run_task: asyncio.Task | None = None

        self._token: str | None = None
        self._token_expiry: float = 0.0

    # ── Adapter interface ──────────────────────────────────────────────────

    async def connect(self) -> None:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            raise ConnectionError("OpenSky adapter requires aiohttp — pip install aiohttp")
        if self._authenticated:
            await self._refresh_token()
        self._connected = True
        self._run_task  = asyncio.create_task(self._run())
        log.info(
            "OpenSky %s: connected, bbox=[%s,%s]-[%s,%s], auth=%s, poll=%.0fs",
            self.name, self._lat_min, self._lon_min, self._lat_max, self._lon_max,
            self._authenticated, self._poll,
        )

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("OpenSky %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        return False   # receive-only

    async def nodes(self) -> list[MeshNode]:
        cutoff = datetime.now(timezone.utc).timestamp() - self._stale_sec
        return [
            n for n in self._nodes.values()
            if n.last_heard and n.last_heard.timestamp() >= cutoff
        ]

    # ── Internal ──────────────────────────────────────────────────────────

    async def _refresh_token(self) -> None:
        import aiohttp
        data = {
            "grant_type":    "client_credentials",
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(_TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise ConnectionError(f"OpenSky OAuth2 token request failed: HTTP {resp.status}")
                body = await resp.json()
        self._token = body["access_token"]
        # Refresh a bit before actual expiry to avoid a race on the next poll
        self._token_expiry = time.monotonic() + max(30, int(body.get("expires_in", 1800)) - 30)

    async def _run(self) -> None:
        try:
            import aiohttp
        except ImportError:
            log.error("OpenSky %s: aiohttp is required — install with: pip install aiohttp", self.name)
            return

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession() as session:
            while self._connected:
                try:
                    if self._authenticated and time.monotonic() >= self._token_expiry:
                        await self._refresh_token()
                    headers = {"Authorization": f"Bearer {self._token}"} if self._authenticated else {}
                    params = {
                        "lamin": self._lat_min, "lomin": self._lon_min,
                        "lamax": self._lat_max, "lomax": self._lon_max,
                    }
                    async with session.get(_STATES_URL, params=params, headers=headers, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            await self._ingest(data)
                        elif resp.status == 429:
                            log.warning("OpenSky %s: rate limited (HTTP 429) — backing off", self.name)
                        else:
                            log.warning("OpenSky %s: HTTP %d from states/all", self.name, resp.status)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("OpenSky %s: poll error: %s", self.name, exc)

                await asyncio.sleep(self._poll)

    async def _ingest(self, data: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        seen: set[str] = set()

        for state in data.get("states") or []:
            icao24 = (state[_I_ICAO24] or "").lower().strip()
            if not icao24:
                continue
            lat = state[_I_LAT]
            lon = state[_I_LON]
            if lat is None or lon is None:
                continue

            node_id      = f"icao:{icao24}"
            callsign_raw = (state[_I_CALLSIGN] or "").strip()
            display_name = callsign_raw if callsign_raw else icao24.upper()

            baro_alt = state[_I_BARO_ALT]
            geo_alt  = state[_I_GEO_ALT]
            altitude = None
            if isinstance(baro_alt, (int, float)):
                altitude = int(baro_alt * 3.28084)   # meters -> feet
            elif isinstance(geo_alt, (int, float)):
                altitude = int(geo_alt * 3.28084)

            velocity   = state[_I_VELOCITY]     # m/s
            true_track = state[_I_TRUE_TRACK]   # degrees
            squawk     = state[_I_SQUAWK] or ""

            meta: dict[str, Any] = {"icao": icao24}
            if callsign_raw:
                meta["flight"] = callsign_raw
            if altitude is not None:
                meta["altitude_ft"] = altitude
            if velocity is not None:
                meta["speed_kts"] = round(float(velocity) * 1.94384, 1)   # m/s -> kts
            if true_track is not None:
                meta["track_deg"] = round(float(true_track), 1)
            if squawk:
                meta["squawk"] = squawk

            node = self._nodes.get(node_id)
            if node is None:
                node = MeshNode(
                    node_id=node_id,
                    display_name=display_name,
                    first_seen=now,
                )
                self._nodes[node_id] = node
            else:
                node.display_name = display_name
            node.last_heard = now
            node.lat   = float(lat)
            node.lon   = float(lon)
            node.meta  = meta
            seen.add(node_id)

            parts = [f"✈ {display_name}"]
            if altitude is not None:
                parts.append(f"{altitude:,}ft")
            if velocity is not None:
                parts.append(f"{round(float(velocity) * 1.94384)}kts")
            if squawk:
                parts.append(f"sqk:{squawk}")
            body = "  ".join(parts)

            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel="ADS-B (OpenSky)",
                from_id=node_id,
                from_display=display_name,
                body=body,
                priority=Priority.NORMAL,
                lat=float(lat),
                lon=float(lon),
                msg_type="position",
                raw={"format": "opensky", **meta},
            )
            await self._enqueue(msg)

        stale = [k for k, n in list(self._nodes.items())
                 if k not in seen and n.last_heard and
                 now_ts - n.last_heard.timestamp() > self._stale_sec]
        for k in stale:
            del self._nodes[k]
        if stale:
            log.debug("OpenSky %s: evicted %d stale aircraft", self.name, len(stale))

    def _health_detail(self) -> dict:
        return {
            "bbox":     [[self._lat_min, self._lon_min], [self._lat_max, self._lon_max]],
            "aircraft": len(self._nodes),
            "auth":     self._authenticated,
            "poll_sec": self._poll,
        }
