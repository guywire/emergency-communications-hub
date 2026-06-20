"""
ech/core/meshcore_bridge.py
----------------------------
MeshCore → MeshMapper MQTT bridge.

Intercepts packets already flowing through ECH's MeshCore adapter
and republishes them to an MQTT broker in the format expected by
meshcore-mqtt-live-map (github.com/yellowcooln/meshcore-mqtt-live-map).

ECH's single USB/TCP connection to the MeshCore node feeds:
  1. ECH unified inbox (existing)
  2. MeshMapper MQTT broker (this module) — same data, no extra hardware

Topic schema (matches ipnet-mesh/meshcore-mqtt and MeshMapper expectations):
  {prefix}/{device_pubkey}/packets   ← raw packet payload bytes (hex or base64)
  {prefix}/{device_pubkey}/status    ← online/offline JSON
  {prefix}/{device_pubkey}/position  ← lat/lon/alt JSON
  {prefix}/{device_pubkey}/messages  ← decoded text messages JSON

MeshMapper subscribes to {prefix}/# and its Node.js decoder
(@michaelhart/meshcore-decoder) parses the raw packet bytes.

Config (under 'meshcore_bridge' key in config.yaml):
  enabled         bool    enable/disable the bridge (default: false)
  mqtt_host       str     broker host (default: localhost)
  mqtt_port       int     broker port (default: 1883)
  mqtt_username   str     broker username (optional)
  mqtt_password   str     broker password (optional)
  mqtt_tls        bool    enable TLS (default: false)
  topic_prefix    str     MQTT topic prefix (default: meshcore)
  adapter_name    str     which ECH adapter to bridge (default: first meshcore* adapter)
  publish_decoded bool    also publish decoded JSON alongside raw packets (default: true)
  qos             int     MQTT QoS level 0/1/2 (default: 0)

Example config.yaml:
  meshcore_bridge:
    enabled: true
    mqtt_host: localhost          # local mosquitto, or letsmesh.com, etc.
    mqtt_port: 1883
    mqtt_username: subscriber
    mqtt_password: changeme
    topic_prefix: meshcore
    adapter_name: meshcore-usb    # the ECH MeshCore adapter to mirror
    publish_decoded: true
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ech.core.models import NormalizedMessage, MeshNode

log = logging.getLogger(__name__)


class MeshCoreMQTTBridge:
    """
    Subscribes to ECH's internal message stream (via a hook on the router)
    and republishes MeshCore packets to an MQTT broker.

    Attach to the router after startup:
        bridge = MeshCoreMQTTBridge(config)
        await bridge.start(router)
    """

    def __init__(self, config: dict):
        cfg = config.get("meshcore_bridge", {})
        self.enabled        = bool(cfg.get("enabled", False))
        self._host          = cfg.get("mqtt_host", "localhost")
        self._port          = int(cfg.get("mqtt_port", 1883))
        self._username      = cfg.get("mqtt_username")
        self._password      = cfg.get("mqtt_password")
        self._tls           = bool(cfg.get("mqtt_tls", False))
        self._prefix        = cfg.get("topic_prefix", "meshcore")
        self._adapter_name  = cfg.get("adapter_name")   # None = auto-detect
        self._pub_decoded   = bool(cfg.get("publish_decoded", True))
        self._qos           = int(cfg.get("qos", 0))
        self._router        = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._connected     = False
        self._pub_count     = 0

    async def start(self, router) -> None:
        if not self.enabled:
            log.info("MeshCoreBridge: disabled in config")
            return
        self._router = router
        # Install a hook on the router's inbound handler
        router._meshcore_bridge = self
        self._task = asyncio.create_task(self._publish_loop(), name="meshcore-bridge")
        log.info("MeshCoreBridge: started → %s:%d prefix=%s",
                 self._host, self._port, self._prefix)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, msg: "NormalizedMessage") -> None:
        """Called by the router for every inbound MeshCore message."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.debug("MeshCoreBridge: publish queue full, dropping packet")

    # ── Publish loop ──────────────────────────────────────────────────────

    async def _publish_loop(self) -> None:
        """
        Consume the queue and publish to MQTT.
        Uses aiomqtt with auto-reconnect.
        """
        try:
            import aiomqtt
        except ImportError:
            log.error("MeshCoreBridge: aiomqtt not installed — pip install aiomqtt")
            return

        backoff = 2.0
        while True:
            try:
                tls = aiomqtt.TLSParameters() if self._tls else None
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    identifier="ech-meshcore-bridge",
                    tls_params=tls,
                ) as client:
                    self._connected = True
                    backoff = 2.0
                    log.info("MeshCoreBridge: MQTT connected to %s:%d", self._host, self._port)

                    # Publish online status for known nodes
                    await self._publish_node_presence(client)

                    while True:
                        msg = await asyncio.wait_for(self._queue.get(), timeout=30.0)
                        await self._publish_message(client, msg)

            except asyncio.TimeoutError:
                # Keepalive — republish node presence
                if self._router and self._connected:
                    try:
                        async with aiomqtt.Client(
                            hostname=self._host, port=self._port,
                            username=self._username, password=self._password,
                            identifier="ech-meshcore-bridge-ka",
                        ) as ka_client:
                            await self._publish_node_presence(ka_client)
                    except Exception:
                        pass
                continue

            except asyncio.CancelledError:
                self._connected = False
                return

            except Exception as exc:
                self._connected = False
                log.warning("MeshCoreBridge: MQTT error (%s), retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _publish_message(self, client, msg: "NormalizedMessage") -> None:
        """Publish one NormalizedMessage to the appropriate MQTT topics."""
        raw = msg.raw or {}
        device_id = self._device_id(msg.from_id)

        # ── Raw packet bytes (if available from MeshCore adapter) ─────────
        raw_bytes = raw.get("raw_bytes") or raw.get("packet_bytes")
        if raw_bytes:
            topic = f"{self._prefix}/{device_id}/packets"
            await client.publish(topic, payload=raw_bytes, qos=self._qos)
            self._pub_count += 1

        # ── Decoded JSON (position, message, telemetry) ───────────────────
        if self._pub_decoded:
            # Position update
            if msg.lat is not None and msg.lon is not None:
                pos_payload = json.dumps({
                    "lat": msg.lat,
                    "lon": msg.lon,
                    "alt": raw.get("altitude"),
                    "ts": msg.timestamp.isoformat(),
                    "device_id": device_id,
                    "display_name": msg.from_display,
                })
                await client.publish(
                    f"{self._prefix}/{device_id}/position",
                    payload=pos_payload.encode(),
                    qos=self._qos,
                )
                self._pub_count += 1

            # Text message
            if msg.body and not msg.body.startswith("POS "):
                msg_payload = json.dumps({
                    "text": msg.body,
                    "from": device_id,
                    "from_display": msg.from_display,
                    "channel": msg.source_channel,
                    "priority": int(msg.priority),
                    "ts": msg.timestamp.isoformat(),
                })
                await client.publish(
                    f"{self._prefix}/{device_id}/messages",
                    payload=msg_payload.encode(),
                    qos=self._qos,
                )
                self._pub_count += 1

            # SNR/RSSI telemetry
            snr  = raw.get("snr")
            rssi = raw.get("rssi")
            if snr is not None or rssi is not None:
                telem = json.dumps({
                    "snr": snr, "rssi": rssi,
                    "device_id": device_id,
                    "ts": msg.timestamp.isoformat(),
                })
                await client.publish(
                    f"{self._prefix}/{device_id}/telemetry",
                    payload=telem.encode(),
                    qos=self._qos,
                )

        # ── Status / presence update ──────────────────────────────────────
        status = json.dumps({
            "online": True,
            "device_id": device_id,
            "display_name": msg.from_display,
            "last_seen": msg.timestamp.isoformat(),
            "adapter": msg.source_adapter,
        })
        await client.publish(
            f"{self._prefix}/{device_id}/status",
            payload=status.encode(),
            qos=self._qos,
            retain=True,   # retained so MeshMapper sees it on connect
        )

        log.debug("MeshCoreBridge: published %d topics for %s", 3, device_id)

    async def _publish_node_presence(self, client) -> None:
        """Publish online presence for all known nodes on startup/keepalive."""
        if not self._router:
            return
        adapter = self._find_meshcore_adapter()
        if not adapter:
            return
        try:
            nodes = await adapter.nodes()
            for node in nodes:
                device_id = self._device_id(node.node_id)
                status = json.dumps({
                    "online": node.last_heard is not None,
                    "device_id": device_id,
                    "display_name": node.display_name,
                    "firmware": node.firmware_version,
                    "last_seen": node.last_heard.isoformat() if node.last_heard else None,
                    "snr": node.snr,
                    "rssi": node.rssi,
                    "battery": node.battery_level,
                })
                await client.publish(
                    f"{self._prefix}/{device_id}/status",
                    payload=status.encode(),
                    qos=self._qos,
                    retain=True,
                )
                if node.lat and node.lon:
                    pos = json.dumps({
                        "lat": node.lat, "lon": node.lon,
                        "device_id": device_id,
                        "display_name": node.display_name,
                    })
                    await client.publish(
                        f"{self._prefix}/{device_id}/position",
                        payload=pos.encode(),
                        qos=self._qos,
                    )
            log.info("MeshCoreBridge: published presence for %d nodes", len(nodes))
        except Exception as exc:
            log.debug("MeshCoreBridge: presence publish error: %s", exc)

    def _find_meshcore_adapter(self):
        """Find the configured (or first available) MeshCore adapter."""
        if not self._router:
            return None
        if self._adapter_name:
            return self._router._adapters.get(self._adapter_name)
        # Auto-detect: first adapter with 'meshcore' in the name
        for name, adapter in self._router._adapters.items():
            if "meshcore" in name.lower():
                return adapter
        return None

    @staticmethod
    def _device_id(node_id: str) -> str:
        """
        Normalize a MeshCore node ID to a clean hex string for use in MQTT topics.
        MeshCore uses 6-byte pubkey prefixes as hex, e.g. 'A1B2C3D4E5F6'.
        Strips leading '!' (Meshtastic convention) and non-hex chars.
        """
        cleaned = node_id.lstrip("!").replace(":", "").replace("-", "").upper()
        # Truncate to 12 chars (6 bytes) if longer
        if len(cleaned) > 12:
            cleaned = cleaned[:12]
        return cleaned or node_id[:12].upper()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self._connected,
            "broker": f"{self._host}:{self._port}",
            "topic_prefix": self._prefix,
            "adapter": self._adapter_name or "auto",
            "published": self._pub_count,
            "queue_depth": self._queue.qsize(),
        }
