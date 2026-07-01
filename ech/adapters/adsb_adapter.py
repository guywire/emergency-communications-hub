"""
ech/adapters/adsb_adapter.py
----------------------------
ADS-B adapter: polls a local dump1090 / PiAware SkyAware JSON feed and
surfaces aircraft as map nodes.  No messages are added to the feed (all
packets have msg_type="position") so the inbox stays clean.

Config keys:
    host          IP or hostname of the dump1090/PiAware device (required)
    port          HTTP port (default 80)
    path          URL path to aircraft.json
                  (default "/skyaware/data/aircraft.json")
    poll_interval Poll cadence in seconds (default 10)
    stale_sec     Seconds without a position update before a node is removed
                  (default 120 — aircraft move fast, 2 min is generous)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

_DEFAULT_PATHS = [
    "/skyaware/data/aircraft.json",
    "/tar1090/data/aircraft.json",
    "/dump1090/data/aircraft.json",
    "/dump1090-fa/data/aircraft.json",
]


class ADSBAdapter(Adapter):
    """Read-only ADS-B receiver via local dump1090 / PiAware HTTP JSON feed."""

    is_mock: bool = False

    def __init__(self, config: dict):
        super().__init__(config)
        self._host         = config["host"]
        self._port         = int(config.get("port", 80))
        self._poll         = float(config.get("poll_interval", 10.0))
        self._stale_sec    = float(config.get("stale_sec", 120.0))
        self._path: str | None = config.get("path")   # None → auto-detect
        self._nodes: dict[str, MeshNode] = {}
        self._run_task: asyncio.Task | None = None

    # ── Adapter interface ──────────────────────────────────────────────────

    async def connect(self) -> None:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            raise ConnectionError("ADS-B adapter requires aiohttp — pip install aiohttp")
        if self._path is None:
            self._path = await self._detect_path()
        self._connected = True
        self._run_task  = asyncio.create_task(self._run())
        log.info("ADS-B %s: connected to http://%s:%d%s", self.name, self._host, self._port, self._path)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("ADS-B %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        return False   # receive-only

    async def nodes(self) -> list[MeshNode]:
        cutoff = datetime.now(timezone.utc).timestamp() - self._stale_sec
        return [
            n for n in self._nodes.values()
            if n.last_heard and n.last_heard.timestamp() >= cutoff
        ]

    # ── Internal ──────────────────────────────────────────────────────────

    async def _detect_path(self) -> str:
        """Try candidate URL paths and return the first that serves aircraft data."""
        try:
            import aiohttp
        except ImportError:
            log.warning("ADS-B %s: aiohttp not installed; defaulting to %s", self.name, _DEFAULT_PATHS[0])
            return _DEFAULT_PATHS[0]

        async with aiohttp.ClientSession() as session:
            for p in _DEFAULT_PATHS:
                url = f"http://{self._host}:{self._port}{p}"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            if "aircraft" in data:
                                log.info("ADS-B %s: found feed at %s", self.name, p)
                                return p
                except Exception:
                    pass
        log.warning("ADS-B %s: no feed found at %s; using %s",
                    self.name, self._host, _DEFAULT_PATHS[0])
        return _DEFAULT_PATHS[0]

    async def _run(self) -> None:
        try:
            import aiohttp
        except ImportError:
            log.error("ADS-B %s: aiohttp is required — install with: pip install aiohttp", self.name)
            return

        url = f"http://{self._host}:{self._port}{self._path}"
        timeout = aiohttp.ClientTimeout(total=8)

        async with aiohttp.ClientSession() as session:
            while self._connected:
                try:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            await self._ingest(data)
                        else:
                            log.warning("ADS-B %s: HTTP %d from %s", self.name, resp.status, url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("ADS-B %s: poll error: %s", self.name, exc)

                await asyncio.sleep(self._poll)

    async def _ingest(self, data: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        seen: set[str] = set()

        for ac in data.get("aircraft", []):
            hex_id = ac.get("hex", "").lower().strip()
            if not hex_id:
                continue
            lat = ac.get("lat")
            lon = ac.get("lon")
            if lat is None or lon is None:
                continue
            # 'seen_pos' = seconds since last position update from dump1090
            seen_pos = float(ac.get("seen_pos", ac.get("seen", 0)))
            if seen_pos > self._stale_sec:
                continue

            node_id      = f"icao:{hex_id}"
            flight_raw   = ac.get("flight", "").strip()
            flight       = flight_raw if flight_raw else ""
            display_name = flight if flight else hex_id.upper()

            alt_baro = ac.get("alt_baro")   # ft or "ground"
            alt_geom = ac.get("alt_geom")
            altitude = None
            if isinstance(alt_baro, (int, float)):
                altitude = int(alt_baro)
            elif isinstance(alt_geom, (int, float)):
                altitude = int(alt_geom)

            speed   = ac.get("gs")     # knots ground speed
            track   = ac.get("track")  # degrees true
            squawk  = ac.get("squawk", "")
            category= ac.get("category", "")

            meta: dict[str, Any] = {"icao": hex_id}
            if flight:
                meta["flight"] = flight
            if altitude is not None:
                meta["altitude_ft"] = altitude
            if speed is not None:
                meta["speed_kts"] = round(float(speed), 1)
            if track is not None:
                meta["track_deg"] = round(float(track), 1)
            if squawk:
                meta["squawk"] = squawk
            if category:
                meta["category"] = category

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

            # Emit a position message (msg_type=position skips the inbox)
            parts = [f"✈ {display_name}"]
            if altitude is not None:
                parts.append(f"{altitude:,}ft")
            if speed is not None:
                parts.append(f"{round(float(speed))}kts")
            if squawk:
                parts.append(f"sqk:{squawk}")
            body = "  ".join(parts)

            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel="ADS-B 1090MHz",
                from_id=node_id,
                from_display=display_name,
                body=body,
                priority=Priority.NORMAL,
                lat=float(lat),
                lon=float(lon),
                msg_type="position",
                raw={"format": "adsb", **meta},
            )
            await self._enqueue(msg)

        # Evict nodes that left the poll area (no longer in feed and stale)
        stale = [k for k, n in list(self._nodes.items())
                 if k not in seen and n.last_heard and
                 now_ts - n.last_heard.timestamp() > self._stale_sec]
        for k in stale:
            del self._nodes[k]
        if stale:
            log.debug("ADS-B %s: evicted %d stale aircraft", self.name, len(stale))

    def _health_detail(self) -> dict:
        return {
            "url":      f"http://{self._host}:{self._port}{self._path}",
            "aircraft": len(self._nodes),
            "poll_sec": self._poll,
        }
