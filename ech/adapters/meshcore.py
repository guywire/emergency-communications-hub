"""
ech/adapters/meshcore.py
------------------------
Real MeshCore adapter implementing the MeshCore Companion Protocol
(v1.12.0+) over USB serial, WiFi TCP, or BLE (via meshcore_py).

Protocol reference: https://docs.meshcore.io/companion_protocol/
Official Python library: https://github.com/meshcore-dev/meshcore_py

Transport options (config 'transport' key):
  serial  — USB serial direct (default) — works everywhere, no BLE stack
  tcp     — WiFi TCP to node's built-in server (ESP32 only)
  ble     — Bluetooth LE via meshcore_py / bleak (requires BlueZ on Linux)

Config keys:
  name          str     adapter name shown in UI
  transport     str     serial | tcp | ble  (default: serial)
  port          str     /dev/ttyUSB0 or /dev/ttyACM0 (serial transport)
  baud          int     baud rate (default: 115200)
  host          str     IP address (tcp transport)
  tcp_port      int     TCP port (default: 4403)
  ble_address   str     BLE MAC address (ble transport)
  channel_idx   int     channel index to monitor/send on (default: 0)
  channel_name  str     named channel to send on, e.g. "TAC-1", "Maine Mesh"
                        looked up from device channel list at startup; overrides channel_idx
  poll_interval float   seconds between CMD_SYNC_NEXT_MESSAGE polls (default: 2.0)
  app_name      str     app identifier sent in CMD_APP_START (default: ECH)

Framing (serial/TCP):
  Outgoing (app → device):  < header + 2-byte little-endian length + payload
  Incoming (device → app):  > header + 2-byte little-endian length + payload
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from datetime import datetime, timezone
from typing import Callable

from ech.adapters.base import Adapter
from ech.core.models import ChannelHealth, MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

# ── Protocol constants ────────────────────────────────────────────────────
CMD_APP_START           = 0x01
CMD_SEND_CHANNEL_MSG    = 0x03
CMD_GET_CONTACTS        = 0x04   # returns CONTACT_START / CONTACT / CONTACT_END packets
CMD_SEND_CONTACT_MSG    = 0x05   # DM to a contact: [dest_pubkey:6][max_hops:1][txt_type:1][text]
CMD_SET_DEVICE_TIME     = 0x06
CMD_SEND_ADVERT         = 0x07   # broadcast local node advertisement to mesh channel
CMD_SET_NAME            = 0x08   # set device name; expects PACKET_OK
CMD_SYNC_NEXT_MESSAGE   = 0x0A
CMD_SEND_TRACEROUTE     = 0x0D   # actually RESET_PATH; kept for backward compat
CMD_SEND_TRACE_PATH     = 0x24   # real traceroute: tag(4)+auth(4)+flags(1)+[path]; returns TRACE_DATA 0x89
CMD_GET_BATTERY         = 0x14
CMD_DEVICE_QUERY        = 0x16
CMD_GET_CHANNEL         = 0x1F
CMD_SET_CHANNEL         = 0x20   # set channel slot; format: idx(1)+name(32)+secret(16)

PACKET_OK               = 0x00
PACKET_ERROR            = 0x01
PACKET_CONTACT_START    = 0x02   # start of GET_CONTACTS response
PACKET_CONTACT          = 0x03   # one contact record: pubkey(32)+type+flags+plen+path(64)+name(32)+last_advert(4)+lat(4)+lon(4)+lastmod(4)
PACKET_CONTACT_END      = 0x04   # end of GET_CONTACTS response
PACKET_SELF_INFO        = 0x05
PACKET_MSG_SENT         = 0x06
PACKET_CONTACT_MSG_RECV = 0x07
PACKET_CHANNEL_MSG_RECV = 0x08
PACKET_NO_MORE_MSGS     = 0x0A
PACKET_DEVICE_INFO      = 0x0D
PACKET_CONTACT_MSG_V3   = 0x10
PACKET_CHANNEL_MSG_V3   = 0x11   # polled V3 format: [SNR][reserved×2][ch_idx][plen][txt_type][ts×4][text]
PACKET_CHANNEL_INFO     = 0x12
PACKET_BATTERY          = 0x0C   # battery response: [volt_lo][volt_hi][pct?][...] uint16le mV + extra fields

PUSH_ADVERT             = 0x80
PUSH_PATH_UPDATED       = 0x81   # path change notification: [pubkey:32]
PUSH_SEND_CONFIRMED     = 0x82   # ACK for sent message: [ack_code:4]
PUSH_MSG_WAITING        = 0x83
PUSH_CHANNEL_MSG        = 0x88   # v1.15 real-time push: [ch_idx][pubkey×6][plen][txt_type][ts×4][text]
TRACE_DATA              = 0x89   # traceroute result: [reserved:1][path_len:1][flags:1][tag:4][auth:4][hashes...][snrs...][final_snr:1]

FRAME_OUT_HEADER = b'<'
FRAME_IN_HEADER  = b'>'

# Module-level registries so the MQTT adapter can discover keys without
# requiring a direct adapter reference at init time.
# Written by MeshCoreAdapter during init; keyed by adapter name.
_pubkey_registry:  dict[str, str] = {}  # adapter_name -> 64-char hex pubkey
_privkey_registry: dict[str, str] = {}  # adapter_name -> 128-char hex privkey (serial only)


def _auto_detect_serial_port() -> str:
    """Scan available serial ports and return the best candidate for a MeshCore device.
    Prefers USB-serial bridges (CP210x, CH340, FTDI). Falls back to first available port."""
    try:
        from serial.tools import list_ports
    except ImportError:
        raise ValueError(
            "pyserial not installed; cannot auto-detect port. "
            "Install pyserial or set port explicitly in config."
        )
    all_ports = list(list_ports.comports())
    if not all_ports:
        raise ValueError("MeshCore auto-detect: no serial ports found")

    # Prefer ports that look like USB-serial adapters
    usb_ports = [
        p for p in all_ports
        if p.vid is not None
        or any(kw in (p.description or "").upper() for kw in ("USB", "UART", "CP210", "CH340", "FTDI", "SERIAL"))
        or "USB" in p.device.upper()
        or "ACM" in p.device
    ]
    chosen = (usb_ports or all_ports)[0]
    log.info(
        "MeshCore auto-detect: chose %s (%s) from %d port(s): %s",
        chosen.device,
        chosen.description or "unknown",
        len(all_ports),
        [p.device for p in all_ports],
    )
    return chosen.device


def _read_null_terminated_strings(data: bytes, start: int = 0, count: int = 3, min_len: int = 4) -> list[str]:
    """Walk null terminators to extract up to `count` null-terminated ASCII strings starting at `start`."""
    results = []
    pos = start
    while pos < len(data) and len(results) < count:
        null = data.find(b'\x00', pos)
        end = null if null != -1 else len(data)
        seg = data[pos:end].decode('ascii', errors='ignore').strip()
        if len(seg) >= min_len and seg.isprintable():
            results.append(seg)
        pos = end + 1
    return results


def _scan_ascii_name(data: bytes, min_offset: int = 34, min_len: int = 3) -> str:
    """Find first run of ≥ min_len consecutive printable ASCII bytes after min_offset."""
    pos = min_offset
    while pos < len(data):
        if 0x20 <= data[pos] <= 0x7e:
            end = pos + 1
            while end < len(data) and 0x20 <= data[end] <= 0x7e:
                end += 1
            run = data[pos:end]
            if len(run) >= min_len:
                return run.decode('ascii')
            pos = end
        else:
            pos += 1
    return ""


def _extract_msg_sender(text: str) -> tuple[str, str]:
    """
    MeshCore embeds the sender's name in the message body as "name: message".
    Return (sender_name, message_body).  If no prefix found, return ("", text).
    """
    if ': ' in text:
        colon = text.index(': ')
        candidate = text[:colon]
        # Valid sender name: 1-64 chars, no control characters or newlines
        if 1 <= len(candidate) <= 64 and '\n' not in candidate and '\r' not in candidate:
            return candidate, text[colon + 2:]
    return "", text


def _is_likely_encrypted(text: str) -> bool:
    """
    Return True when the text is mostly binary garbage (AES-encrypted channel message).
    Uses the UTF-8 replacement character ratio: >20% replacement chars → encrypted.
    Threshold is conservative enough to pass legitimate messages with a few bad chars.
    """
    if not text or len(text) < 4:
        return False
    return text.count('�') / len(text) > 0.20


class MeshCoreTransport:
    """Thin async byte-stream wrapper; concrete subclasses for serial vs TCP."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def write(self, data: bytes) -> None: ...
    async def readexactly(self, n: int) -> bytes: ...

    async def read_raw(self, n: int, timeout: float = 0.5) -> bytes:
        """Read up to n bytes with a timeout. Returns b'' on timeout. Serial only."""
        return b""


class SerialTransport(MeshCoreTransport):
    def __init__(self, port: str, baud: int = 115200):
        self._port = port
        self._baud = baud
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        try:
            import serial_asyncio
        except ImportError as exc:
            raise ImportError(
                "MeshCore serial transport requires serial_asyncio: "
                "pip install pyserial-asyncio"
            ) from exc
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._port, baudrate=self._baud
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in (
                "device or resource busy", "permission denied", "access is denied",
                "errno 16", "cannot open", "in use", "busy",
            )):
                raise RuntimeError(
                    f"[PORT CONFLICT] Cannot open {self._port}: {exc}. "
                    "Another process (e.g. screen, minicom, Arduino IDE) is using this port."
                ) from exc
            raise
        log.info("MeshCore serial: opened %s @ %d", self._port, self._baud)

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def readexactly(self, n: int) -> bytes:
        return await self._reader.readexactly(n)

    async def read_raw(self, n: int, timeout: float = 0.5) -> bytes:
        try:
            return await asyncio.wait_for(self._reader.read(n), timeout=timeout)
        except asyncio.TimeoutError:
            return b""


class TCPTransport(MeshCoreTransport):
    def __init__(self, host: str, port: int = 4403):
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        log.info("MeshCore TCP: connected to %s:%d", self._host, self._port)

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def readexactly(self, n: int) -> bytes:
        return await self._reader.readexactly(n)


class MeshCoreAdapter(Adapter):
    """
    Real MeshCore Companion Protocol adapter.
    Connects to a Companion Radio node over serial or TCP.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "meshcore")
        self._channel_idx = int(config.get("channel_idx", 0))
        self._channel_name = config.get("channel_name", None)   # e.g. "TAC-1", "Maine Mesh"
        self._channel_name_resolved = False
        self._poll_interval      = config.get("poll_interval", 2.0)
        self._discovery_interval = float(config.get("discovery_interval", 300.0))
        # Lightweight contacts-only refresh — just CMD_GET_CONTACTS, no announce.
        # Catches nodes the device learned about via PUSH_ADVERT that haven't sent
        # a channel message yet.  Shorter than discovery_interval.
        self._contacts_poll_interval = float(config.get("contacts_poll_interval", 30.0))
        self._app_name = config.get("app_name", "ECH")
        # Max hops for outgoing channel messages (second byte of CMD_SEND_CHANNEL_MSG).
        # 0 = device default. Set to 3 for typical local mesh (reduces network load).
        self._max_hops           = int(config.get("max_hops", 0))
        self._contacts_refresh_pending = False   # set when new nodes need name resolution
        self._transport_type = config.get("transport", "serial")

        self._transport: MeshCoreTransport = self._make_transport(config)
        self._nodes: dict[str, MeshNode] = {}
        self._channels: dict[int, str] = {}       # index → name
        self._contacts_building: dict = {}        # temporary store during GET_CONTACTS response
        self._contact_path_size: int = 64        # auto-detected on first CONTACT record
        self._contact_path_size_set: bool = False
        self._device_name: str = ""
        self._self_node_id: str = ""              # 6-byte pubkey hex of this device (from SELF_INFO)
        self._hw_model: str = ""
        self._fw_version: str = ""
        self._build_date: str = ""
        self._battery_mv: int | None = None       # last known battery voltage in mV
        self._last_expected_ack: str | None = None  # hex ACK code from MSG_SENT, matched against PUSH_SEND_CONFIRMED
        self._run_task: asyncio.Task | None = None
        self._response_waiters: dict[int, asyncio.Future] = {}
        self._packet_log: list[dict] = []         # recent raw frames for diagnostics
        # Recently sent bodies → (msg_id, sent_time) for relay echo detection (pruned after 2 min)
        self._recent_sent: dict[str, tuple[str, float]] = {}
        # Channel decryption keys: channel_idx → 32-byte secret (AES-128-ECB uses first 16 bytes)
        self._channel_keys: dict[int, bytes] = self._load_channel_keys(config)

    def _load_channel_keys(self, config: dict) -> dict[int, bytes]:
        """Load per-channel PSKs from config. Accepts base64 or hex strings."""
        import base64 as _b64
        keys: dict[int, bytes] = {}
        for ck in config.get("channel_keys", []):
            idx = int(ck.get("idx", 0))
            raw_str = str(ck.get("key", "")).strip()
            if not raw_str:
                continue
            try:
                # hex: 32 or 64 chars
                if all(c in "0123456789abcdefABCDEF" for c in raw_str) and len(raw_str) in (32, 64):
                    raw = bytes.fromhex(raw_str)
                else:
                    padding = (4 - len(raw_str) % 4) % 4
                    raw = _b64.b64decode(raw_str + "=" * padding)
                # Pad to 32 bytes (firmware stores as 32-byte PUB_KEY_SIZE secret)
                secret = (raw + b"\x00" * 32)[:32]
                keys[idx] = secret
                log.info("MeshCore %s: channel %d PSK loaded (%dB raw → 32B secret)",
                         self.name, idx, len(raw))
            except Exception as exc:
                log.warning("MeshCore %s: bad channel_key for idx %d: %s", self.name, idx, exc)
        return keys

    # Common / well-known MeshCore keys to try when the channel key is unknown.
    # Each entry: (label, key_bytes).  Key bytes are 16 bytes (AES-128 block size).
    _COMMON_KEYS: list[tuple[str, bytes]] = []  # populated lazily on first use

    @staticmethod
    def _build_common_keys() -> list[tuple[str, bytes]]:
        """Generate the list of common/default keys to try for unknown-channel messages."""
        import hashlib as _hs
        keys: list[tuple[str, bytes]] = []
        # All-zeros — simplest possible key; used by some mesh deployments by default
        keys.append(("all-zeros", bytes(16)))
        # SHA-256 of common passphrase words, first 16 bytes (how many apps derive AES keys)
        for phrase in ("meshcore", "MeshCore", "public", "Public", "default",
                       "emergency", "Emergency", "admin", "mesh", "lora"):
            k = _hs.sha256(phrase.encode()).digest()[:16]
            keys.append((f'sha256("{phrase}")', k))
        # Common Meshtastic PSK: b64decode("AQ==") padded → used on many out-of-box meshes
        try:
            import base64 as _b64
            mt_key = (_b64.b64decode("AQ==") + bytes(15))[:16]
            keys.append(("meshtastic-default", mt_key))
        except Exception:
            pass
        return keys

    def _try_decrypt_with_key(self, payload: bytes, secret: bytes) -> bytes | None:
        """
        Decrypt a MeshCore channel message payload with an explicit key.
        Format: [mac:2][ciphertext:N], ciphertext is AES-128-ECB, N must be multiple of 16.
        Returns inner text bytes (stripping [ts:4][flags:1] header), or None on failure.
        """
        if len(payload) < 18 or len(secret) < 16:
            return None
        mac_recv = payload[:2]
        ciphertext = payload[2:]
        if len(ciphertext) % 16 != 0:
            return None
        import hmac as _hmac, hashlib
        if _hmac.new(secret, ciphertext, hashlib.sha256).digest()[:2] != mac_recv:
            return None
        try:
            try:
                from Crypto.Cipher import AES as _AES
                plain = _AES.new(secret[:16], _AES.MODE_ECB).decrypt(ciphertext)
            except ImportError:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                d = Cipher(algorithms.AES(secret[:16]), modes.ECB()).decryptor()
                plain = d.update(ciphertext) + d.finalize()
        except Exception as exc:
            log.debug("MeshCore %s: AES error: %s", self.name, exc)
            return None
        if len(plain) < 5:
            return None
        return plain[5:].rstrip(b"\x00")

    def _try_decrypt(self, payload: bytes, ch_idx: int) -> bytes | None:
        """Try decryption using the configured key for ch_idx. Returns plain bytes or None."""
        secret = self._channel_keys.get(ch_idx)
        if not secret:
            return None
        result = self._try_decrypt_with_key(payload, secret)
        if result is None:
            log.debug("MeshCore %s: ch%d MAC mismatch (wrong key?)", self.name, ch_idx)
        return result

    def _try_decrypt_any(self, payload: bytes, ch_idx: int) -> tuple[bytes, str] | None:
        """
        Brute-try all known channel keys + common/default keys.
        Returns (plain_bytes, key_label) for the first key that passes MAC, or None.
        Also caches a successful key back into _channel_keys for future messages.
        """
        if not MeshCoreAdapter._COMMON_KEYS:
            MeshCoreAdapter._COMMON_KEYS = MeshCoreAdapter._build_common_keys()

        # Build candidate list: all known channel keys first, then common/default
        candidates: list[tuple[str, bytes]] = []
        for idx, secret in self._channel_keys.items():
            if idx != ch_idx:   # ch_idx already tried by _try_decrypt()
                ch_name = self._channels.get(idx, f"ch{idx}")
                candidates.append((f"ch{idx}:{ch_name}", secret))
        candidates.extend(MeshCoreAdapter._COMMON_KEYS)

        for label, secret in candidates:
            plain = self._try_decrypt_with_key(payload, secret)
            if plain is not None:
                # Cache this key so future messages on the same channel decrypt automatically
                if ch_idx not in self._channel_keys:
                    self._channel_keys[ch_idx] = secret + bytes(max(0, 32 - len(secret)))
                return plain, label
        return None

    def _make_transport(self, config: dict) -> MeshCoreTransport:
        t = config.get("transport", "serial")
        if t == "tcp":
            return TCPTransport(
                host=config["host"],
                port=config.get("tcp_port", 4403),
            )
        elif t == "serial":
            port = config.get("port", "auto")
            if port == "auto" or not port:
                port = _auto_detect_serial_port()
            return SerialTransport(port=port, baud=config.get("baud", 115200))
        else:
            raise ValueError(f"MeshCore: unsupported transport '{t}'. Use serial or tcp.")

    # ── Frame I/O ─────────────────────────────────────────────────────────

    def _build_frame(self, payload: bytes) -> bytes:
        """Outgoing frame: < + uint16le(len) + payload"""
        return FRAME_OUT_HEADER + struct.pack("<H", len(payload)) + payload

    async def _read_frame(self) -> bytes | None:
        """Read one incoming frame: > + uint16le(len) + payload.

        On framing errors (NMEA text from a wrong serial port, stale binary data)
        we scan byte-by-byte for the next '>' instead of giving up immediately —
        this lets the adapter recover when the serial port streams non-companion data.
        Limit scanning to 256 bytes per call so the loop doesn't stall forever.
        """
        try:
            for _ in range(256):
                header = await asyncio.wait_for(
                    self._transport.readexactly(1), timeout=5.0
                )
                if header == FRAME_IN_HEADER:
                    break
                log.debug("MeshCore %s: resync — skipping 0x%02x ('%s')",
                          self.name, header[0],
                          chr(header[0]) if 0x20 <= header[0] <= 0x7e else '.')
            else:
                return None  # 256 non-header bytes — likely wrong port or baud
            length_bytes = await self._transport.readexactly(2)
            length = struct.unpack("<H", length_bytes)[0]
            if length == 0 or length > 512:
                log.warning("MeshCore %s: suspicious frame length %d, skipping", self.name, length)
                return None
            payload = await self._transport.readexactly(length)
            return payload
        except asyncio.TimeoutError:
            return None
        except asyncio.IncompleteReadError:
            raise ConnectionError("MeshCore: connection closed mid-frame")

    async def _send_cmd(self, payload: bytes) -> None:
        await self._transport.write(self._build_frame(payload))

    # ── Initialization sequence ───────────────────────────────────────────

    async def _fetch_privkey_from_serial(self) -> str | None:
        """Send 'get prv.key' text CLI command and return the 128-char hex private key.

        Only works on serial transport before CMD_APP_START puts the device into
        binary companion mode. TCP (port 4403) is binary-only and cannot use this.

        Response format (meshcoretomqtt parsing scheme):
          The device echoes "-> > <key128hex>" or emits the key on its own line.
          We look for any 128-char contiguous hex run in the first 2 seconds of output.
        """
        import re
        try:
            # Flush stale input
            await self._transport.read_raw(512, timeout=0.3)

            await self._transport.write(b"get prv.key\r\n")

            # Collect response for up to 2 seconds
            buf = b""
            deadline = asyncio.get_event_loop().time() + 2.0
            while asyncio.get_event_loop().time() < deadline:
                chunk = await self._transport.read_raw(512, timeout=0.4)
                if not chunk:
                    break
                buf += chunk
                # Stop early once we have more than enough bytes for a 128-char key
                if len(buf) > 256:
                    break

            text = buf.decode("ascii", errors="ignore")

            # meshcoretomqtt splits on "-> >" to find the key after the echo
            if "-> >" in text:
                after = text.split("-> >", 1)[1].strip()
                candidate = re.sub(r"\s+", "", after.split("\n")[0])
            else:
                # Fallback: find the first 128-char hex run anywhere in the response
                m = re.search(r"[0-9a-fA-F]{128}", text.replace(" ", ""))
                candidate = m.group(0) if m else ""

            if len(candidate) == 128:
                try:
                    int(candidate, 16)
                    log.info("MeshCore %s: private key auto-retrieved from device (%s…)",
                             self.name, candidate[:8])
                    return candidate.upper()
                except ValueError:
                    pass

            if text.strip():
                log.warning("MeshCore %s: could not parse private key from device response: %r",
                            self.name, text[:120])
            else:
                log.debug("MeshCore %s: no response to 'get prv.key' (TCP transport?)", self.name)
            return None
        except Exception as exc:
            log.warning("MeshCore %s: private key fetch error: %s", self.name, exc)
            return None

    async def _init_sequence(self) -> None:
        """Run the mandatory startup handshake per companion protocol spec."""
        # Private key fetch has already been done in connect() before _run_task started,
        # to avoid two coroutines reading the same StreamReader simultaneously.

        # 1. CMD_APP_START
        app_name_bytes = self._app_name.encode()
        await self._send_cmd(bytes([CMD_APP_START, 0, 0, 0, 0, 0, 0, 0]) + app_name_bytes)
        await asyncio.sleep(0.2)

        # 2. CMD_DEVICE_QUERY — request SELF_INFO + DEVICE_INFO
        await self._send_cmd(bytes([CMD_DEVICE_QUERY, 0x03]))
        await asyncio.sleep(0.2)

        # 3. CMD_SET_DEVICE_TIME — sync RTC
        ts = int(time.time())
        await self._send_cmd(bytes([CMD_SET_DEVICE_TIME]) + struct.pack("<I", ts))
        await asyncio.sleep(0.1)

        # 4. Fetch channel 0-7 info
        for idx in range(8):
            await self._send_cmd(bytes([CMD_GET_CHANNEL, idx]))
            await asyncio.sleep(0.1)

        # 5. Fetch contact list — populates node names via CONTACT packets
        await self._send_cmd(bytes([CMD_GET_CONTACTS]))
        await asyncio.sleep(0.5)

        # 6. Drain any queued messages
        for _ in range(20):
            await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
            await asyncio.sleep(0.05)

        log.info("MeshCore %s: init sequence complete", self.name)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("MeshCore %s: connecting via %s", self.name, self._transport_type)
        await self._transport.connect()
        self._connected = True
        # Fetch private key via text CLI BEFORE starting the binary RX loop.
        # Both _fetch_privkey_from_serial and _read_frame (in _run) read from the
        # same asyncio.StreamReader — running them concurrently raises
        # "read() called while another coroutine is already waiting for incoming data".
        if self._transport_type == "serial":
            privkey = await self._fetch_privkey_from_serial()
            if privkey:
                _privkey_registry[self.name] = privkey
        # Now start the binary RX loop; _init_sequence sends binary commands only.
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        await asyncio.sleep(0)  # yield so the task is scheduled
        await self._init_sequence()
        log.info("MeshCore %s: ready, monitoring channel %d", self.name, self._channel_idx)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        await self._transport.disconnect()
        log.info("MeshCore %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """Send a channel broadcast or DM depending on whether to_id is set."""
        ts = int(message.timestamp.timestamp())
        to_id = (message.to_id or "").strip()
        body_bytes = message.body.encode("utf-8")[:200]

        if to_id:
            # Direct message (DM) — wire format from meshcore_py reference:
            # [0x02][0x00][attempt:1][timestamp:4][dest_pubkey:6][body_utf8]
            try:
                dest_bytes = bytes.fromhex(to_id[:12].ljust(12, "0"))[:6]
            except ValueError:
                log.error("MeshCore %s: invalid to_id %r for DM — falling back to channel", self.name, to_id)
                dest_bytes = None
            if dest_bytes:
                payload = (
                    b"\x02\x00"
                    + bytes([0])           # attempt = 0 (first send)
                    + struct.pack("<I", ts)
                    + dest_bytes
                    + body_bytes
                )
                log.info("MeshCore %s: sending DM to %s: %s", self.name, to_id[:12], message.body[:60])
            else:
                to_id = ""  # fall through to channel broadcast
        if not to_id:
            # Channel broadcast — CMD_SEND_CHANNEL_MSG (0x03)
            # Honour a channel_idx hint from the bot/router so replies go back
            # to the originating channel, not the adapter's configured default.
            ch_idx = int((message.raw or {}).get("channel_idx", self._channel_idx))
            payload = (
                bytes([CMD_SEND_CHANNEL_MSG, self._max_hops & 0xFF, ch_idx])
                + struct.pack("<I", ts)
                + body_bytes
            )
        else:
            ch_idx = self._channel_idx
        try:
            self._last_sent_uuid = message.id   # track for PUSH_SEND_CONFIRMED correlation
            # Store body for relay-echo detection; prune entries older than 2 min
            now_mono = time.monotonic()
            self._recent_sent[message.body] = (message.id, now_mono)
            cutoff = now_mono - 120.0
            self._recent_sent = {k: v for k, v in self._recent_sent.items() if v[1] > cutoff}
            await self._send_cmd(payload)
            self._mark_tx(message)
            ch_name = self._channels.get(ch_idx, "")
            log.debug("MeshCore %s: sent to ch%d(%s): %s",
                      self.name, ch_idx, ch_name or "?", message.body[:60])
            return True
        except Exception as exc:
            log.error("MeshCore %s: send failed: %s", self.name, exc)
            return False

    # ── Time sync & announce ──────────────────────────────────────────────

    async def time_sync(self) -> bool:
        """Send CMD_SET_DEVICE_TIME to sync the device RTC to current UTC."""
        if not self._connected:
            return False
        ts = int(time.time())
        try:
            await self._send_cmd(bytes([CMD_SET_DEVICE_TIME]) + struct.pack("<I", ts))
            log.info("MeshCore %s: time_sync sent (epoch %d)", self.name, ts)
            return True
        except Exception as exc:
            log.error("MeshCore %s: time_sync error: %s", self.name, exc)
            return False

    async def announce(self) -> bool:
        """
        Two-phase announce:
        1. CMD_SEND_ADVERT — tells the local radio to broadcast its advertisement
           to the mesh channel so other nodes can hear it (push).
        2. _discovery_pulse() — asks the local radio for its contact list so ECH
           gets PUSH_ADVERT packets for known neighbours (pull).
        """
        if not self._connected:
            return False
        try:
            # Phase 1: push our presence to the mesh
            await self._send_cmd(bytes([CMD_SEND_ADVERT, self._channel_idx]))
            await asyncio.sleep(0.1)
            # Phase 2: pull known contacts + drain queued messages
            await self._discovery_pulse()
            log.info("MeshCore %s: announce — sent advert + discovery pulse on ch%d",
                     self.name, self._channel_idx)
            return True
        except Exception as exc:
            log.error("MeshCore %s: announce error: %s", self.name, exc)
            return False

    async def ping(self, node_id: str) -> dict:
        """Send CMD_SEND_TRACE_PATH (0x24) — broadcasts a trace packet onto the mesh.
        Results arrive as TRACE_DATA (0x89) and appear in the message feed."""
        if not self._connected:
            return {"status": "error", "detail": "not connected"}
        import random as _random
        tag = _random.randint(1, 0xFFFFFFFF)
        auth_code = _random.randint(1, 0xFFFFFFFF)
        flags = 0  # 1-byte path hashes
        # Payload: CMD(1) + tag(4) + auth(4) + flags(1) + pad(1)
        # Firmware requires len > 10; with no path bytes we pad to 11.
        payload = (
            bytes([CMD_SEND_TRACE_PATH])
            + tag.to_bytes(4, 'little')
            + auth_code.to_bytes(4, 'little')
            + bytes([flags, 0x00])   # flags + 1 pad byte → 11 bytes total
        )
        try:
            await self._send_cmd(payload)
            log.info("MeshCore %s: SEND_TRACE_PATH tag=%08x — response arrives as TRACE_DATA (0x89)",
                     self.name, tag)
            return {"status": "sent", "detail": f"Trace sent (tag {tag:08x}) — result will appear in message feed"}
        except Exception as exc:
            log.error("MeshCore %s: SEND_TRACE_PATH error: %s", self.name, exc)
            return {"status": "error", "detail": str(exc)}

    # ── Internal receive loop ─────────────────────────────────────────────

    async def _discovery_pulse(self) -> None:
        """
        Periodic node discovery:
          1. CMD_APP_START  — refresh companion session (device re-sends SELF_INFO)
          2. CMD_GET_CONTACTS — fetch known contact list (nodes device has communicated with)
          3. CMD_SEND_ADVERT — broadcast local node presence on the mesh channel,
             which causes neighbouring nodes to update their routing tables and may
             prompt them to send their own advertisements back.
          4. CMD_SYNC_NEXT_MESSAGE × 10 — drain any queued messages

        Note: CMD_DEVICE_QUERY(0x03) was previously sent here under the incorrect
        assumption that it causes the device to send PUSH_ADVERT for each known
        neighbour. It does not. CMD_SEND_ADVERT is the correct mechanism.
        """
        if not self._connected:
            return
        try:
            app_name_bytes = self._app_name.encode()
            await self._send_cmd(bytes([CMD_APP_START, 0, 0, 0, 0, 0, 0, 0]) + app_name_bytes)
            await asyncio.sleep(0.15)
            # Request contacts — device returns CONTACT packets with adv_name for each stored node
            await self._send_cmd(bytes([CMD_GET_CONTACTS]))
            await asyncio.sleep(0.3)
            # Broadcast our presence — not a discovery request, but prompts neighbours to
            # update routing tables and may cause unsolicited PUSH_ADVERT from them.
            await self._send_cmd(bytes([CMD_SEND_ADVERT, self._channel_idx]))
            await asyncio.sleep(0.2)
            # Drain queued messages
            for _ in range(10):
                await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
                await asyncio.sleep(0.05)
            log.debug("MeshCore %s: discovery pulse sent (ch%d)", self.name, self._channel_idx)
        except Exception as exc:
            log.warning("MeshCore %s: discovery pulse error: %s", self.name, exc)

    async def _run(self) -> None:
        """
        Main loop: interleaves frame reading with periodic polling
        for queued messages (CMD_SYNC_NEXT_MESSAGE) and periodic node
        discovery pulses (CMD_APP_START + CMD_DEVICE_QUERY).
        """
        log.debug("MeshCore %s: RX loop started", self.name)
        now0               = time.monotonic()
        last_poll          = now0    # don't double-poll while _init_sequence is running
        last_discovery     = now0    # init_sequence already did discovery
        last_contacts_poll = now0    # init_sequence already ran CMD_GET_CONTACTS
        last_expiry        = 0.0
        last_node_count    = 0
        last_battery_poll  = 0.0    # poll immediately on start
        _NODE_STALE_SEC  = 3600.0   # remove nodes not heard from in 1 hour
        _BATTERY_INTERVAL = 300.0   # poll battery every 5 minutes
        try:
            while self._connected:
                now = time.monotonic()

                # Periodic poll for queued messages
                if now - last_poll >= self._poll_interval:
                    await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
                    last_poll = now

                # Lightweight contacts poll — fetch node list without sending an advert.
                # Catches nodes the device heard via PUSH_ADVERT before ECH was running.
                if now - last_contacts_poll >= self._contacts_poll_interval:
                    prev_count = len(self._nodes)
                    await self._send_cmd(bytes([CMD_GET_CONTACTS]))
                    await asyncio.sleep(0.3)
                    last_contacts_poll = now
                    if len(self._nodes) != prev_count:
                        log.info(
                            "MeshCore %s: contacts poll — node count changed %d → %d",
                            self.name, prev_count, len(self._nodes),
                        )

                # Full discovery pulse — send advert + solicit neighbour adverts
                if now - last_discovery >= self._discovery_interval:
                    await self._discovery_pulse()
                    last_discovery = now

                # Push node-list update to WebSocket clients when count changes
                current_node_count = len(self._nodes)
                if current_node_count != last_node_count:
                    last_node_count = current_node_count
                    notify = getattr(self, '_router_notify_nodes', None)
                    if notify:
                        try:
                            await notify(self.name, current_node_count)
                        except Exception:
                            pass

                # Periodic battery voltage poll
                if now - last_battery_poll >= _BATTERY_INTERVAL:
                    await self._send_cmd(bytes([CMD_GET_BATTERY]))
                    last_battery_poll = now

                # Quick contacts refresh when a new unrecognized node was heard
                if self._contacts_refresh_pending:
                    self._contacts_refresh_pending = False
                    await self._send_cmd(bytes([CMD_GET_CONTACTS]))
                    await asyncio.sleep(0.2)

                # Expire stale nodes (not heard from in > 1 hour)
                if now - last_expiry >= 300.0:
                    cutoff = datetime.now(timezone.utc).timestamp() - _NODE_STALE_SEC
                    stale = [nid for nid, n in self._nodes.items()
                             if n.last_heard and n.last_heard.timestamp() < cutoff]
                    for nid in stale:
                        log.info("MeshCore %s: expiring stale node %s (last heard >1h ago)", self.name, nid)
                        del self._nodes[nid]
                    last_expiry = now

                frame = await self._read_frame()
                if frame is None:
                    continue

                await self._dispatch_frame(frame)

        except ConnectionError as exc:
            log.error("MeshCore %s: connection lost: %s", self.name, exc)
            self._connected = False
        except asyncio.CancelledError:
            log.debug("MeshCore %s: RX loop cancelled", self.name)

    _PKT_NAMES = {
        0x00: "OK", 0x01: "ERROR",
        0x02: "CONTACT_START", 0x03: "CONTACT", 0x04: "CONTACT_END",
        0x05: "SELF_INFO", 0x06: "MSG_SENT",
        0x07: "CONTACT_MSG", 0x08: "CHANNEL_MSG", 0x0A: "NO_MORE_MSGS",
        0x0C: "BATTERY", 0x0D: "DEVICE_INFO", 0x10: "CONTACT_MSG_V3", 0x11: "CHANNEL_MSG_V3",
        0x12: "CHANNEL_INFO", 0x80: "PUSH_ADVERT", 0x81: "PATH_UPDATE",
        0x82: "ACK", 0x83: "PUSH_MSG_WAITING",
        0x89: "TRACE_DATA",
        0x88: "PUSH_CHANNEL_MSG",
    }

    async def _dispatch_frame(self, frame: bytes) -> None:
        if len(frame) < 1:
            return
        pkt_type = frame[0]
        data = frame[1:]

        # Raw packet log for diagnostics (keep last 500 per Issue 10 / debugging requirement)
        entry = {
            "dir": "rx",
            "type": self._PKT_NAMES.get(pkt_type, f"0x{pkt_type:02X}"),
            "type_hex": f"0x{pkt_type:02X}",
            "len": len(frame),
            "hex": frame.hex(),   # store full frame, not truncated
            "ts": time.time(),
        }
        self._packet_log.append(entry)
        if len(self._packet_log) > 500:
            self._packet_log = self._packet_log[-500:]

        if pkt_type == PACKET_CONTACT_START:
            # Start of GET_CONTACTS response — reset temporary accumulator
            self._contacts_building = {}

        elif pkt_type == PACKET_CONTACT:
            # Layout: pubkey(32) + type(1) + flags(1) + plen(1) + out_path(N) +
            #         adv_name(32) + last_advert(4) + adv_lat(4) + adv_lon(4) + lastmod(4)
            # Trailing fields are always 32+4+4+4+4 = 48 bytes.
            # Fixed header (before out_path) is always 35 bytes.
            # Auto-detect path size from frame length on first contact record.
            _CONTACT_TAIL = 48   # adv_name + last_advert + lat + lon + lastmod
            _CONTACT_HEAD = 35   # pubkey(32) + type + flags + plen
            raw_len = len(data)
            if raw_len > _CONTACT_HEAD + _CONTACT_TAIL:
                detected = raw_len - _CONTACT_HEAD - _CONTACT_TAIL
                # Snap to known path sizes: 1, 3, 8, 32, 64
                known = (64, 32, 8, 3, 1)
                path_size = min(known, key=lambda s: abs(s - detected))
                if not self._contact_path_size_set:
                    self._contact_path_size = path_size
                    self._contact_path_size_set = True
                    log.info("MeshCore %s: auto-detected contact path size=%d bytes "
                             "(frame=%d, detected=%d)", self.name, path_size, raw_len, detected)
            else:
                path_size = getattr(self, '_contact_path_size', 64)
            name_off = _CONTACT_HEAD + path_size
            min_len  = name_off + 32
            if len(data) >= min_len:
                pubkey  = data[:32].hex().upper()
                node_id = pubkey[:12]
                adv_name = data[name_off:name_off+32].decode("utf-8", errors="ignore").rstrip("\x00").strip()
                lat_off = name_off + 32 + 4   # skip last_advert(4)
                lon_off = lat_off + 4
                lat_raw = struct.unpack("<i", data[lat_off:lat_off+4])[0] if len(data) >= lat_off+4 else 0
                lon_raw = struct.unpack("<i", data[lon_off:lon_off+4])[0] if len(data) >= lon_off+4 else 0
                lat = lat_raw / 1e6 if lat_raw else None
                lon = lon_raw / 1e6 if lon_raw else None
                now = datetime.now(timezone.utc)
                if node_id not in self._nodes:
                    self._nodes[node_id] = MeshNode(
                        node_id=node_id, display_name=adv_name or node_id,
                        first_seen=now, last_heard=now, name_source="contact",
                        lat=lat, lon=lon,
                    )
                else:
                    n = self._nodes[node_id]
                    if adv_name and n.name_source not in ("self_info",):
                        n.display_name = adv_name
                        n.name_source = "contact"
                    if lat is not None:
                        n.lat = lat
                    if lon is not None:
                        n.lon = lon
                log.info("MeshCore %s: contact %s = %r lat=%s lon=%s",
                         self.name, node_id, adv_name, lat, lon)
            else:
                log.warning("MeshCore %s: CONTACT too short (%dB, need %d)", self.name, len(data), min_len)

        elif pkt_type == PACKET_CONTACT_END:
            log.info("MeshCore %s: GET_CONTACTS complete, %d nodes registered",
                     self.name, len(self._nodes))

        elif pkt_type == PACKET_SELF_INFO:
            # SELF_INFO structured layout (v1.15, observed from real hardware):
            #   byte 0:      version/type
            #   bytes 1-2:   tx_power, max_hops (or radio params)
            #   bytes 3-34:  32-byte Curve25519 public key
            #   bytes 35-56: radio/battery/capability fields
            #   bytes 57+:   node name (null-terminated UTF-8)
            # Fallback: scan for first printable ASCII run ≥ 4 chars after offset 34.
            log.info("MeshCore %s: self_info raw=%s", self.name, data.hex())
            name = ""
            if len(data) > 57:
                null = data.find(b'\x00', 57)
                end = null if null != -1 else len(data)
                candidate = data[57:end].decode('utf-8', errors='ignore').strip('\x00').strip()
                if len(candidate) >= 1:
                    name = candidate
            if not name:
                name = _scan_ascii_name(data, min_offset=34, min_len=4)

            # Always extract pubkey and register the local device — even if name is empty.
            local_pubkey = data[3:9].hex().upper() if len(data) >= 9 else ""
            if len(data) >= 35:
                full_pubkey = data[3:35].hex().upper()
                _pubkey_registry[self.name] = full_pubkey
                log.info("MeshCore %s: registered pubkey %s…", self.name, full_pubkey[:12])
            if local_pubkey:
                self._self_node_id = local_pubkey
            display = name or local_pubkey or self.name
            if name:
                self._device_name = name
            now = datetime.now(timezone.utc)
            if local_pubkey and local_pubkey not in self._nodes:
                self._nodes[local_pubkey] = MeshNode(
                    node_id=local_pubkey, display_name=display,
                    first_seen=now, last_heard=now,
                    name_source="self_info",
                    firmware_version=self._fw_version,
                    hw_model=self._hw_model,
                )
            elif local_pubkey:
                n = self._nodes[local_pubkey]
                if display != local_pubkey or n.display_name == local_pubkey:
                    n.display_name = display
                n.name_source = "self_info"
                n.last_heard = now
            if name:
                log.info("MeshCore %s: self_info name=%r node=%s", self.name, name, local_pubkey)
            else:
                log.warning("MeshCore %s: self_info — could not extract name (raw=%s); "
                            "registered node as %s", self.name, data.hex(), local_pubkey)

        elif pkt_type == PACKET_DEVICE_INFO:
            # DEVICE_INFO structured layout (v1.15, observed):
            #   bytes 0-6:   header/radio params
            #   null-terminated strings in sequence: build_date, hw_model, fw_version
            # Parse by walking null terminators rather than ASCII scanning.
            log.info("MeshCore %s: device_info raw=%s", self.name, data.hex())
            strings = _read_null_terminated_strings(data, start=7, count=3, min_len=4)
            if len(strings) >= 1:
                self._build_date = strings[0]
            if len(strings) >= 2:
                self._hw_model   = strings[1]
            if len(strings) >= 3:
                self._fw_version = strings[2]
            log.info("MeshCore %s: device_info hw=%r fw=%r built=%r",
                     self.name, self._hw_model, self._fw_version, self._build_date)

        elif pkt_type == PACKET_BATTERY:
            # 0x0C battery info frame (v1.15+):
            #   bytes 0-1: uint16le battery voltage in mV
            #   bytes 2-5: uint32le (possibly mAh or percentage — not fully documented)
            #   bytes 6+:  additional fields (charging state, etc.)
            if len(data) >= 2:
                mv = int.from_bytes(data[:2], "little")
                if 2000 <= mv <= 5000:   # plausible LiPo range 2.0–5.0 V
                    self._battery_mv = mv
                    log.info("MeshCore %s: battery %.2f V (%d mV)", self.name, mv / 1000.0, mv)
                else:
                    log.debug("MeshCore %s: battery 0x0C out of range: %d mV raw=%s",
                              self.name, mv, data.hex())

        elif pkt_type == PACKET_CHANNEL_INFO:
            # Layout: idx(1) + name(32, null-padded) + secret(16)
            if len(data) >= 33:
                idx = data[0]
                name = data[1:33].split(b"\x00")[0].decode("utf-8", errors="replace")
                self._channels[idx] = name
                # Store secret for automatic decryption (pad to 32B; _try_decrypt uses first 16B)
                if len(data) >= 49:
                    secret = data[33:49]
                    if secret != b"\x00" * 16:
                        self._channel_keys[idx] = secret + b"\x00" * 16
                log.debug("MeshCore %s: channel %d = %r (key=%s)", self.name, idx, name,
                          "stored" if idx in self._channel_keys else "none")
                self._try_resolve_channel_name()

        elif pkt_type in (PACKET_CHANNEL_MSG_RECV, PACKET_CHANNEL_MSG_V3, PUSH_CHANNEL_MSG):
            # PUSH_CHANNEL_MSG (0x88): real-time push from v1.15 device — has sender pubkey.
            # CHANNEL_MSG_RECV_V3 (0x11): polled via CMD_SYNC_NEXT_MESSAGE — no sender pubkey,
            #   has [SNR][reserved×2][ch_idx][plen][txt_type][ts×4][text] format.
            # CHANNEL_MSG_RECV (0x08): legacy polled — no sender pubkey.
            if pkt_type == PUSH_CHANNEL_MSG:
                log.info("MeshCore %s: PUSH_CHANNEL_MSG(0x88) len=%d raw=%s",
                         self.name, len(frame), data.hex())
            else:
                log.debug("MeshCore %s: POLLED_CHANNEL_MSG(0x%02x) len=%d raw=%s",
                          self.name, pkt_type, len(frame), data.hex())
            await self._handle_channel_msg(data, pkt_type)

        elif pkt_type in (PACKET_CONTACT_MSG_RECV, PACKET_CONTACT_MSG_V3):
            await self._handle_contact_msg(data, v3=(pkt_type == PACKET_CONTACT_MSG_V3))

        elif pkt_type == PACKET_NO_MORE_MSGS:
            pass  # queue empty, normal

        elif pkt_type == PUSH_MSG_WAITING:
            # Unsolicited: new message queued, fetch immediately
            await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))

        elif pkt_type == PUSH_PATH_UPDATED:
            # 0x81 PATH_UPDATE: routing path to a node changed.
            # Format: [pubkey:32] — identifies the node whose path was updated.
            # This is NOT a traceroute result; use SEND_TRACE_PATH (0x24) → TRACE_DATA (0x89) for that.
            if len(data) >= 6:
                node_id = data[:6].hex().upper()
                log.info("MeshCore %s: PUSH_PATH_UPDATE node=%s raw=%s",
                         self.name, node_id, data[:32].hex())
                now = datetime.now(timezone.utc)
                if node_id not in self._nodes:
                    self._nodes[node_id] = MeshNode(
                        node_id=node_id, display_name=node_id,
                        first_seen=now, last_heard=now, name_source="path",
                    )
                else:
                    self._nodes[node_id].last_heard = now

        elif pkt_type == TRACE_DATA:
            # 0x89 TRACE_DATA: result of CMD_SEND_TRACE_PATH (0x24).
            # Format: [reserved:1][path_len_raw:1][flags:1][tag:4][auth:4]
            #          [path_hashes: path_len × hash_size bytes]
            #          [path_snrs:   path_len × 1 signed bytes]
            #          [final_snr:   1 signed byte]
            # hash_size = 1 << (flags & 3); path_len = path_len_raw / hash_size
            if len(data) < 10:
                log.warning("MeshCore %s: TRACE_DATA too short (%d bytes)", self.name, len(data))
            else:
                reserved = data[0]
                path_len_raw = data[1]
                flags = data[2]
                tag = int.from_bytes(data[3:7], 'little')
                auth_code = int.from_bytes(data[7:11], 'little')
                hash_size = 1 << (flags & 3)
                path_len = path_len_raw // hash_size if hash_size else 0
                offset = 11
                path_nodes = []
                for i in range(path_len):
                    if offset + hash_size > len(data):
                        break
                    path_nodes.append(data[offset:offset + hash_size].hex())
                    offset += hash_size
                # SNR values follow: one per hop, then final SNR
                snr_values = []
                for i in range(path_len + 1):
                    if offset < len(data):
                        raw_snr = data[offset]
                        snr_values.append((raw_snr if raw_snr < 128 else raw_snr - 256) / 4.0)
                        offset += 1
                # Resolve node hashes to names where possible (1-byte hashes match last byte of node_id)
                def _resolve_hash(h: str) -> str:
                    for nid, node in self._nodes.items():
                        if nid.lower().endswith(h.lower()) or h.lower() in nid.lower():
                            return node.display_name if node.display_name != nid else nid[:8]
                    return h
                named_nodes = [_resolve_hash(h) for h in path_nodes]
                snr_strs = [f"{s:+.1f}dB" for s in snr_values]
                if named_nodes:
                    parts = []
                    for i, (name, snr) in enumerate(zip(named_nodes, snr_strs)):
                        parts.append(f"{name}({snr})")
                    if len(snr_values) > len(named_nodes):
                        parts.append(f"dest({snr_strs[-1]})")
                    path_str = " → ".join(parts)
                    body = f"📡 TRACE: {path_len} hop(s) via {path_str}"
                else:
                    final_snr = snr_strs[0] if snr_values else "?"
                    body = f"📡 TRACE: direct ({final_snr})"
                log.info("MeshCore %s: TRACE_DATA tag=%08x hops=%d path=%s",
                         self.name, tag, path_len, named_nodes)
                trace_msg = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel="traceroute",
                    from_id="local",
                    from_display=self._device_name or self.name,
                    body=body,
                    priority=Priority.NORMAL,
                    raw={"tag": tag, "hop_count": path_len, "path": path_nodes,
                         "snr_values": snr_values, "raw_hex": data.hex()},
                )
                await self._enqueue(trace_msg)

        elif pkt_type == PUSH_ADVERT:
            # PUSH_ADVERT format (companion protocol v1.15):
            #   data[0..31] = 32-byte Curve25519 public key of the advertising node.
            #   First 6 bytes are the short node_id prefix used throughout ECH.
            #   There is NO name field — name comes from CMD_GET_CONTACTS response.
            if len(data) >= 6:
                node_id = data[:6].hex().upper()
                log.info("MeshCore %s: PUSH_ADVERT node=%s pubkey=%s",
                         self.name, node_id, data.hex())
                now = datetime.now(timezone.utc)
                if node_id not in self._nodes:
                    self._nodes[node_id] = MeshNode(
                        node_id=node_id, display_name=node_id,
                        first_seen=now, last_heard=now, name_source="advert",
                    )
                    # Trigger a contacts refresh so the name resolves promptly
                    self._contacts_refresh_pending = True
                else:
                    self._nodes[node_id].last_heard = now

        elif pkt_type == PUSH_SEND_CONFIRMED:
            # 0x82 ACK: per Companion Protocol spec, payload is a 4-byte ACK code.
            # The ACK code matches the expected_ack from the preceding MSG_SENT (0x06).
            # Relay node IDs are NOT carried in this packet — use TRACE_DATA (0x89) for path info.
            uid = self._last_sent_uuid
            ack_code = data[:4].hex() if len(data) >= 4 else (data.hex() if data else "")
            expected = self._last_expected_ack
            if expected and ack_code and ack_code == expected:
                status = "confirmed"
                detail = f"delivery confirmed (ACK {ack_code})"
            else:
                status = "confirmed"
                detail = "delivery confirmed"
            log.info("MeshCore %s: PUSH_SEND_CONFIRMED msg=%s ack_code=%s expected=%s raw=%s",
                     self.name, uid, ack_code, expected, data.hex())
            if uid and self._router_notify:
                asyncio.ensure_future(
                    self._router_notify(self.name, uid, status, detail, [])
                )

        elif pkt_type == PACKET_ERROR:
            uid = self._last_sent_uuid
            if uid and self._router_notify:
                asyncio.ensure_future(
                    self._router_notify(self.name, uid, "failed", "device error")
                )
            self._last_sent_uuid = None
            log.warning("MeshCore %s: PACKET_ERROR received", self.name)

        elif pkt_type == PACKET_MSG_SENT:
            # Device accepted the message for transmission.
            # Format: [type:1][expected_ack:4][suggested_timeout_ms:4]
            uid = self._last_sent_uuid
            if len(data) >= 5:
                self._last_expected_ack = data[1:5].hex()
            if uid and self._router_notify:
                asyncio.ensure_future(
                    self._router_notify(self.name, uid, "sent_to_air", "device sent to mesh")
                )
            log.debug("MeshCore %s: MSG_SENT ack (msg=%s, expected_ack=%s)",
                      self.name, uid, self._last_expected_ack)

        elif pkt_type == PACKET_OK:
            pass  # Low-level protocol ACK, no action

        else:
            log.warning("MeshCore %s: unhandled pkt_type=0x%02x len=%d raw=%s",
                        self.name, pkt_type, len(frame), frame[:32].hex())

    async def _handle_channel_msg(self, data: bytes, pkt_type: int) -> None:
        """
        Parse channel messages. Wire format differs by packet type:

        PUSH_CHANNEL_MSG (0x88, v1.15 real-time push — sender pubkey present):
          byte 0:     channel_idx
          bytes 1-6:  sender pubkey prefix
          byte 7:     path_len
          byte 8:     txt_type
          bytes 9-12: timestamp uint32le (device clock)
          bytes 13+:  message text

        CHANNEL_MSG_RECV_V3 (0x11, polled — NO sender pubkey):
          byte 0:     SNR (signed byte / 4)
          bytes 1-2:  reserved
          byte 3:     channel_idx
          byte 4:     path_len (6 LSB = hops, 2 MSB = hash_mode)
          byte 5:     txt_type
          bytes 6-9:  timestamp uint32le (device clock)
          bytes 10+:  message text ("sender_name: body" format identifies sender)

        CHANNEL_MSG_RECV (0x08, legacy polled — NO sender pubkey):
          byte 0:     channel_idx
          byte 1:     path_len
          byte 2:     txt_type
          bytes 3-6:  timestamp uint32le
          bytes 7+:   message text
        """
        now = datetime.now(timezone.utc)
        sender_hex: str | None = None
        snr: float | None = None

        if pkt_type == PUSH_CHANNEL_MSG:
            # Real-time push — contains sender pubkey.
            # Wire format (v1.15 companion protocol):
            #   byte 0:      SNR (signed int8)
            #   bytes 1-6:   sender pubkey prefix (6 bytes)
            #   byte 7:      channel_idx
            #   byte 8:      txt_type
            #   byte 9:      path_len (hop count)
            #   bytes 10-13: timestamp (uint32le)
            #   bytes 14+:   message text
            if len(data) < 14:
                log.debug("MeshCore %s: 0x88 too short (%d bytes)", self.name, len(data))
                return
            snr_byte = data[0]
            snr = (snr_byte if snr_byte < 128 else snr_byte - 256) / 4.0
            sender_hex = data[1:7].hex().upper()
            ch_idx = data[7]
            # data[8] = txt_type (unused for display)
            path_len_raw = data[9]
            ts_raw = struct.unpack("<I", data[10:14])[0]
            raw_payload = data[14:]
            sane_hops = (path_len_raw & 0x3F) if path_len_raw != 255 else None
            if sane_hops is not None and sane_hops > 16:
                sane_hops = None

        elif pkt_type == PACKET_CHANNEL_MSG_V3:
            # Polled V3 — SNR header, then channel/path/type/ts/text, NO pubkey
            if len(data) < 10:
                log.debug("MeshCore %s: 0x11 too short (%d bytes)", self.name, len(data))
                return
            snr_byte = data[0]
            snr = (snr_byte if snr_byte < 128 else snr_byte - 256) / 4.0
            ch_idx = data[3]
            path_len_raw = data[4]
            sane_hops = (path_len_raw & 0x3F) if path_len_raw != 255 else None
            if sane_hops is not None and sane_hops > 16:
                sane_hops = None
            ts_raw = struct.unpack("<I", data[6:10])[0]
            raw_payload = data[10:]

        else:  # PACKET_CHANNEL_MSG_RECV 0x08 — legacy polled
            if len(data) < 8:
                log.debug("MeshCore %s: 0x08 too short (%d bytes)", self.name, len(data))
                return
            ch_idx = data[0]
            path_len_raw = data[1]
            sane_hops = (path_len_raw & 0x3F) if path_len_raw != 255 else None
            if sane_hops is not None and sane_hops > 16:
                sane_hops = None
            ts_raw = struct.unpack("<I", data[3:7])[0]
            raw_payload = data[7:]

        # Track this channel index even if we don't know its name yet
        if ch_idx not in self._channels:
            self._channels[ch_idx] = ""
            log.info("MeshCore %s: discovered new channel index ch%d (name unknown — run scan)", self.name, ch_idx)

        # Attempt AES-128-ECB decryption
        decrypted_ok = False
        decrypt_key_label: str = ""
        if ch_idx in self._channel_keys:
            plain_bytes = self._try_decrypt(raw_payload, ch_idx)
            if plain_bytes is not None:
                text = plain_bytes.decode("utf-8", errors="replace")
                decrypted_ok = True
                decrypt_key_label = f"ch{ch_idx}"
                log.info("MeshCore %s: ch%d decrypted with channel key (%dB)", self.name, ch_idx, len(plain_bytes))
        # Fallback: try all known channel keys + common/default keys
        if not decrypted_ok and _is_likely_encrypted(raw_payload.decode("utf-8", errors="replace")):
            result = self._try_decrypt_any(raw_payload, ch_idx)
            if result is not None:
                plain_bytes, decrypt_key_label = result
                text = plain_bytes.decode("utf-8", errors="replace")
                decrypted_ok = True
                log.info("MeshCore %s: ch%d decrypted with key=%r (%dB)", self.name, ch_idx, decrypt_key_label, len(plain_bytes))
        if not decrypted_ok:
            text = raw_payload.decode("utf-8", errors="replace")

        named = self._channels.get(ch_idx)
        ch_name = f"ch{ch_idx}:{named}" if named else f"ch{ch_idx}"
        extracted_name, body_text = _extract_msg_sender(text)

        if sender_hex:
            # 0x88 push — we have a real pubkey; register/update node
            if sender_hex not in self._nodes:
                self._nodes[sender_hex] = MeshNode(
                    node_id=sender_hex, display_name=sender_hex,
                    first_seen=now, last_heard=now, name_source="",
                )
                log.debug("MeshCore %s: new node from 0x88: %s", self.name, sender_hex)
                # Schedule a contacts refresh to try to resolve the name
                self._contacts_refresh_pending = True
            else:
                self._nodes[sender_hex].last_heard = now
            node = self._nodes[sender_hex]
            # Only update name if we don't have a better source (contact > message_text)
            if extracted_name and node.name_source not in ("self_info", "contact"):
                node.display_name = extracted_name
                node.name_source = "message_text"
            display = node.display_name
            from_id = sender_hex
        else:
            # Polled message — no pubkey; identify sender by name extracted from text.
            # Schedule a contacts refresh so the device's known-node list is re-fetched;
            # this picks up nodes the device has heard even if they haven't sent a 0x88 push.
            display = extracted_name or "unknown"
            from_id = display
            if extracted_name:
                self._contacts_refresh_pending = True
            log.debug("MeshCore %s: polled ch%d msg sender=%r (no pubkey in polled format)",
                      self.name, ch_idx, display)

        if not decrypted_ok and _is_likely_encrypted(body_text):
            log.debug("MeshCore %s: ch%d msg from %r encrypted", self.name, ch_idx, display)
            body_text = "[Encrypted channel message]"

        raw_data: dict = {"channel_idx": ch_idx}
        if ts_raw:
            raw_data["packet_timestamp"] = ts_raw
        if snr is not None:
            raw_data["snr"] = snr
        if decrypted_ok and decrypt_key_label:
            raw_data["decrypted_with"] = decrypt_key_label

        # Detect our own message echoed back through a relay (PUSH_CHANNEL_MSG from a different node)
        if sender_hex and sender_hex != self._self_node_id and self._router_notify:
            entry = self._recent_sent.get(body_text)
            if entry:
                relay_msg_id, _ = entry
                relay_name = display or sender_hex[:8]
                asyncio.ensure_future(
                    self._router_notify(self.name, relay_msg_id, "heard_1",
                                        relay_name, [sender_hex])
                )

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=ch_name,
            from_id=from_id,
            from_display=display,
            body=body_text,
            timestamp=datetime.now(timezone.utc),
            hop_count=sane_hops,
            raw=raw_data,
        )
        await self._enqueue(msg)
        log.debug("MeshCore %s: ch%d msg from %r hops=%s: %s",
                  self.name, ch_idx, display, sane_hops, body_text[:60])

    async def _handle_contact_msg(self, data: bytes, v3: bool) -> None:
        """
        Contact (DM) message formats:

        CONTACT_MSG_RECV (0x07, legacy):
          bytes 0-5:   sender pubkey prefix
          byte 6:      path_len
          byte 7:      txt_type
          bytes 8-11:  timestamp
          bytes 12+:   message text

        CONTACT_MSG_RECV_V3 (0x10, polled V3 — library confirmed format):
          byte 0:      SNR (signed byte / 4)
          bytes 1-2:   reserved
          bytes 3-8:   sender pubkey prefix
          byte 9:      path_len
          byte 10:     txt_type
          bytes 11-14: timestamp
          bytes 15+:   message text
        """
        if v3:
            if len(data) < 15:
                return
            snr = (data[0] if data[0] < 128 else data[0] - 256) / 4.0
            sender_hex = data[3:9].hex().upper()
            path_len_raw = data[9]
            ts_raw = struct.unpack("<I", data[11:15])[0]
            text = data[15:].decode("utf-8", errors="replace")
        else:
            if len(data) < 12:
                return
            snr = None
            sender_hex = data[:6].hex().upper()
            path_len_raw = data[6]
            ts_raw = struct.unpack("<I", data[8:12])[0]
            text = data[12:].decode("utf-8", errors="replace")

        sane_hops = (path_len_raw & 0x3F) if path_len_raw != 255 else None
        if sane_hops is not None and sane_hops > 16:
            sane_hops = None

        now = datetime.now(timezone.utc)
        extracted_name, body_text = _extract_msg_sender(text)

        if sender_hex not in self._nodes:
            self._nodes[sender_hex] = MeshNode(
                node_id=sender_hex, display_name=sender_hex,
                first_seen=now, last_heard=now, name_source="",
            )
        else:
            self._nodes[sender_hex].last_heard = now
        node = self._nodes[sender_hex]
        if extracted_name and node.name_source not in ("self_info", "contact"):
            node.display_name = extracted_name
            node.name_source = "message_text"
        display = node.display_name

        dm_decrypt_label = ""
        if _is_likely_encrypted(body_text):
            # Try all known channel keys + common defaults — DMs share the channel key in some configs
            dm_raw = body_text.encode("utf-8", errors="replace") if isinstance(body_text, str) else body_text
            # body_text at this point is the raw decode of the payload; re-derive raw bytes
            # The DM payload comes from _handle_contact_msg which already decoded the text portion.
            # We need raw_payload — use the encoded bytes if they look like binary
            _dm_raw_bytes = body_text.encode("latin-1") if all(ord(c) < 256 for c in body_text) else None
            if _dm_raw_bytes:
                result = self._try_decrypt_any(_dm_raw_bytes, -1)
                if result is not None:
                    plain_dm, dm_decrypt_label = result
                    plain_text = plain_dm.decode("utf-8", errors="replace")
                    if not _is_likely_encrypted(plain_text):
                        body_text = plain_text
                        log.info("MeshCore %s: DM decrypted with key=%r", self.name, dm_decrypt_label)
            if _is_likely_encrypted(body_text):
                body_text = "[Encrypted direct message]"

        raw_data: dict = {"type": "contact_msg", "v3": v3}
        if dm_decrypt_label:
            raw_data["decrypted_with"] = dm_decrypt_label
        if ts_raw:
            raw_data["packet_timestamp"] = ts_raw
        if snr is not None:
            raw_data["snr"] = snr

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel="DM",
            from_id=sender_hex,
            from_display=display,
            body=body_text,
            timestamp=datetime.now(timezone.utc),
            hop_count=sane_hops,
            raw=raw_data,
        )
        await self._enqueue(msg)
        log.debug("MeshCore %s: DM from %s(%s): %s", self.name, display, sender_hex, body_text[:60])

    # ── Channel name resolution ───────────────────────────────────────────

    def _try_resolve_channel_name(self) -> None:
        """Resolve channel_name → channel_idx once the device channel list arrives."""
        if not self._channel_name or self._channel_name_resolved:
            return
        want = self._channel_name.lower()
        for idx, name in self._channels.items():
            if name.lower() == want:
                self._channel_idx = idx
                self._channel_name_resolved = True
                log.info("MeshCore %s: channel_name %r → index %d", self.name, self._channel_name, idx)
                return
        # Not found yet — will retry on next CHANNEL_INFO packet

    # ── Node list ─────────────────────────────────────────────────────────

    async def rename_device(self, new_name: str) -> bool:
        """Set device name via CMD_SET_NAME (0x08), then re-advertise."""
        if not self._connected:
            return False
        name_bytes = new_name.strip().encode("utf-8")[:32]
        try:
            await self._send_cmd(bytes([CMD_SET_NAME]) + name_bytes)
            await asyncio.sleep(0.3)
            self._device_name = new_name.strip()
            await self._send_cmd(bytes([CMD_SEND_ADVERT]))
            log.info("MeshCore %s: renamed to %r, advert sent", self.name, self._device_name)
            return True
        except Exception as exc:
            log.error("MeshCore %s: rename error: %s", self.name, exc)
            return False

    async def clear_nodes(self) -> int:
        """Clear server-side node cache (device db is unaffected)."""
        count = len(self._nodes)
        self._nodes.clear()
        log.info("MeshCore %s: cleared %d cached nodes", self.name, count)
        return count

    async def create_channel(self, idx: int, name: str, key_hex: str | None = None) -> bool:
        """
        Set a channel slot on the device (CMD_SET_CHANNEL = 0x20).
        Format: 0x20 + idx(1) + name_padded(32) + secret(16)
        secret = provided key_hex[:32] decoded, else SHA256(name)[:16]
        """
        if not self._connected:
            return False
        import hashlib
        name_bytes = name.encode("utf-8")[:32].ljust(32, b'\x00')
        if key_hex:
            try:
                secret = bytes.fromhex(key_hex)[:16].ljust(16, b'\x00')
            except ValueError:
                secret = hashlib.sha256(name.encode()).digest()[:16]
        else:
            secret = hashlib.sha256(name.encode()).digest()[:16]
        payload = bytes([0x20, idx & 0xFF]) + name_bytes + secret
        try:
            await self._send_cmd(payload)
            await asyncio.sleep(0.3)
            self._channels[idx] = name
            log.info("MeshCore %s: channel %d set to %r", self.name, idx, name)
            return True
        except Exception as exc:
            log.error("MeshCore %s: create_channel error: %s", self.name, exc)
            return False

    async def scan_channels(self, max_idx: int = 255) -> dict[int, str]:
        """
        Query the device for channel info on slots 0–max_idx using CMD_GET_CHANNEL (0x1F).
        The device replies with PACKET_CHANNEL_INFO (0x12) for each configured slot;
        unconfigured slots return nothing or an error packet (silently ignored).
        Returns the updated _channels dict snapshot after the scan.
        """
        if not self._connected:
            return {}
        log.info("MeshCore %s: scanning channels 0–%d", self.name, max_idx)
        for idx in range(max_idx + 1):
            await self._send_cmd(bytes([CMD_GET_CHANNEL, idx & 0xFF]))
            await asyncio.sleep(0.05)   # 50 ms per slot; 256 slots = ~13 s max
        await asyncio.sleep(0.3)        # let final responses arrive
        log.info("MeshCore %s: channel scan complete, %d slot(s) known", self.name, len(self._channels))
        return dict(self._channels)

    def get_privkey_hex(self) -> str | None:
        """Return cached private key hex string (available only on serial transport)."""
        return _privkey_registry.get(self.name)

    @property
    def tx_channel(self) -> str:
        """Human-readable name of the current TX channel, e.g. 'ch2:TAC-1'."""
        name = self._channels.get(self._channel_idx, '')
        return f"ch{self._channel_idx}:{name}" if name else f"ch{self._channel_idx}"

    async def nodes(self) -> list[MeshNode]:
        return list(self._nodes.values())

    def _health_detail(self) -> dict:
        active_name = self._channels.get(self._channel_idx, self._channel_name or "unknown")
        d = {
            "device_name": self._device_name,
            "transport": self._transport_type,
            "tx_channel": f"{self._channel_idx}:{active_name}",
            "channel_idx": self._channel_idx,
            "channels_list": [
                {"idx": i, "name": n or f"ch{i}"}
                for i, n in sorted(self._channels.items())
            ],
            "node_count": len(self._nodes),
            "max_hops": self._max_hops,
            "discovery_interval": self._discovery_interval,
        }
        if self._hw_model:
            d["hardware"] = self._hw_model
        if self._fw_version:
            d["firmware"] = self._fw_version
        if self._battery_mv is not None:
            d["battery_mv"] = self._battery_mv
            d["battery_v"] = round(self._battery_mv / 1000.0, 2)
        return d
