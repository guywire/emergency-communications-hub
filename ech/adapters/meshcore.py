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
  app_name      str     app identifier sent in CMD_APP_START (default: SM)

Framing (serial/TCP):
  Outgoing (app → device):  < header + 2-byte little-endian length + payload
  Incoming (device → app):  > header + 2-byte little-endian length + payload
"""

from __future__ import annotations

import asyncio
import collections
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
CMD_SET_ADVERT_LATLON   = 0x0E   # set device's own advert position: lat_e6(4)+lon_e6(4)+reserved(4)
CMD_SYNC_NEXT_MESSAGE   = 0x0A
CMD_SEND_TRACEROUTE     = 0x0D   # actually RESET_PATH; kept for backward compat
CMD_REMOVE_CONTACT      = 0x0F   # delete a stored contact: [pubkey:32] → PACKET_CONTACT_DELETED
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
PUSH_CHANNEL_MSG        = 0x88   # LOG_DATA per meshcore-py v2.3: [SNR:1][RSSI:1][raw_OTA_packet:N]
TRACE_DATA              = 0x89   # traceroute result: [reserved:1][path_len:1][flags:1][tag:4][auth:4][hashes...][snrs...][final_snr:1]
PACKET_CONTACT_DELETED  = 0x8F   # confirmation after CMD_REMOVE_CONTACT: [pubkey:32]

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


class BrowserTransport(MeshCoreTransport):
    """MeshCore node plugged into the OPERATOR'S computer, bridged by the
    browser: the /remote-hw page opens the device with Web Serial and pumps
    raw bytes over /ws/remote-hw; this transport reads/writes those bytes via
    ech.core.remote_hw.registry. Everything above the transport (framing,
    companion protocol, contacts, traces) is the ordinary MeshCore stack."""

    def __init__(self, adapter_name: str):
        self._adapter_name = adapter_name
        self._sess = None
        self._buf = bytearray()

    async def connect(self) -> None:
        from ech.core.remote_hw import registry
        # Wait briefly for the operator's browser to attach; the adapter's
        # normal reconnect loop retries if it isn't there yet.
        self._sess = await registry.wait_for(self._adapter_name, timeout=10.0)
        if self._sess is None:
            raise ConnectionError(
                f"no browser hardware session for {self._adapter_name!r} — open /remote-hw "
                "and connect the MeshCore node via Web Serial")
        self._buf.clear()
        log.info("MeshCore browser: attached to remote session for %r", self._adapter_name)

    async def disconnect(self) -> None:
        self._sess = None

    async def write(self, data: bytes) -> None:
        if self._sess is None:
            raise ConnectionError("browser session not attached")
        await self._sess.write(data)

    async def readexactly(self, n: int) -> bytes:
        if self._sess is None:
            raise ConnectionError("browser session not attached")
        while len(self._buf) < n:
            chunk = await self._sess.read()
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


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
        self._contacts_poll_interval = float(config.get("contacts_poll_interval", 300.0))
        self._app_name = config.get("app_name", "SM")
        # Max hops for outgoing channel messages (second byte of CMD_SEND_CHANNEL_MSG).
        # 0 = device default. Set to 3 for typical local mesh (reduces network load).
        self._max_hops           = int(config.get("max_hops", 0))
        # path.hash.mode: 0=1-byte, 1=2-byte, 2=3-byte addressing. None = don't set.
        _phm = config.get("path_hash_mode")
        self._path_hash_mode: int | None = int(_phm) if _phm is not None else None
        self._contacts_refresh_pending = False   # set when new nodes need name resolution
        self._contacts_last_refresh = 0.0        # monotonic timestamp of last GET_CONTACTS
        self._contacts_min_interval = 600.0      # min seconds between triggered refreshes (10 min)
        self._contacts_in_progress = False       # True while CONTACT_START…CONTACT_END in flight
        self._contacts_stream_started = 0.0     # monotonic time CONTACT_START was received
        self._msg_poll_pending = False           # DM arrived during GET_CONTACTS; poll after
        # Node TTL: remove nodes not heard in this many seconds (0 = use built-in default)
        self._node_ttl_sec: float = float(config.get("node_ttl_hours", 0)) * 3600.0
        # "Online" window for UI purposes (map color/opacity, node list). MeshNode's
        # built-in default is 15 min, which suits Meshtastic's chatty telemetry but
        # not MeshCore, where nodes advertise on multi-hour cadences — with a 15-min
        # window virtually every MeshCore node renders as offline/grey on the map
        # moments after its advert. Default 2h.
        self._online_window_sec: float = float(config.get("online_window_min", 120)) * 60.0
        self._transport_type = config.get("transport", "serial")

        self._transport: MeshCoreTransport = self._make_transport(config)
        self._nodes: dict[str, MeshNode] = {}
        self._channels: dict[int, str] = {}       # index → name
        self._contacts_building: dict = {}        # temporary store during GET_CONTACTS response
        self._pending_contact_removals: list[bytes] = []  # full 32-byte pubkeys to delete from device
        self._device_name: str = ""
        self._self_node_id: str = ""              # 6-byte pubkey hex of this device (from SELF_INFO)
        self._hw_model: str = ""
        self._fw_version: str = ""
        self._build_date: str = ""
        self._battery_mv: int | None = None       # last known battery voltage in mV
        self._last_discovery_ts: float = 0.0      # monotonic time of last discovery pulse
        self._last_expected_ack: str | None = None  # hex ACK code from MSG_SENT, matched against PUSH_SEND_CONFIRMED
        self._run_task: asyncio.Task | None = None
        self._response_waiters: dict[int, asyncio.Future] = {}
        self._packet_log: list[dict] = []         # recent raw frames for diagnostics
        # Recently sent bodies → (msg_id, sent_time) for relay echo detection (pruned after 2 min)
        self._recent_sent: dict[str, tuple[str, float]] = {}
        # Channel decryption keys: channel_idx → 32-byte secret (AES-128-ECB uses first 16 bytes)
        self._channel_keys: dict[int, bytes] = self._load_channel_keys(config)
        self._anomaly_engine = None  # injected by router after registration
        # Nodes already warned for impossible coordinates — suppress repeat alerts on re-poll
        self._bad_coord_warned: set[str] = set()
        # RF channel health stats (reset every 5 min by _run loop)
        self._rf_stats: dict = self._fresh_rf_stats()
        # Ring buffer of recent LOG_DATA (0x88) RF samples for RSSI correlation
        self._recent_log_data: collections.deque = collections.deque(maxlen=20)
        # Rate gate for live RF-traffic WS events (map traffic overlay)
        self._rf_traffic_last_ws: float = 0.0
        # ECH base location, pushed by ECHState at init — used as the rx-end
        # fallback for traffic lines when the radio's own node has no advert
        # position (very common: most companion radios never set one).
        self._base_lat: float | None = None
        self._base_lon: float | None = None
        # Pending awaitable traceroutes, keyed by trace tag — resolved by the
        # TRACE_DATA (0x89) handler so callers (anomaly verification) can await
        # a live path measurement instead of fishing it out of the message feed.
        self._trace_futures: dict[int, asyncio.Future] = {}
        # Tags of traces WE sent, so a raw LOG_DATA sighting of a TRACE-type OTA
        # packet can be recognized as "our probe, still in flight" instead of
        # either being ignored or misreported as a completed reply (only the
        # companion TRACE_DATA/0x89 event — the radio recognizing itself as the
        # final destination — actually confirms completion).
        self._pending_trace_tags: set = set()
        # Observed mesh topology, learned passively from the RX log and from
        # successful traces. The device itself stores no routes (all contacts
        # flood), so this is how directed traces reach repeaters beyond direct
        # range: BFS over these adjacencies builds the relay chain.
        #   _rf_adjacency: 1-byte hash (lowercase hex) → neighbour hashes
        #   _direct_hashes: relays whose transmissions we hear directly
        self._rf_adjacency: dict[str, set] = {}
        self._direct_hashes: set = set()

    def set_base_location(self, lat: float, lon: float) -> None:
        self._base_lat, self._base_lon = lat, lon
        self._stamp_self_position()
        if self._connected:
            asyncio.ensure_future(self.push_advert_location(lat, lon))

    def _stamp_self_position(self) -> None:
        """Set lat/lon on our OWN node entry so this station shows up on ECH's
        map like any other node — pushing the position to the radio (below)
        only helps how the REST of the mesh sees us, not our own map."""
        if self._self_node_id and self._base_lat is not None and self._self_node_id in self._nodes:
            n = self._nodes[self._self_node_id]
            n.lat, n.lon = self._base_lat, self._base_lon

    async def push_advert_location(self, lat: float, lon: float) -> bool:
        """Push ECH's base location onto the radio's own advert position
        (CMD_SET_ADVERT_LATLON, 0x0E) so this station's node shows up on the
        map like any other, instead of only ever being the (unpositioned) rx
        end of trace/traffic lines."""
        if not self._connected:
            return False
        try:
            payload = (
                bytes([CMD_SET_ADVERT_LATLON])
                + int(lat * 1e6).to_bytes(4, "little", signed=True)
                + int(lon * 1e6).to_bytes(4, "little", signed=True)
                + (0).to_bytes(4, "little")
            )
            await self._send_cmd(payload)
            log.info("MeshCore %s: pushed advert location (%.6f, %.6f) to device",
                     self.name, lat, lon)
            return True
        except Exception as exc:
            log.error("MeshCore %s: push_advert_location error: %s", self.name, exc)
            return False

    # OTA wire-format names, verified against meshcore_py v2.3.7 meshcore_parser.py
    _RF_ROUTE_NAMES = ("TC_FLOOD", "FLOOD", "DIRECT", "TC_DIRECT")
    _RF_PAYLOAD_NAMES = ("REQ", "RESPONSE", "TEXT_MSG", "ACK", "ADVERT", "GRP_TXT",
                         "GRP_DATA", "ANON_REQ", "PATH", "TRACE", "MULTIPART", "CONTROL")

    def _emit_rf_traffic(self, pkt: bytes, snr: float, rssi: int) -> None:
        """Parse a raw OTA packet from LOG_DATA (0x88) and push an 'rf_traffic'
        WS event for the map's live-traffic overlay.

        Wire format (verified against meshcore_py v2.3.7 meshcore_parser.py —
        NOT guessed): header:1 [route_type=b0-1, payload_type=b2-5], +4B
        transport code when route_type is TC_FLOOD(0)/TC_DIRECT(3), then
        path_byte:1 [hash_size=(b6-7)+1, path_len=b0-5 in HOPS], then
        path_len×hash_size relay-hash bytes, then the (possibly encrypted)
        payload. ADVERT (0x04) payloads start with the origin's 32-byte pubkey.
        Relay hashes are pubkey PREFIXES (hash_size bytes), so they resolve
        against known node_ids via startswith."""
        if len(pkt) < 2 or self._router_broadcast is None:
            return
        header = pkt[0]
        route_type = header & 0x03
        payload_type = (header & 0x3C) >> 2
        off = 1
        if route_type in (0, 3):        # TC_* routes carry a 4-byte transport code
            off += 4
        if off >= len(pkt):
            return
        path_byte = pkt[off]
        hash_size = ((path_byte & 0xC0) >> 6) + 1
        path_len = path_byte & 0x3F
        off += 1
        if off + path_len * hash_size > len(pkt):
            return
        hashes = [pkt[off + i * hash_size: off + (i + 1) * hash_size].hex()
                  for i in range(path_len)]
        off += path_len * hash_size
        payload = pkt[off:]

        origin_id = None
        if payload_type == 0x04 and len(payload) >= 36:    # ADVERT: pubkey(32)+ts(4)+…
            origin_id = payload[:6].hex().upper()

        # Passive topology learning happens on EVERY heard path — before the
        # WS rate gate, which only limits what browsers see.
        #
        # Prepend the ADVERT origin so its hash joins the graph as the far end
        # of the chain: [origin, r1, …, rN] where rN is the relay we heard.
        # Crucially, a 0-relay advert becomes the single-element chain [origin],
        # which teaches _learn_topology that we heard that origin DIRECTLY. That
        # was the missing signal: without it, a node we hear over the air (empty
        # path) was never added to _direct_hashes, so a node like LandOfLucy —
        # which we hear directly AND which we'd once seen relayed via RKDRPTMON —
        # got routed through the relay (a needless 3-hop loop) instead of a bare
        # direct probe. Direct reception now wins.
        origin_hash = origin_id[:2].lower() if origin_id else None
        self._learn_topology(([origin_hash] if origin_hash else []) + hashes)
        # Any node we hear as a relay in someone else's traffic is proof it's
        # still on the mesh RIGHT NOW — just as much as a PUSH_ADVERT is, which
        # was previously the ONLY thing that refreshed last_heard. A repeater
        # that relays plenty of traffic but adverts infrequently (or whose
        # adverts we happen to miss) was expiring out of the node list — and
        # off the map — while still very much alive and reachable, which read
        # exactly like "not getting their adverts" even when RF was fine.
        for h in ([origin_hash] if origin_hash else []) + hashes:
            self._touch_heard_node(h)
        # Stamp the path onto the just-cached RF sample so the poll-correlation
        # (which already attaches SNR/RSSI to the decrypted copy of this same
        # packet) can attach the relay path too — polled messages carry only a
        # hop COUNT, but the raw log heard the actual chain.
        if self._recent_log_data:
            self._recent_log_data[-1]["path"] = [h.lower() for h in hashes]
            self._recent_log_data[-1]["ptype"] = payload_type

        # Nothing the map could draw → don't spam the WS. Rate-cap the rest so
        # a busy mesh can't flood browser clients (worst case ~5 events/sec).
        if not hashes and origin_id is None:
            return
        now_mono = time.monotonic()
        if now_mono - self._rf_traffic_last_ws < 0.2:
            return
        self._rf_traffic_last_ws = now_mono

        route = self._RF_ROUTE_NAMES[route_type]
        ptype = (self._RF_PAYLOAD_NAMES[payload_type]
                 if payload_type < len(self._RF_PAYLOAD_NAMES) else f"0x{payload_type:X}")
        # TRACE-type OTA packets carry the sender's tag as the first 4 bytes of
        # payload (same tag we put in our own CMD_SEND_TRACE_PATH). If it's one
        # of ours, we can tell the operator their probe is actually moving
        # through the mesh — NOT a completed reply (only the companion
        # TRACE_DATA/0x89 event, the radio recognizing itself as the final
        # destination, confirms that), just evidence it's alive in flight.
        seen_tag = None
        if payload_type == 9 and len(payload) >= 4:
            candidate = int.from_bytes(payload[:4], "little")
            if candidate in self._pending_trace_tags:
                seen_tag = candidate
        self._broadcast_traffic_event(route, ptype, snr, rssi, path_len, hashes, origin_id, tag=seen_tag)

    def _learn_topology(self, hashes: list, confirmed_from_us: bool = False) -> None:
        """Update the observed adjacency graph from a relay-hash chain.

        Heard traffic (confirmed_from_us=False): the chain is in traversal
        order and WE heard the last relay's transmission — so the LAST hash is
        direct to us, and each consecutive pair is adjacent.
        Successful trace (confirmed_from_us=True): the probe went out from us
        along the chain — the FIRST hash is direct to us."""
        hashes = [h.lower() for h in hashes if h]
        if not hashes:
            return
        self._direct_hashes.add(hashes[0] if confirmed_from_us else hashes[-1])
        for a, b in zip(hashes, hashes[1:]):
            self._rf_adjacency.setdefault(a, set()).add(b)
            self._rf_adjacency.setdefault(b, set()).add(a)
        self._topology_dirty = True

    async def _persist_topology(self) -> None:
        """Save the learned graph to the DB kv store. Without this the graph
        died on every service restart — and a deploy restarts the service, so
        every field test of multi-hop tracing started from an empty graph and
        silently fell back to bare probes."""
        if not self._db or not getattr(self, "_topology_dirty", False):
            return
        import json as _json
        try:
            await self._db.set_kv(
                f"meshcore_topology_{self.name}",
                _json.dumps({"direct": sorted(self._direct_hashes),
                             "adj": {k: sorted(v) for k, v in self._rf_adjacency.items()}}))
            self._topology_dirty = False
        except Exception as exc:
            log.debug("MeshCore %s: topology persist error: %s", self.name, exc)

    async def _restore_topology(self) -> None:
        if not self._db:
            return
        import json as _json
        try:
            raw = await self._db.get_kv(f"meshcore_topology_{self.name}")
            if raw:
                data = _json.loads(raw)
                self._direct_hashes = set(data.get("direct", []))
                self._rf_adjacency = {k: set(v) for k, v in data.get("adj", {}).items()}
                log.info("MeshCore %s: restored topology (%d direct, %d nodes in graph)",
                         self.name, len(self._direct_hashes), len(self._rf_adjacency))
        except Exception as exc:
            log.debug("MeshCore %s: topology restore error: %s", self.name, exc)

    def _infer_route(self, target_hash: str, max_hops: int = 8) -> "list | None":
        """BFS from us over observed adjacencies → relay chain to reach
        target_hash (relays only, target excluded). [] = target is direct.
        None = no known path."""
        target_hash = target_hash.lower()
        if target_hash in self._direct_hashes:
            return []
        from collections import deque
        q = deque([d] for d in sorted(self._direct_hashes))
        seen = set(self._direct_hashes)
        while q:
            path = q.popleft()
            if len(path) >= max_hops:
                continue
            for nb in sorted(self._rf_adjacency.get(path[-1], ())):
                if nb == target_hash:
                    return path
                if nb not in seen:
                    seen.add(nb)
                    q.append(path + [nb])
        return None

    def _resolve_traffic_node(self, prefix_hex: str) -> dict | None:
        """Resolve a 1-byte path hash to a node. Hashes collide constantly on
        a big mesh (256 values vs. hundreds of contacts, including whole other
        regions), and taking the FIRST prefix match drew traffic lines to
        Boston repeaters that merely shared a hash byte with the local relay.
        Among colliding candidates, prefer the most recently heard node —
        the relay that just forwarded a packet was, by definition, active."""
        p = prefix_hex.lower()
        matches = [n for nid, n in self._nodes.items() if nid.lower().startswith(p)]
        if not matches:
            return None
        best = max(matches, key=lambda n: n.last_heard.timestamp() if n.last_heard else 0.0)
        d = {"id": best.node_id, "name": best.display_name, "lat": best.lat, "lon": best.lon}
        if len(matches) > 1:
            d["ambiguous"] = len(matches)
        return d

    def _touch_heard_node(self, prefix_hex: str) -> None:
        """Refresh last_heard for a node resolved from an overheard hash, but
        only when the resolution is UNAMBIGUOUS (single match) — display can
        tolerate a recency-weighted guess among colliding hashes, but survival
        (stale-node expiry) deserves a higher bar, or a hash collision could
        keep the WRONG node alive indefinitely off someone else's traffic."""
        if not prefix_hex:
            return
        p = prefix_hex.lower()
        matches = [nid for nid in self._nodes if nid.lower().startswith(p)]
        if len(matches) == 1:
            self._nodes[matches[0]].last_heard = datetime.now(timezone.utc)

    def _broadcast_traffic_event(self, route: str, ptype: str, snr, rssi,
                                 hops: int, hashes: list, origin_id=None, tag=None,
                                 confirmed: bool = False) -> None:
        """Resolve a relay-hash chain and push an rf_traffic WS event (map overlay).
        Used for both heard OTA packets (0x88) and our own TRACE_DATA results."""
        if self._router_broadcast is None:
            return
        # The receiving end of every heard packet is us. The radio's own node
        # rarely has an advert position, so fall back to ECH's configured base
        # location — without a positioned rx, 1-hop traffic and direct adverts
        # only ever yield ONE placeable point and no line can be drawn (this
        # was exactly the "saw one path, then nothing" field symptom).
        rx = self._resolve_traffic_node(self._self_node_id) if self._self_node_id else None
        if (rx is None or rx.get("lat") is None) and self._base_lat is not None:
            rx = {"id": self._self_node_id or "base", "name": "ECH base",
                  "lat": self._base_lat, "lon": self._base_lon}
        event = {
            "adapter": self.name,
            "route": route,
            "ptype": ptype,
            "snr": snr,
            "rssi": rssi,
            "hops": hops,
            "path": hashes,
            "nodes": [self._resolve_traffic_node(h) for h in hashes],
            "origin_id": origin_id,
            "origin": self._resolve_traffic_node(origin_id) if origin_id else None,
            "rx": rx,
        }
        if tag is not None:
            event["tag"] = tag
        event["confirmed"] = confirmed
        asyncio.ensure_future(self._router_broadcast("rf_traffic", event))

    @staticmethod
    def _fresh_rf_stats() -> dict:
        return {
            "advert_count": 0,
            "channel_msg_count": 0,
            "contact_msg_count": 0,
            "contact_count": 0,
            "trace_count": 0,
            "snr_samples": [],
            "rssi_samples": [],
            "hop_samples": [],
            "since": time.monotonic(),
        }

    def _log_rf_stats(self) -> None:
        s = self._rf_stats
        elapsed = max(time.monotonic() - s["since"], 1.0)
        adv_rate = s["advert_count"] / elapsed * 60
        msg_rate = (s["channel_msg_count"] + s["contact_msg_count"]) / elapsed * 60
        snr_avg  = (sum(s["snr_samples"])  / len(s["snr_samples"]))  if s["snr_samples"]  else None
        rssi_avg = (sum(s["rssi_samples"]) / len(s["rssi_samples"])) if s.get("rssi_samples") else None
        hop_avg  = (sum(s["hop_samples"])  / len(s["hop_samples"]))  if s["hop_samples"]  else None
        snr_str  = f"{snr_avg:+.1f}dB"  if snr_avg  is not None else "n/a"
        rssi_str = f"{rssi_avg:.0f}dBm" if rssi_avg is not None else "n/a"
        hop_str  = f"{hop_avg:.1f}"     if hop_avg  is not None else "n/a"
        log.info(
            "MeshCore %s RF summary (%.0fmin): adverts=%.1f/min msgs=%.1f/min "
            "contacts=%d snr=%s rssi=%s avg_hops=%s",
            self.name, elapsed / 60, adv_rate, msg_rate,
            s["contact_count"], snr_str, rssi_str, hop_str,
        )

    def _health_rf_stats(self) -> dict:
        s = self._rf_stats
        elapsed = max(time.monotonic() - s["since"], 1.0)
        return {
            "advert_rate_per_min": round(s["advert_count"] / elapsed * 60, 1),
            "msg_rate_per_min": round(
                (s["channel_msg_count"] + s["contact_msg_count"]) / elapsed * 60, 1
            ),
            "snr_avg_db": round(sum(s["snr_samples"]) / len(s["snr_samples"]), 1)
            if s["snr_samples"] else None,
            "rssi_avg_dbm": round(sum(s["rssi_samples"]) / len(s["rssi_samples"]), 0)
            if s.get("rssi_samples") else None,
            "hop_avg": round(sum(s["hop_samples"]) / len(s["hop_samples"]), 1)
            if s["hop_samples"] else None,
            "window_min": round(elapsed / 60, 1),
        }

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
        elif t == "browser":
            return BrowserTransport(adapter_name=config.get("name", "meshcore"))
        else:
            raise ValueError(f"MeshCore: unsupported transport '{t}'. Use serial, tcp, or browser.")

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
        # Remember what we sent so a PACKET_ERROR can name its likely cause —
        # the error push carries no correlation data of its own.
        self._last_cmd_sent = (payload[:1].hex(), time.monotonic())
        await self._transport.write(self._build_frame(payload))

    # ── Initialization sequence ───────────────────────────────────────────

    async def _send_text_cmd(self, cmd: str) -> str:
        """Send a raw text command (serial CLI mode) and return the one-line response.
        Must only be called before _run_task starts (no concurrent frame reader)."""
        try:
            await self._transport.read_raw(256, timeout=0.2)   # flush stale bytes
            await self._transport.write((cmd + "\r\n").encode("ascii"))
            await asyncio.sleep(0.2)
            raw = await self._transport.read_raw(256, timeout=0.5)
            resp = raw.decode("ascii", errors="ignore").strip() if raw else ""
            log.info("MeshCore %s: text cmd %r → %r", self.name, cmd, resp[:80])
            return resp
        except Exception as exc:
            log.warning("MeshCore %s: text cmd %r error: %s", self.name, cmd, exc)
            return ""

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

        # 3.5 Push ECH's base location onto the device's own advert position, if
        # already known (set_base_location() runs before connect() during startup,
        # so self._connected was still False and couldn't push it at the time).
        if self._base_lat is not None and self._base_lon is not None:
            await self.push_advert_location(self._base_lat, self._base_lon)
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
            # Apply persistent device settings via text CLI while we still own the reader.
            if self._path_hash_mode is not None:
                await self._send_text_cmd(f"set path.hash.mode {self._path_hash_mode}")
            # Diagnostic only: log the radio's configured TX duty cycle. Firmware
            # enforces an airtime budget (src/Dispatcher.cpp) — a low duty cycle
            # (common where regulation requires it, e.g. 1% in some regions) can
            # silently DELAY sends once the budget is spent, which reads exactly
            # like "the first trace worked, then nothing" even though the command
            # went out fine every time — the delay just isn't visible from ECH.
            dc_resp = await self._send_text_cmd("get dutycycle")
            if dc_resp.strip():
                log.info("MeshCore %s: radio TX duty cycle = %s (a low value here can silently "
                         "delay/drop repeated sends once the airtime budget is spent)",
                         self.name, dc_resp.strip())
        # Pre-warm node cache from DB so relay hashes can be resolved before GET_CONTACTS completes.
        await self._prewarm_nodes_from_db()
        await self._restore_topology()
        # Now start the binary RX loop; _init_sequence sends binary commands only.
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        await asyncio.sleep(0)  # yield so the task is scheduled
        await self._init_sequence()
        log.info("MeshCore %s: ready, monitoring channel %d", self.name, self._channel_idx)

    async def _prewarm_nodes_from_db(self) -> None:
        """Load previously persisted nodes so relay hashes resolve before GET_CONTACTS finishes."""
        if not self._db:
            return
        import json as _json
        rows = await self._db.get_mesh_nodes(self.name)
        loaded = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            nid = row["node_id"]
            if nid in self._nodes:
                continue
            lh = None
            if row.get("last_heard"):
                try:
                    lh = datetime.fromisoformat(row["last_heard"])
                except ValueError:
                    pass
            meta = {}
            if row.get("meta_json"):
                try:
                    meta = _json.loads(row["meta_json"])
                except Exception:
                    pass
            node = MeshNode(
                node_id=nid, display_name=row["display_name"],
                first_seen=lh or now, last_heard=lh,
                lat=row.get("lat"), lon=row.get("lon"),
                name_source="db",
            )
            node.meta = meta
            self._nodes[nid] = node
            loaded += 1
        if loaded:
            log.info("MeshCore %s: pre-warmed %d node(s) from DB", self.name, loaded)

    async def _persist_nodes(self) -> None:
        """Upsert all named nodes to the mesh_nodes DB table."""
        if not self._db:
            return
        for node in list(self._nodes.values()):
            if node.display_name and node.display_name != node.node_id:
                try:
                    await self._db.upsert_mesh_node(self.name, node)
                except Exception as exc:
                    log.debug("MeshCore %s: node persist error: %s", self.name, exc)

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

    def _resolve_sender_node_id(self, display_name: str) -> str:
        """Map a polled channel message's "name: body" sender name back to the
        node's pubkey id, so from_id lines up with the node_id used everywhere
        else (map popups, node list, /api/messages?from_id=). Falls back to the
        display name itself when it's unknown or ambiguous — same identity the
        message would otherwise have been stored under."""
        want = (display_name or "").strip().lower()
        if not want:
            return display_name
        matches = [n for n in self._nodes.values()
                   if (n.display_name or "").strip().lower() == want]
        if len(matches) == 1:
            return matches[0].node_id
        return display_name

    def _resolve_dm_dest(self, to_id: str) -> bytes | None:
        """Turn a DM destination — pubkey hex or node display name — into the
        6 destination-pubkey bytes the wire format needs. Returns None if the
        value is neither valid hex nor a unique known node name."""
        want = to_id.strip().lower()
        matches = [n for n in self._nodes.values()
                   if (n.display_name or "").strip().lower() == want]
        if len(matches) == 1:
            key = matches[0].node_id
        elif len(matches) > 1:
            log.warning("MeshCore %s: DM destination %r matches %d different nodes — "
                        "refusing the ambiguous send; use the pubkey instead",
                        self.name, to_id, len(matches))
            return None
        else:
            key = to_id
        try:
            return bytes.fromhex(key[:12].ljust(12, "0"))[:6]
        except ValueError:
            return None

    async def send(self, message: NormalizedMessage) -> bool:
        """Send a channel broadcast or DM depending on whether to_id is set."""
        ts = int(message.timestamp.timestamp())
        to_id = (message.to_id or "").strip()
        body_bytes = message.body.encode("utf-8")[:200]

        if to_id:
            # Direct message (DM) — wire format from meshcore_py reference:
            # [0x02][0x00][attempt:1][timestamp:4][dest_pubkey:6][body_utf8]
            # to_id may be a display NAME rather than a pubkey: polled channel
            # messages carry no sender pubkey, so the UI's click-a-sender path
            # hands us the parsed name (e.g. "Deej"). Resolve names against the
            # node registry before interpreting the value as hex — name-first
            # also stops a short hex-looking name ("beef") from being silently
            # zero-padded into the wrong destination pubkey prefix.
            dest_bytes = self._resolve_dm_dest(to_id)
            if dest_bytes is None:
                # Deliberately a hard failure, NOT a channel-broadcast fallback:
                # silently broadcasting the body of a failed PRIVATE message to
                # the whole channel is a privacy leak, and the sender gets a
                # visible delivery failure this way instead of a false success.
                log.error("MeshCore %s: cannot DM %r — not a pubkey and no unique "
                          "known node has that name; send failed", self.name, to_id)
                return False
            payload = (
                b"\x02\x00"
                + bytes([0])           # attempt = 0 (first send)
                + struct.pack("<I", ts)
                + dest_bytes
                + body_bytes
            )
            log.info("MeshCore %s: sending DM to %s: %s", self.name, to_id[:12], message.body[:60])
        if not to_id:
            # Channel broadcast — CMD_SEND_CHANNEL_MSG (0x03)
            # Honour a channel_idx hint from the bot/router so replies go back
            # to the originating channel, not the adapter's configured default.
            # Also accept channel_name (from weather auto-broadcast or bot) and resolve it.
            raw_dict = message.raw or {}
            ch_name_hint = str(raw_dict.get("channel_name", "")).strip()
            if ch_name_hint:
                want = ch_name_hint.lstrip("#").lower()
                # Support numeric string ("2") as a direct channel index
                try:
                    ch_idx = int(want)
                    if ch_idx < 0 or ch_idx > 7:
                        raise ValueError("out of range")
                except ValueError:
                    resolved = next(
                        (idx for idx, n in self._channels.items() if n.lower() == want),
                        None,
                    )
                    if resolved is None:
                        known = {i: n for i, n in self._channels.items()}
                        log.warning(
                            "MeshCore %s: channel_name %r not found in device channel list %s — "
                            "falling back to adapter default ch%d",
                            self.name, ch_name_hint, known, self._channel_idx,
                        )
                    ch_idx = resolved if resolved is not None else self._channel_idx
            else:
                ch_idx = int(raw_dict.get("channel_idx", self._channel_idx))
            payload = (
                bytes([CMD_SEND_CHANNEL_MSG, self._max_hops & 0xFF, ch_idx])
                + struct.pack("<I", ts)
                + body_bytes
            )
            # Record which channel this actually went out on — the caller-supplied
            # source_channel ("outbound") would otherwise hide the resolved target,
            # especially when a channel_name hint diverted it from the adapter default.
            ch_name = self._channels.get(ch_idx)
            message.source_channel = f"ch{ch_idx}:{ch_name}" if ch_name else f"ch{ch_idx}"
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

    async def _expire_pending_tag(self, tag: int, after: float) -> None:
        await asyncio.sleep(after)
        self._pending_trace_tags.discard(tag)

    async def ping(self, node_id: str, via: "list[str] | None" = None) -> dict:
        """Send CMD_SEND_TRACE_PATH (0x24) — broadcasts a trace packet onto the mesh.
        Results arrive as TRACE_DATA (0x89) and appear in the message feed.

        `via`, if given, is an ordered list of repeater node_ids (e.g. from an
        operator clicking repeaters on the map) that OVERRIDES the device's
        stored route / ECH's inferred topology route entirely — the probe is
        built from exactly that hop sequence instead."""
        if not self._connected:
            return {"status": "error", "detail": "not connected"}
        import random as _random
        tag = _random.randint(1, 0xFFFFFFFF)
        # auth_code 0 matches the reference implementation's default
        # (meshcore_py send_trace); a random nonzero value was one more
        # unverified difference in a command that was getting no replies.
        auth_code = 0
        # Trace path hash size. Default 1 byte — the ecosystem convention
        # (meshcore-bot's proven trace defaults to one_byte mode); wider hashes
        # are available (flags: 0→1B, 1→2B, 2→4B) but off-mainstream on real
        # meshes. Ambiguity is handled at RESOLUTION time (recency-weighted
        # prefix match) rather than on the wire.
        hash_bytes = int(self.config.get("trace_hash_bytes", 1))
        if hash_bytes not in (1, 2, 4):
            hash_bytes = 1
        flags = {1: 0, 2: 1, 4: 2}[hash_bytes]
        # DIRECTED trace: route the probe along the device's own stored path
        # to the target (contact out_path — the repeater hashes between us and
        # it) plus the target's hash. A bare single-hop path only works for
        # repeaters we can hear DIRECTLY; for anything further out the probe
        # died in silence, which field-matched "trace does nothing" even
        # against a known repeater. Only repeaters process trace paths —
        # companions/sensors typically never answer.
        path = b""
        target = None
        path_truncated = False
        hops = 0
        route_src = ""
        node_id = (node_id or "").strip()
        hop_prefixes: list = []   # per-hop node pubkey prefixes at hash_bytes width
        if node_id:
            n = self._nodes.get(node_id) or self._nodes.get(node_id.upper())
            if n is not None:
                target = n.display_name
                # Build the relay chain as 1-byte hex hashes — these ARE the
                # wire format at the default width, so an unresolvable hop is
                # NOT a reason to discard the route (an earlier version threw
                # away every inferred route containing one unnamed hash and
                # silently fell back to a bare probe — the field symptom was
                # "still only works on 0-hop nodes"). Name resolution is for
                # display only.
                relay_hashes: list = []
                if via:
                    # Operator-drawn path: resolve each clicked node to its
                    # hash prefix, in the order clicked. Unknown ids are
                    # dropped with a log warning rather than aborting the
                    # whole probe — better a shorter drawn path goes out than
                    # nothing at all.
                    for vid in via:
                        vn = self._nodes.get(vid) or self._nodes.get(vid.upper())
                        if vn is None:
                            log.warning("MeshCore %s: manual path hop %r not a known node — skipped",
                                        self.name, vid)
                            continue
                        relay_hashes.append(vn.node_id[:2].lower())
                    route_src = "manual path"
                else:
                    try:
                        stored = bytes.fromhex((n.meta or {}).get("out_path") or "")
                    except ValueError:
                        stored = b""
                    if 0 < len(stored) <= 16:
                        relay_hashes = [f"{b:02x}" for b in stored]
                        route_src = "device route"
                    else:
                        inferred = self._infer_route(n.node_id[:2])
                        if inferred:
                            relay_hashes = list(inferred)
                            route_src = "observed topology"
                        elif inferred == []:
                            route_src = "heard direct"
                # Don't double the target if the chain already ends at it
                if relay_hashes and relay_hashes[-1] == n.node_id[:2].lower():
                    relay_hashes = relay_hashes[:-1]
                path_truncated = via is not None and len(relay_hashes) > 5
                if hash_bytes == 1:
                    outbound = relay_hashes[:5] + [n.node_id[:2].lower()]
                else:
                    # Wider hashes need full node identities per hop — drop
                    # hops that don't resolve (rare, opt-in mode only).
                    width = 2 * hash_bytes
                    resolved = [self._resolve_traffic_node(h) for h in relay_hashes[:5]]
                    outbound = ([r["id"][:width].lower() for r in resolved if r is not None]
                                + [n.node_id[:width].lower()])
                # RECIPROCAL path (the meshcore-bot pattern that actually
                # works in the field): a trace reply does not route itself
                # home — the probe must carry its own return path. Mirror the
                # outbound chain minus the far end: [15,8b] → [15,8b,15], so
                # the packet goes out through the relays, turns around at the
                # target, and comes back where we can hear it. A bare 1-hop
                # probe stays as-is (the target's own rebroadcast reaches us).
                hop_prefixes = (outbound + list(reversed(outbound[:-1]))
                                if len(outbound) >= 2 else outbound)
                path = b"".join(bytes.fromhex(hp) for hp in hop_prefixes)
                hops = len(hop_prefixes)
        payload = (
            bytes([CMD_SEND_TRACE_PATH])
            + tag.to_bytes(4, 'little')
            + auth_code.to_bytes(4, 'little')
            + bytes([flags])
            + path
        )
        # Firmware requires strict len > 10 (MyMesh.cpp:1620, per meshcore_py's
        # own N05 note) — an empty-path trace is exactly 10 bytes and gets
        # PACKET_ERROR'd without this pad. (This pad was here originally, was
        # removed after an incomplete read of the reference, and the firmware
        # immediately proved the reference right.)
        if len(payload) <= 10:
            payload += b"\x00"
        # Resolve the attempted route so the UI can name and draw it
        route_points = [self._resolve_traffic_node(hp) or {"id": hp, "name": f"{hp}?", "lat": None, "lon": None}
                        for hp in hop_prefixes]
        route_names = " → ".join(p["name"] for p in route_points)
        try:
            await self._send_cmd(payload)
            self._pending_trace_tags.add(tag)
            asyncio.ensure_future(self._expire_pending_tag(tag, 30.0))
            log.info("MeshCore %s: SEND_TRACE_PATH tag=%08x via=%s hops=%d src=%s — response arrives as TRACE_DATA (0x89)",
                     self.name, tag, route_names or "flood", hops, route_src or "-")
            detail = (f"Trace sent via {route_names} ({route_src})" if target
                      else f"Trace probe sent (tag {tag:08x})") + \
                     " — waiting for a reply; only repeaters answer traces"
            if path_truncated:
                detail += " (path truncated to first 5 repeaters — protocol limit)"
            return {"status": "sent", "detail": detail, "tag": tag, "via": target,
                    "hops": hops, "route_src": route_src, "route_points": route_points,
                    "path_truncated": path_truncated}
        except Exception as exc:
            log.error("MeshCore %s: SEND_TRACE_PATH error: %s", self.name, exc)
            return {"status": "error", "detail": str(exc)}

    async def trace_and_wait(self, timeout: float = 25.0) -> dict:
        """Send a trace probe and await its TRACE_DATA result — used by anomaly
        verification to get a live path measurement synchronously. The probe is
        a mesh flood (no destination), so the result describes the current
        relay depth/SNR of the reachable mesh, not a route to one node."""
        res = await self.ping("")
        if res.get("status") != "sent":
            return {"status": "error", "detail": res.get("detail", "trace send failed")}
        tag = res["tag"]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._trace_futures[tag] = fut
        try:
            data = await asyncio.wait_for(fut, timeout=timeout)
            return {"status": "ok", **data}
        except asyncio.TimeoutError:
            return {"status": "timeout",
                    "detail": f"no TRACE_DATA reply within {timeout:.0f}s — mesh may be "
                              "quiet/unreachable, or no repeater echoed the probe"}
        finally:
            self._trace_futures.pop(tag, None)

    # ── Internal receive loop ─────────────────────────────────────────────

    async def _flush_contact_removals(self) -> None:
        """Send CMD_REMOVE_CONTACT for each stale contact queued during GET_CONTACTS.

        Sends deletions after the contact stream completes so we never interrupt
        an in-progress GET_CONTACTS response.  The device confirms each delete with
        PACKET_CONTACT_DELETED (0x8F), which is logged in _dispatch_frame.
        """
        removals = self._pending_contact_removals[:]
        self._pending_contact_removals.clear()
        log.info("MeshCore %s: purging %d stale contact(s) from device flash",
                 self.name, len(removals))
        for pubkey_bytes in removals:
            try:
                await self._send_cmd(bytes([CMD_REMOVE_CONTACT]) + pubkey_bytes)
                await asyncio.sleep(0.3)   # let device write flash between deletions
            except Exception as exc:
                log.warning("MeshCore %s: error removing contact %s: %s",
                            self.name, pubkey_bytes[:6].hex().upper(), exc)

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
        last_rf_stats      = now0
        # Named nodes should survive a quiet day on the mesh — many stations
        # advertise once at power-up and then stay silent, and the operator
        # expects to still see them on the map/node list well after that.
        _NODE_STALE_SEC  = self._node_ttl_sec if self._node_ttl_sec > 0 else 86400.0
        _BATTERY_INTERVAL = 300.0   # poll battery every 5 minutes
        _RF_STATS_INTERVAL = 300.0  # log RF health summary every 5 minutes
        try:
            while self._connected:
                now = time.monotonic()

                # Safety valve: if contacts stream started but CONTACT_END never arrived
                # (device restart or serial glitch mid-stream), clear the flag after 30s
                # so message polling is not blocked indefinitely.
                if self._contacts_in_progress and now - self._contacts_stream_started > 30.0:
                    log.warning("MeshCore %s: contacts stream timeout — resetting _contacts_in_progress", self.name)
                    self._contacts_in_progress = False
                    if self._msg_poll_pending:
                        self._msg_poll_pending = False
                        await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))

                # Periodic poll for queued messages — skip while contacts list is streaming
                # to avoid PACKET_ERROR from command collision on the serial bus
                if now - last_poll >= self._poll_interval and not self._contacts_in_progress:
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
                    self._last_discovery_ts = now

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

                # Triggered contacts refresh — rate-limited to once per 2 minutes
                import time as _time_mod
                if (self._contacts_refresh_pending
                        and _time_mod.monotonic() - self._contacts_last_refresh >= self._contacts_min_interval):
                    self._contacts_refresh_pending = False
                    self._contacts_last_refresh = _time_mod.monotonic()
                    await self._send_cmd(bytes([CMD_GET_CONTACTS]))
                    await asyncio.sleep(0.2)
                elif self._contacts_refresh_pending:
                    pass  # too soon — wait for cooldown

                # Periodic RF channel health summary
                if now - last_rf_stats >= _RF_STATS_INTERVAL:
                    self._log_rf_stats()
                    self._rf_stats = self._fresh_rf_stats()
                    last_rf_stats = now

                # Expire stale nodes.
                # Named nodes expire after node_ttl_hours (default 24h).
                # Hex-only nodes (no resolved name) expire after 15 min.
                # Nodes with last_heard=None use first_seen as the age reference.
                if now - last_expiry >= 300.0:
                    wall_now = datetime.now(timezone.utc).timestamp()
                    cutoff_named = wall_now - _NODE_STALE_SEC
                    cutoff_hex   = wall_now - 900.0
                    future_cutoff = wall_now + 3600.0  # allow 1h clock skew
                    stale = []
                    for nid, n in self._nodes.items():
                        # Use last_heard if set, else fall back to first_seen for age check
                        ref_ts = (n.last_heard or n.first_seen)
                        if ref_ts is None:
                            continue
                        lh = ref_ts.timestamp()
                        # Expire nodes whose timestamp is impossibly far in the future —
                        # these come from devices with corrupted/unset RTCs (e.g. uint32
                        # overflow) and would otherwise live in the list forever since a
                        # future lh is never < cutoff.
                        if lh > future_cutoff:
                            stale.append((nid, f"future timestamp ({lh:.0f})"))
                            continue
                        is_hex_only = (n.display_name == nid or
                                       n.display_name == nid.upper())
                        if is_hex_only and lh < cutoff_hex:
                            stale.append((nid, "hex-only, >15min"))
                        elif not is_hex_only and lh < cutoff_named:
                            stale.append((nid, "named, >TTL"))
                    for nid, reason in stale:
                        log.debug("MeshCore %s: expiring node %s (%s)",
                                  self.name, nid, reason)
                        del self._nodes[nid]
                    if stale:
                        log.info("MeshCore %s: expired %d stale node(s)", self.name, len(stale))
                    last_expiry = now
                    # Piggyback on the 5-min housekeeping tick: persist the
                    # learned topology so it survives restarts/deploys.
                    asyncio.ensure_future(self._persist_topology())

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
        # 0x84–0x87: observed in newer firmware builds; protocol not yet documented upstream
        0x84: "PUSH_CONTACT_MSG_WAITING", 0x85: "PUSH_UNKNOWN_85",
        0x86: "PUSH_UNKNOWN_86", 0x87: "PUSH_UNKNOWN_87",
        0x88: "PUSH_CHANNEL_MSG",
        0x89: "TRACE_DATA",
        0x8A: "PUSH_UNKNOWN_8A", 0x8B: "PUSH_UNKNOWN_8B",
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
            # Start of GET_CONTACTS response — reset temporary accumulator and block msg poll
            self._contacts_building = {}
            self._contacts_in_progress = True
            self._contacts_stream_started = time.monotonic()

        elif pkt_type == PACKET_CONTACT:
            # Layout: pubkey(32) + type(1) + flags(1) + plen(1) + out_path(N) +
            #         adv_name(32) + last_advert(4) + adv_lat(4) + adv_lon(4) + lastmod(4)
            # Trailing fields are always 32+4+4+4+4 = 48 bytes.
            # Fixed header (before out_path) is always 35 bytes.
            # Compute path_size per-record from raw frame length — do NOT reuse a globally
            # detected value, because different contact records can have different path sizes
            # depending on how many relay hops MeshCore stored for that node.
            _CONTACT_TAIL = 48   # adv_name + last_advert + lat + lon + lastmod
            _CONTACT_HEAD = 35   # pubkey(32) + type + flags + plen
            raw_len = len(data)
            plen_byte = data[34] if raw_len > 34 else 0   # hop count stored by firmware
            path_size = max(0, raw_len - _CONTACT_HEAD - _CONTACT_TAIL)
            # The device's stored route to this contact — its EXPECTED path.
            # Semantics verified against meshcore_py reader.py: the out_path
            # field is a FIXED 64-byte zero-filled buffer; plen==255 means
            # "flood" (NO stored route); otherwise plen&0x3F is the number of
            # valid path bytes and plen>>6 the hash-size mode. Storing the
            # whole 64-byte buffer (as this parser originally did) produced
            # 65-hop garbage trace paths that the firmware PACKET_ERROR'd.
            if plen_byte == 0xFF:
                expected_hops = None                    # flood — no stored route
                out_path_hex = ""
            else:
                _hash_mode = plen_byte >> 6
                _n_valid = plen_byte & 0x3F
                if _hash_mode == 0 and _n_valid <= 16:  # 1-byte hashes only
                    expected_hops = _n_valid
                    out_path_hex = data[_CONTACT_HEAD:_CONTACT_HEAD + _n_valid].hex()
                else:
                    expected_hops = None
                    out_path_hex = ""
            name_off = _CONTACT_HEAD + path_size
            min_len  = name_off + 32
            if len(data) >= min_len:
                pubkey  = data[:32].hex().upper()
                node_id = pubkey[:12]
                # Contact TYPE byte (offset 32, right after the pubkey) — was
                # parsed past for months. CLI = companion (messageable), REP =
                # repeater (routes only, no message queue — DMs to it fail),
                # ROOM = room server (messageable), SENS = sensor.
                _CONTACT_TYPES = ("NONE", "CLI", "REP", "ROOM", "SENS")
                ctype = data[32]
                node_type = _CONTACT_TYPES[ctype] if ctype < len(_CONTACT_TYPES) else f"T{ctype}"
                adv_name = data[name_off:name_off+32].decode("utf-8", errors="ignore").rstrip("\x00").strip()
                advert_off = name_off + 32
                lat_off = advert_off + 4
                lon_off = lat_off + 4
                # last_advert is uint32 Unix seconds — use as last_heard so nodes
                # don't all appear "just heard" every time contacts are polled.
                last_advert_raw = struct.unpack("<I", data[advert_off:advert_off+4])[0] if len(data) >= advert_off+4 else 0
                try:
                    last_advert_dt = datetime.fromtimestamp(last_advert_raw, tz=timezone.utc) if last_advert_raw > 0 else None
                except (OSError, OverflowError):
                    last_advert_dt = None
                lat_raw = struct.unpack("<i", data[lat_off:lat_off+4])[0] if len(data) >= lat_off+4 else 0
                lon_raw = struct.unpack("<i", data[lon_off:lon_off+4])[0] if len(data) >= lon_off+4 else 0
                # 0x7FC00000 is the float32 NaN bit pattern used by MeshCore firmware as
                # the "no GPS" sentinel — treat it and 0 as absent, not as a coordinate.
                _NO_LOC = 0x7FC00000
                lat = (lat_raw / 1e6) if (lat_raw and lat_raw != _NO_LOC) else None
                lon = (lon_raw / 1e6) if (lon_raw and lon_raw != _NO_LOC) else None
                # Check for remaining out-of-range values (corrupted GPS in contact DB)
                if lat is not None and not (-90.0 <= lat <= 90.0):
                    if node_id not in self._bad_coord_warned:
                        # Only alert once per node per session; these come from other nodes'
                        # stored GPS in MeshCore's contact database, not from our device.
                        self._bad_coord_warned.add(node_id)
                        # Diagnostic: log raw parsing details so we can tell if it's a
                        # genuine bad GPS in the mesh vs. a parse offset bug.
                        # plen_byte=hop_count path_size=computed_bytes lat_raw=0xHH...
                        log.warning(
                            "MeshCore %s: bad coords for %s (%r) lat=%.2f lon=%.2f "
                            "[parse diag: raw_len=%d plen_byte=%d path_size=%d lat_raw=0x%08X lon_raw=0x%08X]",
                            self.name, node_id, adv_name, lat, lon or 0,
                            raw_len, plen_byte, path_size, lat_raw & 0xFFFFFFFF, lon_raw & 0xFFFFFFFF,
                        )
                        if self._anomaly_engine:
                            _age_str = None
                            if last_advert_dt:
                                _age_sec = (datetime.now(timezone.utc) - last_advert_dt).total_seconds()
                                _age_str = f"{int(_age_sec//86400)}d {int(_age_sec%86400//3600)}h ago"
                            asyncio.ensure_future(
                                self._anomaly_engine.process_contact(
                                    self.name, node_id, lat, lon, adv_name,
                                    extra={
                                        "pubkey": pubkey,
                                        "lat_raw_int": lat_raw,
                                        "lon_raw_int": lon_raw,
                                        "last_advert": last_advert_dt.isoformat() if last_advert_dt else "never",
                                        "last_advert_age": _age_str or "unknown",
                                        "source": "GET_CONTACTS (stored in MeshCore flash)",
                                        "parse_diag": f"raw_len={raw_len} plen_byte={plen_byte} path_size={path_size}",
                                    }
                                )
                            )
                    lat = None
                    lon = None
                elif lat is not None and lon is not None and self._anomaly_engine:
                    asyncio.ensure_future(
                        self._anomaly_engine.process_contact(self.name, node_id, lat, lon, adv_name)
                    )
                self._rf_stats["contact_count"] += 1
                now = datetime.now(timezone.utc)
                _stale_sec = self._node_ttl_sec if self._node_ttl_sec > 0 else 3600.0
                if node_id not in self._nodes:
                    # Only register contacts from GET_CONTACTS if they have been heard recently.
                    # Without this guard the expiry/re-registration yo-yo never resolves:
                    # nodes get pruned every 5 min but GET_CONTACTS re-adds them with the same
                    # old last_advert_dt on the very next contact poll.
                    if last_advert_dt is not None:
                        age_sec = (now - last_advert_dt).total_seconds()
                        # Reject contacts that are either:
                        #   • too old  (age_sec >  TTL) — stale, normal case
                        #   • in the future (age_sec < -3600) — corrupted device RTC;
                        #     without this guard, future timestamps pass the > TTL check
                        #     (negative age is not > 64800) and then the expiry never
                        #     fires (future lh is never < cutoff), so they live forever.
                        if age_sec > _stale_sec or age_sec < -3600:
                            log.debug("MeshCore %s: skipping contact %s (%r), "
                                      "last advert age %.0fs (stale or future RTC)",
                                      self.name, node_id, adv_name, age_sec)
                            self._pending_contact_removals.append(data[:32])
                            return   # one contact per PACKET_CONTACT frame
                    # Contacts with last_advert=0 (unknown) are registered but with no
                    # last_heard so the hex-only 15-min expiry clears them quickly if unnamed.
                    self._nodes[node_id] = MeshNode(
                        node_id=node_id, display_name=adv_name or node_id,
                        first_seen=last_advert_dt or now,
                        last_heard=last_advert_dt,
                        name_source="contact",
                        lat=lat, lon=lon,
                    )
                    self._nodes[node_id].meta["expected_hops"] = expected_hops
                    self._nodes[node_id].meta["out_path"] = out_path_hex
                    self._nodes[node_id].meta["node_type"] = node_type
                else:
                    n = self._nodes[node_id]
                    if adv_name and n.name_source not in ("self_info",):
                        n.display_name = adv_name
                        n.name_source = "contact"
                    if lat is not None:
                        n.lat = lat
                    if lon is not None:
                        n.lon = lon
                    # Only backfill last_heard from contact record if we have no RF-heard time yet
                    if n.last_heard is None and last_advert_dt is not None:
                        n.last_heard = last_advert_dt
                    n.meta["expected_hops"] = expected_hops
                    n.meta["out_path"] = out_path_hex
                    n.meta["node_type"] = node_type
                log.debug("MeshCore %s: contact %s = %r lat=%s lon=%s",
                          self.name, node_id, adv_name, lat, lon)
            else:
                log.warning("MeshCore %s: CONTACT too short (%dB, need %d)", self.name, len(data), min_len)

        elif pkt_type == PACKET_CONTACT_END:
            self._contacts_in_progress = False
            log.info("MeshCore %s: GET_CONTACTS complete, %d nodes registered",
                     self.name, len(self._nodes))
            # Persist all named nodes so they survive restarts
            asyncio.ensure_future(self._persist_nodes())
            # Purge stale contacts from device flash so they stop cycling back in
            if self._pending_contact_removals:
                asyncio.ensure_future(self._flush_contact_removals())
            # Drain any DM that arrived while contacts were streaming
            if self._msg_poll_pending:
                self._msg_poll_pending = False
                await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))

        elif pkt_type == PACKET_CONTACT_DELETED:
            node_hex = data[:6].hex().upper() if len(data) >= 6 else data.hex()
            log.info("MeshCore %s: device confirmed contact deleted: %s…", self.name, node_hex)

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
            self._stamp_self_position()
            if name:
                log.info("MeshCore %s: self_info name=%r node=%s", self.name, name, local_pubkey)
                if local_pubkey and local_pubkey in self._nodes:
                    asyncio.ensure_future(self._db.upsert_mesh_node(self.name, self._nodes[local_pubkey])) if self._db else None
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

        elif pkt_type == PUSH_CHANNEL_MSG:
            # 0x88 = LOG_DATA per meshcore-py library v2.3 PacketType enum.
            # Format: [SNR:1][RSSI:1][raw_OTA_mesh_packet:N]
            # This is the raw radio receive log pushed for every OTA packet the device hears.
            # It contains advertisements, channel msgs, DMs etc. in their OTA (possibly
            # AES-encrypted) wire format — NOT the companion-protocol formatted message.
            # Channel/DM messages are delivered via PUSH_MSG_WAITING → CMD_SYNC_NEXT_MESSAGE
            # → 0x08/0x11, which the device decrypts before forwarding to the companion.
            # ECH uses 0x88 only for RF metrics (SNR/RSSI); message parsing happens via poll.
            log.info("MeshCore %s: LOG_DATA(0x88) len=%d raw=%s",
                     self.name, len(frame), data[:32].hex())
            if len(data) >= 2:
                snr_byte  = data[0]
                rssi_byte = data[1]
                snr  = (snr_byte  if snr_byte  < 128 else snr_byte  - 256) / 4.0
                rssi =  rssi_byte if rssi_byte < 128 else rssi_byte - 256
                self._rf_stats["snr_samples"].append(snr)
                self._rf_stats["rssi_samples"].append(rssi)
                if len(self._rf_stats["snr_samples"]) > 1000:
                    self._rf_stats["snr_samples"]  = self._rf_stats["snr_samples"][-500:]
                    self._rf_stats["rssi_samples"]  = self._rf_stats["rssi_samples"][-500:]
                # Cache for RSSI correlation when the same packet arrives via poll (0x11/0x08)
                self._recent_log_data.append({"snr": snr, "rssi": rssi})
                # Live traffic visualization: the raw OTA packet after the two
                # metric bytes carries the routing header incl. relay-path hashes —
                # exactly the data the map's traffic overlay needs.
                try:
                    self._emit_rf_traffic(data[2:], snr, rssi)
                except Exception as exc:
                    log.debug("MeshCore %s: rf traffic parse error: %s", self.name, exc)

        elif pkt_type in (PACKET_CHANNEL_MSG_RECV, PACKET_CHANNEL_MSG_V3):
            # Polled channel messages via CMD_SYNC_NEXT_MESSAGE.
            # The device has already decrypted these using its stored channel PSKs.
            log.debug("MeshCore %s: POLLED_CHANNEL_MSG(0x%02x) len=%d raw=%s",
                      self.name, pkt_type, len(frame), data.hex())
            await self._handle_channel_msg(data, pkt_type)

        elif pkt_type in (PACKET_CONTACT_MSG_RECV, PACKET_CONTACT_MSG_V3):
            await self._handle_contact_msg(data, v3=(pkt_type == PACKET_CONTACT_MSG_V3))

        elif pkt_type == PACKET_NO_MORE_MSGS:
            pass  # queue empty, normal

        elif pkt_type == PUSH_MSG_WAITING:
            # Unsolicited: new message queued; fetch immediately unless contacts are
            # streaming — sending a command mid-stream causes serial bus collision.
            if self._contacts_in_progress:
                self._msg_poll_pending = True
            else:
                await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))

        elif pkt_type == 0x84:
            # 0x84 observed in newer MeshCore firmware — likely "contact/DM message waiting".
            # Protocol not yet documented; treat same as PUSH_MSG_WAITING and poll for messages.
            log.debug("MeshCore %s: 0x84 PUSH_CONTACT_MSG_WAITING — polling for messages", self.name)
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
                # Resolve node hashes to names — prefix + recency, same
                # resolver as the traffic overlay. (The original resolver here
                # used SUBSTRING matching, so hash "6b" matched inside
                # RKDRPTMON's "1551f6bb…" pubkey and a 3-hop reply displayed
                # as the same repeater three times.)
                def _resolve_hash(h: str) -> str:
                    r = self._resolve_traffic_node(h)
                    if r is None:
                        return h
                    return r["name"] + ("?" if r.get("ambiguous") else "")
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
                # A successful trace CONFIRMS the chain outward from us
                self._learn_topology(path_nodes, confirmed_from_us=True)
                for h in path_nodes:
                    self._touch_heard_node(h)
                # Resolve an awaiting trace_and_wait() caller (anomaly verification)
                fut = self._trace_futures.get(tag)
                if fut is not None and not fut.done():
                    fut.set_result({"hops": path_len, "path": path_nodes,
                                    "named": named_nodes, "snrs": snr_values})
                # Draw the measured trace path on the map — a trace result is
                # the highest-quality path data we ever get (per-hop SNR from
                # a probe we sent), so the operator should SEE it, not just
                # find a line in the message feed. No rate gate: traces are
                # rare and operator-initiated.
                avg_snr = (sum(snr_values) / len(snr_values)) if snr_values else None
                self._pending_trace_tags.discard(tag)
                self._broadcast_traffic_event("DIRECT", "TRACE", avg_snr, None,
                                              path_len, path_nodes, tag=tag, confirmed=True)
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
                self._rf_stats["advert_count"] += 1
                log.debug("MeshCore %s: PUSH_ADVERT node=%s", self.name, node_id)
                # Rate-limit check — suppress contacts refresh on flooded nodes
                flood = None
                if self._anomaly_engine:
                    flood = await self._anomaly_engine.record_packet(self.name, node_id)
                now = datetime.now(timezone.utc)
                if node_id not in self._nodes:
                    self._nodes[node_id] = MeshNode(
                        node_id=node_id, display_name=node_id,
                        first_seen=now, last_heard=now, name_source="advert",
                    )
                    # Only trigger contacts refresh if this node isn't flooding us
                    if flood is None:
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
            last_cmd, last_ts = getattr(self, "_last_cmd_sent", (None, 0.0))
            log.warning("MeshCore %s: PACKET_ERROR received (last cmd sent: 0x%s, %.1fs ago)",
                        self.name, last_cmd or "??",
                        time.monotonic() - last_ts if last_cmd else -1.0)

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
            log.debug("MeshCore %s: unhandled pkt_type=0x%02x len=%d raw=%s",
                      self.name, pkt_type, len(frame), frame[:32].hex())

    async def _handle_channel_msg(self, data: bytes, pkt_type: int) -> None:
        """
        Parse polled channel messages (delivered via CMD_SYNC_NEXT_MESSAGE).
        The device decrypts these using its stored channel PSKs before forwarding.

        CHANNEL_MSG_RECV_V3 (0x11, polled — NO sender pubkey):
          byte 0:     SNR (signed byte / 4)
          bytes 1-2:  reserved
          byte 3:     channel_idx
          byte 4:     path_len (6 LSB = hop count; display-only, no bytes follow it —
                      hop hashes are OTA-routing metadata already stripped by the
                      device before queuing the decrypted message for the app)
          byte 5:     txt_type
          bytes 6-9:  timestamp uint32le (device clock)
          bytes 10+:  message text ("sender_name: body" format identifies sender)

        CHANNEL_MSG_RECV (0x08, legacy polled — NO sender pubkey):
          byte 0:     channel_idx
          byte 1:     path_len (display-only, same as above)
          byte 2:     txt_type
          bytes 3-6:  timestamp uint32le
          bytes 7+:   message text

        Note: PUSH_CHANNEL_MSG (0x88) is LOG_DATA (raw radio RX log) and is handled
        separately in _dispatch_frame — it is NOT routed here.
        """
        now = datetime.now(timezone.utc)
        snr: float | None = None

        if pkt_type == PACKET_CHANNEL_MSG_V3:
            # Polled V3 — SNR header, then channel/path/type/ts/text, NO pubkey
            if len(data) < 10:
                log.debug("MeshCore %s: 0x11 too short (%d bytes)", self.name, len(data))
                return
            snr_byte = data[0]
            snr = (snr_byte if snr_byte < 128 else snr_byte - 256) / 4.0
            ch_idx = data[3]
            path_len_raw = data[4]
            # path_len_raw is hop-count metadata for display only. The device has
            # already decrypted and routed this message before queuing it for the
            # companion app — no relay-hash bytes are present in this payload (unlike
            # TRACE_DATA 0x89, which does inline hop hashes). Consuming phantom hash
            # bytes here shifted txt_type/timestamp/text for any relayed (hop>0)
            # message, truncating the start of the text. txt_type always starts at
            # byte 5, per the documented fixed layout above.
            sane_hops = (path_len_raw & 0x3F) if path_len_raw != 255 else None
            if sane_hops is not None and sane_hops > 16:
                sane_hops = None
            off = 5          # txt_type byte — fixed, no relay-hash bytes to skip
            if len(data) < off + 5:
                log.debug("MeshCore %s: 0x11 too short after path (%d)", self.name, len(data))
                return
            ts_raw      = struct.unpack("<I", data[off + 1:off + 5])[0]
            raw_payload = data[off + 5:]

        else:  # PACKET_CHANNEL_MSG_RECV 0x08 — legacy polled
            if len(data) < 8:
                log.debug("MeshCore %s: 0x08 too short (%d bytes)", self.name, len(data))
                return
            ch_idx = data[0]
            path_len_raw = data[1]
            # See note in the V3 branch above: no relay-hash bytes are present in
            # this polled/decrypted payload, so txt_type is always at a fixed offset.
            sane_hops = (path_len_raw & 0x3F) if path_len_raw != 255 else None
            if sane_hops is not None and sane_hops > 16:
                sane_hops = None
            off = 2          # txt_type byte — fixed, no relay-hash bytes to skip
            if len(data) < off + 5:
                log.debug("MeshCore %s: 0x08 too short after path (%d)", self.name, len(data))
                return
            ts_raw      = struct.unpack("<I", data[off + 1:off + 5])[0]
            raw_payload = data[off + 5:]

        # Track this channel index even if we don't know its name yet.
        # High indices (>15) are usually from packets relayed off other mesh networks.
        if ch_idx not in self._channels:
            self._channels[ch_idx] = ""
            if ch_idx <= 15:
                log.info("MeshCore %s: discovered new channel index ch%d (name unknown — run scan)", self.name, ch_idx)
            else:
                log.debug("MeshCore %s: relayed packet on foreign channel ch%d (not our channel)", self.name, ch_idx)

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

        # Polled channel messages have no sender pubkey in the packet; sender identity
        # comes from the "name: body" prefix in the decrypted text.
        display = extracted_name or "unknown"
        from_id = self._resolve_sender_node_id(display)
        if from_id in self._nodes:
            # A decrypted channel message from a resolvable sender is direct
            # proof they're active right now — same "still alive" signal a
            # PUSH_ADVERT gives, just via chat instead of an advert.
            self._nodes[from_id].last_heard = datetime.now(timezone.utc)
        log.debug("MeshCore %s: polled ch%d msg sender=%r", self.name, ch_idx, display)

        if not decrypted_ok and _is_likely_encrypted(body_text):
            log.debug("MeshCore %s: ch%d msg from %r encrypted", self.name, ch_idx, display)
            body_text = "[Encrypted channel message]"

        # Correlate RSSI from the LOG_DATA (0x88) that preceded this polled message.
        # The device pushes 0x88 for every received OTA packet then 0x83 to signal a
        # queued message; the SNR in the polled 0x11 matches the 0x88 SNR for the same
        # OTA packet, so we use SNR proximity (±0.5 dB) to find the right RSSI sample.
        rssi: int | None = None
        rf_path: list | None = None
        if snr is not None:
            for entry in reversed(self._recent_log_data):
                if abs(entry["snr"] - snr) < 0.5:
                    rssi = entry["rssi"]
                    # Same correlation gives us the relay path the raw log
                    # heard for this packet — hop-count sanity check guards
                    # against matching a different packet with similar SNR.
                    ep = entry.get("path")
                    if ep is not None and (sane_hops is None or len(ep) == sane_hops):
                        rf_path = ep
                    break

        raw_data: dict = {"channel_idx": ch_idx}
        if ts_raw:
            raw_data["packet_timestamp"] = ts_raw
        if snr is not None:
            raw_data["snr"] = snr
        if rssi is not None:
            raw_data["rssi"] = rssi
        if rf_path:
            raw_data["rf_path"] = rf_path
        if decrypted_ok and decrypt_key_label:
            raw_data["decrypted_with"] = decrypt_key_label

        # RF stats
        self._rf_stats["channel_msg_count"] += 1
        if snr is not None:
            self._rf_stats["snr_samples"].append(snr)
            if len(self._rf_stats["snr_samples"]) > 1000:
                self._rf_stats["snr_samples"] = self._rf_stats["snr_samples"][-500:]
        if sane_hops is not None:
            self._rf_stats["hop_samples"].append(sane_hops)
            if len(self._rf_stats["hop_samples"]) > 1000:
                self._rf_stats["hop_samples"] = self._rf_stats["hop_samples"][-500:]

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=ch_name,
            from_id=from_id,
            from_display=display,
            body=body_text,
            timestamp=datetime.now(timezone.utc),
            hop_count=sane_hops,
            path=",".join(rf_path) if rf_path else None,
            raw=raw_data,
        )
        await self._enqueue(msg)
        log.debug("MeshCore %s: ch%d msg from %r hops=%s path=%s: %s",
                  self.name, ch_idx, display, sane_hops, rf_path, body_text[:60])

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

        # RF stats
        self._rf_stats["contact_msg_count"] += 1
        if snr is not None:
            self._rf_stats["snr_samples"].append(snr)
            if len(self._rf_stats["snr_samples"]) > 1000:
                self._rf_stats["snr_samples"] = self._rf_stats["snr_samples"][-500:]
        if sane_hops is not None:
            self._rf_stats["hop_samples"].append(sane_hops)
            if len(self._rf_stats["hop_samples"]) > 1000:
                self._rf_stats["hop_samples"] = self._rf_stats["hop_samples"][-500:]

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
        log.info("MeshCore %s: DM from %s(%s): %s", self.name, display, sender_hex, body_text[:60])

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
        ns = list(self._nodes.values())
        for n in ns:
            # Per-node override of MeshNode's 15-min default (see __init__).
            n._ONLINE_TIMEOUT_SEC = int(self._online_window_sec)
        return ns

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
            "last_discovery_ago": round(time.monotonic() - self._last_discovery_ts) if self._last_discovery_ts else None,
        }
        if self._hw_model:
            d["hardware"] = self._hw_model
        if self._fw_version:
            d["firmware"] = self._fw_version
        if self._battery_mv is not None:
            d["battery_mv"] = self._battery_mv
            d["battery_v"] = round(self._battery_mv / 1000.0, 2)
        d["rf_stats"] = self._health_rf_stats()
        return d
