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
  channel_name  str     named channel to send on, e.g. "LongFast", "Public", "Maine Mesh"
                        looked up at connect time; overrides channel_idx if found
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
PORTNUM_ROUTING      = 5
PORTNUM_TELEMETRY    = 67
PORTNUM_TRACEROUTE   = 70


class MeshtasticAdapter(Adapter):
    """
    Meshtastic adapter — supports serial, TCP, and BLE transports.
    Uses pubsub callbacks bridged to asyncio via call_soon_threadsafe.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name         = config.get("name", "meshtastic")
        self._transport   = config.get("transport", "serial")
        self._port        = config.get("port", None)       # None = auto-detect
        self._host        = config.get("host", "localhost")
        self._ble_addr    = config.get("ble_address", None)
        self._channel_idx = int(config.get("channel_idx", 0))
        self._channel_name = config.get("channel_name", None)
        # channel_indices: list of channel indices to receive from; None/[] = all channels
        raw_mon = config.get("channel_indices", None)
        self._monitored_channels: list[int] | None = (
            [int(x) for x in raw_mon] if raw_mon else None
        )
        self._channel_list: list[dict] = []                # populated at connect
        self._iface: Any = None                            # meshtastic interface object
        self._loop: asyncio.AbstractEventLoop | None = None
        self._nodes: dict[str, MeshNode] = {}
        self._my_node_id: str = ""
        self._connect_event = threading.Event()
        self._connect_error: Exception | None = None
        self._packet_count = 0
        self._pending_acks: dict[int, str] = {}   # meshtastic packet_id → ECH msg UUID
        self._gps_position: dict | None = None    # lat/lon/alt from own node GPS

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
            dest    = message.to_id or "^all"
            is_dm   = bool(message.to_id)
            pkt = await self._loop.run_in_executor(
                None,
                lambda: self._iface.sendText(
                    message.body,
                    destinationId=dest,
                    channelIndex=self._channel_idx,
                    wantAck=is_dm,      # request delivery ACK only for direct messages
                )
            )
            # For DMs, correlate returned packet ID with our message UUID
            if is_dm and pkt is not None:
                try:
                    pkt_id = pkt.id if hasattr(pkt, 'id') else (pkt.get('id') if isinstance(pkt, dict) else None)
                    if pkt_id:
                        self._pending_acks[int(pkt_id)] = message.id
                except Exception:
                    pass
            self._mark_tx(message)
            log.debug("Meshtastic %s: TX ch%d → %s (wantAck=%s): %s",
                      self.name, self._channel_idx, dest, is_dm, message.body[:60])
            return True
        except Exception as exc:
            log.error("Meshtastic %s: send error: %s", self.name, exc)
            return False

    # ── Time sync & announce ──────────────────────────────────────────────

    async def time_sync(self) -> bool:
        """Set device RTC to current UTC via the Meshtastic admin API."""
        if not self._iface or not self._connected:
            return False
        import time as _time
        ts = int(_time.time())
        try:
            await self._loop.run_in_executor(
                None,
                lambda: self._iface.localNode.setTime(ts),
            )
            log.info("Meshtastic %s: time_sync sent (epoch %d)", self.name, ts)
            return True
        except Exception as exc:
            log.error("Meshtastic %s: time_sync error: %s", self.name, exc)
            return False

    async def announce(self) -> bool:
        """Broadcast this node's NodeInfo to the mesh so other nodes can see it."""
        if not self._iface or not self._connected:
            return False
        try:
            my_info = self._iface.getMyNodeInfo() or {}
            user     = my_info.get("user", {})
            long_name  = user.get("longName")  or "ECH Gateway"
            short_name = user.get("shortName") or "ECH"
            await self._loop.run_in_executor(
                None,
                lambda: self._iface.localNode.setOwner(long_name, short_name),
            )
            log.info("Meshtastic %s: announce sent (NodeInfo broadcast as %r / %r)",
                     self.name, long_name, short_name)
            return True
        except Exception as exc:
            log.error("Meshtastic %s: announce error: %s", self.name, exc)
            return False

    async def rename_device(self, new_name: str, short_name: str | None = None) -> bool:
        """Set this node's long name (and optional short name) via setOwner."""
        if not self._iface or not self._connected:
            return False
        if not short_name:
            short_name = new_name.upper().replace(" ", "")[:4]
        try:
            ln, sn = new_name.strip(), short_name.strip()
            await self._loop.run_in_executor(
                None,
                lambda: self._iface.localNode.setOwner(ln, sn),
            )
            log.info("Meshtastic %s: renamed to %r / %r", self.name, ln, sn)
            return True
        except Exception as exc:
            log.error("Meshtastic %s: rename error: %s", self.name, exc)
            return False

    async def clear_nodes(self) -> int:
        """Clear server-side node cache (device db is unaffected)."""
        count = len(self._nodes)
        self._nodes.clear()
        log.info("Meshtastic %s: cleared %d cached nodes", self.name, count)
        return count

    async def ping(self, node_id: str) -> dict:
        """Send a traceroute to node_id. Response arrives as a TRACEROUTE message."""
        if not self._iface or not self._connected:
            return {"status": "error", "detail": "not connected"}
        try:
            await self._loop.run_in_executor(
                None,
                lambda: self._iface.sendTraceRoute(node_id, hopLimit=3),
            )
            log.info("Meshtastic %s: traceroute sent to %s", self.name, node_id)
            return {"status": "sent", "node_id": node_id}
        except AttributeError:
            log.warning("Meshtastic %s: sendTraceRoute not available in this library version", self.name)
            return {"status": "error", "detail": "sendTraceRoute not supported"}
        except Exception as exc:
            log.error("Meshtastic %s: ping error: %s", self.name, exc)
            return {"status": "error", "detail": str(exc)}

    # ── Pubsub callbacks (called from meshtastic's internal thread) ───────

    def _on_connection(self, interface, topic=None) -> None:
        """Called when meshtastic connects/reconnects."""
        log.info("Meshtastic %s: onConnection fired", self.name)
        # Clear state that may be stale from a previous session (e.g. after device reboot)
        self._pending_acks.clear()
        self._channel_list = []
        try:
            # Extract our own node ID
            my_info = interface.getMyNodeInfo()
            if my_info:
                self._my_node_id = my_info.get("user", {}).get("id", "")

            # Build initial node list from node DB
            node_db = interface.nodes or {}
            for node_id, node_data in node_db.items():
                self._update_node_from_dict(node_id, node_data)

            # Enumerate channels from device
            try:
                raw_channels = getattr(interface.localNode, "channels", []) or []
                self._channel_list = []
                for ch in raw_channels:
                    idx  = getattr(ch, "index", None)
                    role = getattr(ch, "role", None)
                    name = getattr(getattr(ch, "settings", None), "name", "") or ""
                    if role is not None and str(role) not in ("0", "DISABLED"):
                        self._channel_list.append({"index": idx, "name": name})

                log.info("Meshtastic %s: device channels: %s",
                         self.name,
                         ", ".join(f"{c['index']}:{c['name'] or 'primary'}" for c in self._channel_list))

                # Resolve channel_name → channel_idx
                if self._channel_name:
                    want = self._channel_name.lower()
                    for ch in self._channel_list:
                        ch_name = (ch["name"] or "").lower()
                        # match "longfast" on primary (index 0, empty name) or by exact name
                        if ch_name == want or (ch["index"] == 0 and want in ("longfast", "long fast", "primary", "")):
                            self._channel_idx = ch["index"]
                            log.info("Meshtastic %s: channel_name %r → index %d",
                                     self.name, self._channel_name, self._channel_idx)
                            break
                    else:
                        log.warning("Meshtastic %s: channel_name %r not found on device, using index %d",
                                    self.name, self._channel_name, self._channel_idx)
            except Exception as exc:
                log.debug("Meshtastic %s: channel enumeration error (non-fatal): %s", self.name, exc)

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

            # Filter by monitored channels — NODEINFO/TELEMETRY always pass through
            if (self._monitored_channels is not None
                    and portnum not in ("NODEINFO_APP", str(PORTNUM_NODEINFO),
                                        "TELEMETRY_APP", str(PORTNUM_TELEMETRY))
                    and channel not in self._monitored_channels):
                return

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

            elif portnum in ("ROUTING_APP", str(PORTNUM_ROUTING)):
                await self._handle_routing(packet, decoded, from_id)

            elif portnum in ("TRACEROUTE_APP", str(PORTNUM_TRACEROUTE)):
                await self._handle_traceroute(packet, decoded, from_id)

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

        hop_start = packet.get("hopStart")
        hop_limit = packet.get("hopLimit")
        hop_count = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None
        via_mqtt  = bool(packet.get("viaMqtt", False))

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=ch_name,
            from_id=from_id,
            from_display=display,
            to_id=to_id if is_dm else None,
            body=text,
            priority=priority,
            hop_count=hop_count,
            hop_start=hop_start,
            via_mqtt=via_mqtt,
            raw={
                "snr": snr,
                "rssi": rssi,
                "channel": channel,
                "packet_id": packet.get("id"),
            },
        )
        await self._enqueue(msg)
        log.debug("Meshtastic %s: text from %s (ch%s) hops=%s via_mqtt=%s: %s",
                  self.name, display, channel, hop_count, via_mqtt, text[:60])

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

        # Track own-node GPS for ECH location awareness
        if from_id == self._my_node_id:
            self._gps_position = {"lat": lat, "lon": lon, "alt": alt}
            log.info("Meshtastic %s: own GPS fix: %.6f, %.6f alt=%s",
                     self.name, lat, lon, alt)

        body = f"POS {display}: {lat:.5f},{lon:.5f}"
        if alt:
            body += f" alt {alt}m"

        hop_start = packet.get("hopStart")
        hop_limit = packet.get("hopLimit")
        hop_count = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None

        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"ch{channel}",
            from_id=from_id,
            from_display=display,
            body=body,
            lat=lat,
            lon=lon,
            hop_count=hop_count,
            hop_start=hop_start,
            via_mqtt=bool(packet.get("viaMqtt", False)),
            msg_type="position",
            raw={"type": "position", "altitude": alt},
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

    async def _handle_routing(self, packet, decoded, from_id) -> None:
        """Handle ROUTING_APP ACK/NACK — correlate with pending DM sends."""
        routing  = decoded.get("routing", {})
        err      = routing.get("errorReason", "NONE")
        req_id   = routing.get("requestId") or routing.get("request_id")
        if req_id is None:
            return
        msg_uuid = self._pending_acks.pop(int(req_id), None)
        if msg_uuid and self._router_notify:
            if err in ("NONE", "", None):
                await self._router_notify(self.name, msg_uuid, "delivered", f"ACK from {from_id}")
            else:
                await self._router_notify(self.name, msg_uuid, "failed", f"NACK: {err}")
        log.debug("Meshtastic %s: routing ACK req_id=%s err=%s msg=%s",
                  self.name, req_id, err, msg_uuid)

    async def _handle_traceroute(self, packet, decoded, from_id) -> None:
        """Handle TRACEROUTE_APP response — surface as a path-annotated message."""
        tr = decoded.get("traceroute", {})
        route = tr.get("route", []) or tr.get("routeBack", [])
        # route is a list of node IDs in traversal order
        hops = len(route)
        path_str = " → ".join(str(n) for n in route) if route else "(direct)"
        node = self._nodes.get(from_id)
        display = node.display_name if node else from_id
        body = f"TRACEROUTE {display}: {path_str} ({hops} hop{'s' if hops != 1 else ''})"
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel="traceroute",
            from_id=from_id,
            from_display=display,
            body=body,
            hop_count=hops,
            path=path_str,
        )
        await self._enqueue(msg)
        log.info("Meshtastic %s: traceroute from %s: %s", self.name, from_id, path_str)

    def _handle_telemetry(self, decoded, from_id) -> None:
        telem   = decoded.get("telemetry", {})
        dm      = telem.get("deviceMetrics", {})
        em      = telem.get("environmentMetrics", {})

        node = self._nodes.get(from_id)
        if node is None:
            return

        changed: dict = {}

        # Device metrics
        if dm.get("batteryLevel") is not None:
            node.battery_level = int(dm["batteryLevel"])
            changed["battery_level"] = node.battery_level
        if dm.get("voltage") is not None:
            node.battery_voltage = round(float(dm["voltage"]), 3)
            changed["battery_voltage"] = node.battery_voltage
        if dm.get("channelUtilization") is not None:
            node.ch_util = round(float(dm["channelUtilization"]), 1)
            changed["ch_util"] = node.ch_util
        if dm.get("airUtilTx") is not None:
            node.air_util_tx = round(float(dm["airUtilTx"]), 1)
            changed["air_util_tx"] = node.air_util_tx
        if dm.get("uptimeSeconds") is not None:
            node.uptime_secs = int(dm["uptimeSeconds"])
            changed["uptime_secs"] = node.uptime_secs

        # Environment metrics
        if em.get("temperature") is not None:
            node.temperature = round(float(em["temperature"]), 1)
            changed["temperature"] = node.temperature
        if em.get("relativeHumidity") is not None:
            node.humidity = round(float(em["relativeHumidity"]), 1)
            changed["humidity"] = node.humidity
        if em.get("barometricPressure") is not None:
            node.pressure = round(float(em["barometricPressure"]), 1)
            changed["pressure"] = node.pressure

        if changed:
            log.debug("Meshtastic %s: telemetry %s: %s", self.name, from_id, changed)
            # Push WS telemetry event so UI updates node card live
            if self._loop and self._router_notify_nodes:
                self._loop.call_soon_threadsafe(
                    self._loop.create_task,
                    self._push_telemetry_event(from_id, changed)
                )

    async def _push_telemetry_event(self, node_id: str, fields: dict) -> None:
        notify = getattr(self, '_router_broadcast', None)
        if notify:
            try:
                await notify("node_telemetry", {"node_id": node_id, "adapter": self.name, **fields})
            except Exception:
                pass

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
        batt_v = metrics.get("voltage")
        ch_util = metrics.get("channelUtilization")
        air_tx  = metrics.get("airUtilTx")
        uptime  = metrics.get("uptimeSeconds")
        self._nodes[node_id] = MeshNode(
            node_id=node_id,
            display_name=user.get("longName", node_id),
            short_name=user.get("shortName", ""),
            last_heard=last_heard,
            snr=data.get("snr"),
            rssi=data.get("rssi"),
            battery_level=metrics.get("batteryLevel"),
            battery_voltage=round(float(batt_v), 3) if batt_v is not None else None,
            ch_util=round(float(ch_util), 1) if ch_util is not None else None,
            air_util_tx=round(float(air_tx), 1) if air_tx is not None else None,
            uptime_secs=int(uptime) if uptime is not None else None,
            firmware_version=data.get("metadata", {}).get("firmwareVersion", ""),
            hw_model=user.get("hwModel", ""),
            lat=lat if lat != 0.0 else None,
            lon=lon if lon != 0.0 else None,
        )

    # ── Live channel control ──────────────────────────────────────────────

    def set_tx_channel(self, idx: int) -> None:
        """Change the TX channel index live (no restart needed)."""
        self._channel_idx = idx
        log.info("Meshtastic %s: TX channel set to %d", self.name, idx)

    def set_monitored_channels(self, indices: list[int] | None) -> None:
        """Set which channel indices to receive from. None = all."""
        self._monitored_channels = [int(i) for i in indices] if indices else None
        log.info("Meshtastic %s: monitored channels → %s",
                 self.name, self._monitored_channels or "all")

    async def set_lora_config(self,
                              region: str | int | None = None,
                              modem_preset: str | None = None,
                              channel_num: int | None = None) -> dict:
        """Set LoRa region, modem preset, and/or frequency slot on the device."""
        if not self._iface or not self._connected:
            return {"status": "error", "detail": "not connected"}

        REGION_MAP = {
            "us": 1, "eu_433": 2, "eu_868": 3, "cn": 4, "jp": 5,
            "anz": 6, "kr": 7, "tw": 8, "ru": 9, "in": 10,
            "nz_865": 11, "th": 12, "lora_24": 13, "ua_433": 14,
            "ua_868": 15, "my_433": 16, "my_919": 17, "sg_923": 18,
        }
        PRESET_MAP = {
            "longfast": "LONG_FAST", "longslow": "LONG_SLOW",
            "mediumfast": "MEDIUM_FAST", "mediumslow": "MEDIUM_SLOW",
            "shortfast": "SHORT_FAST", "shortslow": "SHORT_SLOW",
        }

        def _do():
            try:
                from meshtastic.protobuf import config_pb2
            except ImportError:
                try:
                    from meshtastic import config_pb2
                except ImportError:
                    return {"status": "error", "detail": "meshtastic protobuf unavailable"}
            changed = []
            try:
                lora = self._iface.localNode.localConfig.lora
                if region is not None:
                    region_int = (
                        REGION_MAP.get(str(region).lower().replace("-", "_"))
                        if isinstance(region, str) else int(region)
                    )
                    if region_int is None:
                        return {"status": "error", "detail": f"Unknown region: {region!r}"}
                    # Prefer enum value for type safety; fall back to raw int
                    try:
                        region_val = config_pb2.Config.LoRaConfig.RegionCode.Value(
                            str(region).upper().replace("-", "_")
                        )
                    except Exception:
                        region_val = region_int
                    lora.region = region_val
                    changed.append(f"region={region_int}(US)"
                                   if region_int == 1 else f"region={region_int}")
                if modem_preset:
                    key = modem_preset.lower().replace("_", "").replace(" ", "")
                    preset_str = PRESET_MAP.get(key)
                    if preset_str is None:
                        return {"status": "error", "detail": f"Unknown modem preset: {modem_preset!r}"}
                    preset_val = config_pb2.Config.LoRaConfig.ModemPreset.Value(preset_str)
                    lora.modem_preset = preset_val
                    changed.append(f"preset={preset_str}")
                if channel_num is not None:
                    lora.channel_num = int(channel_num)
                    changed.append(f"channel_num={channel_num}")
                if not changed:
                    return {"status": "error", "detail": "No region, preset, or channel_num specified"}
                self._iface.localNode.writeConfig("lora")
                log.info("Meshtastic %s: LoRa config updated: %s", self.name, ", ".join(changed))
                return {"status": "ok", "changed": changed}
            except Exception as exc:
                log.error("Meshtastic %s: set_lora_config error: %s", self.name, exc)
                return {"status": "error", "detail": str(exc)}

        return await self._loop.run_in_executor(None, _do)

    async def configure_gps(self, gps_mode: str = "enabled",
                             broadcast_secs: int | None = None) -> dict:
        """
        Configure GPS on the device.
        gps_mode: 'enabled' | 'disabled' | 'not_present'
        broadcast_secs: position broadcast interval in seconds (None = unchanged)
        """
        if not self._iface or not self._connected:
            return {"status": "error", "detail": "not connected"}

        GPS_MODE_MAP = {"enabled": 1, "disabled": 2, "not_present": 3}

        def _do():
            try:
                from meshtastic.protobuf import config_pb2
            except ImportError:
                try:
                    from meshtastic import config_pb2
                except ImportError:
                    return {"status": "error", "detail": "meshtastic protobuf unavailable"}
            try:
                mode_key = gps_mode.lower().replace(" ", "_").replace("-", "_")
                mode_val = GPS_MODE_MAP.get(mode_key)
                if mode_val is None:
                    return {"status": "error", "detail": f"Unknown gps_mode: {gps_mode!r}"}
                pos_cfg = self._iface.localNode.localConfig.position
                # Support both old (gps_enabled bool) and new (gps_mode enum) firmware APIs
                if hasattr(pos_cfg, "gps_mode"):
                    pos_cfg.gps_mode = mode_val
                if hasattr(pos_cfg, "gps_enabled"):
                    pos_cfg.gps_enabled = (mode_val == 1)
                if broadcast_secs is not None:
                    pos_cfg.position_broadcast_secs = int(broadcast_secs)
                self._iface.localNode.writeConfig("position")
                log.info("Meshtastic %s: GPS configured: mode=%s", self.name, gps_mode)
                return {"status": "ok", "gps_mode": gps_mode,
                        "broadcast_secs": broadcast_secs}
            except Exception as exc:
                log.error("Meshtastic %s: configure_gps error: %s", self.name, exc)
                return {"status": "error", "detail": str(exc)}

        return await self._loop.run_in_executor(None, _do)

    async def configure_channel(self, idx: int, name: str,
                                psk_b64: str | None = None,
                                modem_preset: str | None = None) -> dict:
        """
        Push channel settings to the device.
        idx: slot 0–7 (0 = primary)
        name: channel name (empty string = primary default displayed as modem preset name)
        psk_b64: base64-encoded PSK; None or 'default' uses the firmware default key (0x01)
        modem_preset: LongFast | LongSlow | MediumFast | MediumSlow | ShortFast | ShortSlow
                      (only applied when idx == 0, ignored otherwise)
        """
        if not self._iface or not self._connected:
            return {"status": "error", "detail": "not connected"}
        import base64
        if psk_b64 and psk_b64.lower() != "default":
            try:
                psk_bytes = base64.b64decode(psk_b64)
            except Exception:
                return {"status": "error", "detail": "psk_b64 is not valid base64"}
        else:
            psk_bytes = b'\x01'   # 0x01 = use firmware default key

        def _do():
            try:
                from meshtastic.protobuf import channel_pb2, config_pb2
            except ImportError:
                try:
                    from meshtastic import channel_pb2, config_pb2
                except ImportError:
                    return {"status": "error", "detail": "meshtastic protobuf unavailable"}
            try:
                ch = channel_pb2.Channel()
                ch.index = idx
                ch.settings.name = name
                ch.settings.psk = psk_bytes
                ch.role = (channel_pb2.Channel.Role.PRIMARY
                           if idx == 0
                           else channel_pb2.Channel.Role.SECONDARY)
                self._iface.localNode.setChannel(ch)
                # Commit channel to device flash
                if hasattr(self._iface.localNode, "writeChannel"):
                    self._iface.localNode.writeChannel(idx)
                # Set modem preset on primary channel
                if modem_preset and idx == 0:
                    preset_map = {
                        "longfast":   "LONG_FAST",
                        "longslow":   "LONG_SLOW",
                        "mediumfast": "MEDIUM_FAST",
                        "mediumslow": "MEDIUM_SLOW",
                        "shortfast":  "SHORT_FAST",
                        "shortslow":  "SHORT_SLOW",
                    }
                    key = modem_preset.lower().replace("_", "").replace(" ", "")
                    preset_str = preset_map.get(key)
                    if preset_str:
                        try:
                            preset_val = config_pb2.Config.LoRaConfig.ModemPreset.Value(preset_str)
                            self._iface.localNode.localConfig.lora.modem_preset = preset_val
                            self._iface.localNode.writeConfig("lora")
                        except Exception as exc:
                            log.warning("Meshtastic %s: modem preset set failed: %s", self.name, exc)
                # Refresh local channel list after change
                try:
                    raw_channels = getattr(self._iface.localNode, "channels", []) or []
                    self._channel_list = []
                    for ch_raw in raw_channels:
                        role = getattr(ch_raw, "role", None)
                        if role is not None and str(role) not in ("0", "DISABLED"):
                            self._channel_list.append({
                                "index": getattr(ch_raw, "index", None),
                                "name": getattr(getattr(ch_raw, "settings", None), "name", "") or "",
                            })
                except Exception:
                    pass
                return {"status": "ok", "idx": idx, "name": name}
            except Exception as exc:
                log.error("Meshtastic %s: configure_channel error: %s", self.name, exc)
                return {"status": "error", "detail": str(exc)}

        return await self._loop.run_in_executor(None, _do)

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
        ch_str = ", ".join(
            f"{c['index']}:{c['name']}" if c.get("name") else str(c["index"])
            for c in self._channel_list
        ) or "none"
        return {
            "transport": self._transport,
            "port": self._port or "auto",
            "node_id": self._my_node_id,
            "channel_idx": self._channel_idx,
            "channel_name": self._channel_name,
            "channels": ch_str,
            "channels_list": [
                {"idx": c["index"], "name": c["name"] or "primary"}
                for c in self._channel_list
            ],
            "monitored_channels": self._monitored_channels,
            "node_count": len(self._nodes),
            "packets_received": self._packet_count,
            "gps_position": self._gps_position,
        }
