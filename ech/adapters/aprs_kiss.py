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
import time
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core import aprs_wx
from ech.core.aprs_hub import (MIN_BEACON_INTERVAL_SEC, build_ack_info, build_message_info,
                               build_position_info, hub as aprs_hub)
from ech.core.models import MeshNode, NormalizedMessage, Priority

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
    aprs_kind = "rf"   # APRS hub: radio path (vs "is")

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
        # AX.25 destination: the APRS "tocall". tx_path is only the digipeater path.
        self._tocall = str(config.get("tocall", "APRS")).upper()
        # Position beacon over RF — off unless beacon_interval_sec is set
        # (min 600 s on RF) or set from the APRS page.
        self._beacon_lat = config.get("beacon_lat")
        self._beacon_lon = config.get("beacon_lon")
        self._beacon_symbol = config.get("beacon_symbol", "/-")
        self._beacon_comment = config.get("beacon_comment", "SignalMatrix")
        _iv = int(config.get("beacon_interval_sec", 0))
        self._beacon_interval = max(_iv, MIN_BEACON_INTERVAL_SEC["rf"]) if _iv > 0 else 0
        self._last_beacon_ts = 0.0
        self._beacon_task: asyncio.Task | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._run_task: asyncio.Task | None = None
        self._packet_count = 0
        self._buf = bytearray()
        self._nodes: dict[str, MeshNode] = {}
        self._node_ttl = int(config.get("node_ttl_sec", 3600))

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
        self._beacon_task = asyncio.create_task(self._auto_beacon_loop(), name=f"{self.name}-beacon")
        log.info("APRS-KISS %s: connected, monitoring all APRS traffic", self.name)

    async def disconnect(self) -> None:
        self._connected = False
        if self._beacon_task:
            self._beacon_task.cancel()
            self._beacon_task = None
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
        if not message.to_id:
            log.warning("APRS-KISS %s: to_id required for APRS message send", self.name)
            return False
        # Proper APRS message (":ADDRESSEE:text{n}") — previously the bare body
        # went out as the info field, which no APRS station decodes as a message.
        seq = aprs_hub.next_msgno()
        info = build_message_info(message.to_id, message.body, seq)
        if not await self.send_info(info):
            return False
        self._mark_tx(message)
        aprs_hub.track_outgoing(self.name, message.to_id, message.body, seq, info)
        return True

    def can_transmit(self) -> bool:
        return self._connected and self._writer is not None

    async def send_info(self, info: str) -> bool:
        """Transmit one APRS packet over RF with this info field."""
        if not self.can_transmit():
            return False
        try:
            ax25 = self._build_ax25_ui(info)
            self._writer.write(kiss_encode(ax25))
            await self._writer.drain()
            log.info("APRS-KISS %s: TX %s", self.name, info[:80])
            return True
        except Exception as exc:
            log.error("APRS-KISS %s: send error: %s", self.name, exc)
            return False

    # ── Position beacon (RF) ──────────────────────────────────────────────

    def set_base_location(self, lat: float, lon: float) -> None:
        self._beacon_lat, self._beacon_lon = lat, lon

    async def send_beacon(self, lat: float | None = None, lon: float | None = None,
                          comment: str | None = None) -> dict:
        use_lat = lat if lat is not None else self._beacon_lat
        use_lon = lon if lon is not None else self._beacon_lon
        if use_lat is None or use_lon is None:
            return {"status": "error", "detail": "no coordinates configured"}
        if comment:
            self._beacon_comment = comment
        info = build_position_info(float(use_lat), float(use_lon), self._beacon_symbol, self._beacon_comment)
        if not await self.send_info(info):
            return {"status": "error", "detail": "not connected"}
        self._last_beacon_ts = time.time()
        return {"status": "ok", "packet": f"{self._callsign}>{self._tocall},{self._tx_path}:{info}",
                "lat": use_lat, "lon": use_lon}

    async def _auto_beacon_loop(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(15)
                iv = self._beacon_interval
                if iv > 0 and time.time() - self._last_beacon_ts >= iv:
                    await self.send_beacon()
                    if time.time() - self._last_beacon_ts > iv:
                        self._last_beacon_ts = time.time()   # don't retry every tick on errors
        except asyncio.CancelledError:
            pass

    def _is_directed_to_us(self, addresse: str) -> bool:
        own_full = self._callsign.upper()
        return addresse.strip().upper() in (own_full.split("-")[0], own_full)

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

        # Destination = tocall (APRS); tx_path (WIDE1-1,WIDE2-1) = digipeaters.
        # Previously the first path element was used as the destination, so
        # frames went out addressed to WIDE1-1 with only WIDE2-1 as a digi.
        dest_call = self._tocall
        digis = [d.strip() for d in self._tx_path.split(",") if d.strip()] if self._tx_path else []

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
                try:
                    chunk = await asyncio.wait_for(self._reader.read(1024), timeout=2.0)
                except asyncio.TimeoutError:
                    continue  # quiet channel, keep listening (this used to end the RX loop)
                if not chunk:
                    raise ConnectionError("APRS-KISS: EOF on stream")
                self._buf.extend(chunk)
                await self._process_buffer()

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
        aprs_hub.ingest(packet, raw_str, self.name)

        # Ack messages addressed to us; a retry of one already delivered is
        # acked again but not shown twice.
        if (packet.get("format") == "message" and packet.get("msgNo") and not packet.get("response")
                and self._is_directed_to_us(packet.get("addresse", ""))):
            await self.send_info(build_ack_info(from_id, str(packet["msgNo"])))
            if aprs_hub.incoming_message_seen(from_id, str(packet["msgNo"])):
                return

        # NWS weather bulletins heard on RF → map weather layer
        if packet.get("format") == "message" and aprs_wx.store.ingest(
                from_id, packet.get("addresse", ""), packet.get("message_text", ""), via=self.name):
            return

        lat = packet.get("latitude")
        lon = packet.get("longitude")
        if lat is not None and lon is not None:
            # Station cache for the map, with the APRS symbol (aprs.fi icons)
            node_id = (packet.get("object_name") or "").strip() if packet.get("format") == "object" else ""
            node_id = node_id or from_id
            now = datetime.now(timezone.utc)
            node = self._nodes.get(node_id)
            if node is None:
                node = MeshNode(node_id=node_id, display_name=node_id, first_seen=now)
                self._nodes[node_id] = node
            node.last_heard = now
            node.lat, node.lon = float(lat), float(lon)
            if packet.get("symbol") and packet.get("symbol_table"):
                node.meta["aprs_symbol"] = str(packet["symbol_table"])[:1] + str(packet["symbol"])[:1]

        body = self._packet_to_body(packet, raw_str)
        if not body:
            return

        # Extract digipeater path: "SRC>DEST,DIGI1*,DIGI2:..." → "DEST,DIGI1*,DIGI2"
        aprs_path = None
        if ">" in raw_str and ":" in raw_str:
            aprs_path = raw_str.split(">", 1)[1].split(":", 1)[0] or None

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"144.390 (KISS)",
            from_id=from_id,
            from_display=from_id,
            body=body,
            lat=float(lat) if lat else None,
            lon=float(lon) if lon else None,
            path=aprs_path,
            raw={"format": packet.get("format", "")},
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

    async def nodes(self):
        """Stations heard on RF within node_ttl_sec (default 1 hr)."""
        cutoff = datetime.now(timezone.utc).timestamp() - self._node_ttl
        return [n for n in self._nodes.values()
                if n.last_heard and n.last_heard.timestamp() >= cutoff]

    def _health_detail(self) -> dict:
        return {
            "transport": self._transport_type,
            "port": self._serial_port if self._transport_type == "serial" else f"{self._host}:{self._tcp_port}",
            "callsign": self._callsign,
            "packets_received": self._packet_count,
        }
