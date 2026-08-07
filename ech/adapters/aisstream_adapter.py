"""
ech/adapters/aisstream_adapter.py
----------------------------------
AIS adapter via AISstream.io real-time WebSocket feed.

AISstream.io (https://aisstream.io) provides a global AIS stream over WebSocket.
Free API keys available at https://aisstream.io/authenticate

This adapter supplements the local AIS-catcher receiver with vessels beyond
RF range.  All messages have msg_type="position" so they go to the map only.

Config keys:
    api_key         AISstream.io API key (required)
    bounding_box    [[min_lat, min_lon], [max_lat, max_lon]] (required)
                    Example: [[43.0, -72.0], [47.0, -65.0]]  (Gulf of Maine)
    stale_sec       Remove vessels not updated for this long (default 600)
    reconnect_delay Seconds to wait before reconnecting on drop (default 10)
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

_WS_URL = "wss://stream.aisstream.io/v0/stream"

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


class AISStreamAdapter(Adapter):
    """Real-time global AIS via AISstream.io WebSocket feed.

    The WebSocket loop runs in a dedicated thread with its own asyncio event loop
    so it is completely isolated from uvicorn's event loop.
    """

    is_mock: bool = False
    send_enabled: bool = False

    def __init__(self, config: dict):
        super().__init__(config)
        self._api_key         = config["api_key"]
        self._bbox            = config["bounding_box"]
        self._stale_sec       = float(config.get("stale_sec", 600.0))
        self._reconnect_delay = float(config.get("reconnect_delay", 10.0))
        self._nodes: dict[str, MeshNode] = {}
        self._static: dict[str, dict] = {}
        # Thread-safe queue: worker thread puts parsed packets, asyncio task drains them
        self._pkt_queue: queue.Queue = queue.Queue(maxsize=500)
        self._worker_thread: threading.Thread | None = None
        self._run_task: asyncio.Task | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None

    # ── Adapter interface ──────────────────────────────────────────────────

    async def connect(self) -> None:
        self._connected  = True
        self._main_loop  = asyncio.get_running_loop()
        self._worker_thread = threading.Thread(
            target=self._ws_thread, daemon=True, name=f"aisstream-{self.name}"
        )
        self._worker_thread.start()
        self._run_task = asyncio.create_task(self._drain_loop())
        log.info("AISstream %s: starting (bbox=%s)", self.name, self._bbox)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("AISstream %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        return False

    async def nodes(self) -> list[MeshNode]:
        cutoff = datetime.now(timezone.utc).timestamp() - self._stale_sec
        return [
            n for n in self._nodes.values()
            if n.last_heard and n.last_heard.timestamp() >= cutoff
        ]

    # ── Background thread (own event loop, isolated from uvicorn) ─────────

    def _ws_thread(self) -> None:
        """Run WebSocket receive loop in a dedicated thread with its own event loop."""
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_loop())
        finally:
            loop.close()

    async def _ws_loop(self) -> None:
        import websockets
        import websockets.exceptions

        sub = json.dumps({
            "APIKey": self._api_key,
            "BoundingBoxes": [self._bbox],
            "FilterMessageTypes": [
                "PositionReport",
                "StandardClassBPositionReport",
                "ShipStaticData",
                "AidToNavigationReport",
            ],
        })

        backoff = self._reconnect_delay

        while self._connected:
            t_start = time.monotonic()
            n_msgs = 0
            try:
                async with websockets.connect(_WS_URL) as ws:
                    await ws.send(sub)
                    log.info("AISstream %s: connected, subscribed to bbox %s",
                             self.name, self._bbox)
                    self._state = "connected"

                    async for raw in ws:
                        if not self._connected:
                            break
                        try:
                            text = raw if isinstance(raw, str) else raw.decode()
                            pkt = json.loads(text)
                            err = pkt.get("error") if isinstance(pkt, dict) else None
                            if err:
                                log.error("AISstream %s: server error: %s", self.name, err)
                                self._connected = False
                                break
                            try:
                                self._pkt_queue.put_nowait(pkt)
                            except queue.Full:
                                pass  # drop oldest on full rather than block
                            n_msgs += 1
                            if n_msgs == 1:
                                log.info("AISstream %s: receiving data", self.name)
                                backoff = self._reconnect_delay

                        except Exception as exc:
                            log.debug("AISstream %s: parse error: %s", self.name, exc)

            except websockets.exceptions.ConnectionClosedError as exc:
                elapsed = time.monotonic() - t_start
                log.warning("AISstream %s: connection closed: %s (%.0fs, %d msgs)",
                            self.name, exc, elapsed, n_msgs)
                if n_msgs == 0 and elapsed < 5.0:
                    backoff = min(backoff * 2, 300.0)
                    log.warning("AISstream %s: backing off %.0fs (possible rate limit)",
                                self.name, backoff)
            except Exception as exc:
                log.warning("AISstream %s: connection error: %s", self.name, exc)
                backoff = min(backoff * 2, 300.0)

            if self._connected:
                self._state = "error"
                # Interruptible sleep so disconnect() is responsive
                for _ in range(int(backoff * 10)):
                    if not self._connected:
                        break
                    time.sleep(0.1)

    # ── Asyncio drain loop (runs in main ECH event loop) ──────────────────

    async def _drain_loop(self) -> None:
        """Pull packets from the thread-safe queue and process them in the main loop."""
        while self._connected:
            try:
                pkt = self._pkt_queue.get_nowait()
                await self._handle(pkt)
            except queue.Empty:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug("AISstream %s: drain error: %s", self.name, exc)

    # ── AIS message handler ────────────────────────────────────────────────

    async def _handle(self, msg: dict) -> None:
        msg_type = msg.get("MessageType", "")
        meta     = msg.get("MetaData", {})
        payload  = msg.get("Message", {})

        mmsi = str(meta.get("MMSI") or meta.get("MMSI_String") or "").strip()
        if not mmsi or not mmsi.isdigit():
            return

        node_id = f"mmsi:{mmsi}"
        now     = datetime.now(timezone.utc)

        if msg_type == "ShipStaticData":
            s = payload.get("ShipStaticData") or payload.get("ShipStaticAndVoyageRelatedData") or {}
            static: dict[str, Any] = {}
            name = (s.get("Name") or meta.get("ShipName") or "").strip()
            if name:
                static["vessel_name"] = name
            cs = (s.get("CallSign") or "").strip()
            if cs:
                static["callsign"] = cs
            dest = (s.get("Destination") or "").strip()
            if dest:
                static["destination"] = dest
            stype = s.get("Type")
            if stype is not None:
                static["ship_type"] = _ship_type_label(stype)
            if static:
                self._static[node_id] = {**self._static.get(node_id, {}), **static}
            if node_id in self._nodes and name:
                self._nodes[node_id].display_name = name
                self._nodes[node_id].meta = {
                    **self._nodes[node_id].meta,
                    **self._static[node_id],
                }
            return

        pos = (payload.get("PositionReport")
               or payload.get("StandardClassBPositionReport")
               or payload.get("AidToNavigationReport")
               or {})

        lat = pos.get("Latitude") or meta.get("latitude")
        lon = pos.get("Longitude") or meta.get("longitude")
        if lat is None or lon is None:
            return
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return
        if lat == 0.0 and lon == 0.0:
            return

        cached = self._static.get(node_id, {})
        ship_name = (meta.get("ShipName") or cached.get("vessel_name") or "").strip()
        display_name = ship_name if ship_name else mmsi

        node_meta: dict[str, Any] = {"mmsi": mmsi, **cached}
        if ship_name:
            node_meta["vessel_name"] = ship_name

        sog = pos.get("Sog")
        cog = pos.get("Cog")
        nav = pos.get("NavigationalStatus")

        if sog is not None:
            try:
                node_meta["speed_kts"] = round(float(sog), 1)
            except (TypeError, ValueError):
                pass
        if cog is not None:
            try:
                v = round(float(cog), 1)
                if v < 360:
                    node_meta["course_deg"] = v
            except (TypeError, ValueError):
                pass
        if nav is not None:
            try:
                node_meta["nav_status"] = _NAV_STATUS.get(int(nav), str(nav))
            except (TypeError, ValueError):
                pass

        node = self._nodes.get(node_id)
        if node is None:
            node = MeshNode(node_id=node_id, display_name=display_name, first_seen=now)
            self._nodes[node_id] = node
        else:
            node.display_name = display_name
        node.last_heard = now
        node.lat  = lat
        node.lon  = lon
        node.meta = node_meta

        parts = [f"🌐 {display_name}"]
        if node_meta.get("ship_type"):  parts.append(node_meta["ship_type"])
        if node_meta.get("speed_kts"):  parts.append(f"{node_meta['speed_kts']}kts")
        if node_meta.get("nav_status"): parts.append(node_meta["nav_status"])
        if cached.get("destination"):   parts.append(f"→ {cached['destination']}")
        body = "  ".join(parts)

        await self._enqueue(NormalizedMessage(
            source_adapter=self.name,
            source_channel="AISstream.io",
            from_id=node_id,
            from_display=display_name,
            body=body,
            priority=Priority.NORMAL,
            lat=lat,
            lon=lon,
            msg_type="position",
            raw={"format": "aisstream", **node_meta},
        ))

    def _health_detail(self) -> dict:
        return {
            "url":     _WS_URL,
            "vessels": len(self._nodes),
            "bbox":    self._bbox,
            "queued":  self._pkt_queue.qsize(),
        }
