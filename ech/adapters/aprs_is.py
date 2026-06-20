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
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)


class APRSISAdapter(Adapter):
    """
    APRS-IS adapter using aprslib (synchronous library bridged to asyncio).
    aprslib uses blocking I/O internally so we run it in a thread and
    post decoded packets onto an asyncio queue.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "aprs-is")
        self._callsign = config.get("callsign", "N0CALL-9")
        self._passcode = int(config.get("passcode", -1))
        self._server = config.get("server", "rotate.aprs2.net")
        self._port = int(config.get("port", 14580))
        self._filter = config.get("filter", "r/44.1/-69.1/100")
        self._beacon = config.get("beacon", True)
        self._ais: aprslib.IS | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._packet_count = 0

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
            await self._loop.run_in_executor(
                None,
                self._ais.sendall,
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

    async def disconnect(self) -> None:
        self._connected = False
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
        aprs_str = f"{self._callsign}>APRS,TCPIP*::{dest}:{message.body}"
        try:
            await self._loop.run_in_executor(None, self._ais.sendall, aprs_str)
            self._mark_tx(message)
            log.debug("APRS-IS %s: sent to %s: %s", self.name, message.to_id, message.body[:60])
            return True
        except Exception as exc:
            log.error("APRS-IS %s: send error: %s", self.name, exc)
            return False

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
            from_id = packet.get("from", "UNKNOWN")
            body = self._packet_to_body(packet)
            if not body:
                return

            lat = packet.get("latitude")
            lon = packet.get("longitude")

            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel=f"144.390 (IS)",
                from_id=from_id,
                from_display=from_id,
                body=body,
                priority=Priority.NORMAL,
                lat=float(lat) if lat else None,
                lon=float(lon) if lon else None,
                raw={
                    "format": packet.get("format", ""),
                    "path": packet.get("path", ""),
                },
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

    # ── Overrides ─────────────────────────────────────────────────────────

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
        }
