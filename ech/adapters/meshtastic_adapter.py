"""
ech/adapters/meshtastic_adapter.py
-----------------------------------
Real Meshtastic adapter using the official meshtastic Python library.

The meshtastic library uses a blocking pub/sub callback model (PyPubSub).
We bridge it to asyncio using call_soon_threadsafe — same pattern as the
APRS-IS adapter. The meshtastic interface object lives in a background
thread; all decoded packets are posted back onto the asyncio event loop.

Transport options (config 'transport' key):
  serial  — USB serial, auto-detect or explicit port  (default)
  tcp     — TCP/IP to node's built-in WiFi server
  ble     — Bluetooth LE (requires BlueZ; experimental on Linux)

Config keys:
  name          str     adapter name shown in UI (default: meshtastic)
  transport     str     serial | tcp | ble  (default: serial)
  port          str     /dev/ttyUSB0 or /dev/ttyACM0  (serial; None = auto-detect)
  host          str     IP address or hostname  (tcp)
  ble_address   str     BLE MAC address  (ble; None = first found)
  channel_idx   int     channel index to send on (default: 0, primary)
  node_id       str     destination node hex ID for DMs (None = broadcast)

Note on T-LoRa v2.1 / v1.0 (TTGO LoRa32):
  These older boards often enumerate as CP210x (VID 10c4 PID ea60) or CH340
  (VID 1a86 PID 7523). If auto-detect fails, set port explicitly in config.
  The meshtastic library supports them — just set transport: serial and port:
  /dev/ttyUSB0 (or whatever udev assigns after the 99-ech-serial.rules fire).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ech.adapters.base import Adapter
from ech.core.models import ChannelHealth, MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

# Meshtastic portnum constants (from meshtastic.portnums_pb2)
PORTNUM_TEXT_MESSAGE = 1
PORTNUM_POSITION     = 3
PORTNUM_NODEINFO     = 4
PORTNUM_TELEMETRY    = 67


class MeshtasticAdapter(Adapter):
    """
    Meshtastic adapter — supports serial, TCP, and BLE transports.
    Uses pubsub callbacks bridged to asyncio via call_soon_threadsafe.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name        = config.get("name", "meshtastic")
        self._transport  = config.get("transport", "serial")
        self._port       = config.get("port", None)       # None = auto-detect
        self._host       = config.get("host", "localhost")
        self._ble_addr   = config.get("ble_address", None)
        self._channel_idx = int(config.get("channel_idx", 0))
        self._iface: Any = None                            # meshtastic interface object
        self._loop: asyncio.AbstractEventLoop | None = None
        self._nodes: dict[str, MeshNode] = {}
        self._my_node_id: str = ""
        self._connect_event = threading.Event()
        self._connect_error: Exception | None = None
        self._packet_count = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        log.info("Meshtastic %s: connecting via %s", self.name, self._transport)

        # Run the blocking meshtastic connect in a thread executor
        await self._loop.run_in_executor(None, self._sync_connect)

        if self._connect_error:
            raise self._connect_error

        self._connected = True
        log.info("Meshtastic %s: connected, node_id=%s, %d nodes in mesh",
                 self.name, self._my_node_id, len(self._nodes))

    def _sync_connect(self) -> None:
        """
        Runs in executor thread. Sets up the meshtastic interface and
        registers pubsub callbacks. Blocks until onConnection fires or
        a timeout expires.
        """
        try:
            # Import here so meshtastic is optional at module level
            import meshtastic.serial_interface
            import meshtastic.tcp_interface
            import meshtastic.ble_interface
            from pubsub import pub

            # Register callbacks before creating interface
            pub.subscribe(self._on_receive,    "meshtastic.receive")
            pub.subscribe(self._on_connection, "meshtastic.connection.established")
            pub.subscribe(self._on_lost,       "meshtastic.connection.lost")

            if self._transport == "serial":
                self._iface = meshtastic.serial_interface.SerialInterface(
                    devPath=self._port  # None = auto-detect
                )
            elif self._transport == "tcp":
                self._iface = meshtastic.tcp_interface.TCPInterface(
                    hostname=self._host
                )
            elif self._transport == "ble":
                self._iface = meshtastic.ble_interface.BLEInterface(
                    address=self._ble_addr
                )
            else:
                raise ValueError(f"Unknown Meshtastic transport: {self._transport!r}")

            # Wait for onConnection callback (up to 15s)
            if not self._connect_event.wait(timeout=15.0):
                raise TimeoutError("Meshtastic: timed out waiting for connection established")

        except Exception as exc:
            self._connect_error = exc
            log.error("Meshtastic %s: connect error: %s", self.name, exc)

    async def disconnect(self) -> None:
        self._connected = False
        if self._iface:
            try:
                await self._loop.run_in_executor(None, self._iface.close)
            except Exception as exc:
                log.debug("Meshtastic %s: close error (ignored): %s", self.name, exc)
        log.info("Meshtastic %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        if not self._iface or not self._connected:
            return False
        try:
            dest = message.to_id or "^all"   # Meshtastic broadcast address
            await self._loop.run_in_executor(
                None,
                lambda: self._iface.sendText(
                    message.body,
                    destinationId=dest,
                    channelIndex=self._channel_idx,
                    wantAck=False,
                )
            )
            self._mark_tx(message)
            log.debug("Meshtastic %s: TX ch%d → %s: %s",
                      self.name, self._channel_idx, dest, message.body[:60])
            return True
        except Exception as exc:
            log.error("Meshtastic %s: send error: %s", self.name, exc)
            return False

    # ── Pubsub callbacks (called from meshtastic's internal thread) ───────

    def _on_connection(self, interface, topic=None) -> None:
        """Called when meshtastic connects/reconnects."""
        log.info("Meshtastic %s: onConnection fired", self.name)
        try:
            # Extract our own node ID
            my_info = interface.getMyNodeInfo()
            if my_info:
                self._my_node_id = my_info.get("user", {}).get("id", "")

            # Build initial node list from node DB
            node_db = interface.nodes or {}
            for node_id, node_data in node_db.items():
                self._update_node_from_dict(node_id, node_data)

        except Exception as exc:
            log.warning("Meshtastic %s: onConnection metadata error: %s", self.name, exc)

        self._connect_event.set()

    def _on_lost(self, interface, topic=None) -> None:
        log.warning("Meshtastic %s: connection lost", self.name)
        self._connected = False

    def _on_receive(self, packet: dict, interface=None) -> None:
        """
        Called by meshtastic on every incoming packet.
        Runs in meshtastic's thread — post to asyncio loop safely.
        """
        if self._loop is None or not self._connected:
            return
        self._loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self._process_packet(packet),
        )

    # ── Packet processing (asyncio context) ──────────────────────────────

    async def _process_packet(self, packet: dict) -> None:
        self._packet_count += 1
        try:
            decoded   = packet.get("decoded", {})
            portnum   = decoded.get("portnum", "")
            from_id   = packet.get("fromId", packet.get("from", "unknown"))
            channel   = packet.get("channel", self._channel_idx)
            rx_snr    = packet.get("rxSnr")
            rx_rssi   = packet.get("rxRssi")

            # Update node if we've seen this sender before
            if from_id in self._nodes:
                n = self._nodes[from_id]
                n.last_heard = datetime.now(timezone.utc)
                if rx_snr is not None:
                    n.snr = float(rx_snr)
                if rx_rssi is not None:
                    n.rssi = int(rx_rssi)

            if portnum in ("TEXT_MESSAGE_APP", str(PORTNUM_TEXT_MESSAGE)):
                await self._handle_text(packet, decoded, from_id, channel, rx_snr, rx_rssi)

            elif portnum in ("POSITION_APP", str(PORTNUM_POSITION)):
                await self._handle_position(packet, decoded, from_id, channel)

            elif portnum in ("NODEINFO_APP", str(PORTNUM_NODEINFO)):
                self._handle_nodeinfo(decoded, from_id, rx_snr, rx_rssi)

            elif portnum in ("TELEMETRY_APP", str(PORTNUM_TELEMETRY)):
                self._handle_telemetry(decoded, from_id)

        except Exception as exc:
            log.debug("Meshtastic %s: packet dispatch error: %s", self.name, exc)

    async def _handle_text(self, packet, decoded, from_id, channel, snr, rssi) -> None:
        text = decoded.get("text", "").strip()
        if not text:
            return

        # Resolve display name
        node = self._nodes.get(from_id)
        display = node.display_name if node else from_id

        # Detect DM vs channel broadcast
        to_id = packet.get("toId", "")
        is_dm = to_id and to_id != "^all" and to_id != "0xffffffff"

        ch_name = f"ch{channel}"
        if is_dm:
            ch_name = "DM"
            text = f"[DM] {text}"

        # Priority heuristic: check for emergency keywords
        ltext = text.lower()
        priority = Priority.NORMAL
        if any(w in ltext for w in ("emergency", "mayday", "help", "911", "sos")):
            priority = Priority.ELEVATED
        if any(w in ltext for w in ("emergency!", "mayday mayday", "life safety")):
            priority = Priority.EMERGENCY

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=ch_name,
            from_id=from_id,
            from_display=display,
            to_id=to_id if is_dm else None,
            body=text,
            priority=priority,
            raw={
                "snr": snr,
                "rssi": rssi,
                "channel": channel,
                "packet_id": packet.get("id"),
            },
        )
        await self._enqueue(msg)
        log.debug("Meshtastic %s: text from %s (ch%s): %s", self.name, display, channel, text[:60])

    async def _handle_position(self, packet, decoded, from_id, channel) -> None:
        pos = decoded.get("position", {})
        lat = pos.get("latitudeI", 0) / 1e7
        lon = pos.get("longitudeI", 0) / 1e7
        alt = pos.get("altitude")
        if lat == 0.0 and lon == 0.0:
            return

        node = self._nodes.get(from_id)
        display = node.display_name if node else from_id

        # Update stored position
        if node:
            node.lat = lat
            node.lon = lon

        body = f"POS {display}: {lat:.5f},{lon:.5f}"
        if alt:
            body += f" alt {alt}m"

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"ch{channel}",
            from_id=from_id,
            from_display=display,
            body=body,
            lat=lat,
            lon=lon,
            raw={"type": "position"},
        )
        await self._enqueue(msg)

    def _handle_nodeinfo(self, decoded, from_id, snr, rssi) -> None:
        user = decoded.get("user", {})
        long_name  = user.get("longName", from_id)
        short_name = user.get("shortName", "")
        hw_model   = user.get("hwModel", "")

        node = self._nodes.get(from_id)
        if node:
            node.display_name = long_name
            node.short_name   = short_name
            node.last_heard   = datetime.now(timezone.utc)
            if snr:  node.snr  = float(snr)
            if rssi: node.rssi = int(rssi)
        else:
            self._nodes[from_id] = MeshNode(
                node_id=from_id,
                display_name=long_name,
                short_name=short_name,
                last_heard=datetime.now(timezone.utc),
                snr=float(snr) if snr else None,
                rssi=int(rssi) if rssi else None,
                firmware_version=hw_model,
            )
        log.debug("Meshtastic %s: nodeinfo %s = %r", self.name, from_id, long_name)

    def _handle_telemetry(self, decoded, from_id) -> None:
        telem = decoded.get("telemetry", {})
        device_metrics = telem.get("deviceMetrics", {})
        battery = device_metrics.get("batteryLevel")
        if battery is not None and from_id in self._nodes:
            self._nodes[from_id].battery_level = int(battery)

    def _update_node_from_dict(self, node_id: str, data: dict) -> None:
        user    = data.get("user", {})
        pos     = data.get("position", {})
        metrics = data.get("deviceMetrics", {})
        lat = pos.get("latitudeI", 0) / 1e7
        lon = pos.get("longitudeI", 0) / 1e7
        last_heard_ts = data.get("lastHeard")
        last_heard = (
            datetime.fromtimestamp(last_heard_ts, tz=timezone.utc)
            if last_heard_ts else datetime.now(timezone.utc)
        )
        self._nodes[node_id] = MeshNode(
            node_id=node_id,
            display_name=user.get("longName", node_id),
            short_name=user.get("shortName", ""),
            last_heard=last_heard,
            snr=data.get("snr"),
            rssi=data.get("rssi"),
            battery_level=metrics.get("batteryLevel"),
            firmware_version=data.get("metadata", {}).get("firmwareVersion", ""),
            lat=lat if lat != 0.0 else None,
            lon=lon if lon != 0.0 else None,
        )

    # ── Required abstract stub (meshtastic drives RX via pubsub) ─────────

    async def _run(self) -> None:
        """
        Meshtastic drives inbound packets via pubsub callbacks — _run is
        not used as the receive loop here, but must be implemented.
        We just idle until disconnect() sets _connected = False.
        """
        while self._connected:
            await asyncio.sleep(1.0)

    # ── Overrides ─────────────────────────────────────────────────────────

    async def nodes(self) -> list[MeshNode]:
        return list(self._nodes.values())

    def _health_detail(self) -> dict:
        return {
            "transport": self._transport,
            "port": self._port or "auto",
            "node_id": self._my_node_id,
            "channel_idx": self._channel_idx,
            "node_count": len(self._nodes),
            "packets_received": self._packet_count,
        }
