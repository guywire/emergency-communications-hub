"""
ech/adapters/aprs_is.py
-----------------------
APRS-IS internet gateway adapter using aprslib.
Connects to rotate.aprs2.net (or configurable server) and subscribes
to a filter. Decodes position, message, status, and object packets.
Sends APRS messages to specific callsigns via the IS connection.

Config keys:
  name          str     adapter name (default: aprs-is)
  callsign      str     your callsign + SSID, e.g. W1ABC-9  (REQUIRED)
  passcode      int     APRS-IS passcode (use -1 for receive-only)
  server        str     APRS-IS server (default: rotate.aprs2.net)
  port          int     APRS-IS port (default: 14580)
  filter        str     APRS-IS filter string (default: r/44.1/-69.1/100)
  beacon        bool    send a login beacon on connect (default: True)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

import aprslib

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)


class APRSISAdapter(Adapter):
    """
    APRS-IS adapter using aprslib (synchronous library bridged to asyncio).
    aprslib uses blocking I/O internally so we run it in a thread and
    post decoded packets onto an asyncio queue.
    """

    # APRS position-only formats — update node cache but don't emit as messages
    _POS_FORMATS = {"uncompressed", "compressed", "mic-e", "base91"}

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "aprs-is")
        self._callsign = config.get("callsign", "N0CALL-9")
        self._passcode = int(config.get("passcode", -1))
        self._server = config.get("server", "rotate.aprs2.net")
        self._port = int(config.get("port", 14580))
        self._filter = config.get("filter", "r/44.1/-69.1/100")
        self._beacon = config.get("beacon", True)
        self._node_ttl = int(config.get("node_ttl_sec", 3600))
        # Position beacon config
        self._beacon_lat: float | None = config.get("beacon_lat") or config.get("base_lat") or None
        self._beacon_lon: float | None = config.get("beacon_lon") or config.get("base_lon") or None
        self._beacon_symbol = config.get("beacon_symbol", "/-")   # /- = house
        self._beacon_comment = config.get("beacon_comment", "ECH Emergency Hub")
        self._beacon_interval = int(config.get("beacon_interval_sec", 0))  # 0 = disabled
        self._ais: aprslib.IS | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._packet_count = 0
        self._nodes: dict[str, MeshNode] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._beacon_task: asyncio.Task | None = None
        self._msg_seq = 1   # APRS message sequence number (1-99999)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        log.info("APRS-IS %s: connecting to %s:%d as %s",
                 self.name, self._server, self._port, self._callsign)

        self._ais = aprslib.IS(
            self._callsign,
            passwd=self._passcode,
            host=self._server,
            port=self._port,
        )
        self._ais.set_filter(self._filter)

        # aprslib.IS.connect() is blocking — run in thread
        await self._loop.run_in_executor(None, self._ais.connect)

        if self._beacon:
            if self._beacon_lat is not None and self._beacon_lon is not None:
                pos_pkt = self._make_beacon_packet(self._beacon_lat, self._beacon_lon)
                await self._loop.run_in_executor(None, self._ais.sendall, pos_pkt)
                log.info("APRS-IS %s: sent position beacon (%.5f, %.5f)",
                         self.name, self._beacon_lat, self._beacon_lon)
            else:
                # No coordinates — send status only
                await self._loop.run_in_executor(
                    None, self._ais.sendall,
                    f"{self._callsign}>APRS,TCPIP*:>ECH online",
                )

        self._connected = True
        log.info("APRS-IS %s: connected, filter=%r", self.name, self._filter)

        # Start the blocking consumer loop in a daemon thread
        self._thread = threading.Thread(
            target=self._consume_loop,
            name=f"{self.name}-thread",
            daemon=True,
        )
        self._thread.start()
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name=f"{self.name}-cleanup"
        )
        if self._beacon_interval > 0 and self._passcode != -1:
            self._beacon_task = asyncio.create_task(
                self._auto_beacon_loop(), name=f"{self.name}-beacon"
            )

    async def disconnect(self) -> None:
        self._connected = False
        tasks = [t for t in (self._cleanup_task, self._beacon_task) if t]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._cleanup_task = None
        self._beacon_task = None
        if self._ais:
            try:
                self._ais.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)
        log.info("APRS-IS %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Send an APRS message packet to message.to_id (callsign).
        No-op if passcode is -1 (receive-only).
        """
        if self._passcode == -1:
            log.warning("APRS-IS %s: receive-only mode, cannot send", self.name)
            return False
        if not message.to_id:
            log.warning("APRS-IS %s: to_id required for APRS message send", self.name)
            return False
        if not self._ais or not self._connected:
            return False

        # APRS message format: SRC>APRS,TCPIP*::DEST     :body{seq}
        dest = message.to_id.upper().ljust(9)
        seq = self._msg_seq
        self._msg_seq = (self._msg_seq % 99999) + 1
        aprs_str = f"{self._callsign}>APRS,TCPIP*::{dest}:{message.body}{{{seq}}}"
        try:
            await self._loop.run_in_executor(None, self._ais.sendall, aprs_str)
            self._mark_tx(message)
            log.info("APRS-IS %s: sent to %s (seq %d): %s",
                     self.name, message.to_id, seq, message.body[:60])
            return True
        except Exception as exc:
            log.error("APRS-IS %s: send error: %s", self.name, exc)
            return False

    # ── Position beacon ───────────────────────────────────────────────────

    @staticmethod
    def _format_position_beacon(callsign: str, lat: float, lon: float,
                                 symbol: str = "/-",
                                 comment: str = "ECH") -> str:
        """Build an APRS uncompressed position packet. symbol: table+code e.g. '/-' house."""
        lat_d = int(abs(lat))
        lat_m = (abs(lat) - lat_d) * 60.0
        lon_d = int(abs(lon))
        lon_m = (abs(lon) - lon_d) * 60.0
        lat_str = f"{lat_d:02d}{lat_m:05.2f}{'N' if lat >= 0 else 'S'}"
        lon_str = f"{lon_d:03d}{lon_m:05.2f}{'E' if lon >= 0 else 'W'}"
        sym_table = symbol[0] if len(symbol) >= 1 else "/"
        sym_code  = symbol[1] if len(symbol) >= 2 else "-"
        return f"{callsign}>APRS,TCPIP*:={lat_str}{sym_table}{lon_str}{sym_code}{comment}"

    def _make_beacon_packet(self, lat: float, lon: float) -> str:
        return self._format_position_beacon(
            self._callsign, lat, lon, self._beacon_symbol, self._beacon_comment
        )

    async def send_beacon(self, lat: float | None = None, lon: float | None = None,
                          comment: str | None = None) -> dict:
        """
        Send an APRS position beacon. Uses adapter's configured coordinates if
        lat/lon not provided. Returns {status, packet}.
        """
        if self._passcode == -1:
            return {"status": "error", "detail": "receive-only mode (passcode=-1)"}
        if not self._ais or not self._connected:
            return {"status": "error", "detail": "not connected"}
        use_lat = lat if lat is not None else self._beacon_lat
        use_lon = lon if lon is not None else self._beacon_lon
        if use_lat is None or use_lon is None:
            return {"status": "error", "detail": "no coordinates configured"}
        if comment:
            self._beacon_comment = comment
        pkt = self._make_beacon_packet(use_lat, use_lon)
        try:
            await self._loop.run_in_executor(None, self._ais.sendall, pkt)
            log.info("APRS-IS %s: position beacon sent (%.5f, %.5f): %s",
                     self.name, use_lat, use_lon, pkt)
            return {"status": "ok", "packet": pkt, "lat": use_lat, "lon": use_lon}
        except Exception as exc:
            log.error("APRS-IS %s: beacon send error: %s", self.name, exc)
            return {"status": "error", "detail": str(exc)}

    async def _auto_beacon_loop(self) -> None:
        """Send periodic position beacons if beacon_interval_sec > 0."""
        try:
            while self._connected:
                await asyncio.sleep(self._beacon_interval)
                if self._beacon_lat is not None and self._beacon_lon is not None:
                    await self.send_beacon()
        except asyncio.CancelledError:
            pass

    def set_base_location(self, lat: float, lon: float) -> None:
        self._beacon_lat = lat
        self._beacon_lon = lon
        log.info("APRS-IS %s: beacon position updated to (%.5f, %.5f)", self.name, lat, lon)

    # ── Internal consume loop (runs in thread) ────────────────────────────

    def _consume_loop(self) -> None:
        """
        aprslib consumer callback loop. Runs in a background thread.
        Posts decoded packets back to the asyncio event loop via call_soon_threadsafe.
        """
        log.debug("APRS-IS %s: consumer thread started", self.name)
        try:
            self._ais.consumer(
                callback=self._on_packet,
                raw=False,
                blocking=True,
            )
        except StopIteration:
            log.info("APRS-IS %s: consumer stopped cleanly", self.name)
        except Exception as exc:
            if self._connected:
                log.error("APRS-IS %s: consumer error: %s", self.name, exc)
                self._connected = False

    def _on_packet(self, packet: dict) -> None:
        """Called by aprslib for each decoded packet. Thread context."""
        if not self._connected or self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self._process_packet(packet),
        )

    async def _process_packet(self, packet: dict) -> None:
        self._packet_count += 1
        try:
            fmt     = packet.get("format", "")
            from_id = packet.get("from", "UNKNOWN")
            lat     = packet.get("latitude")
            lon     = packet.get("longitude")
            now     = datetime.now(timezone.utc)

            # Always update node position cache for any packet with a position
            if lat is not None and lon is not None:
                node = self._nodes.get(from_id)
                if node is None:
                    node = MeshNode(
                        node_id=from_id,
                        display_name=from_id,
                        first_seen=now,
                    )
                    self._nodes[from_id] = node
                node.last_heard = now
                node.lat = float(lat)
                node.lon = float(lon)

            # Position-only beacons → node cache only, not message stream
            if fmt in self._POS_FORMATS:
                return

            body = self._packet_to_body(packet)
            if not body:
                return

            raw_path = packet.get("path", "")
            if isinstance(raw_path, list):
                raw_path = ",".join(str(x) for x in raw_path)
            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel="144.390 (IS)",
                from_id=from_id,
                from_display=from_id,
                body=body,
                priority=Priority.NORMAL,
                lat=float(lat) if lat else None,
                lon=float(lon) if lon else None,
                path=raw_path or None,
                raw={"format": fmt},
            )
            await self._enqueue(msg)

        except Exception as exc:
            log.debug("APRS-IS %s: packet processing error: %s", self.name, exc)

    def _packet_to_body(self, packet: dict) -> str:
        """Convert an aprslib decoded packet dict to a human-readable body string."""
        fmt = packet.get("format", "")
        from_id = packet.get("from", "?")

        if fmt == "message":
            addresse = packet.get("addresse", "").strip()
            text = packet.get("message_text", "").strip()
            if not text:
                return ""
            if addresse and addresse.upper() != self._callsign.upper().split("-")[0]:
                return f"MSG {from_id}→{addresse}: {text}"
            return f"MSG {from_id}: {text}"

        elif fmt in ("uncompressed", "compressed", "mic-e", "base91"):
            comment = packet.get("comment", "").strip()
            speed = packet.get("speed")
            course = packet.get("course")
            alt = packet.get("altitude")
            parts = []
            if comment:
                parts.append(comment)
            if speed is not None:
                parts.append(f"{speed:.0f}km/h")
            if course is not None:
                parts.append(f"HDG {course:.0f}°")
            if alt is not None:
                parts.append(f"ALT {alt:.0f}m")
            suffix = " ".join(parts)
            return f"POS {from_id}" + (f": {suffix}" if suffix else "")

        elif fmt == "status":
            status = packet.get("status", "").strip()
            return f"STATUS {from_id}: {status}" if status else ""

        elif fmt == "object":
            obj_name = packet.get("object_name", "").strip()
            comment = packet.get("comment", "").strip()
            return f"OBJ {obj_name}: {comment}" if obj_name else ""

        elif fmt == "wx":
            temp = packet.get("wx_temp")
            wind_speed = packet.get("wx_wind_speed")
            wx_parts = []
            if temp is not None:
                wx_parts.append(f"{temp:.0f}°F")
            if wind_speed is not None:
                wx_parts.append(f"wind {wind_speed:.0f}mph")
            return f"WX {from_id}: " + " ".join(wx_parts) if wx_parts else ""

        elif fmt == "bulletin":
            text = packet.get("message_text", "").strip()
            return f"BLT {from_id}: {text}" if text else ""

        else:
            # Generic fallback — raw comment if present
            comment = packet.get("comment", "").strip()
            raw = packet.get("raw", b"")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            return comment or raw[:120] or ""

    # ── Background cleanup ────────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Evict nodes from _nodes that haven't been heard from within node_ttl_sec."""
        try:
            while self._connected:
                await asyncio.sleep(300)   # check every 5 minutes
                cutoff = datetime.now(timezone.utc).timestamp() - self._node_ttl
                stale = [k for k, n in list(self._nodes.items())
                         if n.last_heard and n.last_heard.timestamp() < cutoff]
                for k in stale:
                    del self._nodes[k]
                if stale:
                    log.info("APRS-IS %s: evicted %d stale nodes (TTL %ds)",
                             self.name, len(stale), self._node_ttl)
        except asyncio.CancelledError:
            pass

    async def clear_nodes(self) -> int:
        """Clear all cached APRS stations from memory."""
        count = len(self._nodes)
        self._nodes.clear()
        log.info("APRS-IS %s: cleared %d cached nodes", self.name, count)
        return count

    # ── Overrides ─────────────────────────────────────────────────────────

    async def nodes(self):
        """Return cached APRS stations seen within node_ttl_sec (default 1 hr)."""
        cutoff = datetime.now(timezone.utc).timestamp() - self._node_ttl
        return [
            n for n in self._nodes.values()
            if n.last_heard and n.last_heard.timestamp() >= cutoff
        ]

    async def _run(self) -> None:
        """Not used — consumer runs in thread. Satisfies abstract requirement."""
        pass

    def _health_detail(self) -> dict:
        return {
            "server": f"{self._server}:{self._port}",
            "callsign": self._callsign,
            "filter": self._filter,
            "packets_received": self._packet_count,
            "rx_only": self._passcode == -1,
            "cached_stations": len(self._nodes),
            "node_ttl_sec": self._node_ttl,
        }
