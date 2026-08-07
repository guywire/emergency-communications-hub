"""
ech/adapters/aishub_adapter.py
-------------------------------
AIS adapter via AISHub REST API (https://www.aishub.net).

Polls every poll_interval seconds (minimum 300s per AISHub free-tier rate limit).
Returns the latest position for each MMSI within the bounding box.

Config keys:
    username        AISHub username / API key (required)
    bounding_box    [[min_lat, min_lon], [max_lat, max_lon]] (required)
    poll_interval   Seconds between polls (default 300 — AISHub minimum)
    stale_sec       Remove vessels not updated for this long (default 900)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

_API_URL = "https://data.aishub.net/ws.php"

_SHIP_TYPE: dict[int, str] = {
    0: "Unknown", 20: "WIG", 30: "Fishing", 31: "Towing", 32: "Towing>200m",
    33: "Dredging", 34: "Diving ops", 35: "Military", 36: "Sailing",
    37: "Pleasure", 40: "HSC", 50: "Pilot", 51: "SAR", 52: "Tug",
    53: "Port tender", 55: "Law enforcement", 58: "Medical", 60: "Passenger",
    70: "Cargo", 80: "Tanker", 90: "Other",
}

_NAV_STATUS: dict[int, str] = {
    0: "Underway", 1: "At anchor", 2: "Not under command", 3: "Restricted maneuverability",
    4: "Constrained by draught", 5: "Moored", 6: "Aground", 7: "Engaged in fishing",
    8: "Underway sailing", 15: "Undefined",
}


def _ship_type_label(code: Any) -> str:
    if code is None:
        return ""
    c = int(code)
    for base, label in ((60, "Passenger"), (70, "Cargo"), (80, "Tanker"), (90, "Other")):
        if base <= c < base + 10:
            return label
    return _SHIP_TYPE.get(c, f"Type {c}")


class AISHubAdapter(Adapter):
    """AIS vessel data via AISHub REST API, polled every poll_interval seconds."""

    is_mock: bool = False
    send_enabled: bool = False

    def __init__(self, config: dict):
        super().__init__(config)
        self._username  = config["username"]
        self._bbox      = config["bounding_box"]   # [[min_lat,min_lon],[max_lat,max_lon]]
        self._poll_interval = float(config.get("poll_interval", 300.0))
        self._stale_sec     = float(config.get("stale_sec", 900.0))
        self._nodes: dict[str, MeshNode] = {}
        self._run_task: asyncio.Task | None = None

    # ── Adapter interface ──────────────────────────────────────────────────

    async def connect(self) -> None:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            raise ConnectionError("AISHub adapter requires aiohttp — pip install aiohttp")
        self._connected = True
        self._run_task  = asyncio.create_task(self._run())
        log.info("AISHub %s: starting (bbox=%s, poll=%.0fs)",
                 self.name, self._bbox, self._poll_interval)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("AISHub %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        return False

    async def nodes(self) -> list[MeshNode]:
        cutoff = datetime.now(timezone.utc).timestamp() - self._stale_sec
        return [
            n for n in self._nodes.values()
            if n.last_heard and n.last_heard.timestamp() >= cutoff
        ]

    # ── Internal ──────────────────────────────────────────────────────────

    async def _run(self) -> None:
        import aiohttp

        (min_lat, min_lon), (max_lat, max_lon) = self._bbox
        params = {
            "username": self._username,
            "format":   "1",      # one row per MMSI (latest position)
            "output":   "json",
            "compress": "0",
            "latmin":   min_lat,
            "latmax":   max_lat,
            "lonmin":   min_lon,
            "lonmax":   max_lon,
        }

        timeout = aiohttp.ClientTimeout(total=30)

        while self._connected:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(_API_URL, params=params) as resp:
                        if resp.status != 200:
                            log.warning("AISHub %s: HTTP %d", self.name, resp.status)
                        else:
                            data = await resp.json(content_type=None)
                            await self._process(data)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("AISHub %s: poll error: %s", self.name, exc)

            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                return

    async def _process(self, data: Any) -> None:
        """Parse AISHub JSON response — format is [meta_dict, [vessel, ...]]."""
        if not isinstance(data, list) or len(data) < 2:
            log.warning("AISHub %s: unexpected response format", self.name)
            return

        meta = data[0]
        if isinstance(meta, dict) and meta.get("ERROR"):
            log.error("AISHub %s: API error: %s", self.name, meta)
            return

        vessels = data[1]
        if not isinstance(vessels, list):
            return

        now = datetime.now(timezone.utc)
        n_new = n_updated = 0

        for v in vessels:
            try:
                mmsi = str(v.get("MMSI", "")).strip()
                if not mmsi or not mmsi.isdigit():
                    continue

                lat = v.get("LATITUDE")
                lon = v.get("LONGITUDE")
                if lat is None or lon is None:
                    continue
                lat, lon = float(lat), float(lon)
                if lat == 0.0 and lon == 0.0:
                    continue

                node_id   = f"mmsi:{mmsi}"
                ship_name = str(v.get("NAME") or "").strip()
                display   = ship_name if ship_name else mmsi

                node_meta: dict[str, Any] = {"mmsi": mmsi}
                if ship_name:
                    node_meta["vessel_name"] = ship_name
                cs = str(v.get("CALLSIGN") or "").strip()
                if cs:
                    node_meta["callsign"] = cs
                dest = str(v.get("DEST") or "").strip()
                if dest:
                    node_meta["destination"] = dest
                stype = v.get("TYPE")
                if stype is not None:
                    node_meta["ship_type"] = _ship_type_label(stype)

                sog = v.get("SOG")
                cog = v.get("COG")
                nav = v.get("NAVSTAT")

                if sog is not None:
                    try:
                        node_meta["speed_kts"] = round(float(sog), 1)
                    except (TypeError, ValueError):
                        pass
                if cog is not None:
                    try:
                        c = round(float(cog), 1)
                        if c < 360:
                            node_meta["course_deg"] = c
                    except (TypeError, ValueError):
                        pass
                if nav is not None:
                    try:
                        node_meta["nav_status"] = _NAV_STATUS.get(int(nav), str(nav))
                    except (TypeError, ValueError):
                        pass

                node = self._nodes.get(node_id)
                if node is None:
                    node = MeshNode(node_id=node_id, display_name=display, first_seen=now)
                    self._nodes[node_id] = node
                    n_new += 1
                else:
                    node.display_name = display
                    n_updated += 1
                node.last_heard = now
                node.lat  = lat
                node.lon  = lon
                node.meta = node_meta

                parts = [f"⚓ {display}"]
                if node_meta.get("ship_type"):  parts.append(node_meta["ship_type"])
                if node_meta.get("speed_kts"):  parts.append(f"{node_meta['speed_kts']}kts")
                if node_meta.get("nav_status"): parts.append(node_meta["nav_status"])
                if dest:                        parts.append(f"→ {dest}")

                await self._enqueue(NormalizedMessage(
                    source_adapter=self.name,
                    source_channel="AISHub",
                    from_id=node_id,
                    from_display=display,
                    body="  ".join(parts),
                    priority=Priority.NORMAL,
                    lat=lat,
                    lon=lon,
                    msg_type="position",
                    raw={"format": "aishub", **node_meta},
                ))

            except Exception as exc:
                log.debug("AISHub %s: vessel parse error: %s", self.name, exc)

        log.info("AISHub %s: %d vessels (%d new, %d updated)",
                 self.name, len(vessels), n_new, n_updated)
        self._state = "connected"

    def _health_detail(self) -> dict:
        return {
            "url":           _API_URL,
            "vessels":       len(self._nodes),
            "poll_interval": self._poll_interval,
            "bbox":          self._bbox,
        }
