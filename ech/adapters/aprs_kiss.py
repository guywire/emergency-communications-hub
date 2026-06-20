"""
ech/adapters/aprs_kiss.py
-------------------------
APRS adapter for direct TNC access via KISS protocol.
Supports three connection modes:

  serial  — hardware TNC on a serial port (Kantronics, Byonics, etc.)
  tcp     — Direwolf KISS-over-TCP (default port 8001)
  agwpe   — Direwolf AGWPE TCP interface (default port 8000)

KISS framing:
  FEND = 0xC0 (frame delimiter)
  FESC = 0xDB (escape character)
  TFEND= 0xDC (escaped FEND)
  TFESC= 0xDD (escaped FESC)
  Frame: FEND + (port<<4 | cmd) + <escaped data> + FEND

AGWPE framing (Direwolf):
  Fixed 36-byte header + variable data body.
  We use port 0, kind='K' for sending raw KISS-like frames.

Config keys:
  name          str     adapter name (default: aprs-kiss)
  transport     str     serial | tcp | agwpe  (default: serial)
  port          str     serial device, e.g. /dev/ttyUSB0
  baud          int     baud rate (default: 9600)
  host          str     host for tcp/agwpe (default: localhost)
  tcp_port      int     port for tcp (default: 8001) or agwpe (default: 8000)
  callsign      str     your callsign + SSID for TX (default: N0CALL-9)
  tx_path       str     AX.25 digipeater path (default: WIDE1-1,WIDE2-1)
"""

from __future__ import annotations

import asyncio
import logging
import struct
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

# ── KISS constants ────────────────────────────────────────────────────────
FEND  = 0xC0
FESC  = 0xDB
TFEND = 0xDC
TFESC = 0xDD
CMD_DATA = 0x00    # KISS data frame command byte (port 0)


def kiss_encode(data: bytes) -> bytes:
    """Wrap raw AX.25 frame in KISS framing."""
    escaped = bytearray()
    for byte in data:
        if byte == FEND:
            escaped += bytes([FESC, TFEND])
        elif byte == FESC:
            escaped += bytes([FESC, TFESC])
        else:
            escaped.append(byte)
    return bytes([FEND, CMD_DATA]) + bytes(escaped) + bytes([FEND])


def kiss_decode(raw: bytes) -> list[bytes]:
    """Extract all complete KISS frames from a byte buffer. Returns list of payloads."""
    frames = []
    buf = raw
    while FEND in buf[1:]:  # at least two FENDs needed
        start = buf.find(FEND)
        end = buf.find(FEND, start + 1)
        if end == -1:
            break
        frame_bytes = buf[start + 1: end]  # between FENDs
        buf = buf[end:]
        if len(frame_bytes) < 2:
            continue
        # Unescape
        unescaped = bytearray()
        i = 0
        while i < len(frame_bytes):
            b = frame_bytes[i]
            if b == FESC and i + 1 < len(frame_bytes):
                nxt = frame_bytes[i + 1]
                unescaped.append(FEND if nxt == TFEND else FESC if nxt == TFESC else nxt)
                i += 2
            else:
                unescaped.append(b)
                i += 1
        # First byte is KISS cmd byte; AX.25 payload follows
        if unescaped and (unescaped[0] & 0x0F) == 0:  # cmd 0 = data
            frames.append(bytes(unescaped[1:]))
    return frames


def ax25_to_aprs_string(ax25_frame: bytes) -> str | None:
    """
    Minimal AX.25 → APRS string decoder.
    Returns the APRS-IS style 'CALL>PATH:payload' string or None on parse error.
    Uses aprslib for final packet decode.
    """
    try:
        if len(ax25_frame) < 17:
            return None
        # Destination (7 bytes), source (7 bytes), digipeaters (7 bytes each until C-bit)
        def decode_callsign(b7: bytes) -> str:
            call = "".join(chr(b >> 1) for b in b7[:6]).strip()
            ssid = (b7[6] >> 1) & 0x0F
            return f"{call}-{ssid}" if ssid else call

        dest = decode_callsign(ax25_frame[0:7])
        src  = decode_callsign(ax25_frame[7:14])

        # Walk digipeaters
        digis = []
        pos = 14
        while not (ax25_frame[pos - 1] & 0x01):  # last address byte has low bit set
            if pos + 7 > len(ax25_frame):
                break
            digi = decode_callsign(ax25_frame[pos:pos + 7])
            has_been_repeated = bool(ax25_frame[pos + 6] & 0x80)
            digis.append(digi + ("*" if has_been_repeated else ""))
            pos += 7

        # Control + PID bytes
        pos += 2
        info = ax25_frame[pos:].decode("latin-1", errors="replace")
        path = ",".join([dest] + digis) if digis else dest
        return f"{src}>{path}:{info}"
    except Exception:
        return None


class APRSKISSAdapter(Adapter):
    """
    APRS KISS TNC adapter — serial (hardware TNC) or TCP (Direwolf).
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "aprs-kiss")
        self._transport_type = config.get("transport", "serial")
        self._serial_port = config.get("port", "/dev/ttyUSB0")
        self._baud = int(config.get("baud", 9600))
        self._host = config.get("host", "localhost")
        self._tcp_port = int(config.get("tcp_port", 8001))
        self._callsign = config.get("callsign", "N0CALL-9").upper()
        self._tx_path = config.get("tx_path", "WIDE1-1,WIDE2-1")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._run_task: asyncio.Task | None = None
        self._packet_count = 0
        self._buf = bytearray()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("APRS-KISS %s: connecting via %s", self.name, self._transport_type)
        if self._transport_type == "serial":
            try:
                import serial_asyncio
            except ImportError as exc:
                raise ImportError(
                    "APRS-KISS serial transport requires serial_asyncio: "
                    "pip install pyserial-asyncio"
                ) from exc
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._serial_port, baudrate=self._baud
            )
            log.info("APRS-KISS %s: serial %s @ %d baud", self.name, self._serial_port, self._baud)
        else:
            # tcp or agwpe (agwpe uses port 8000 default; set tcp_port accordingly)
            self._reader, self._writer = await asyncio.open_connection(self._host, self._tcp_port)
            log.info("APRS-KISS %s: TCP %s:%d", self.name, self._host, self._tcp_port)

        # Send KISS initialization: set TX delay, persistence
        await self._send_kiss_cmd(0x01, bytes([50]))   # TX delay = 500ms
        await self._send_kiss_cmd(0x02, bytes([63]))   # persistence = 63
        await self._send_kiss_cmd(0x03, bytes([10]))   # slot time = 100ms

        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("APRS-KISS %s: connected, monitoring all APRS traffic", self.name)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        log.info("APRS-KISS %s: disconnected", self.name)

    async def _send_kiss_cmd(self, cmd: int, data: bytes) -> None:
        """Send a KISS command frame (non-data, e.g. TXDelay)."""
        port_cmd = (0 << 4) | (cmd & 0x0F)
        frame = bytes([FEND, port_cmd]) + data + bytes([FEND])
        self._writer.write(frame)
        await self._writer.drain()

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Encode and transmit an APRS packet via KISS.
        Builds a minimal AX.25 UI frame from the message body.
        """
        if not self._connected:
            return False
        try:
            ax25 = self._build_ax25_ui(message.body)
            frame = kiss_encode(ax25)
            self._writer.write(frame)
            await self._writer.drain()
            self._mark_tx(message)
            log.debug("APRS-KISS %s: TX %d bytes: %s", self.name, len(ax25), message.body[:60])
            return True
        except Exception as exc:
            log.error("APRS-KISS %s: send error: %s", self.name, exc)
            return False

    def _build_ax25_ui(self, info: str) -> bytes:
        """Build minimal AX.25 UI frame for APRS transmission."""
        def encode_callsign(call: str, last: bool = False) -> bytes:
            call = call.upper()
            if "-" in call:
                base, ssid_str = call.split("-", 1)
                ssid = int(ssid_str) & 0x0F
            else:
                base, ssid = call, 0
            b = bytearray()
            base_padded = base.ljust(6)[:6]
            for c in base_padded:
                b.append(ord(c) << 1)
            ssid_byte = 0x60 | (ssid << 1)
            if last:
                ssid_byte |= 0x01
            b.append(ssid_byte)
            return bytes(b)

        dest_parts = self._tx_path.split(",") if self._tx_path else []
        dest_call = dest_parts[0] if dest_parts else "APRS"
        digis = dest_parts[1:] if len(dest_parts) > 1 else []

        frame = bytearray()
        frame += encode_callsign(dest_call)
        is_last = len(digis) == 0
        frame += encode_callsign(self._callsign, last=is_last)
        for i, digi in enumerate(digis):
            frame += encode_callsign(digi, last=(i == len(digis) - 1))

        frame += bytes([0x03, 0xF0])  # Control (UI) + PID (no layer 3)
        frame += info.encode("ascii", errors="replace")
        return bytes(frame)

    # ── Internal receive loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        log.debug("APRS-KISS %s: RX loop started", self.name)
        try:
            while self._connected:
                chunk = await asyncio.wait_for(
                    self._reader.read(1024), timeout=2.0
                )
                if not chunk:
                    raise ConnectionError("APRS-KISS: EOF on stream")
                self._buf.extend(chunk)
                await self._process_buffer()

        except asyncio.TimeoutError:
            pass  # Normal: no data for 2s, loop back
        except asyncio.CancelledError:
            log.debug("APRS-KISS %s: RX loop cancelled", self.name)
        except ConnectionError as exc:
            log.error("APRS-KISS %s: %s", self.name, exc)
            self._connected = False

    async def _process_buffer(self) -> None:
        """Extract complete KISS frames from the accumulation buffer."""
        frames = kiss_decode(bytes(self._buf))
        if frames:
            # Trim buffer: keep only data after the last consumed FEND
            last_fend = bytes(self._buf).rfind(FEND)
            if last_fend != -1:
                self._buf = self._buf[last_fend:]

        for ax25_frame in frames:
            aprs_str = ax25_to_aprs_string(ax25_frame)
            if not aprs_str:
                continue
            try:
                import aprslib
                packet = aprslib.parse(aprs_str)
                await self._process_packet(packet, aprs_str)
            except ImportError as exc:
                log.warning("APRS-KISS: aprslib not installed — pip install aprslib")
                return
            except Exception as exc:
                log.debug("APRS-KISS %s: parse error on %r: %s", self.name, aprs_str[:60], exc)

    async def _process_packet(self, packet: dict, raw_str: str) -> None:
        self._packet_count += 1
        from_id = packet.get("from", raw_str.split(">")[0] if ">" in raw_str else "UNKNOWN")
        body = self._packet_to_body(packet, raw_str)
        if not body:
            return

        lat = packet.get("latitude")
        lon = packet.get("longitude")

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"144.390 (KISS)",
            from_id=from_id,
            from_display=from_id,
            body=body,
            lat=float(lat) if lat else None,
            lon=float(lon) if lon else None,
            raw={"format": packet.get("format", ""), "raw": raw_str[:120]},
        )
        await self._enqueue(msg)
        log.debug("APRS-KISS %s: pkt from %s: %s", self.name, from_id, body[:60])

    def _packet_to_body(self, packet: dict, raw_str: str) -> str:
        fmt = packet.get("format", "")
        from_id = packet.get("from", "?")

        if fmt == "message":
            text = packet.get("message_text", "").strip()
            dest = packet.get("addresse", "").strip()
            return f"MSG {from_id}→{dest}: {text}" if text else ""

        elif fmt in ("uncompressed", "compressed", "mic-e", "base91"):
            comment = packet.get("comment", "").strip()
            speed = packet.get("speed")
            parts = [comment] if comment else []
            if speed:
                parts.append(f"{speed:.0f}km/h")
            return f"POS {from_id}" + (f": {' '.join(parts)}" if parts else "")

        elif fmt == "status":
            status = packet.get("status", "").strip()
            return f"STATUS {from_id}: {status}" if status else ""

        elif fmt == "wx":
            temp = packet.get("wx_temp")
            return f"WX {from_id}: {temp:.0f}°F" if temp else f"WX {from_id}"

        # Fallback to raw
        info_part = raw_str.split(":", 1)[-1] if ":" in raw_str else raw_str
        return info_part[:120].strip() or ""

    def _health_detail(self) -> dict:
        return {
            "transport": self._transport_type,
            "port": self._serial_port if self._transport_type == "serial" else f"{self._host}:{self._tcp_port}",
            "callsign": self._callsign,
            "packets_received": self._packet_count,
        }
