"""
ech/adapters/dmr_brandmeister.py
-----------------------------------
DMR (Digital Mobile Radio) adapter speaking the Homebrew/DMRplus Repeater
Protocol (HBP) directly over UDP -- the same protocol BrandMeister, DMR+,
and hblink-based networks use. No radio hardware required; this connects
as a software "repeater", the same way a hotspot (Pi-Star, OpenSpot) or a
software bridge (hblink3 itself) would.

STATUS (2026-08-16): The connection layer -- login handshake, keepalive,
reconnect -- is REAL and LIVE-TESTED against a private hblink3 test master
(see ECH_REQUIREMENTS_AND_PROGRESS.md's DMR section, O78). DMRD frame
receive/parsing is implemented per the verified byte layout below.

The SMS/short-data payload codec (BPTC forward-error-correction, CRC16,
CSBK block sequencing) now lives in dmr_sms_codec.py -- vendored and
adapted from kf7eel/hbnet + n0mjs710/dmr_utils3 (see that module's
docstring for exact provenance and the deliberate simplifications made
vs. hbnet, e.g. always ETSI UTF-16BE rather than hbnet's per-destination
format learning). Verified via round-trip self-tests (encode then decode
recovers the original text, including multi-block messages and non-ASCII
text) since there's no live DMR SMS traffic available to check against
instead -- treat this as "believed correct by construction and self-test",
not "confirmed against a real phone/radio received a real text".

Protocol reference: HBLink-org/hblink3 (hblink.py, const.py) -- every byte
offset below was extracted directly from that source and confirmed against
a real login exchange with a live hblink3 master, not re-derived from
memory or the original research notes (which had two errors, corrected
here: RPTACK is 6 bytes not 4, and the RPTC config block is 302 bytes on
the wire, not 298).

Homebrew Protocol handshake (UDP, master listens on one port):
  RPTL(4)    + RADIO_ID(4)                          -> login request
  RPTACK(6)  + SALT(4)                               <- challenge
  RPTK(4)    + RADIO_ID(4) + SHA256(salt+pass)(32)  -> challenge response
  RPTACK(6)  + RADIO_ID(4)                           <- auth accepted
  RPTC(4)    + RADIO_ID(4) + 294-byte config block  -> repeater config
  RPTACK(6)  + RADIO_ID(4)                           <- config accepted, CONNECTED
  RPTPING(7) + RADIO_ID(4)                          -> keepalive (every ping_interval)
  MSTPONG(7) + RADIO_ID(4)                           <- keepalive ack
  MSTNAK(6)  + RADIO_ID(4)                           <- rejected, at any step

RPTC config block field offsets (294 bytes, after RPTC(4)+RADIO_ID(4)):
  CALLSIGN(8) RX_FREQ(9) TX_FREQ(9) TX_POWER(2) COLORCODE(2) LATITUDE(8)
  LONGITUDE(9) HEIGHT(3) LOCATION(20) DESCRIPTION(19) SLOTS(1) URL(124)
  SOFTWARE_ID(40) PACKAGE_ID(40)  [ASCII, space-padded to field width]

DMRD (encapsulated DMR traffic) frame layout:
  DMRD(4) seq(1) rf_src(3) dst_id(3) peer_id(4) bits(1) stream_id(4) payload(33+)
  bits: 0x80=slot2 (else slot1), 0x40=private call (else group talkgroup),
        0x30>>4=frame_type (0=voice,1=voice-sync,2=data-sync,3=CSBK-ish --
        see hblink.py's own call_type logic for the (bits&0x23)==0x23 case),
        0x0F=dtype_vseq (voice burst A-F, or data sub-type for data frames)

BrandMeister SMS routing (from the original Phase-13 research; NOT
re-verified this session): talkgroups are irrelevant to SMS -- it routes
via a PRIVATE CALL (bits & 0x40 set) to a service DMR ID: 262995 = SMS
gateway (phone delivery, "SMSGTE @<number> <msg>"), 262993/262994 =
info/weather/DAPNET-routing queries.

Config keys:
  name           str    adapter name (default: dmr)
  master_host    str    HBP master hostname/IP (REQUIRED)
  master_port    int    HBP master UDP port (REQUIRED -- there is no
                         universal default; e.g. 54000 for a private
                         hblink3 test rig, a BrandMeister-assigned port
                         for a real master shard)
  passphrase     str    shared login passphrase / repeater password (REQUIRED)
  radio_id       int    repeater-level DMR ID (REQUIRED -- from BrandMeister
                         self-care or your test master's config; NOT the
                         same as a personal subscriber DMR ID)
  callsign       str    repeater callsign, max 8 chars (REQUIRED)
  rx_freq_hz     int    receive frequency in Hz (default: 0 -- hotspot/software-only)
  tx_freq_hz     int    transmit frequency in Hz (default: 0)
  tx_power       int    0-99 (default: 0)
  colorcode      int    DMR color code 0-15 (default: 1)
  latitude       float  (default: 0.0)
  longitude      float  (default: 0.0)
  height_m       int    antenna height in meters (default: 0)
  location       str    free text, max 20 chars (default: "")
  description    str    free text, max 19 chars (default: "")
  url            str    max 124 chars (default: "")
  slots          int    1 or 2 -- which timeslot(s) this repeater uses (default: 1)
  sms_gateway_id int    BrandMeister SMS gateway DMR ID (default: 262995)
  sms_format     str    "etsi_be" (default, the actual standard) or
                         "motorola" -- which framing send() uses for
                         outbound text. Real hardware varies (e.g. AnyTone
                         radios commonly need "Motorola" selected in
                         BrandMeister self-care); inbound messages are
                         auto-detected regardless of this setting.
  ping_interval  float  seconds between RPTPING keepalives (default: 5.0,
                         matches hblink3's own default PING_TIME)
  ping_timeout   float  seconds of silence before the link is presumed
                         dead and a reconnect is forced (default: 30.0)

Example config (private hblink3 test rig -- see ECH_REQUIREMENTS_AND_PROGRESS.md):
  - type: dmr_brandmeister
    name: dmr-test
    master_host: 192.168.6.38
    master_port: 54000
    passphrase: echdmrtest2026
    radio_id: 3129999
    callsign: ECHTEST
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from hashlib import sha256

from ech.adapters.base import Adapter
from ech.adapters.dmr_sms_codec import SMSCodecError, decode_sms_stream, encode_sms
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

DEFAULT_SMS_GATEWAY_ID = 262995
DATA_SYNC_FRAME_TYPE = 2      # (bits & 0x30) >> 4 -- all SMS-related blocks use this, per hbnet
DTYPE_CSBK = 3
DTYPE_HEADER = 6
DTYPE_DATA_CONT = 7
MAX_REASSEMBLY_STREAMS = 32   # cap on concurrent in-progress inbound SMS reassemblies


def _priority(text: str) -> Priority:
    lower = text.lower()
    if any(w in lower for w in ("mayday", "emergency", "sos")):
        return Priority.EMERGENCY
    if any(w in lower for w in ("urgent", "priority", "immediate")):
        return Priority.ELEVATED
    return Priority.NORMAL


def _pad(s: str, n: int) -> bytes:
    """ASCII-encode and space-pad/truncate to exactly n bytes, per the RPTC
    config block's fixed-width ASCII fields."""
    b = str(s).encode("ascii", errors="replace")[:n]
    return b + b" " * (n - len(b))


def _int_id(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _build_dmrd_frame(seq: int, rf_src: int, dst_id: int, peer_id: int,
                       is_private: bool, slot: int, dtype_vseq: int,
                       stream_id: int, payload33: bytes) -> bytes:
    """Wrap a 33-byte DMR burst payload in a DMRD frame. bits layout and
    frame_type=2 (Data Sync, fixed for CSBK/header/data blocks alike) match
    hbnet's mmdvm_encapsulate() -- see dmr_sms_codec.py's module docstring."""
    bits = 0
    if slot == 2:
        bits |= 0x80
    if is_private:
        bits |= 0x40
    bits |= (DATA_SYNC_FRAME_TYPE & 0x3) << 4
    bits |= (dtype_vseq & 0xF)
    return (b"DMRD" + bytes([seq & 0xFF]) + rf_src.to_bytes(3, "big")
            + dst_id.to_bytes(3, "big") + peer_id.to_bytes(4, "big")
            + bytes([bits]) + stream_id.to_bytes(4, "big") + payload33)


class _HBProtocol(asyncio.DatagramProtocol):
    def __init__(self, adapter: "DMRBrandmeisterAdapter"):
        self._adapter = adapter
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self._adapter._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        log.warning("DMR %s: socket error: %s", self._adapter.name, exc)


class DMRBrandmeisterAdapter(Adapter):
    """Homebrew/DMRplus Repeater Protocol client (BrandMeister, DMR+, or a
    private hblink3 test master). See module docstring for protocol/status."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "dmr")
        self._host = config.get("master_host", "")
        self._port = int(config.get("master_port", 0))
        self._passphrase = config.get("passphrase", "").encode("utf-8")
        radio_id = config.get("radio_id")
        self._callsign = config.get("callsign", "").upper().strip()

        if not self._host:
            raise ValueError("DMRBrandmeisterAdapter: 'master_host' is required")
        if not self._port:
            raise ValueError("DMRBrandmeisterAdapter: 'master_port' is required")
        if not self._passphrase:
            raise ValueError("DMRBrandmeisterAdapter: 'passphrase' is required")
        if not radio_id:
            raise ValueError("DMRBrandmeisterAdapter: 'radio_id' is required")
        if not self._callsign:
            raise ValueError("DMRBrandmeisterAdapter: 'callsign' is required")

        self._radio_id = int(radio_id)
        self._radio_id_bytes = self._radio_id.to_bytes(4, "big")

        self._rx_freq_hz = int(config.get("rx_freq_hz", 0))
        self._tx_freq_hz = int(config.get("tx_freq_hz", 0))
        self._tx_power = int(config.get("tx_power", 0))
        self._colorcode = int(config.get("colorcode", 1))
        self._latitude = float(config.get("latitude", 0.0))
        self._longitude = float(config.get("longitude", 0.0))
        self._height_m = int(config.get("height_m", 0))
        self._location = config.get("location", "")
        self._description = config.get("description", "")
        self._url = config.get("url", "")
        self._slots = int(config.get("slots", 1))
        self._sms_gateway_id = int(config.get("sms_gateway_id", DEFAULT_SMS_GATEWAY_ID))
        self._sms_format = config.get("sms_format", "etsi_be")
        if self._sms_format not in ("etsi_be", "motorola"):
            raise ValueError(f"DMRBrandmeisterAdapter: sms_format must be 'etsi_be' or "
                              f"'motorola', got {self._sms_format!r}")
        self._ping_interval = float(config.get("ping_interval", 5.0))
        self._ping_timeout = float(config.get("ping_timeout", 30.0))

        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _HBProtocol | None = None
        self._run_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._pending: asyncio.Future | None = None
        self._last_rx_from_master = 0.0
        self._rx_count = 0
        self._tx_count = 0
        self._data_frame_count = 0
        self._sms_decoded_count = 0
        self._last_error: str | None = None
        # In-progress inbound SMS reassembly, keyed by (rf_src, stream_id) ->
        # list of raw 33-byte bursts received so far, in receipt order.
        # decode_sms_stream() is retried on every new burst for a given key
        # (cheap; a full message is only a handful of blocks) and its own
        # "incomplete" error just means "not done yet" -- no separate BTF
        # tracking needed here. Bounded by MAX_REASSEMBLY_STREAMS so a burst
        # of unrelated/garbled data traffic can't grow this unboundedly.
        self._sms_reassembly: dict[tuple, list[bytes]] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        loop = asyncio.get_event_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _HBProtocol(self),
            remote_addr=(self._host, self._port),
        )
        try:
            await self._login()
        except Exception:
            self._transport.close()
            self._transport = None
            raise

        self._connected = True
        self._last_rx_from_master = time.monotonic()
        self._last_error = None
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        self._watchdog_task = asyncio.create_task(self._watchdog(), name=f"{self.name}-watchdog")
        log.info("DMR %s: connected to %s:%d as %s (radio_id=%d)",
                 self.name, self._host, self._port, self._callsign, self._radio_id)

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
            self._transport.close()
            self._transport = None
        log.info("DMR %s: disconnected", self.name)

    async def _login(self) -> None:
        """The Homebrew login handshake, byte-for-byte as verified against a
        live hblink3 master -- see module docstring."""
        self._send_raw(b"RPTL" + self._radio_id_bytes)
        resp = await self._await_reply(timeout=10.0)
        if resp[:6] == b"MSTNAK":
            raise ConnectionError(f"DMR {self.name}: login rejected by master (MSTNAK) "
                                   f"-- radio_id {self._radio_id} may be denied by REG_ACL, "
                                   f"or the master is full (MAX_PEERS)")
        if resp[:6] != b"RPTACK" or len(resp) < 10:
            raise ConnectionError(f"DMR {self.name}: unexpected login reply: {resp!r}")
        salt = resp[6:10]

        pass_hash = sha256(salt + self._passphrase).digest()
        self._send_raw(b"RPTK" + self._radio_id_bytes + pass_hash)
        resp = await self._await_reply(timeout=10.0)
        if resp[:6] == b"MSTNAK":
            raise ConnectionError(f"DMR {self.name}: authentication rejected -- check 'passphrase'")
        if resp[:6] != b"RPTACK" or resp[6:10] != self._radio_id_bytes:
            raise ConnectionError(f"DMR {self.name}: unexpected auth reply: {resp!r}")

        self._send_raw(b"RPTC" + self._build_config_block())
        resp = await self._await_reply(timeout=10.0)
        if resp[:6] == b"MSTNAK":
            raise ConnectionError(f"DMR {self.name}: configuration rejected by master")
        if resp[:6] != b"RPTACK" or resp[6:10] != self._radio_id_bytes:
            raise ConnectionError(f"DMR {self.name}: unexpected config-ack reply: {resp!r}")

    def _build_config_block(self) -> bytes:
        return b"".join([
            self._radio_id_bytes,
            _pad(self._callsign, 8),
            _pad(str(self._rx_freq_hz).zfill(9), 9),
            _pad(str(self._tx_freq_hz).zfill(9), 9),
            _pad(str(self._tx_power).zfill(2), 2),
            _pad(str(self._colorcode).zfill(2), 2),
            _pad(f"{self._latitude:.4f}", 8),
            _pad(f"{self._longitude:.4f}", 9),
            _pad(str(self._height_m), 3),
            _pad(self._location, 20),
            _pad(self._description, 19),
            str(self._slots).encode("ascii")[:1] or b"1",
            _pad(self._url, 124),
            _pad(time.strftime("%Y%m%d"), 40),
            _pad("ECH_DMR", 40),
        ])

    # ── Send ──────────────────────────────────────────────────────────────

    async def send(self, message: NormalizedMessage) -> bool:
        """Encode message.body as an ETSI TS 102 361-3 SMS (UTF-16BE) and
        transmit it as a private-call DMRD burst sequence -- CSBK preamble,
        data header, then data blocks, in the seq/dtype_vseq order hbnet's
        reference sender uses (see dmr_sms_codec.py for the codec itself).

        message.to_id is always a DMR ID (int as string), not a phone
        number -- to reach BrandMeister's SMS-to-phone gateway, set to_id
        to the gateway ID (sms_gateway_id, default 262995) and put the
        gateway's own convention in the body yourself, e.g.
        "SMSGTE @+15551234567 message text" (see module docstring).
        """
        if not self._transport or not self._connected:
            return False

        try:
            dst_id = int(message.to_id) if message.to_id else self._sms_gateway_id
        except (TypeError, ValueError):
            log.error("DMR %s: to_id must be a DMR ID, got %r", self.name, message.to_id)
            return False

        try:
            bursts = encode_sms(dst_id, self._radio_id, message.body,
                                 is_private=True, sms_format=self._sms_format)
        except SMSCodecError as exc:
            log.error("DMR %s: SMS encode failed: %s", self.name, exc)
            return False

        stream_id = random.randint(1, 999999)
        # bursts[0]=CSBK (seq 0), bursts[1]=header (seq 0), bursts[2:]=data
        # blocks (seq 1, 2, 3...) -- matches hbnet's create_sms_seq() ordering.
        framed = [(0, DTYPE_CSBK, bursts[0]), (0, DTYPE_HEADER, bursts[1])]
        framed += [(i, DTYPE_DATA_CONT, b) for i, b in enumerate(bursts[2:], start=1)]

        for seq, dtype, payload in framed:
            frame = _build_dmrd_frame(seq, self._radio_id, dst_id, self._radio_id,
                                       True, self._slots, dtype, stream_id, payload)
            self._send_raw(frame)

        self._tx_count += 1
        self._mark_tx(message)
        log.info("DMR %s: sent SMS (%d chars, %d blocks) to DMR ID %d",
                 self.name, len(message.body), len(bursts), dst_id)
        return True

    def _send_raw(self, data: bytes) -> None:
        if self._transport:
            self._transport.sendto(data)

    # ── Receive ───────────────────────────────────────────────────────────

    def _on_datagram(self, data: bytes) -> None:
        self._last_rx_from_master = time.monotonic()

        if self._pending is not None and not self._pending.done():
            self._pending.set_result(data)
            return

        if data[:4] == b"DMRD":
            self._handle_dmrd(data)
        elif data[:7] == b"MSTPONG":
            pass   # keepalive ack -- _last_rx_from_master already updated above
        elif data[:6] == b"MSTNAK":
            self._last_error = "MSTNAK received post-login -- master dropped the connection"
            log.warning("DMR %s: %s", self.name, self._last_error)
            self._connected = False
        elif data[:5] == b"MSTCL":
            self._last_error = "MSTCL received -- master is closing the connection"
            log.warning("DMR %s: %s", self.name, self._last_error)
            self._connected = False

    def _handle_dmrd(self, data: bytes) -> None:
        """Parse a DMRD frame per the verified byte layout (module docstring).
        Voice frames are not decoded (this is a text/SMS adapter, not a
        vocoder) and silently ignored. Data-sync frames (CSBK/header/data
        blocks -- see dmr_sms_codec.py) are accumulated per (rf_src,
        stream_id) and decode_sms_stream() is retried on each new one;
        its own "incomplete" error just means the sequence isn't done yet."""
        if len(data) < 53:   # DMRD header(20) + payload(33) minimum
            return
        self._rx_count += 1
        rf_src = _int_id(data[5:8])
        dst_id = _int_id(data[8:11])
        bits = data[15]
        slot = 2 if (bits & 0x80) else 1
        is_private = bool(bits & 0x40)
        frame_type = (bits & 0x30) >> 4
        stream_id = _int_id(data[16:20])
        payload = data[20:53]

        if frame_type != DATA_SYNC_FRAME_TYPE:
            return   # voice frame -- no vocoder here, nothing to do with it

        self._data_frame_count += 1
        key = (rf_src, stream_id)
        if key not in self._sms_reassembly and len(self._sms_reassembly) >= MAX_REASSEMBLY_STREAMS:
            oldest = next(iter(self._sms_reassembly))
            del self._sms_reassembly[oldest]
        bursts = self._sms_reassembly.setdefault(key, [])
        bursts.append(payload)

        try:
            text = decode_sms_stream(bursts)
        except SMSCodecError as exc:
            log.debug("DMR %s: SMS stream from %d not decodable yet (%d block(s) so far): %s",
                      self.name, rf_src, len(bursts), exc)
            return

        del self._sms_reassembly[key]
        self._sms_decoded_count += 1
        log.info("DMR %s: decoded SMS from %d to %d (slot %d, %s): %r",
                 self.name, rf_src, dst_id, slot,
                 "private call" if is_private else "group call", text[:60])
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"DMR TS{slot}",
            from_id=str(rf_src),
            from_display=str(rf_src),
            to_id=str(dst_id) if is_private else None,
            body=text,
            priority=_priority(text),
            raw={"slot": slot, "dst_id": dst_id, "is_private": is_private, "decoded": True},
        )
        asyncio.ensure_future(self._enqueue(msg))

    async def _await_reply(self, timeout: float) -> bytes:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending = None

    # ── Keepalive ─────────────────────────────────────────────────────────
    # Doubles as the required Adapter._run() override -- inbound traffic
    # arrives via the UDP protocol's datagram_received() callback
    # (_on_datagram), not a poll loop, so this coroutine's only job is the
    # periodic RPTPING keepalive.

    async def _run(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(self._ping_interval)
                self._send_raw(b"RPTPING" + self._radio_id_bytes)
        except asyncio.CancelledError:
            pass

    async def _watchdog(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(5.0)
                if time.monotonic() - self._last_rx_from_master > self._ping_timeout:
                    self._last_error = f"no traffic from master in >{self._ping_timeout:.0f}s -- link presumed dead"
                    log.warning("DMR %s: %s", self.name, self._last_error)
                    self._connected = False
        except asyncio.CancelledError:
            pass

    # ── Health ────────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        return {
            "master": f"{self._host}:{self._port}",
            "callsign": self._callsign,
            "radio_id": self._radio_id,
            "sms_format": self._sms_format,
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "data_frames_seen": self._data_frame_count,
            "sms_decoded": self._sms_decoded_count,
            "sms_reassembling": len(self._sms_reassembly),
            "last_error": self._last_error,
        }
