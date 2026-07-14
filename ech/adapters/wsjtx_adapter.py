"""
ech/adapters/wsjtx_adapter.py
-----------------------------
FT8/FT4 (and every other WSJT-X mode) via the WSJT-X UDP protocol — AUD-7.

WSJT-X does all its own audio DSP and broadcasts decodes as binary UDP
datagrams (Settings → Reporting → "UDP Server", default 127.0.0.1:2237).
This adapter listens on that port, so no soundcard work is needed at all —
the same integration pattern as JS8Call. Point WSJT-X's UDP Server at the
ECH machine and every FT8/FT4 decode lands in the message feed with SNR,
frequency offset, and time drift attached.

Wire format (WSJT-X NetworkMessage.hpp, QDataStream big-endian):
  header: magic uint32 0xadbccbda, schema uint32, msgtype uint32,
          id utf8 (uint32 len, 0xFFFFFFFF = null)
  type 0 Heartbeat: max_schema u32, version utf8, revision utf8
  type 1 Status:    dial_freq u64, mode utf8, dx_call utf8, report utf8,
                    tx_mode utf8, tx_enabled u8, transmitting u8, decoding u8, …
  type 2 Decode:    is_new u8, qtime_ms u32, snr i32, delta_t f64 (double),
                    delta_f u32, mode utf8, message utf8, low_conf u8, off_air u8

RX-only: replying to an FT8 CQ requires driving WSJT-X's own TX sequencing
(message type 4 Reply) — deliberately out of scope for now.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

_MAGIC = 0xADBCCBDA


class _Reader:
    """Minimal QDataStream reader (big-endian) for the WSJT-X wire format."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("short datagram")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:  return self._take(1)[0]
    def u32(self) -> int: return struct.unpack(">I", self._take(4))[0]
    def i32(self) -> int: return struct.unpack(">i", self._take(4))[0]
    def u64(self) -> int: return struct.unpack(">Q", self._take(8))[0]
    def f64(self) -> float: return struct.unpack(">d", self._take(8))[0]

    def utf8(self) -> str:
        n = self.u32()
        if n == 0xFFFFFFFF:      # QDataStream null string
            return ""
        return self._take(n).decode("utf-8", errors="replace")


def _guess_caller(message: str) -> str:
    """Best-effort 'who sent this' from an FT8 payload: 'CQ W1ABC FN43' → W1ABC;
    'K1XYZ W1ABC -12' → W1ABC (the second token is the sender in a standard
    directed exchange). Heuristic only — kept in from_display, never trusted."""
    toks = message.strip().split()
    if not toks:
        return ""
    if toks[0] == "CQ":
        # CQ [modifier] CALL [grid] — modifier (DX/POTA/…) is ≤4 chars, no digit-letter mix
        for t in toks[1:3]:
            if any(c.isdigit() for c in t) and any(c.isalpha() for c in t):
                return t
        return toks[1] if len(toks) > 1 else ""
    return toks[1] if len(toks) > 1 else toks[0]


class WSJTXAdapter(Adapter):
    send_enabled = False   # RX-only (see module docstring)

    def __init__(self, config: dict):
        super().__init__(config)
        self._host = config.get("host", "0.0.0.0")
        self._port = int(config.get("port", 2237))
        self._transport = None
        self._client_id = ""
        self._client_version = ""
        self._dial_freq = 0
        self._mode = ""
        self._decode_count = 0
        self._last_heartbeat: datetime | None = None

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        adapter = self

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    adapter._handle_datagram(data)
                except Exception as exc:
                    log.debug("WSJT-X %s: datagram parse error: %s", adapter.name, exc)

        self._transport, _ = await loop.create_datagram_endpoint(
            _Proto, local_addr=(self._host, self._port))
        self._connected = True
        log.info("WSJT-X %s: listening for UDP on %s:%d — point WSJT-X's "
                 "'UDP Server' setting here", self.name, self._host, self._port)

    async def disconnect(self) -> None:
        self._connected = False
        if self._transport:
            self._transport.close()
            self._transport = None
        log.info("WSJT-X %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        return False

    async def _run(self) -> None:
        # All work happens in datagram_received; nothing to poll.
        while self._connected:
            await asyncio.sleep(3600)

    # ── datagram handling ─────────────────────────────────────────────────

    def _handle_datagram(self, data: bytes) -> None:
        r = _Reader(data)
        if r.u32() != _MAGIC:
            return
        r.u32()                      # schema — fields we read are stable across 2/3
        msgtype = r.u32()
        self._client_id = r.utf8()

        if msgtype == 0:             # Heartbeat
            r.u32()                  # max schema
            self._client_version = r.utf8()
            self._last_heartbeat = datetime.now(timezone.utc)

        elif msgtype == 1:           # Status
            self._dial_freq = r.u64()
            self._mode = r.utf8()
            self._last_rx = datetime.now(timezone.utc)

        elif msgtype == 2:           # Decode
            is_new = r.u8()
            r.u32()                  # QTime ms since midnight (decode window)
            snr = r.i32()
            delta_t = r.f64()
            delta_f = r.u32()
            mode = r.utf8()
            message = r.utf8()
            if not is_new or not message.strip():
                return
            self._decode_count += 1
            self._last_rx = datetime.now(timezone.utc)
            mode_name = {"~": "FT8", "+": "FT4", "`": "FST4", "#": "JT65",
                         "@": "JT9", ":": "Q65", "&": "MSK144"}.get(mode, mode or self._mode or "FT8")
            dial_mhz = self._dial_freq / 1e6 if self._dial_freq else None
            channel = f"{mode_name.lower()} {dial_mhz:.3f}MHz" if dial_mhz else mode_name.lower()
            caller = _guess_caller(message)
            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel=channel,
                from_id=caller or "wsjtx",
                from_display=f"{caller or mode_name} ({snr:+d}dB)",
                body=message.strip(),
                priority=Priority.NORMAL,
                raw={"mode": mode_name, "snr_db": snr, "delta_t": round(delta_t, 2),
                     "delta_f_hz": delta_f, "dial_freq_hz": self._dial_freq or None},
            )
            asyncio.ensure_future(self._enqueue(msg))

    def _health_detail(self) -> dict:
        return {
            "listen": f"{self._host}:{self._port}",
            "wsjtx_client": self._client_id or "none seen yet",
            "wsjtx_version": self._client_version,
            "dial_freq_mhz": round(self._dial_freq / 1e6, 4) if self._dial_freq else None,
            "mode": self._mode,
            "decodes": self._decode_count,
            "last_heartbeat": self._last_heartbeat.isoformat() if self._last_heartbeat else None,
        }
