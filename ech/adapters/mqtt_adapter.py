"""
ech/adapters/mqtt_adapter.py
-----------------------------
MQTT adapter for ECH. Supports multiple brokers simultaneously —
each broker is a separate adapter instance in config.yaml.

Uses aiomqtt (async, no callbacks, pure asyncio).

Auto-detects payload formats:
  - Meshtastic MQTT JSON  (topic contains 'meshtastic' or payload has 'from')
  - MeshCore MQTT JSON    (topic contains 'meshcore' or payload has 'sender')
  - Generic JSON          (any other JSON payload)
  - Raw string            (fallback)

Can also PUBLISH messages to topics — used for bridge rules and
broadcasting alerts to mesh nodes via their MQTT gateways.

Config keys:
  name              str     adapter name (default: mqtt)
  host              str     broker hostname (REQUIRED)
  port              int     broker port (default: 1883)
  username          str     broker username (optional)
  password          str     broker password (optional)
  pubkey_auth       str     derive credentials from a MeshCore adapter's hardware key.
                            Value is the MeshCore adapter name (e.g. "meshcore").
                            Scheme: username=first8hexchars, password=full64hexkey.
                            Overrides username/password if the pubkey is available.
  pubkey_auth_mode  str     "letsmesh" (default) — username=8-char shortkey, password=full64hex
                            "full"               — username=full64hex, password=full64hex
  tls               bool    enable TLS (default: False)
  client_id         str     MQTT client ID (default: ech-{name})
  topics            list    topics to subscribe to (default: ['#'])
  publish_topic     str     topic to publish outbound messages on (optional)
  keepalive         int     keepalive seconds (default: 60)
  qos               int     subscription QoS 0/1/2 (default: 0)

Example configs:
  # Meshtastic public MQTT
  - type: mqtt
    name: meshtastic-mqtt
    host: mqtt.meshtastic.org
    port: 1883
    topics: ["msh/US/ME/#"]

  # Local MeshCore broker
  - type: mqtt
    name: meshcore-local
    host: 192.168.1.10
    port: 1883
    username: subscriber
    password: changeme
    topics: ["meshcore/#"]

  # LetsMesh broker — hardware-key JWT auth (meshcoretomqtt scheme)
  # username = "v1_{PUBKEY_HEX}"  password = Ed25519-signed JWT
  # Obtain private_key by running "get prv.key" on the MeshCore serial console.
  - type: mqtt
    name: letsmesh-us
    host: mqtt-us-v1.letsmesh.net
    port: 443
    tls: true
    transport: websockets
    pubkey_auth: meshcore        # MeshCore adapter name to pull pubkey from
    private_key: ""              # 64-byte hex private key from "get prv.key" (keep out of git)
    token_ttl: 3600              # JWT lifetime in seconds (default 1 hour)
    topics: ["meshcore/+/+/packets"]
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

EMRG_WORDS = {"emergency", "mayday", "sos", "911", "fire", "flood"}
ELVT_WORDS  = {"urgent", "priority", "immediate"}


def _priority(text: str) -> Priority:
    lower = text.lower()
    if any(w in lower for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in lower for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


class MQTTAdapter(Adapter):
    """
    MQTT adapter using aiomqtt. One instance per broker.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name              = config.get("name", "mqtt")
        self._host             = config["host"]
        self._port             = int(config.get("port", 1883))
        self._username         = config.get("username")
        self._password         = config.get("password")
        self._pubkey_auth      = config.get("pubkey_auth")        # MeshCore adapter name
        self._pubkey_auth_mode = config.get("pubkey_auth_mode", "letsmesh")
        self._private_key      = config.get("private_key", "").strip()  # 64-byte hex Ed25519 key
        self._token_ttl        = int(config.get("token_ttl", 3600))
        self._tls              = bool(config.get("tls", False))
        self._client_id        = config.get("client_id", f"ech-{self.name}")
        self._topics           = config.get("topics", ["#"])
        self._pub_topic        = config.get("publish_topic")
        self._keepalive        = int(config.get("keepalive", 60))
        self._qos              = int(config.get("qos", 0))
        self._transport        = config.get("transport", "tcp")  # "tcp" or "websockets"
        self._msg_count        = 0
        self._run_task: asyncio.Task | None = None
        self._client = None

    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _make_jwt(self, pubkey_hex: str, privkey_hex: str) -> str:
        """Generate a meshcoretomqtt-compatible Ed25519 JWT.

        Format: base64url(header).base64url(payload).HEX_SIGNATURE
        Signed with the MeshCore device's Ed25519 private key.
        Private key is 64 bytes: seed(32) || pubkey(32).
        """
        header = self._b64url(json.dumps({"alg": "Ed25519", "typ": "JWT"},
                                         separators=(",", ":")).encode())
        iat = int(time.time())
        payload_obj: dict = {
            "publicKey": pubkey_hex.upper(),
            "iat": iat,
            "exp": iat + self._token_ttl,
            "aud": self._host,
        }
        payload = self._b64url(json.dumps(payload_obj, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode()

        from Crypto.PublicKey import ECC
        from Crypto.Signature import eddsa
        # MeshCore private key: 64 bytes = 32-byte seed || 32-byte pubkey
        seed = bytes.fromhex(privkey_hex)[:32]
        key = ECC.construct(curve="Ed25519", seed=seed)
        sig = eddsa.new(key, "rfc8032").sign(signing_input).hex().upper()
        return f"{header}.{payload}.{sig}"

    def _resolve_credentials(self) -> tuple[str | None, str | None]:
        """Return (username, password).

        When pubkey_auth is set and a private_key is configured, generates a
        meshcoretomqtt-compatible Ed25519 JWT (same scheme as LetsMesh):
          username = "v1_{PUBKEY_HEX}"
          password = JWT signed with device private key
        Falls back to username/password from config if not configured.
        """
        if self._pubkey_auth:
            try:
                from ech.adapters.meshcore import _pubkey_registry, _privkey_registry
                pubkey  = _pubkey_registry.get(self._pubkey_auth)
                # Auto-retrieved key (serial transport) takes priority over config value
                privkey = _privkey_registry.get(self._pubkey_auth) or self._private_key
                if not pubkey:
                    log.warning("MQTT %s: pubkey not yet available from adapter %r — "
                                "will retry on reconnect", self.name, self._pubkey_auth)
                    return self._username, self._password
                if not privkey:
                    log.warning("MQTT %s: private key not available — for serial transport it is "
                                "fetched automatically; for TCP add 'private_key: <128-hex-chars>' "
                                "(run 'get prv.key' on the device serial console to get it)",
                                self.name)
                    return self._username, self._password
                try:
                    token = self._make_jwt(pubkey, privkey)
                    return f"v1_{pubkey.upper()}", token
                except Exception as exc:
                    log.error("MQTT %s: JWT generation failed: %s", self.name, exc)
            except ImportError:
                log.warning("MQTT %s: pubkey_auth requires meshcore adapter", self.name)
        return self._username, self._password

    async def connect(self) -> None:
        log.info("MQTT %s: connecting to %s:%d", self.name, self._host, self._port)
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("MQTT %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """Publish a message to the configured publish_topic."""
        if not self._pub_topic:
            log.warning("MQTT %s: no publish_topic configured", self.name)
            return False
        try:
            import aiomqtt
            username, password = self._resolve_credentials()
            pub_kw = self._build_client_kwargs(aiomqtt, username, password, self._tls, f"{self._client_id}-pub")
            async with aiomqtt.Client(**pub_kw) as client:
                await client.publish(
                    self._pub_topic,
                    payload=message.body.encode(),
                    qos=self._qos,
                )
            self._mark_tx(message)
            log.debug("MQTT %s: published to %s", self.name, self._pub_topic)
            return True
        except Exception as exc:
            log.error("MQTT %s: publish error: %s", self.name, exc)
            return False

    def _build_client_kwargs(self, aiomqtt, username, password, use_tls: bool, client_id):
        """Build aiomqtt.Client kwargs for aiomqtt >=2.3 (paho-mqtt 2.x backend)."""
        kw = {
            "hostname":  self._host,
            "port":      self._port,
            "username":  username,
            "password":  password,
            "identifier": client_id,
            "keepalive": self._keepalive,
        }
        # TLS: aiomqtt 2.x wants an ssl.SSLContext via tls_context
        if use_tls:
            import ssl as _ssl
            kw["tls_context"] = _ssl.create_default_context()
        # WebSocket: paho-mqtt 2.x requires BOTH transport AND websocket_path
        if self._transport == "websockets":
            kw["transport"] = "websockets"
            kw["websocket_path"] = "/mqtt"
        return kw

    async def _run(self) -> None:
        """Main MQTT receive loop with auto-reconnect."""
        import aiomqtt
        backoff = 2.0
        while self._connected:
            try:
                username, password = self._resolve_credentials()
                if username:
                    log.debug("MQTT %s: connecting as %s…", self.name, username[:16])
                client_kw = self._build_client_kwargs(aiomqtt, username, password, self._tls, self._client_id)
                async with aiomqtt.Client(**client_kw) as client:
                    self._client = client
                    backoff = 2.0
                    token_born = time.time()
                    log.info("MQTT %s: connected, subscribing to %s", self.name, self._topics)

                    for topic in self._topics:
                        await client.subscribe(topic, qos=self._qos)

                    async for mqtt_msg in client.messages:
                        if not self._connected:
                            break
                        # Reconnect before JWT expiry to get a fresh token
                        if self._pubkey_auth and self._private_key:
                            age = time.time() - token_born
                            if age >= self._token_ttl * 0.9:
                                log.info("MQTT %s: JWT near expiry, reconnecting for fresh token",
                                         self.name)
                                break
                        await self._handle_message(str(mqtt_msg.topic), mqtt_msg.payload)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._connected:
                    log.warning("MQTT %s: connection lost (%s), retry in %.0fs",
                                self.name, exc, backoff)
                    self._client = None
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        self._msg_count += 1
        try:
            raw_str = payload.decode("utf-8", errors="replace")
        except Exception:
            return

        # Try to parse as JSON
        parsed = None
        try:
            parsed = json.loads(raw_str)
        except (json.JSONDecodeError, ValueError):
            pass

        # Route to format-specific parser
        if parsed:
            if self._looks_meshtastic(topic, parsed):
                msg = self._parse_meshtastic(topic, parsed, raw_str)
            elif self._looks_meshcore(topic, parsed):
                msg = self._parse_meshcore(topic, parsed, raw_str)
            else:
                msg = self._parse_generic_json(topic, parsed, raw_str)
        else:
            msg = self._parse_raw(topic, raw_str)

        if msg:
            await self._enqueue(msg)
            log.debug("MQTT %s: RX on %s: %s", self.name, topic, msg.body[:60])

    def _looks_meshtastic(self, topic: str, data: dict) -> bool:
        return (
            "meshtastic" in topic.lower() or
            "msh/" in topic or
            ("from" in data and "type" in data)
        )

    def _looks_meshcore(self, topic: str, data: dict) -> bool:
        return (
            "meshcore" in topic.lower() or
            "sender" in data or
            "pubkey" in data
        )

    def _parse_meshtastic(self, topic: str, data: dict, raw: str) -> NormalizedMessage | None:
        """Parse Meshtastic MQTT JSON format."""
        msg_type = data.get("type", "")
        payload  = data.get("payload", {})

        if msg_type == "text":
            text    = payload.get("text", "")
            from_id = str(data.get("from", "unknown"))
            sender  = data.get("sender", from_id)
            if not text:
                return None
            return NormalizedMessage(
                source_adapter=self.name,
                source_channel=f"mqtt:{topic.split('/')[2] if '/' in topic else topic}",
                from_id=from_id,
                from_display=sender,
                body=text,
                priority=_priority(text),
                lat=payload.get("latitude"),
                lon=payload.get("longitude"),
                raw={"topic": topic, "type": msg_type, "mqtt": True},
            )

        elif msg_type == "position":
            from_id = str(data.get("from", "unknown"))
            lat = payload.get("latitudeI", 0) / 1e7
            lon = payload.get("longitudeI", 0) / 1e7
            alt = payload.get("altitude")
            if lat == 0 and lon == 0:
                return None
            body = f"POS {from_id}: {lat:.5f},{lon:.5f}"
            if alt:
                body += f" alt {alt}m"
            return NormalizedMessage(
                source_adapter=self.name,
                source_channel=f"mqtt:position",
                from_id=from_id,
                from_display=from_id,
                body=body,
                lat=lat, lon=lon,
                raw={"topic": topic, "type": "position", "mqtt": True,
                     "altitude": alt},
            )
        return None

    def _parse_meshcore(self, topic: str, data: dict, raw: str) -> NormalizedMessage | None:
        """Parse MeshCore MQTT JSON format."""
        sender  = data.get("sender", data.get("pubkey", "unknown"))
        text    = data.get("text", data.get("message", data.get("msg", "")))
        lat     = data.get("lat")
        lon     = data.get("lon")

        if not text and not lat:
            return None

        body = text or f"POS {sender}: {lat},{lon}"
        return NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"mqtt:{topic.split('/')[1] if '/' in topic else topic}",
            from_id=str(sender),
            from_display=str(sender),
            body=body,
            priority=_priority(body),
            lat=float(lat) if lat else None,
            lon=float(lon) if lon else None,
            raw={"topic": topic, "mqtt": True},
        )

    def _parse_generic_json(self, topic: str, data: dict, raw: str) -> NormalizedMessage:
        """Generic JSON payload - extract what we can."""
        # Try common field names for body
        body = (
            data.get("message") or data.get("text") or data.get("msg") or
            data.get("body") or data.get("payload") or
            raw[:200]
        )
        from_id = str(
            data.get("from") or data.get("sender") or
            data.get("source") or data.get("node") or
            topic.split("/")[-1]
        )
        return NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"mqtt:{topic}",
            from_id=from_id,
            from_display=from_id,
            body=str(body)[:500],
            priority=_priority(str(body)),
            lat=data.get("lat") or data.get("latitude"),
            lon=data.get("lon") or data.get("longitude"),
            raw={"topic": topic, "mqtt": True},
        )

    def _parse_raw(self, topic: str, text: str) -> NormalizedMessage:
        """Raw (non-JSON) MQTT payload."""
        return NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"mqtt:{topic}",
            from_id=topic,
            from_display=topic,
            body=text[:500],
            priority=_priority(text),
            raw={"topic": topic, "mqtt": True, "raw": True},
        )

    def _health_detail(self) -> dict:
        if self._pubkey_auth:
            try:
                from ech.adapters.meshcore import _pubkey_registry, _privkey_registry
                pk  = _pubkey_registry.get(self._pubkey_auth, "")
                prv = _privkey_registry.get(self._pubkey_auth) or self._private_key
                auth = f"jwt:v1_{pk[:8]}…" if pk else "jwt:pubkey-pending"
                if not prv:
                    auth += " (no privkey — TCP: add private_key to config)"
                else:
                    src = "auto" if _privkey_registry.get(self._pubkey_auth) else "config"
                    auth += f" privkey:{src}"
            except ImportError:
                auth = "jwt:no-meshcore"
        elif self._username:
            auth = f"password:{self._username}"
        else:
            auth = "none"
        return {
            "host": f"{self._host}:{self._port}",
            "topics": self._topics,
            "messages_received": self._msg_count,
            "tls": self._tls,
            "connected": self._client is not None,
            "auth": auth,
        }


class MockMQTTAdapter(Adapter):
    """Mock MQTT adapter - simulates MQTT traffic without a real broker."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "mqtt-mock")
        self._host = config.get("host", "localhost")
        self._topics = config.get("topics", ["msh/US/ME/#"])
        self._interval = config.get("interval_sec", 18.0)
        self._run_task: asyncio.Task | None = None
        self._msg_count = 0

    async def connect(self) -> None:
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: mock MQTT connected (topics=%s)", self.name, self._topics)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: NormalizedMessage) -> bool:
        self._mark_tx(message)
        return True

    async def _run(self) -> None:
        import random
        nodes = ["!a1b2c3d4", "!b2c3d4e5", "!c3d4e5f6"]
        msgs  = [
            "MQTT node online, signal good",
            "Position update via MQTT gateway",
            "Message bridged from internet gateway",
            "MQTT uplink active, 3 nodes visible",
        ]
        try:
            while self._connected:
                if self.is_paused():
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-5, 5))
                self._msg_count += 1
                node = random.choice(nodes)
                topic = self._topics[0].replace("#", "text") if self._topics else "mqtt/test"
                nm = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel=f"mqtt:{topic}",
                    from_id=node,
                    from_display=node,
                    body=random.choice(msgs),
                    raw={"topic": topic, "mqtt": True, "mock": True},
                )
                await self._enqueue(nm)
        except asyncio.CancelledError:
            pass

    def _health_detail(self) -> dict:
        return {
            "host": self._host,
            "topics": self._topics,
            "messages_received": self._msg_count,
            "mode": "mock",
        }
