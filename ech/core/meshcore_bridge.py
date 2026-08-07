"""
ech/core/meshcore_bridge.py
----------------------------
MeshCore → MeshMapper / LetsMesh MQTT bridge.

Publishes decoded MeshCore packets to MQTT in the letsmesh packet-capture
format so that MeshMapper and LetsMesh visualisation tools can consume ECH
data without running a separate meshcore-packet-capture process.

## Topic schema  (matches meshcore-packet-capture defaults)

  meshcore/{IATA}/{PUBLIC_KEY}/packets   ← decoded packet JSON (one per message)
  meshcore/{IATA}/{PUBLIC_KEY}/status    ← observer/node status JSON (retained)
  meshcore/{IATA}/{PUBLIC_KEY}/decoded   ← same as packets (alias, some tools use this)

Where:
  {IATA}       = 3-letter regional code, e.g. BOS, MAN, LON  (upper-case)
  {PUBLIC_KEY} = observer's 64-char hex public key OR the 12-char
                 device-ID prefix ECH learns from PACKET_SELF_INFO

## Packet JSON schema  (meshcore-packet-capture / letsmesh format)

  {
    "origin":      "Device Name",          // from PACKET_SELF_INFO / config
    "origin_id":   "aabbcc...",            // 64-char pubkey or 12-char prefix
    "timestamp":   "2024-01-01T12:00:00.000000",  // ISO-8601 receive time
    "type":        "PACKET",
    "direction":   "rx",
    "time":        "12:00:00",
    "date":        "01/01/2024",
    "len":         "45",                   // raw length where known, else payload est.
    "packet_type": "5",                    // 0–15: 2=TXT_MSG,4=ADVERT,5=GRP_TXT
    "route":       "F",                    // F=flood (MeshCore default)
    "payload_len": "32",
    "raw":         "",                     // hex bytes — empty; companion protocol
                                           // does not expose raw LoRa frames
    "SNR":         "12.5",
    "RSSI":        "-65",
    "hash":        "A1B2C3D4E5F6"          // first 12 chars of message ID
  }

## Status JSON schema

  {
    "online":       true,
    "origin":       "Device Name",
    "origin_id":    "aabbcc...",
    "last_seen":    "2024-01-01T12:00:00.000000",
    "channel":      "TAC-1",
    "node_count":   4,
    "firmware":     "mc-0.9.4",
    "snr":          12.5,
    "rssi":         -65,
    "battery":      85
  }

## Authentication

MeshMapper and LetsMesh require JWT-based MeshCore Auth Token authentication.
ECH does not generate JWT (it would need the device's private key).
Recommended deployment pattern:
  1. ECH bridges to a local mosquitto on localhost:1883  (no auth needed)
  2. meshcore-packet-capture or a mosquitto bridge forwards to MeshMapper/LetsMesh
     with proper JWT auth

For direct connection to MeshMapper/LetsMesh with username/password,
set mqtt_username and mqtt_password in config.yaml.  WebSocket transport
is required for external brokers.

## Config keys  (under 'meshcore_bridge' in config.yaml)

  enabled         bool    enable bridge (default: false)
  mqtt_host       str     broker hostname (default: localhost)
  mqtt_port       int     broker port (default: 1883)
  mqtt_username   str     username (optional)
  mqtt_password   str     password (optional)
  mqtt_tls        bool    TLS (default: false)
  mqtt_websocket  bool    use WebSocket transport — required for
                          mqtt.meshmapper.net and letsmesh brokers (default: false)
  iata_code       str     3-letter IATA regional code, e.g. BOS (default: ECH)
  device_pubkey   str     64-char hex public key for topic; auto-detected
                          from PACKET_SELF_INFO if not set (default: "")
  topic_prefix    str     topic root (default: meshcore)
  adapter_name    str     which ECH adapter to bridge (default: first meshcore*)
  status_interval int     seconds between periodic status publishes (default: 300)
  qos             int     MQTT QoS 0/1/2 (default: 0)

## Example config.yaml entries

  # Local mosquitto — no auth, plain TCP
  meshcore_bridge:
    enabled: true
    iata_code: BOS
    mqtt_host: localhost
    mqtt_port: 1883

  # Direct to MeshMapper (needs WebSocket + credentials)
  meshcore_bridge:
    enabled: true
    iata_code: BOS
    device_pubkey: "aabbccddeeff..."   # 64-char hex from your MeshCore device
    mqtt_host: mqtt.meshmapper.net
    mqtt_port: 443
    mqtt_websocket: true
    mqtt_tls: true
    mqtt_username: ""                   # use JWT auth token as password if required
    mqtt_password: ""

  # LetsMesh US
  meshcore_bridge:
    enabled: true
    iata_code: BOS
    mqtt_host: mqtt-us-v1.letsmesh.net
    mqtt_port: 443
    mqtt_websocket: true
    mqtt_tls: true
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ech.core.models import NormalizedMessage

log = logging.getLogger(__name__)

# MeshCore packet_type codes (PayloadType enum from meshcore-packet-capture)
_PTYPE_TXT_MSG = 2    # direct text message
_PTYPE_ADVERT  = 4    # node advertisement
_PTYPE_GRP_TXT = 5    # group / channel text (most common from companion protocol)
_PTYPE_CONTROL = 11   # control / discovery


class MeshCoreMQTTBridge:
    """
    Subscribes to ECH's internal MeshCore message stream and republishes
    packets in the meshcore-packet-capture / letsmesh JSON format.

    Attach to the router after startup:
        bridge = MeshCoreMQTTBridge(config)
        await bridge.start(router)
    """

    def __init__(self, config: dict):
        cfg = config.get("meshcore_bridge", {})
        self.enabled         = bool(cfg.get("enabled", False))
        self._host           = cfg.get("mqtt_host", "localhost")
        self._port           = int(cfg.get("mqtt_port", 1883))
        self._username       = cfg.get("mqtt_username") or None
        self._password       = cfg.get("mqtt_password") or None
        self._tls            = bool(cfg.get("mqtt_tls", False))
        self._websocket      = bool(cfg.get("mqtt_websocket", False))
        self._iata           = cfg.get("iata_code", "ECH").upper()
        self._device_pubkey  = cfg.get("device_pubkey", "")   # filled at runtime
        self._prefix         = cfg.get("topic_prefix", "meshcore")
        self._adapter_name   = cfg.get("adapter_name")
        self._status_interval = int(cfg.get("status_interval", 300))
        self._qos            = int(cfg.get("qos", 0))

        # Runtime state — populated from PACKET_SELF_INFO once adapter connects
        self._origin_name    = cfg.get("device_name", "ECH-Observer")
        self._router         = None
        self._task: asyncio.Task | None = None
        self._status_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.connected       = False
        self._pub_count      = 0

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self, router) -> None:
        if not self.enabled:
            log.info("MeshCoreBridge: disabled in config")
            return
        self._router = router
        router._meshcore_bridge = self
        self._task = asyncio.create_task(self._publish_loop(), name="meshcore-bridge")
        log.info("MeshCoreBridge: started → %s:%d iata=%s prefix=%s websocket=%s",
                 self._host, self._port, self._iata, self._prefix, self._websocket)

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
        # Sync device name from adapter if we don't have it yet
        self._sync_device_info()
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.debug("MeshCoreBridge: queue full, dropping")

    # ── Topic helpers ─────────────────────────────────────────────────────

    def _origin_id(self) -> str:
        """Return the 64-char pubkey or 12-char prefix used in MQTT topics."""
        return self._device_pubkey or "000000000000"

    def _topic(self, suffix: str) -> str:
        return f"{self._prefix}/{self._iata}/{self._origin_id()}/{suffix}"

    # ── Publish loop ──────────────────────────────────────────────────────

    async def _publish_loop(self) -> None:
        try:
            import aiomqtt
        except ImportError:
            log.error("MeshCoreBridge: aiomqtt not installed — pip install aiomqtt")
            return

        backoff = 2.0
        while True:
            try:
                tls    = aiomqtt.TLSParameters() if self._tls else None
                transport = "websockets" if self._websocket else "tcp"

                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    identifier="ech-meshcore-bridge",
                    tls_params=tls,
                    transport=transport,
                ) as client:
                    self.connected = True
                    backoff = 2.0
                    log.info("MeshCoreBridge: MQTT connected (%s:%d transport=%s)",
                             self._host, self._port, transport)

                    # Publish initial status
                    await self._publish_status(client)

                    # Periodic status republish task
                    async def _periodic_status():
                        while True:
                            await asyncio.sleep(self._status_interval)
                            try:
                                await self._publish_status(client)
                            except Exception:
                                pass

                    status_task = asyncio.create_task(_periodic_status())
                    try:
                        while True:
                            msg = await asyncio.wait_for(
                                self._queue.get(), timeout=60.0
                            )
                            await self._publish_packet(client, msg)
                    finally:
                        status_task.cancel()
                        try:
                            await status_task
                        except asyncio.CancelledError:
                            pass

            except asyncio.TimeoutError:
                # Queue was idle — publish keepalive status
                continue

            except asyncio.CancelledError:
                self.connected = False
                return

            except Exception as exc:
                self.connected = False
                log.warning("MeshCoreBridge: MQTT error (%s), retry in %.0fs",
                            exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ── Packet format (meshcore-packet-capture / letsmesh) ────────────────

    async def _publish_packet(self, client, msg: "NormalizedMessage") -> None:
        """Publish one ECH NormalizedMessage as a letsmesh-format packet JSON."""
        raw = msg.raw or {}
        now = msg.timestamp  # already receive time since our meshcore.py fix

        # Determine packet_type from adapter raw data or message channel
        ptype = self._infer_packet_type(msg)

        # SNR / RSSI from raw dict
        snr  = raw.get("snr")
        rssi = raw.get("rssi")

        packet = {
            "origin":      self._origin_name,
            "origin_id":   self._origin_id(),
            "timestamp":   now.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "type":        "PACKET",
            "direction":   "rx",
            "time":        now.strftime("%H:%M:%S"),
            "date":        now.strftime("%d/%m/%Y"),
            "len":         str(len(msg.body.encode()) + 20),  # rough estimate
            "packet_type": str(ptype),
            "route":       "F",           # flood — MeshCore channel msgs are flood
            "payload_len": str(len(msg.body.encode())),
            "raw":         "",            # companion protocol doesn't expose raw LoRa frames
            "SNR":         str(snr) if snr is not None else "",
            "RSSI":        str(rssi) if rssi is not None else "",
            "hash":        msg.id[:12].upper(),
            # Extended fields — not in spec but useful for ECH consumers
            "channel":     msg.source_channel,
            "from_id":     msg.from_id,
            "from_display": msg.from_display,
            "body":        msg.body,
            "hop_count":   msg.hop_count,
        }
        if msg.lat is not None and msg.lon is not None:
            packet["lat"] = msg.lat
            packet["lon"] = msg.lon

        payload = json.dumps(packet).encode()

        # Publish to /packets and /decoded (some tools subscribe to one, some both)
        await client.publish(self._topic("packets"), payload=payload, qos=self._qos)
        await client.publish(self._topic("decoded"), payload=payload, qos=self._qos)
        self._pub_count += 1

        log.debug("MeshCoreBridge: published pkt_type=%d from %s channel=%s",
                  ptype, msg.from_id[:8], msg.source_channel)

    async def _publish_status(self, client) -> None:
        """Publish observer status (retained). Called on connect and periodically."""
        adapter = self._find_meshcore_adapter()
        node_count = 0
        last_channel = ""
        firmware = ""
        if adapter:
            try:
                nodes = await adapter.nodes()
                node_count = len(nodes)
            except Exception:
                pass
            if hasattr(adapter, '_channels') and adapter._channels:
                last_channel = adapter._channels.get(
                    getattr(adapter, '_channel_idx', 0), "")
            if hasattr(adapter, '_fw_version'):
                firmware = adapter._fw_version

        status = {
            "online":      True,
            "origin":      self._origin_name,
            "origin_id":   self._origin_id(),
            "last_seen":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "iata":        self._iata,
            "channel":     last_channel,
            "node_count":  node_count,
            "firmware":    firmware,
            "ech_version": "1.0.0",
        }
        await client.publish(
            self._topic("status"),
            payload=json.dumps(status).encode(),
            qos=self._qos,
            retain=True,
        )
        log.debug("MeshCoreBridge: status published (nodes=%d channel=%s)",
                  node_count, last_channel)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _infer_packet_type(self, msg: "NormalizedMessage") -> int:
        """Map ECH NormalizedMessage to a MeshCore PayloadType integer."""
        raw = msg.raw or {}
        # Direct check if adapter stored it
        if "packet_type" in raw:
            return int(raw["packet_type"])
        # DM → TXT_MSG; channel → GRP_TXT
        if msg.source_channel == "DM" or (msg.to_id and msg.to_id != "broadcast"):
            return _PTYPE_TXT_MSG
        return _PTYPE_GRP_TXT

    def _sync_device_info(self) -> None:
        """Pull device name and pubkey from the adapter's PACKET_SELF_INFO if available."""
        adapter = self._find_meshcore_adapter()
        if not adapter:
            return
        if hasattr(adapter, '_device_name') and adapter._device_name:
            self._origin_name = adapter._device_name
        # Use the adapter name as pubkey placeholder until real key is known
        if not self._device_pubkey and hasattr(adapter, '_device_name') and adapter._device_name:
            # Derive a stable 12-char hex ID from the device name for use in topics
            self._device_pubkey = hashlib.md5(
                adapter._device_name.encode()
            ).hexdigest()[:12].upper()

    def _find_meshcore_adapter(self):
        if not self._router:
            return None
        if self._adapter_name:
            return self._router._adapters.get(self._adapter_name)
        for name, adapter in self._router._adapters.items():
            if "meshcore" in name.lower() and "mock" not in name.lower():
                return adapter
        return None

    def status(self) -> dict:
        return {
            "enabled":      self.enabled,
            "connected":    self.connected,
            "broker":       f"{self._host}:{self._port}",
            "transport":    "websockets" if self._websocket else "tcp",
            "iata":         self._iata,
            "origin_id":    self._origin_id(),
            "topic_prefix": self._prefix,
            "topic_example": self._topic("packets"),
            "adapter":      self._adapter_name or "auto",
            "published":    self._pub_count,
            "queue_depth":  self._queue.qsize(),
        }
