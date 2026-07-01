"""
ech/adapters/ais_catcher_adapter.py
------------------------------------
AIS receiver adapter via AIS-catcher's built-in HTTP JSON API.

AIS-catcher (https://github.com/jvde-github/AIS-catcher) runs a web server
(default port 8100) that exposes live vessel state as JSON.  This adapter
polls that feed and surfaces each vessel as a map node.  All messages have
msg_type="position" so they stay out of the text inbox.

Config keys:
    host          IP / hostname of the AIS-catcher device (required)
    port          HTTP port (default 8100)
    poll_interval Seconds between polls (default 30)
    stale_sec     Remove vessels not updated for this long (default 300 = 5 min)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

# AIS-catcher JSON endpoint candidates (tried in order)
_API_PATHS = [
    "/vessels.json",
    "/ships.json",
    "/json",
    "/",
]

# AIS ship-type codes → human label (abridged)
_SHIP_TYPE: dict[int, str] = {
    0: "Unknown", 20: "WIG", 30: "Fishing", 31: "Towing", 32: "Towing>200m",
    33: "Dredging", 34: "Diving ops", 35: "Military", 36: "Sailing",
    37: "Pleasure", 40: "HSC", 50: "Pilot", 51: "SAR", 52: "Tug",
    53: "Port tender", 55: "Law enforcement", 58: "Medical", 60: "Passenger",
    70: "Cargo", 80: "Tanker", 90: "Other",
}

# AIS nav-status codes
_NAV_STATUS: dict[int, str] = {
    0: "Underway", 1: "At anchor", 2: "Not under command", 3: "Restricted maneuverability",
    4: "Constrained by draught", 5: "Moored", 6: "Aground", 7: "Engaged in fishing",
    8: "Underway sailing", 15: "Undefined",
}


def _ship_type_label(code: Any) -> str:
    if code is None:
        return ""
    c = int(code)
    # Ranges: 60-69 Passenger, 70-79 Cargo, 80-89 Tanker, 90-99 Other
    for base, label in ((60, "Passenger"), (70, "Cargo"), (80, "Tanker"), (90, "Other")):
        if base <= c < base + 10:
            return label
    return _SHIP_TYPE.get(c, f"Type {c}")


class AISCatcherAdapter(Adapter):
    """Read-only AIS receiver via local AIS-catcher HTTP JSON feed."""

    is_mock: bool = False

    def __init__(self, config: dict):
        super().__init__(config)
        self._host      = config["host"]
        self._port      = int(config.get("port", 8100))
        self._poll      = float(config.get("poll_interval", 30.0))
        self._stale_sec = float(config.get("stale_sec", 300.0))
        self._path: str | None = config.get("path")
        self._nodes: dict[str, MeshNode] = {}
        self._run_task: asyncio.Task | None = None

    # ── Adapter interface ──────────────────────────────────────────────────

    async def connect(self) -> None:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            raise ConnectionError("AIS-catcher adapter requires aiohttp — pip install aiohttp")
        if self._path is None:
            self._path = await self._detect_path()
        self._connected = True
        self._run_task  = asyncio.create_task(self._run())
        log.info("AIS-catcher %s: connected to http://%s:%d%s",
                 self.name, self._host, self._port, self._path)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("AIS-catcher %s: disconnected", self.name)

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
        try:
            import aiohttp
        except ImportError:
            return _API_PATHS[0]

        async with aiohttp.ClientSession() as session:
            for p in _API_PATHS:
                url = f"http://{self._host}:{self._port}{p}"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                        if resp.status == 200:
                            ct = resp.headers.get("Content-Type", "")
                            if "json" in ct:
                                data = await resp.json(content_type=None)
                                if _extract_vessels(data):
                                    log.info("AIS-catcher %s: found JSON feed at %s", self.name, p)
                                    return p
                except Exception:
                    pass

        log.warning("AIS-catcher %s: could not auto-detect API path at %s:%d; using %s",
                    self.name, self._host, self._port, _API_PATHS[0])
        return _API_PATHS[0]

    async def _run(self) -> None:
        try:
            import aiohttp
        except ImportError:
            log.error("AIS-catcher %s: aiohttp is required — pip install aiohttp", self.name)
            return

        url     = f"http://{self._host}:{self._port}{self._path}"
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession() as session:
            while self._connected:
                try:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            await self._ingest(data)
                        else:
                            log.warning("AIS-catcher %s: HTTP %d from %s",
                                        self.name, resp.status, url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("AIS-catcher %s: poll error: %s", self.name, exc)

                await asyncio.sleep(self._poll)

    async def _ingest(self, data: dict[str, Any]) -> None:
        now    = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        seen: set[str] = set()

        for v in _extract_vessels(data):
            mmsi = v.get("mmsi")
            if not mmsi:
                continue
            mmsi_str = str(int(mmsi))

            lat = v.get("lat") or v.get("latitude")
            lon = v.get("lon") or v.get("longitude") or v.get("lng")
            if lat is None or lon is None:
                continue
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if lat == 0.0 and lon == 0.0:
                continue

            name      = (v.get("name") or v.get("shipname") or "").strip()
            callsign  = (v.get("callsign") or "").strip()
            speed     = v.get("speed") or v.get("sog")
            course    = v.get("course") or v.get("cog")
            heading   = v.get("heading") or v.get("hdg")
            status    = v.get("status") or v.get("nav_status")
            ship_type = v.get("shiptype") or v.get("ship_type")
            dest      = (v.get("destination") or "").strip()
            signal_db = v.get("signal_power") or v.get("rssi")

            display_name = name if name else mmsi_str
            node_id      = f"mmsi:{mmsi_str}"

            meta: dict[str, Any] = {"mmsi": mmsi_str}
            if name:      meta["vessel_name"] = name
            if callsign:  meta["callsign"]    = callsign
            if dest:      meta["destination"] = dest
            if ship_type is not None:
                meta["ship_type"] = _ship_type_label(ship_type)
            if speed is not None:
                try:
                    meta["speed_kts"] = round(float(speed), 1)
                except (TypeError, ValueError):
                    pass
            if course is not None:
                try:
                    meta["course_deg"] = round(float(course), 1)
                except (TypeError, ValueError):
                    pass
            if status is not None:
                try:
                    meta["nav_status"] = _NAV_STATUS.get(int(status), str(status))
                except (TypeError, ValueError):
                    pass
            if signal_db is not None:
                try:
                    meta["signal_db"] = round(float(signal_db), 1)
                except (TypeError, ValueError):
                    pass

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
            node.lat  = lat
            node.lon  = lon
            node.meta = meta
            seen.add(node_id)

            # Position-only message (skips inbox, updates map)
            parts = [f"🚢 {display_name}"]
            if meta.get("ship_type"):      parts.append(meta["ship_type"])
            if meta.get("speed_kts"):      parts.append(f"{meta['speed_kts']}kts")
            if meta.get("nav_status"):     parts.append(meta["nav_status"])
            if dest:                       parts.append(f"→ {dest}")
            body = "  ".join(parts)

            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel="AIS 161/162 MHz",
                from_id=node_id,
                from_display=display_name,
                body=body,
                priority=Priority.NORMAL,
                lat=lat,
                lon=lon,
                msg_type="position",
                raw={"format": "ais", **meta},
            )
            await self._enqueue(msg)

        # Evict vessels not seen in this poll
        stale = [k for k, n in list(self._nodes.items())
                 if k not in seen and n.last_heard and
                 now_ts - n.last_heard.timestamp() > self._stale_sec]
        for k in stale:
            del self._nodes[k]
        if stale:
            log.debug("AIS-catcher %s: evicted %d stale vessels", self.name, len(stale))

        log.debug("AIS-catcher %s: %d vessels active", self.name, len(self._nodes))

    def _health_detail(self) -> dict:
        return {
            "url":      f"http://{self._host}:{self._port}{self._path}",
            "vessels":  len(self._nodes),
            "poll_sec": self._poll,
        }


def _extract_vessels(data: Any) -> list[dict]:
    """Pull the vessel list out of various AIS-catcher JSON response shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("vessels", "ships", "targets", "history", "data", "result"):
            if isinstance(data.get(key), list):
                return data[key]
        # Some versions wrap each vessel in a top-level MMSI-keyed dict
        if data and all(str(k).isdigit() for k in list(data.keys())[:5]):
            return list(data.values())
    return []
