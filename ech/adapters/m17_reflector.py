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
  module            str   single letter A-Z, the reflector "room" to join (REQUIRED).
                          The operator can switch rooms at runtime (set_module, from the
                          compose bar); the API writes the new letter back here, so the
                          choice is remembered until changed (O152).
  callsign          str   your callsign, used to identify this connection (REQUIRED)
  listen_only       bool  join as a listen-only client (LSTN, never transmits) (default: False).
                          A second adapter with this set watches another reflector's text
                          traffic without a second transmit path (O185).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
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


# Stream mode LSF TYPE: bit 0 = 1 (stream), bits 1-2 = 2 (voice, Codec2 3200),
# no encryption, CAN 0. Same bit layout parse_m17_stream() reads on receive.
VOICE_TYPE = 0x0005
VOICE_FRAME_S = 0.04          # one stream frame = 2 Codec2 3200 frames = 40 ms
VOICE_FRAME_PCM = 640         # 320 samples of s16le
MAX_TX_S = 180.0              # transmit time-out, like app_rpt's totime
TX_IDLE_S = 1.0               # no mic audio this long = the talker let go
BUSY_S = 1.0                  # someone else's stream heard this recently = channel busy


class Leveller:
    """Transmit level control in front of Codec2, which garbles clipped input.
    Hot audio is turned down toward TARGET (fast attack, slow release); quiet
    audio is left alone (no boost, so background noise isn't raised). A final
    limiter keeps peaks under LIMIT. 2026-11-21: a desk phone sent -6 dBFS
    average / 0 dBFS peaks and the M17 echo came back unintelligible."""

    TARGET = 32768 * 10 ** (-20 / 20)       # -20 dBFS RMS
    LIMIT = 32768 * 10 ** (-3 / 20)         # -3 dBFS peak
    ATTACK, RELEASE = 0.5, 0.03             # per 40 ms block

    def __init__(self):
        self.gain = 1.0
        self.min_gain = 1.0

    def process(self, pcm: bytes) -> bytes:
        import array
        import math
        a = array.array("h", pcm[: len(pcm) // 2 * 2])
        if not a:
            return pcm
        rms = math.sqrt(sum(x * x for x in a) / len(a))
        want = min(1.0, self.TARGET / rms) if rms > 0 else 1.0
        k = self.ATTACK if want < self.gain else self.RELEASE
        self.gain += (want - self.gain) * k
        self.min_gain = min(self.min_gain, self.gain)
        peak = max(abs(x) for x in a) * self.gain
        g = self.gain * (self.LIMIT / peak if peak > self.LIMIT else 1.0)
        if g >= 0.999:
            return pcm
        return array.array("h", (int(x * g) for x in a)).tobytes()

    def text(self) -> str:
        import math
        return "no level change" if self.min_gain > 0.99 else f"turned down up to {-20 * math.log10(self.min_gain):.0f} dB"


META_GNSS = 1 << 5            # TYPE bits 5-6 (META type, no encryption): 01 = GNSS position


def gnss_meta(lat: float, lon: float) -> bytes:
    """14-byte META, GNSS position (M17 spec 2.0 "GNSS Data", same as libm17's
    set_LSF_meta_position): data source 0 = M17 client, station type 0 = fixed,
    validity = lat/lon only (altitude, velocity and radius zeroed, as the spec
    requires for invalid fields), 24-bit two's complement fractions of 90 / 180 deg."""
    la = int(max(-90.0, min(90.0, lat)) / 90.0 * 8388607)
    lo = int(max(-180.0, min(180.0, lon)) / 180.0 * 8388607)
    return (bytes([0x00, 0x80, 0x00]) + (la & 0xFFFFFF).to_bytes(3, "big")
            + (lo & 0xFFFFFF).to_bytes(3, "big") + bytes(5))


def build_stream_frame(sid: int, dst: bytes, src: bytes, fn: int, payload: bytes,
                       last: bool = False, type_field: int = VOICE_TYPE, meta: bytes = b"") -> bytes:
    """54-byte M17-over-IP stream frame: 'M17 ' + stream id + LSF (dst, src, type,
    14-byte meta) + frame number (bit 15 = last) + 16-byte payload + CRC-16 over
    everything before it."""
    body = (b"M17 " + (sid & 0xFFFF).to_bytes(2, "big") + dst + src + type_field.to_bytes(2, "big")
            + meta[:14].ljust(14, b"\x00") + (((fn & 0x7FFF) | (0x8000 if last else 0)).to_bytes(2, "big"))
            + payload[:16].ljust(16, b"\x00"))
    return body + _crc16_m17(body).to_bytes(2, "big")


class _VoiceTx:
    """One transmission: mic PCM in, paced 40 ms stream frames out."""

    def __init__(self, owner: str, dst: bytes, encoder):
        self.owner, self.dst, self.enc = owner, dst, encoder
        self.sid = int.from_bytes(os.urandom(2), "big") or 1
        self.fn = 0
        self.pcm = bytearray()
        self.frames: collections.deque = collections.deque()
        self.started = self.last_audio = time.monotonic()
        self.ending = False
        self.task: asyncio.Task | None = None
        self.underruns = 0          # 40 ms frames we had to fill with silence (gaps)
        self.leveller = Leveller()
        self.meta = b""
        self.type_field = VOICE_TYPE
        self.peak = 0               # mic level, for the log: silence vs speech
        self.sumsq = 0
        self.nsamp = 0

    def level(self, pcm: bytes) -> None:
        import array
        a = array.array("h", pcm[: len(pcm) // 2 * 2])
        if a:
            self.peak = max(self.peak, max(abs(x) for x in a))
            self.sumsq += sum(x * x for x in a)
            self.nsamp += len(a)

    def level_text(self) -> str:
        import math
        if not self.nsamp:
            return "no audio"
        rms = math.sqrt(self.sumsq / self.nsamp)
        db = lambda v: f"{20 * math.log10(v / 32768):.0f} dBFS" if v > 0 else "silent"
        return f"peak {db(self.peak)}, average {db(rms)}"


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
        self._voice_streams: dict = {}   # stream id -> (Codec2Decoder, callsign), only while someone listens
        self._tx_count = 0
        self._last_error: str | None = None
        self._switching = False      # set_module() in progress: a DISC echo is expected
        self._vtx: _VoiceTx | None = None    # our transmission in progress (O191 PTT)
        self._rx_voice_at = 0.0               # last time someone else's voice stream arrived
        self._rx_voice_who = ""
        self._voice_tx_count = 0
        # Position sent with voice (dashboards show it as a grid square):
        # position: grid6 (default; centre of the 6-character square, about 5 km),
        # exact, or off. grid: "FN54kc" overrides the hub location.
        self._position_mode = str(config.get("position", "grid6")).lower()
        self._base = None
        if config.get("grid"):
            from ech.core.grid import grid_to_latlon
            la, lo = grid_to_latlon(str(config["grid"]))
            if la is not None:
                self._base = (la, lo)
                self._position_mode = "exact"     # an explicit grid is already the precision wanted

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        loop = asyncio.get_event_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _M17Protocol(self),
            remote_addr=(self._host, self._port),
        )

        # LSTN: confirmed 2026-10-30 against mrefd's protocol.cpp - same 11-byte
        # layout as CONN, callsign must match ^[0-9]?[A-Z][A-Z0-9]{2,8}$, and
        # listen-only clients are sent packet (SMS) data from local clients
        # and peered reflectors alike. Anything they send is dropped.
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
        try:
            from ech.core.audio_hub import hub
            hub.register(f"m17:{self.name}", f"M17 {self._host} module {self._module}", "m17", tx=self)
        except Exception:
            pass
        log.info("M17 %s: linked to %s:%d module %s as %s",
                  self.name, self._host, self._port, self._module, self._callsign)

    async def set_module(self, module: str) -> str:
        """Move this link to another reflector module ("room") in place. mrefd
        routes per module, so this is DISC + CONN on the new letter; if the
        reflector doesn't ACKN within 10 s the link goes back to the previous
        module and ValueError says why. Persisting the choice (config.yaml) is
        the API's job, same as the Settings server switch (O99)."""
        module = (module or "").strip().upper()
        if len(module) != 1 or not module.isalpha():
            raise ValueError("module must be a single letter A-Z")
        if not self._transport or not self._connected:
            raise ValueError("M17 link is not connected")
        if module == self._module:
            return module
        old = self._module

        async def link(letter: str) -> bool:
            self._linked.clear()
            self._transport.sendto(b"DISC" + self._own_addr)
            self._module = letter
            self._transport.sendto((b"LSTN" if self._listen_only else b"CONN")
                                   + self._own_addr + letter.encode("ascii"))
            try:
                await asyncio.wait_for(self._linked.wait(), timeout=10.0)
                return True
            except asyncio.TimeoutError:
                return False

        self._switching = True
        try:
            ok = await link(module)
            relinked = ok or await link(old)
        finally:
            self._switching = False
        if not ok:
            log.warning("M17 %s: module %s refused or silent - back on %s%s", self.name, module, old,
                        "" if relinked else " (relink also unanswered)")
            raise ValueError(f"reflector did not accept module {module} - still on {old}")
        self._last_rx_from_reflector = time.monotonic()
        self.config["module"] = module
        log.info("M17 %s: switched from module %s to %s on %s:%d", self.name, old, module,
                 self._host, self._port)
        return module

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
        if self._listen_only:
            # mrefd drops (and logs) anything a listen-only client sends.
            log.warning("M17 %s: listen-only link, not sending", self.name)
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

    def set_base_location(self, lat: float, lon: float) -> None:
        if not self.config.get("grid") and lat is not None and lon is not None:
            self._base = (float(lat), float(lon))

    def position(self) -> tuple[float, float] | None:
        """What voice transmissions report, or None (no position, or position: off)."""
        if not self._base or self._position_mode == "off":
            return None
        if self._position_mode == "exact":
            return self._base
        from ech.core.grid import latlon_to_grid, grid_to_latlon
        return grid_to_latlon(latlon_to_grid(*self._base, 6))

    # ── Voice transmit (push-to-talk from the web page or a desk phone) ──
    # Callers: audio_hub.PhoneKeyer (*99 / # on 76x) and app.py /ws/audio (hold-to-talk).

    def can_transmit(self) -> bool:
        from ech.core.audio_hub import Codec2Encoder
        return bool(self._connected and self._transport and not self._listen_only
                    and Codec2Encoder.available())

    def voice_start(self, owner: str, dst: str = "") -> str | None:
        """Key up. Returns None, or why not (busy, listen-only, ...)."""
        from ech.core.audio_hub import Codec2Encoder
        if self._listen_only:
            return "this M17 link is listen-only"
        if not self._connected or not self._transport:
            return "M17 link is not connected"
        if self._vtx and self._vtx.owner != owner:
            return "another ECH user is transmitting on this link"
        if self._vtx and not self._vtx.ending:
            return None
        if self._vtx:
            return "still finishing the last transmission, try again"
        if time.monotonic() - self._rx_voice_at < BUSY_S:
            return f"channel busy: {self._rx_voice_who or 'someone'} is talking"
        try:
            dst_addr = encode_callsign(dst) if dst else BROADCAST_ADDR
        except ValueError as exc:
            return f"bad destination: {exc}"
        try:
            enc = Codec2Encoder(Codec2Encoder.MODE_3200)
        except RuntimeError as exc:
            return str(exc)
        v = self._vtx = _VoiceTx(owner, dst_addr, enc)
        pos = self.position()
        if pos:
            v.meta, v.type_field = gnss_meta(*pos), VOICE_TYPE | META_GNSS
        v.task = asyncio.ensure_future(self._voice_pacer(v))
        log.info("M17 %s: voice TX start (stream %04x, dst=%s, owner=%s)", self.name, v.sid,
                 dst or "BROADCAST", owner[:12])
        return None

    def voice_push(self, owner: str, pcm: bytes) -> bool:
        """Mic audio (8 kHz s16le). False = not transmitting (time-out, link down)."""
        v = self._vtx
        if not v or v.owner != owner or v.ending:
            return False
        if time.monotonic() - v.started > MAX_TX_S:
            log.info("M17 %s: voice TX time-out after %.0f s", self.name, MAX_TX_S)
            self.voice_end(owner)
            return False
        v.last_audio = time.monotonic()
        v.level(pcm)
        v.pcm += pcm
        while len(v.pcm) >= VOICE_FRAME_PCM:
            chunk, v.pcm = bytes(v.pcm[:VOICE_FRAME_PCM]), v.pcm[VOICE_FRAME_PCM:]
            v.frames.append(v.enc.encode(v.leveller.process(chunk)))
        return True

    def voice_end(self, owner: str) -> None:
        v = self._vtx
        if v and v.owner == owner and not v.ending:
            v.ending = True        # the pacer drains what is queued, then sends the last frame

    async def _voice_pacer(self, v: _VoiceTx) -> None:
        """Send one frame per 40 ms on a steady clock (mic audio arrives in bursts)."""
        silence = v.enc.encode(bytes(VOICE_FRAME_PCM))
        await asyncio.sleep(VOICE_FRAME_S * 2)               # small pre-buffer
        nxt = time.monotonic()
        try:
            while True:
                if not self._connected or not self._transport:
                    break
                if not v.ending and time.monotonic() - v.last_audio > TX_IDLE_S:
                    log.info("M17 %s: voice TX idle, unkeying", self.name)
                    v.ending = True
                if v.ending and not v.frames:
                    break
                if not v.frames and not v.ending:
                    v.underruns += 1
                payload = v.frames.popleft() if v.frames else silence
                self._send_voice_frame(v, payload, last=False)
                nxt += VOICE_FRAME_S
                await asyncio.sleep(max(0.0, nxt - time.monotonic()))
            if self._transport:
                self._send_voice_frame(v, silence, last=True)
        except asyncio.CancelledError:
            pass
        finally:
            v.enc.close()
            if self._vtx is v:
                self._vtx = None
            self._voice_tx_count += 1
            log.info("M17 %s: voice TX end (stream %04x, %d frames, %.1f s, %d gap frames, mic %s)", self.name,
                     v.sid, v.fn, v.fn * VOICE_FRAME_S, v.underruns, v.level_text() + ", " + v.leveller.text())

    def _send_voice_frame(self, v: _VoiceTx, payload: bytes, last: bool) -> None:
        try:
            self._transport.sendto(build_stream_frame(v.sid, v.dst, self._own_addr, v.fn, payload, last,
                                                      v.type_field, v.meta))
        except OSError as exc:
            self._last_error = str(exc)
        v.fn = (v.fn + 1) & 0x7FFF

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
            if self._switching:
                return   # the reflector acknowledging our own DISC during a module switch
            log.warning("M17 %s: reflector sent DISC — link dropped (blacklisted? config changed?)", self.name)
            self._connected = False
            return
        if magic == b"M17 ":
            self._on_voice(data)
            return
        if magic != b"M17P":
            return

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
            # LSF TYPE + META as the sender's client set them: compare with
            # what send() uses (TYPE 0000, META zeros) when replies go unseen.
            raw={"dst": decode_callsign(dst), "lsf_type": data[16:18].hex(),
                 "lsf_meta": data[18:32].hex(), "payload_len": len(payload)},
        )
        asyncio.ensure_future(self._enqueue(msg))

    def _on_voice(self, data: bytes) -> None:
        """M17 voice stream frame -> Codec2 -> web UI listeners (O191). Nothing is
        decoded unless a browser is listening to this adapter."""
        from ech.core.audio_hub import hub, parse_m17_stream, Codec2Decoder
        key = f"m17:{self.name}"
        f = parse_m17_stream(data)
        if not f or f["data_type"] not in (2, 3):
            return
        self._rx_voice_at = time.monotonic()      # channel-busy check for our own PTT
        if f["sid"] not in self._voice_streams:
            self._rx_voice_who = decode_callsign(f["src"])
        if not hub.has_listeners(key):
            st = self._voice_streams.pop(f["sid"], None)
            if st:
                st[0].close()
            return
        st = self._voice_streams.get(f["sid"])
        if st is None:
            try:
                dec = Codec2Decoder(Codec2Decoder.MODE_3200 if f["data_type"] == 2 else Codec2Decoder.MODE_1600)
            except RuntimeError:
                return
            st = self._voice_streams[f["sid"]] = (dec, decode_callsign(f["src"]))
        dec, who = st
        bits = f["payload"] if dec.mode == Codec2Decoder.MODE_3200 else f["payload"][:8]
        hub.publish(key, dec.decode(bits), talker=who)
        if f["last"]:
            dec.close()
            self._voice_streams.pop(f["sid"], None)

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
            "listen_only": self._listen_only,
            "voice_tx_count": self._voice_tx_count,
            "transmitting": bool(self._vtx),
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "last_error": self._last_error,
        }
