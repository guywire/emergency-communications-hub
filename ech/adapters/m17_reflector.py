"""
ech/adapters/m17_reflector.py
-------------------------------
M17 digital radio text messaging via a software-only connection to an M17
reflector (e.g. mrefd/urfd) — no radio hardware required. This speaks the
M17 "Internet Interface" UDP protocol directly, the same protocol used by
reflector-to-reflector interlinks and lightweight software clients (this is
how tools like M17Web's browser player work — no RF, just UDP to the
reflector).

STATUS: Implemented from primary sources (protocol docs + reference
reflector source, Aug 2026), NOT live-tested — no M17 reflector was
reachable from the environment this was written in. Verify against a real
reflector before relying on this. Sources used:
  https://github.com/n7tae/mrefd/blob/master/Packet-Description.md
    (UDP packet types: CONN/ACKN/NACK/DISC/LSTRN/PING/PONG, M17/M17P framing)
  https://github.com/n7tae/mrefd/blob/master/packet.cpp
    (exact byte offsets for the non-stream/M17P packet layout, confirmed
    from the reference reflector implementation's own accessor code)
  https://github.com/M17-Project/M17_spec (M17_spec.tex)
    (base-40 callsign encoding table; LSF field layout; Packet Mode
    protocol-type byte 0x05 = SMS, null-terminated UTF-8 string)
  M17 CRC: CRC-16, poly 0x5935, init 0xFFFF, MSB-first, not reflected
    (M17 spec section 2.5.4, confirmed via community references)

Known unverified assumptions (flagged in code below — check against a real
reflector/client before trusting):
  - BROADCAST destination address = 6 bytes of 0xFF. This is the commonly
    cited M17-ecosystem convention but was not independently confirmed
    against mrefd's own source in this session.
  - The exact byte encoding of the reflector's "#PARROT" self-test
    pseudo-station (mentioned in mrefd's README as working for both Stream
    and Packet modes) was NOT found — it's very likely NOT a plain base-40
    encoding of the literal string "#PARROT" ('#' isn't in the M17 base-40
    alphabet at all), so it is deliberately NOT implemented here rather than
    guessed. To test this adapter for real: send with `to_id` unset
    (broadcasts to the whole module) and check a reflector's web dashboard
    (e.g. a module status page like the ones at m17.hblink.network or
    m17-awv.kc1awv.net/modules.html) to see who else is currently linked to
    the module you're testing against, or connect a second M17 client
    yourself and watch for the message.
  - The TYPE field's exact bit values for packet-mode SMS are set to a
    conservative 0x0005 — mrefd's own packet.cpp never parses or validates
    this field for routing (it just relays raw bytes), so this only matters
    if a receiving client's own UI inspects it; the payload's own protocol-
    type byte (0x05) is what actually identifies this as SMS content.

Config keys:
  name              str   adapter name (default: m17)
  reflector_host    str   reflector hostname/IP (REQUIRED)
  reflector_port    int   reflector UDP port (default: 17000)
  module            str   single letter A-Z, the reflector "room" to join (REQUIRED)
  callsign          str   your callsign, used to identify this connection (REQUIRED)
  listen_only       bool  join as a listen-only client (LSTRN, never transmits) (default: False)
"""

from __future__ import annotations

import asyncio
import logging
import time

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

_ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/."
_CHAR_TO_VAL = {c: i for i, c in enumerate(_ALPHABET)}

BROADCAST_ADDR = b"\xff" * 6   # see module docstring — unverified convention
PING_INTERVAL = 3.0            # matches mrefd's own ~3s keepalive cadence
LINK_TIMEOUT = 30.0            # per Packet-Description.md: assume dead if silent this long

EMRG_WORDS = {"emergency", "mayday", "sos", "evacuate now"}
ELVT_WORDS = {"urgent", "priority", "immediate"}


def _priority(text: str) -> Priority:
    lower = text.lower()
    if any(w in lower for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in lower for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


def encode_callsign(callsign: str) -> bytes:
    """Base-40 encode a callsign into 6 bytes, per the M17 address encoding scheme.

    Per spec: "the first character of the callsign is in the least
    significant bits of the address, while the last character is encoded
    into the most significant bits" — the opposite of ordinary left-to-right
    base-N encoding. Confirmed against the spec's own worked example
    (AB1CD -> 0x9fdd51) during implementation.
    """
    cs = callsign.upper().strip()
    if len(cs) > 9:
        raise ValueError(f"callsign too long for M17 base-40 encoding: {callsign!r}")
    value = 0
    for ch in reversed(cs):
        v = _CHAR_TO_VAL.get(ch)
        if v is None:
            raise ValueError(f"character {ch!r} not valid in M17 callsigns (in {callsign!r})")
        value = value * 40 + v
    return value.to_bytes(6, "big")


def decode_callsign(addr: bytes) -> str:
    """Inverse of encode_callsign. Returns '' for the all-zero address."""
    if addr == BROADCAST_ADDR:
        return "*BROADCAST*"
    value = int.from_bytes(addr, "big")
    if value == 0:
        return ""
    chars = []
    while value > 0:
        chars.append(_ALPHABET[value % 40])
        value //= 40
    # Extraction order (least-significant digit first) already matches the
    # callsign's natural first-to-last order — do NOT reverse (see
    # encode_callsign's docstring for why the significance is flipped here).
    return "".join(chars).strip()


# ── M17 CRC-16 (poly 0x5935, init 0xFFFF, MSB-first, not reflected) ────────
_CRC_POLY = 0x5935


def _crc16_m17(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ _CRC_POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


class _M17Protocol(asyncio.DatagramProtocol):
    def __init__(self, adapter: "M17ReflectorAdapter"):
        self._adapter = adapter
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self._adapter._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        log.warning("M17 %s: socket error: %s", self._adapter.name, exc)


class M17ReflectorAdapter(Adapter):
    """Software-only M17 reflector client for text (SMS payload type) messaging."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "m17")
        self._host = config.get("reflector_host", "")
        self._port = int(config.get("reflector_port", 17000))
        self._module = config.get("module", "").upper().strip()
        self._callsign = config.get("callsign", "").upper().strip()
        self._listen_only = bool(config.get("listen_only", False))

        if not self._host:
            raise ValueError("M17ReflectorAdapter: 'reflector_host' is required in config")
        if not self._module or len(self._module) != 1 or not self._module.isalpha():
            raise ValueError("M17ReflectorAdapter: 'module' must be a single letter A-Z")
        if not self._callsign:
            raise ValueError("M17ReflectorAdapter: 'callsign' is required in config")

        self._own_addr = encode_callsign(self._callsign)
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _M17Protocol | None = None
        self._run_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._linked = asyncio.Event()
        self._last_rx_from_reflector = 0.0
        self._rx_count = 0
        self._tx_count = 0
        self._last_error: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        loop = asyncio.get_event_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _M17Protocol(self),
            remote_addr=(self._host, self._port),
        )

        # LSTRN is named as 5 chars in mrefd's own doc but every other magic
        # is exactly 4 bytes and LSTRN's packet size (11 bytes) is described
        # as "identical" to CONN's — so the actual 4-byte wire magic is
        # probably "LSTN", but this is a genuine ambiguity in the source
        # doc, not confirmed. If wrong, the reflector will just NACK or
        # silently ignore it and connect() below will time out.
        magic = b"LSTN" if self._listen_only else b"CONN"
        pkt = magic + self._own_addr + self._module.encode("ascii")
        self._transport.sendto(pkt)

        try:
            await asyncio.wait_for(self._linked.wait(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            self._transport.close()
            raise ConnectionError(
                f"M17 {self.name}: no ACKN from {self._host}:{self._port} module {self._module} "
                f"within 10s — check host/port/module, or the reflector rejected/blacklisted this callsign"
            ) from exc

        self._connected = True
        self._last_rx_from_reflector = time.monotonic()
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        self._watchdog_task = asyncio.create_task(self._watchdog(), name=f"{self.name}-watchdog")
        log.info("M17 %s: linked to %s:%d module %s as %s",
                  self.name, self._host, self._port, self._module, self._callsign)

    async def disconnect(self) -> None:
        self._connected = False
        for task in (self._run_task, self._watchdog_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._transport:
            try:
                self._transport.sendto(b"DISC" + self._own_addr)
            except Exception:
                pass
            self._transport.close()
        log.info("M17 %s: disconnected", self.name)

    # ── Send ──────────────────────────────────────────────────────────────

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Send message.body as an M17 packet-mode SMS (protocol type 0x05).
        message.to_id, if set, is base-40 encoded as the destination
        callsign; otherwise broadcasts to everyone linked to the module.
        """
        if not self._transport or not self._connected:
            return False

        try:
            dst = encode_callsign(message.to_id) if message.to_id else BROADCAST_ADDR
        except ValueError as exc:
            log.error("M17 %s: bad to_id: %s", self.name, exc)
            return False

        text = message.body
        payload = b"\x05" + text.encode("utf-8") + b"\x00"

        type_field = (0x0005).to_bytes(2, "big")   # see module docstring — TYPE bits are best-effort
        meta = b"\x00" * 14
        lsf_part = dst + self._own_addr + type_field + meta   # 28 bytes
        lsf_crc = _crc16_m17(lsf_part).to_bytes(2, "big")
        payload_crc = _crc16_m17(payload).to_bytes(2, "big")

        pkt = b"M17P" + lsf_part + lsf_crc + payload + payload_crc
        if len(pkt) > 860:   # MAX_PACKET_SIZE per mrefd's defines.h
            log.error("M17 %s: message too long (%d bytes, max ~800)", self.name, len(text))
            return False

        try:
            self._transport.sendto(pkt)
        except OSError as exc:
            self._last_error = str(exc)
            log.error("M17 %s: send error: %s", self.name, exc)
            return False

        self._tx_count += 1
        self._last_error = None
        self._mark_tx(message)
        log.info("M17 %s: sent %d-byte packet to %s (dst=%s, tx_count=%d)",
                  self.name, len(pkt), f"{self._host}:{self._port}",
                  message.to_id or "BROADCAST", self._tx_count)
        return True

    # ── Receive ───────────────────────────────────────────────────────────

    def _on_datagram(self, data: bytes) -> None:
        self._last_rx_from_reflector = time.monotonic()
        if len(data) < 4:
            return
        magic = data[:4]

        if magic == b"ACKN":
            if not self._linked.is_set():
                self._linked.set()
            return
        if magic == b"NACK":
            log.warning("M17 %s: NACK received — reflector refused the link "
                        "(unknown module, or this callsign is blacklisted)", self.name)
            return
        if magic in (b"PING", b"PONG"):
            return   # keepalive only
        if magic == b"DISC":
            log.warning("M17 %s: reflector sent DISC — link dropped (blacklisted? config changed?)", self.name)
            self._connected = False
            return
        if magic != b"M17P":
            return   # ignore M17 (stream/voice) and anything else — text-only adapter

        if len(data) < 36:
            return
        dst = data[4:10]
        src = data[10:16]
        payload = data[34:-2]
        if not payload or payload[0] != 0x05:
            return   # not an SMS-type packet mode frame

        text = payload[1:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        if not text:
            return

        self._rx_count += 1
        sender = decode_callsign(src)
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"module-{self._module}",
            from_id=sender or "unknown",
            from_display=sender,
            body=text,
            priority=_priority(text),
            raw={"dst": decode_callsign(dst)},
        )
        asyncio.ensure_future(self._enqueue(msg))

    # ── Keepalive ─────────────────────────────────────────────────────────
    # This doubles as the required Adapter._run() override — inbound
    # messages arrive via the UDP protocol's datagram_received() callback
    # (_on_datagram), not a poll loop, so this coroutine's only job is the
    # periodic PING keepalive; it just also satisfies the abstract method.

    async def _run(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(PING_INTERVAL)
                if self._transport:
                    self._transport.sendto(b"PING" + self._own_addr)
        except asyncio.CancelledError:
            pass

    async def _watchdog(self) -> None:
        """Per Packet-Description.md: assume the link is dead if nothing (not
        even a keepalive) has been heard from the reflector in 30s."""
        try:
            while self._connected:
                await asyncio.sleep(5.0)
                if time.monotonic() - self._last_rx_from_reflector > LINK_TIMEOUT:
                    self._last_error = "no traffic from reflector in >30s — link presumed dead"
                    log.warning("M17 %s: %s", self.name, self._last_error)
                    self._connected = False
        except asyncio.CancelledError:
            pass

    # ── Health ────────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        return {
            "reflector": f"{self._host}:{self._port}",
            "module": self._module,
            "callsign": self._callsign,
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "last_error": self._last_error,
        }
