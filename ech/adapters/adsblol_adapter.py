"""
ech/adapters/adsblol_adapter.py
--------------------------------
ADS-B from adsb.lol, a free community network of volunteer receivers
(https://api.adsb.lol, open data, no key). Fills in what the local receiver and
OpenSky miss, and carries per-aircraft database fields the others don't:
type (t, e.g. C17, H60), registration (r) and a military flag (dbFlags bit 0).

Read-only like the other aircraft adapters: aircraft become map nodes
(node_id "icao:<hex>"), nothing goes into the message feed.

Config keys:
    lat, lon        centre of the query (default: the station base location)
    radius_nm       query radius in nautical miles (default 150, max 250)
    poll_interval   seconds between polls (default 60, floored at 20: be polite)
    stale_sec       drop an aircraft this long after its last update (default 180)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage

log = logging.getLogger(__name__)

_POINT_URL = "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius:d}"


class ADSBLolAdapter(Adapter):
    """Read-only aircraft feed from the adsb.lol community API."""

    is_mock: bool = False
    send_enabled: bool = False

    def __init__(self, config: dict):
        super().__init__(config)
        self._lat = config.get("lat")
        self._lon = config.get("lon")
        self._radius = int(min(250, max(10, float(config.get("radius_nm", 150)))))
        self._poll = max(20.0, float(config.get("poll_interval", 60)))
        self._stale_sec = float(config.get("stale_sec", max(180.0, self._poll * 2.5)))
        self._nodes: dict[str, MeshNode] = {}
        self._run_task: asyncio.Task | None = None
        self._last_count = 0
        self._last_error: str | None = None
        self._military = 0

    def set_base_location(self, lat: float, lon: float) -> None:
        if self.config.get("lat") is None and lat is not None and lon is not None:
            self._lat, self._lon = float(lat), float(lon)

    async def connect(self) -> None:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            raise ConnectionError("adsb.lol adapter requires aiohttp")
        self._connected = True
        self._run_task = asyncio.create_task(self._run())
        log.info("adsb.lol %s: connected, radius %d nm, poll %.0fs", self.name, self._radius, self._poll)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: NormalizedMessage) -> bool:
        return False

    async def nodes(self) -> list[MeshNode]:
        cutoff = datetime.now(timezone.utc).timestamp() - self._stale_sec
        return [n for n in self._nodes.values() if n.last_heard and n.last_heard.timestamp() >= cutoff]

    async def _run(self) -> None:
        import aiohttp
        from ech.core import identity
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(headers={"User-Agent": identity.user_agent()}) as session:
            while self._connected:
                if self._lat is None or self._lon is None:
                    self._last_error = "no centre: set lat/lon or the station base location"
                else:
                    try:
                        url = _POINT_URL.format(lat=float(self._lat), lon=float(self._lon), radius=self._radius)
                        async with session.get(url, timeout=timeout) as resp:
                            if resp.status == 200:
                                self.ingest(await resp.json(content_type=None))
                                self._last_error = None
                            else:
                                self._last_error = f"HTTP {resp.status}"
                                log.warning("adsb.lol %s: HTTP %d", self.name, resp.status)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error = type(exc).__name__
                        log.warning("adsb.lol %s: poll error: %s", self.name, exc)
                await asyncio.sleep(self._poll)

    def ingest(self, data: dict[str, Any]) -> int:
        now = datetime.now(timezone.utc)
        seen = set()
        mil = 0
        for ac in data.get("ac") or []:
            hex_id = str(ac.get("hex", "")).lower().strip().lstrip("~")
            lat, lon = ac.get("lat"), ac.get("lon")
            if not hex_id or lat is None or lon is None:
                continue
            if float(ac.get("seen_pos", ac.get("seen", 0)) or 0) > self._stale_sec:
                continue
            node_id = f"icao:{hex_id}"
            flight = str(ac.get("flight") or "").strip()
            alt = ac.get("alt_baro")
            if not isinstance(alt, (int, float)):
                alt = ac.get("alt_geom") if isinstance(ac.get("alt_geom"), (int, float)) else None
            meta: dict[str, Any] = {"icao": hex_id}
            if flight:
                meta["flight"] = flight
            if alt is not None:
                meta["altitude_ft"] = int(alt)
            if ac.get("gs") is not None:
                meta["speed_kts"] = round(float(ac["gs"]), 1)
            if ac.get("track") is not None:
                meta["track_deg"] = round(float(ac["track"]), 1)
            for src, dst in (("squawk", "squawk"), ("category", "category"), ("t", "aircraft_type"),
                             ("r", "registration"), ("emergency", "emergency")):
                if ac.get(src) not in (None, "", "none"):
                    meta[dst] = ac[src]
            if int(ac.get("dbFlags") or 0) & 1:
                meta["military"] = True
                mil += 1
            if ac.get("mlat"):
                meta["mlat"] = True
            node = self._nodes.get(node_id)
            label = flight or meta.get("registration") or hex_id.upper()
            if node is None:
                node = MeshNode(node_id=node_id, display_name=label, first_seen=now)
                self._nodes[node_id] = node
            node.display_name = label
            node.last_heard = now
            node.lat, node.lon = float(lat), float(lon)
            node.meta = meta
            seen.add(node_id)
        cutoff = now.timestamp() - self._stale_sec
        for k in [k for k, n in self._nodes.items() if k not in seen and n.last_heard and n.last_heard.timestamp() < cutoff]:
            del self._nodes[k]
        self._last_count, self._military = len(seen), mil
        return len(seen)

    def _health_detail(self) -> dict:
        return {"aircraft": self._last_count, "military": self._military, "radius_nm": self._radius,
                "centre": [self._lat, self._lon], "poll_sec": self._poll, "last_error": self._last_error,
                "source": "adsb.lol community network"}
