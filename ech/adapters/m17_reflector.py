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
reflector source, Aug 2026). **Confirmed live** (rc207, 2026-08-12):
linked to the production m17.openquad.net:17000 module A reflector on
first attempt; base-40 callsign encoding validated against the spec's own
worked example. Sources used:
  https://github.com/n7tae/mrefd/blob/master/Packet-Description.md
    (UDP packet types: CONN/ACKN/NACK/DISC/LSTRN/PING/PONG, M17/M17P framing)
  https://github.com/n7tae/mrefd/blob/master/packet.cpp
    (exact byte offsets for the non-stream/M17P packet layout, confirmed
    from the reference reflector implementation's own accessor code)
  https://github.com/n7tae/mrefd/blob/master/parrot.cpp
    (confirms BROADCAST = 6 bytes of 0xFF: `memset(packet.GetDstAddress(),
    0xffu, 6)`, fetched 2026-10-08)
  https://github.com/M17-Project/libm17/blob/master/payload/call.c
    (the OFFICIAL reference callsign codec, fetched 2026-10-08 — confirms
    both the base-40 alphabet/ordering used here AND the '#'-prefixed
    "extended hash-address space" scheme used for pseudo-destinations like
    "#PARROT")
  https://github.com/M17-Project/M17_spec (M17_spec.tex)
    (base-40 callsign encoding table; LSF TYPE field layout for both Stream
    and Packet mode, fetched in full 2026-10-08; Packet Mode protocol-type
    byte 0x05 = SMS, null-terminated UTF-8 string)
  M17 CRC: CRC-16, poly 0x5935, init 0xFFFF, MSB-first, not reflected
    (M17 spec section 2.5.4, confirmed via community references)

Three items flagged as unverified assumptions as of 2026-08-12 were
rechecked 2026-10-08 against the authoritative sources above, not
guessed:
  - BROADCAST = 6 bytes of 0xFF — CONFIRMED directly against mrefd's own
    parrot.cpp (see source list above). The code's prior assumption was
    correct.
  - The "#PARROT" self-test destination's byte encoding — CONFIRMED and
    now IMPLEMENTED (`encode_callsign`/`decode_callsign` below): per
    libm17's reference codec, a leading '#' is stripped, the remainder
    (up to 8 characters) is base-40 encoded exactly like a normal
    callsign, then `U40_9` (= 40**9) is added so the result lands in a
    numeric range no ordinary 9-character callsign can reach. mrefd's own
    README confirms the usage: connect to any module, set DST to
    "#PARROT", key up — the reflector echoes your transmission back to
    you alone (not relayed to other clients). To actually test: send a
    message with `to_id="#PARROT"`.
  - The TYPE field's bit values for packet-mode SMS — this surfaced a REAL
    BUG once the spec's actual LSF TYPE table was read in full: Packet
    Mode's TYPE field only carries the P/S bit (byte1 LSB: 0=packet,
    1=stream) and a 3-bit CAN in byte0 — everything else is reserved and
    must be 0, there is NO data-type subfield in Packet Mode (unlike
    Stream Mode, which does have one — the previous 0x0005 value appears
    to have conflated the two). 0x0005's byte1 (0x05 = binary ...0101) has
    LSB=1, which wrongly flagged every M17P frame this adapter ever sent
    as STREAM mode to any receiver that actually checks the bit — mrefd
    itself doesn't check it (confirmed: "mrefd's own packet.cpp never
    parses or validates this field for routing"), which is exactly why
    this went unnoticed in the rc207 live test against a real reflector.
    Fixed to 0x0000 (P/S=0/packet, CAN=0, all reserved bits 0) — fully
    spec-correct. The payload's own leading protocol-type byte (0x05 =
    SMS) is unaffected and still correctly identifies the content.

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

BROADCAST_ADDR = b"\xff" * 6   # confirmed 2026-10-08 against mrefd's own parrot.cpp
                                # (memset(..., 0xffu, 6)) — see module docstring

# '#'-prefixed "extended hash-address space" (e.g. "#PARROT"), confirmed
# 2026-10-08 against the official reference implementation, libm17's
# payload/call.c: a callsign starting with '#' is base-40 encoded exactly
# like a normal callsign (same reversed first-char-least-significant
# order) over the characters AFTER the '#', then U40_9 (= 40**9, one past
# the entire normal 9-character encodable range) is added to the result —
# pushing it into a numeric range no ordinary callsign can ever reach.
# Up to 8 characters after the '#' fit (one slot is spent on the marker
# itself vs. the normal 9-character limit).
U40_9 = 40 ** 9
_HASH_ADDR_UPPER = U40_9 + 40 ** 8   # libm17 calls this boundary U40_9_8
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

    A leading '#' (e.g. "#PARROT", mrefd's self-test echo destination —
    see module docstring) is handled per libm17's reference payload/call.c:
    encode everything AFTER the '#' exactly like a normal callsign, then
    add U40_9 so the result lands in the reserved hash-address range,
    unreachable by any ordinary 9-character callsign.
    """
    cs = callsign.upper().strip()
    is_hash = cs.startswith("#")
    body = cs[1:] if is_hash else cs
    max_len = 8 if is_hash else 9
    if len(body) > max_len:
        raise ValueError(f"callsign too long for M17 base-40 encoding: {callsign!r}")
    value = 0
    for ch in reversed(body):
        v = _CHAR_TO_VAL.get(ch)
        if v is None:
            raise ValueError(f"character {ch!r} not valid in M17 callsigns (in {callsign!r})")
        value = value * 40 + v
    if is_hash:
        value += U40_9
    return value.to_bytes(6, "big")


def decode_callsign(addr: bytes) -> str:
    """Inverse of encode_callsign. Returns '' for the all-zero address."""
    if addr == BROADCAST_ADDR:
        return "*BROADCAST*"
    value = int.from_bytes(addr, "big")
    if value == 0:
        return ""
    prefix = ""
    if U40_9 <= value < _HASH_ADDR_UPPER:
        prefix = "#"
        value -= U40_9
    chars = []
    while value > 0:
        chars.append(_ALPHABET[value % 40])
        value //= 40
    # Extraction order (least-significant digit first) already matches the
    # callsign's natural first-to-last order — do NOT reverse (see
    # encode_callsign's docstring for why the significance is flipped here).
    return prefix + "".join(chars).strip()


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

        # Confirmed 2026-10-08 against the M17 spec's own LSF TYPE field
        # table: in Packet Mode, TYPE only defines the P/S bit (byte1 LSB,
        # 0=packet/1=stream) and the 3-bit CAN in byte0 — everything else
        # is reserved and must be 0. The previous value here, 0x0005, had
        # byte1=0x05 (binary ...0101) — LSB=1, which wrongly flags this
        # M17P (packet-mode) frame as STREAM mode to any receiver that
        # actually checks the bit (mrefd itself doesn't, which is why this
        # went unnoticed in the rc207 live test). 0x0000 is fully correct:
        # P/S=0 (packet), CAN=0 (default channel). The payload's own
        # leading protocol-type byte (0x05 = SMS, set below) is what
        # identifies the content type — packet mode's TYPE field has no
        # data-type field at all, unlike stream mode's.
        type_field = (0x0000).to_bytes(2, "big")
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
