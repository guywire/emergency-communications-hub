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
CMD_DEVICE_QUERY        = 0x16
CMD_SET_DEVICE_TIME     = 0x06
CMD_GET_CHANNEL         = 0x1F
CMD_SEND_CHANNEL_MSG    = 0x03
CMD_SYNC_NEXT_MESSAGE   = 0x0A
CMD_GET_BATTERY         = 0x14

PACKET_OK               = 0x00
PACKET_ERROR            = 0x01
PACKET_SELF_INFO        = 0x05
PACKET_MSG_SENT         = 0x06
PACKET_CONTACT_MSG_RECV = 0x07
PACKET_CHANNEL_MSG_RECV = 0x08
PACKET_NO_MORE_MSGS     = 0x0A
PACKET_CHANNEL_INFO     = 0x12
PACKET_DEVICE_INFO      = 0x0D
PACKET_CONTACT_MSG_V3   = 0x10
PACKET_CHANNEL_MSG_V3   = 0x11

PUSH_ADVERT             = 0x80
PUSH_PATH_UPDATED       = 0x81
PUSH_SEND_CONFIRMED     = 0x82
PUSH_MSG_WAITING        = 0x83

FRAME_OUT_HEADER = b'<'
FRAME_IN_HEADER  = b'>'


class MeshCoreTransport:
    """Thin async byte-stream wrapper; concrete subclasses for serial vs TCP."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def write(self, data: bytes) -> None: ...
    async def read(self, n: int) -> bytes: ...
    async def readexactly(self, n: int) -> bytes: ...


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
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baud
        )
        log.info("MeshCore serial: opened %s @ %d", self._port, self._baud)

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def readexactly(self, n: int) -> bytes:
        return await self._reader.readexactly(n)


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
        self._channel_idx = config.get("channel_idx", 0)
        self._poll_interval = config.get("poll_interval", 2.0)
        self._app_name = config.get("app_name", "ECH")
        self._transport_type = config.get("transport", "serial")

        self._transport: MeshCoreTransport = self._make_transport(config)
        self._nodes: dict[str, MeshNode] = {}
        self._channels: dict[int, str] = {}       # index → name
        self._device_name: str = ""
        self._battery_mv: int = 0
        self._run_task: asyncio.Task | None = None
        self._response_waiters: dict[int, asyncio.Future] = {}

    def _make_transport(self, config: dict) -> MeshCoreTransport:
        t = config.get("transport", "serial")
        if t == "tcp":
            return TCPTransport(
                host=config["host"],
                port=config.get("tcp_port", 4403),
            )
        elif t == "serial":
            return SerialTransport(
                port=config.get("port", "/dev/ttyUSB0"),
                baud=config.get("baud", 115200),
            )
        else:
            raise ValueError(f"MeshCore: unsupported transport '{t}'. Use serial or tcp.")

    # ── Frame I/O ─────────────────────────────────────────────────────────

    def _build_frame(self, payload: bytes) -> bytes:
        """Outgoing frame: < + uint16le(len) + payload"""
        return FRAME_OUT_HEADER + struct.pack("<H", len(payload)) + payload

    async def _read_frame(self) -> bytes | None:
        """Read one incoming frame: > + uint16le(len) + payload"""
        try:
            header = await asyncio.wait_for(
                self._transport.readexactly(1), timeout=5.0
            )
            if header != FRAME_IN_HEADER:
                # Resync: drain until we see >
                log.debug("MeshCore: unexpected byte 0x%02x, resyncing", header[0])
                return None
            length_bytes = await self._transport.readexactly(2)
            length = struct.unpack("<H", length_bytes)[0]
            if length == 0 or length > 512:
                log.warning("MeshCore: suspicious frame length %d, skipping", length)
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

    async def _init_sequence(self) -> None:
        """Run the mandatory startup handshake per companion protocol spec."""
        # 1. CMD_APP_START
        app_name_bytes = self._app_name.encode()
        await self._send_cmd(bytes([CMD_APP_START, 0, 0, 0, 0, 0, 0, 0]) + app_name_bytes)
        await asyncio.sleep(0.2)

        # 2. CMD_DEVICE_QUERY
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

        # 5. Drain any queued messages
        for _ in range(20):
            await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
            await asyncio.sleep(0.05)

        log.info("MeshCore %s: init sequence complete", self.name)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("MeshCore %s: connecting via %s", self.name, self._transport_type)
        await self._transport.connect()
        self._connected = True
        await self._init_sequence()
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
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
        """Send a channel message via CMD_SEND_CHANNEL_MSG."""
        ts = int(message.timestamp.timestamp())
        payload = (
            bytes([CMD_SEND_CHANNEL_MSG, 0x00, self._channel_idx])
            + struct.pack("<I", ts)
            + message.body.encode("utf-8")[:200]
        )
        try:
            await self._send_cmd(payload)
            self._mark_tx(message)
            log.debug("MeshCore %s: sent to ch%d: %s", self.name, self._channel_idx, message.body[:60])
            return True
        except Exception as exc:
            log.error("MeshCore %s: send failed: %s", self.name, exc)
            return False

    # ── Internal receive loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        """
        Main loop: interleaves frame reading with periodic polling
        for queued messages (CMD_SYNC_NEXT_MESSAGE).
        """
        log.debug("MeshCore %s: RX loop started", self.name)
        last_poll = 0.0
        try:
            while self._connected:
                # Periodic poll for queued messages
                now = time.monotonic()
                if now - last_poll >= self._poll_interval:
                    await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
                    last_poll = now

                frame = await self._read_frame()
                if frame is None:
                    continue

                await self._dispatch_frame(frame)

        except ConnectionError as exc:
            log.error("MeshCore %s: connection lost: %s", self.name, exc)
            self._connected = False
        except asyncio.CancelledError:
            log.debug("MeshCore %s: RX loop cancelled", self.name)

    async def _dispatch_frame(self, frame: bytes) -> None:
        if len(frame) < 1:
            return
        pkt_type = frame[0]
        data = frame[1:]

        if pkt_type == PACKET_SELF_INFO:
            # Device's own info — extract name
            self._device_name = data.decode("utf-8", errors="replace").strip("\x00")
            log.info("MeshCore %s: self_info name=%r", self.name, self._device_name)

        elif pkt_type == PACKET_DEVICE_INFO:
            # firmware version, battery, etc.
            if len(data) >= 2:
                self._battery_mv = struct.unpack("<H", data[:2])[0]
            log.debug("MeshCore %s: device_info batt=%dmV", self.name, self._battery_mv)

        elif pkt_type == PACKET_CHANNEL_INFO:
            # Byte 0: index, bytes 1-32: name (null-padded)
            if len(data) >= 33:
                idx = data[0]
                name = data[1:33].split(b"\x00")[0].decode("utf-8", errors="replace")
                self._channels[idx] = name
                log.debug("MeshCore %s: channel %d = %r", self.name, idx, name)

        elif pkt_type in (PACKET_CHANNEL_MSG_RECV, PACKET_CHANNEL_MSG_V3):
            await self._handle_channel_msg(data, v3=(pkt_type == PACKET_CHANNEL_MSG_V3))

        elif pkt_type in (PACKET_CONTACT_MSG_RECV, PACKET_CONTACT_MSG_V3):
            await self._handle_contact_msg(data, v3=(pkt_type == PACKET_CONTACT_MSG_V3))

        elif pkt_type == PACKET_NO_MORE_MSGS:
            pass  # queue empty, normal

        elif pkt_type == PUSH_MSG_WAITING:
            # Unsolicited: new message queued, fetch immediately
            await self._send_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))

        elif pkt_type == PUSH_ADVERT:
            # Node advertisement — extract 6-byte pubkey prefix as node ID
            if len(data) >= 6:
                node_id = data[:6].hex().upper()
                if node_id not in self._nodes:
                    self._nodes[node_id] = MeshNode(
                        node_id=node_id,
                        display_name=node_id,
                        last_heard=datetime.now(timezone.utc),
                        firmware_version="",
                    )
                else:
                    self._nodes[node_id].last_heard = datetime.now(timezone.utc)
                log.debug("MeshCore %s: advert from node %s", self.name, node_id)

        elif pkt_type == PUSH_SEND_CONFIRMED:
            log.debug("MeshCore %s: send confirmed", self.name)

        elif pkt_type in (PACKET_OK, PACKET_ERROR, PACKET_MSG_SENT):
            pass  # ACK frames, no action needed

        else:
            log.debug("MeshCore %s: unhandled pkt_type=0x%02x len=%d", self.name, pkt_type, len(frame))

    async def _handle_channel_msg(self, data: bytes, v3: bool) -> None:
        """
        Channel message format (v3):
          Byte 0:      channel index
          Bytes 1-6:   sender pubkey prefix (6 bytes)
          Byte 7:      path length
          Byte 8:      text type (0=plain, 1=CLI, 2=signed)
          Bytes 9-12:  timestamp (uint32le)
          Bytes 13+:   message text (UTF-8)
        Legacy (non-v3) same minus path byte.
        """
        if len(data) < (13 if v3 else 12):
            log.debug("MeshCore %s: channel_msg too short (%d bytes)", self.name, len(data))
            return

        ch_idx = data[0]
        sender_hex = data[1:7].hex().upper()
        offset = 8 if v3 else 7
        # text_type = data[offset]  # 0=plain
        ts_raw = struct.unpack("<I", data[offset + 1: offset + 5])[0]
        text = data[offset + 5:].decode("utf-8", errors="replace")

        ch_name = self._channels.get(ch_idx, f"ch{ch_idx}")
        node = self._nodes.get(sender_hex)
        display = node.display_name if node else sender_hex

        ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc) if ts_raw else datetime.now(timezone.utc)

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=ch_name,
            from_id=sender_hex,
            from_display=display,
            body=text,
            timestamp=ts,
            raw={"channel_idx": ch_idx, "v3": v3},
        )
        await self._enqueue(msg)
        log.debug("MeshCore %s: channel msg from %s: %s", self.name, display, text[:60])

    async def _handle_contact_msg(self, data: bytes, v3: bool) -> None:
        """
        Contact (DM) message format:
          Bytes 0-5:   sender pubkey prefix
          Byte 6:      path length (v3 only)
          Byte 7/6:    text type
          Bytes 8/7-11/10: timestamp
          Bytes 13+/11+: message text
        """
        if len(data) < (13 if v3 else 11):
            return

        sender_hex = data[:6].hex().upper()
        offset = 6 if v3 else 5
        ts_offset = offset + 1
        ts_raw = struct.unpack("<I", data[ts_offset: ts_offset + 4])[0]
        text = data[ts_offset + 4:].decode("utf-8", errors="replace")

        node = self._nodes.get(sender_hex)
        display = node.display_name if node else sender_hex
        ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc) if ts_raw else datetime.now(timezone.utc)

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel="DM",
            from_id=sender_hex,
            from_display=display,
            body=f"[DM] {text}",
            timestamp=ts,
            raw={"type": "contact_msg", "v3": v3},
        )
        await self._enqueue(msg)
        log.debug("MeshCore %s: DM from %s: %s", self.name, display, text[:60])

    # ── Node list ─────────────────────────────────────────────────────────

    async def nodes(self) -> list[MeshNode]:
        return list(self._nodes.values())

    def _health_detail(self) -> dict:
        return {
            "transport": self._transport_type,
            "device_name": self._device_name,
            "channel_idx": self._channel_idx,
            "channel_name": self._channels.get(self._channel_idx, "unknown"),
            "node_count": len(self._nodes),
            "battery_mv": self._battery_mv,
        }
