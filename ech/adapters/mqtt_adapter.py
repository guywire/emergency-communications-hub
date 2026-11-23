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
  jwt_owner         str     DEPRECATED (O75, 2026-10-09) — no longer sent; not part of
                            LetsMesh's real auth scheme, see _jwt_header_and_payload()
  jwt_email         str     DEPRECATED (O75, 2026-10-09) — same, no longer sent
  tls               bool    enable TLS (default: False)
  client_id         str     MQTT client ID (default: ech-{name})
  topics            list    topics to subscribe to (default: ['#'])
  publish_topic     str     topic to publish outbound messages on (optional)
  keepalive         int     keepalive seconds (default: 60)
  qos               int     subscription QoS 0/1/2 (default: 0)
  channel_keys      dict    Meshtastic channel name -> PSK (base64 or hex; "AQ==" = default key).
                            Used to decrypt protobuf uplinks on msh/.../2/e/<channel>/...
                            (default: {LongFast: "AQ=="})
  fallback_keys     list    PSKs to try for channels NOT listed in channel_keys (default: ["AQ=="])
  meshtastic_publish bool   send() publishes Meshtastic protobuf text to msh/.../2/e/<channel>/<node>
                            (encrypted with that channel's key) instead of publish_topic (default: False)
  publish_roots     list    topic roots to publish under, e.g. ["msh/US/ME", "msh/US"]
                            (default: every root the channel has been heard on)
  tx_channel        str     channel name send() uses when the message names none (default: first in channel_keys)
  node_id           str     this gateway's Meshtastic node id, e.g. "!ec0a1b2c" (default: derived from name+host)
  long_name         str     NodeInfo long name announced on the TX channel (default: "ECH <name>")
  short_name        str     NodeInfo short name, max 4 chars (default: "ECH")
  nodeinfo_interval int     seconds between NodeInfo announcements, 0 = never (default: 10800)
  bridge_node       dict|false  a second virtual node that bridged messages come from, so the
                            gateway's own name isn't on every relayed post:
                            {long_name: "MeshCore Bridge", short_name: "MCB", node_id: "!ec..."}
                            (default: on with those names, id derived from the gateway; false = off)
  allow_dm          bool    let send() publish channel-key DMs to a !node id (default: False — modern
                            firmware expects PKI DMs, so these are normally dropped)
  dedup_with        list   other Meshtastic MQTT adapter names (same ECH process) this one defers to:
                            a text those adapters already delivered is dropped here, and their own
                            gateway node's packets are ignored. Use it on a second, read-only broker
                            that overlaps the main one (e.g. public mqtt.meshtastic.org + a state broker)
  dedup_delay       float   seconds to hold a text before the dedup_with check, so the main
                            broker's copy wins the race (default: 3)

Example configs:
  # Meshtastic public MQTT
  - type: mqtt
    name: meshtastic-mqtt
    host: mqtt.meshtastic.org
    port: 1883
    topics: ["msh/US/ME/#"]

  # Private/regional Meshtastic broker (protobuf uplinks, decrypted locally).
  # Name it with "meshtastic" in it so the UI treats it as Meshtastic chat.
  - type: mqtt
    name: meshtastic-mqtt-me
    host: broker.example.org
    username: user           # live config only, never commit
    password: pass
    topics: ["msh/US/ME/#"]
    channel_keys: {LongFast: "AQ=="}

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
  # Signed ON-DEVICE via CMD_SIGN_START/DATA/FINISH (see MeshCoreAdapter.sign_data) —
  # the raw private key never leaves the radio and never needs to be typed
  # into this file. Works over serial, TCP, or BLE identically. Only set
  # private_key below as a fallback for when the meshcore adapter is
  # disconnected at JWT-refresh time (rare — signing just fails until it
  # reconnects, the same as any other MeshCore feature would).
  - type: mqtt
    name: letsmesh-us
    host: mqtt-us-v1.letsmesh.net
    port: 443
    tls: true
    transport: websockets
    pubkey_auth: meshcore        # MeshCore adapter name to pull pubkey from / sign with
    private_key: ""              # optional fallback: 64-byte hex, exported via the MeshCore
                                  # app (Settings -> Manage Identity Key). Keep out of git.
    token_ttl: 3600              # JWT lifetime in seconds (default 1 hour)
    topics: ["meshcore/+/+/packets"]
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
import weakref
import zlib
from collections import OrderedDict
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import MeshNode, NormalizedMessage, Priority

log = logging.getLogger(__name__)

# Channel label for DMs between OTHER nodes that a gateway exposed in plain JSON.
OTHER_DMS_CHANNEL = "Other DMs"

# Meshtastic MQTT adapters by name, for dedup_with lookups between brokers.
_MT_REGISTRY: "weakref.WeakValueDictionary[str, MQTTAdapter]" = weakref.WeakValueDictionary()

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
        # jwt_owner/jwt_email: DEPRECATED (O75, 2026-10-09) — these drove an
        # `owner`/`email` JWT claim pair that, per a more accurate reference
        # found after the original ones kept getting rejected, isn't part
        # of LetsMesh's actual auth scheme at all. No longer sent (see
        # _jwt_header_and_payload's docstring); kept here only so existing
        # config.yaml files with these keys don't raise, and to emit a
        # one-time warning pointing at why.
        self._jwt_owner        = config.get("jwt_owner", "").strip()
        self._jwt_email        = config.get("jwt_email", "").strip()
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
        # Meshtastic protobuf uplinks (msh/<region>/2/e/<channel>/<gateway>)
        from ech.adapters import meshtastic_mqtt_codec as codec
        expand_psk = codec.expand_psk
        self._mt_enabled = (
            "channel_keys" in config or "meshtastic" in self.name.lower()
            or any("msh/" in str(t) for t in self._topics)
        )
        ck = config.get("channel_keys")
        if not isinstance(ck, dict):
            ck = {"LongFast": "AQ=="}
        self._mt_keys: dict[str, bytes | None] = {}
        for ch, key in ck.items():
            try:
                self._mt_keys[str(ch)] = expand_psk(key)
            except ValueError as exc:
                log.error("MQTT %s: channel_keys[%s] invalid: %s", self.name, ch, exc)
        self._mt_fallback: list[bytes] = []
        for key in config.get("fallback_keys", ["AQ=="]) or []:
            try:
                k = expand_psk(key)
            except ValueError as exc:
                log.error("MQTT %s: fallback_keys entry invalid: %s", self.name, exc)
                continue
            if k:
                self._mt_fallback.append(k)
        self._mt_seen: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._mt_nodes: dict[str, MeshNode] = {}
        self._mt_stats = {"decoded": 0, "undecryptable": 0, "duplicates": 0, "text": 0,
                          "sent": 0, "own_heard": 0}
        # Index order is stable: configured channels first (bridge rules refer to
        # these indexes), channels discovered on the broker appended after.
        self._mt_channel_names: list[str] = list(self._mt_keys)
        self._mt_learned_keys: dict[str, bytes] = {}   # discovered channel -> fallback key that opened it
        self._mt_roots: dict[str, set[str]] = {}          # channel -> topic roots seen
        self._mt_undecodable: dict[str, int] = {}         # channel -> packets we had no key for
        self._mt_publish = bool(config.get("meshtastic_publish", False))
        self._mt_publish_roots = [str(r).rstrip("/") for r in (config.get("publish_roots") or []) if r]
        num = codec.parse_node_id(config["node_id"]) if config.get("node_id") else None
        if num is None:
            num = 0xEC000000 | (zlib.crc32(f"ech-{self.name}-{self._host}".encode()) & 0x00FFFFFF)
        self._mt_node_num = num
        self._mt_gateway_id = codec.node_id_str(num)
        self._mt_long_name = str(config.get("long_name") or f"ECH {self.name}")[:39]
        self._mt_short_name = str(config.get("short_name") or "ECH")[:4]
        bn = config.get("bridge_node", {})
        self._mt_bridge_num: int | None = None
        if bn is not False and bn is not None:
            bn = bn if isinstance(bn, dict) else {}
            bnum = codec.parse_node_id(bn["node_id"]) if bn.get("node_id") else None
            if bnum is None:
                bnum = 0xEC000000 | (zlib.crc32(f"ech-{self.name}-{self._host}-bridge".encode()) & 0x00FFFFFF)
            if bnum != num:
                self._mt_bridge_num = bnum
            self._mt_bridge_long = str(bn.get("long_name") or "MeshCore Bridge")[:39]
            self._mt_bridge_short = str(bn.get("short_name") or "MCB")[:4]
        self._mt_bridge_announced: set[str] = set()     # channels the bridge node has introduced itself on
        self._mt_nodeinfo_interval = int(config.get("nodeinfo_interval", 10800))
        tx = str(config.get("tx_channel") or "")
        self._mt_tx_idx = self._mt_channel_names.index(tx) if tx in self._mt_channel_names else 0
        self._mt_own_heard: OrderedDict[int, set[str]] = OrderedDict()   # our packet id -> gateways
        self._mt_announce_task: asyncio.Task | None = None
        # Cross-broker dedup: texts this adapter delivered, keyed (from, packet id).
        self._mt_text_seen: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._mt_dedup_with = [str(n) for n in (config.get("dedup_with") or []) if n]
        self._mt_dedup_delay = float(config.get("dedup_delay", 3.0))
        self._mt_stats["cross_duplicates"] = 0
        self._mt_pending: set[asyncio.Task] = set()
        self._mt_allow_dm = bool(config.get("allow_dm", False))
        self.last_send_error: str | None = None
        _MT_REGISTRY[self.name] = self

    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _jwt_header_and_payload(self, pubkey_hex: str) -> tuple[str, str]:
        """Build the base64url header/payload halves of a LetsMesh-
        compatible Ed25519 JWT. Shared by both signing paths below — only
        the signature (device vs. local) differs.

        REVISED 2026-10-08/09 (O75): the claim set below had two prior fix
        attempts (rc211, rc216) that both failed to resolve a persistent
        broker-side `[code:135] Not authorized` rejection. A fresh pass
        found a more accurate, more actively-maintained reference than the
        original `Cisien/meshcoretomqtt` this was modeled on —
        `Colorado-Mesh/mesh-client`'s `docs/letsmesh-mqtt-auth.md` and
        `michaelhart/meshcore-mqtt-broker` (broker-side source, same author
        as the `meshcore-decoder` npm package that generates the token) —
        and it describes a MINIMAL claim set, not the richer one rc216
        built: only `publicKey`, `aud`, `iat`, and optional `exp`. The
        `client`/`owner`/`email` claims added in rc216 (chasing the older,
        less-authoritative reference) are NOT part of this scheme at all
        and were a credible candidate for why a strict broker-side
        validator kept rejecting the token. Removed here rather than kept
        "just in case" — an extra, unexpected claim is exactly the kind of
        thing a strict JWT validator can reject outright.

        `aud` must equal the literal MQTT connect hostname (e.g.
        `mqtt-us-v1.letsmesh.net`), not an apex/API domain — this already
        matches `self._host` for a correctly-configured LetsMesh adapter
        (see the example config in this module's docstring), so no change
        needed there.

        STATUS: implemented from the new reference, NOT yet live-verified
        against the real broker — the operator doesn't currently run this
        adapter against LetsMesh. Re-test live before trusting this fixes
        the rejection; if it still fails, the next lead is forum.letsmesh.net
        (thread "Updates to meshcoretomqtt" was actively discussing this
        exact auth path as of the research pass, but requires a login to
        read).
        """
        header = self._b64url(json.dumps({"alg": "Ed25519", "typ": "JWT"},
                                         separators=(",", ":")).encode())
        iat = int(time.time())
        payload_obj: dict = {
            "publicKey": pubkey_hex.upper(),
            "aud": self._host,
            "iat": iat,
            "exp": iat + self._token_ttl,
        }
        payload = self._b64url(json.dumps(payload_obj, separators=(",", ":")).encode())
        return header, payload

    def _make_jwt(self, pubkey_hex: str, privkey_hex: str) -> str:
        """Sign locally with a raw private key from config. Format:
        base64url(header).base64url(payload).HEX_SIGNATURE
        Private key is 64 bytes: seed(32) || pubkey(32)."""
        header, payload = self._jwt_header_and_payload(pubkey_hex)
        signing_input = f"{header}.{payload}".encode()

        from Crypto.PublicKey import ECC
        from Crypto.Signature import eddsa
        seed = bytes.fromhex(privkey_hex)[:32]
        key = ECC.construct(curve="Ed25519", seed=seed)
        sig = eddsa.new(key, "rfc8032").sign(signing_input).hex().upper()
        return f"{header}.{payload}.{sig}"

    async def _make_device_jwt(self, pubkey_hex: str) -> str | None:
        """Sign on-device: ask the live MeshCore adapter to sign header.payload
        with its own Ed25519 identity key (MeshCoreAdapter.sign_data — see
        that method's docstring). The raw key never leaves the radio. Returns
        None if the adapter isn't connected or the sign round-trip fails, so
        the caller can fall back to a configured private_key."""
        from ech.adapters.meshcore import _adapter_registry
        adapter = _adapter_registry.get(self._pubkey_auth)
        if adapter is None or not getattr(adapter, "_connected", False):
            return None
        header, payload = self._jwt_header_and_payload(pubkey_hex)
        signing_input = f"{header}.{payload}".encode()
        try:
            sig_bytes = await adapter.sign_data(signing_input)
        except Exception as exc:
            log.warning("MQTT %s: device-side signing errored: %s", self.name, exc)
            return None
        if sig_bytes is None:
            return None
        return f"{header}.{payload}.{sig_bytes.hex().upper()}"

    async def _resolve_credentials(self) -> tuple[str | None, str | None]:
        """Return (username, password).

        When pubkey_auth is set, generates a meshcoretomqtt-compatible
        Ed25519 JWT (same scheme as LetsMesh):
          username = "v1_{PUBKEY_HEX}"
          password = JWT signed with the device's identity key
        Signing prefers the live device (sign_data() over the binary
        companion protocol — no key material ever leaves the radio) and
        falls back to a locally-configured private_key only if the device
        isn't reachable right now. Falls back to plain username/password
        from config if pubkey_auth isn't set at all.
        """
        if self._pubkey_auth:
            try:
                from ech.adapters.meshcore import _pubkey_registry
                pubkey = _pubkey_registry.get(self._pubkey_auth)
                if not pubkey:
                    log.warning("MQTT %s: pubkey not yet available from adapter %r — "
                                "will retry on reconnect", self.name, self._pubkey_auth)
                    return self._username, self._password

                token = await self._make_device_jwt(pubkey)
                if token is not None:
                    return f"v1_{pubkey.upper()}", token

                if self._private_key:
                    try:
                        token = self._make_jwt(pubkey, self._private_key)
                        return f"v1_{pubkey.upper()}", token
                    except Exception as exc:
                        log.error("MQTT %s: local JWT generation failed: %s", self.name, exc)
                else:
                    log.warning("MQTT %s: no signature available — MeshCore adapter %r isn't "
                                "connected and no fallback private_key is configured",
                                self.name, self._pubkey_auth)
            except ImportError:
                log.warning("MQTT %s: pubkey_auth requires meshcore adapter", self.name)
        return self._username, self._password

    async def connect(self) -> None:
        if self._jwt_owner or self._jwt_email:
            log.warning(
                "MQTT %s: jwt_owner/jwt_email are configured but no longer sent — "
                "see _jwt_header_and_payload()'s docstring (O75): the owner/email "
                "claims aren't part of LetsMesh's real auth scheme and were a "
                "likely cause of the broker rejecting this adapter's JWT. Safe to "
                "remove these from config.yaml.", self.name,
            )
        log.info("MQTT %s: connecting to %s:%d", self.name, self._host, self._port)
        self._connected = True
        if self._mt_enabled:
            await self._mt_load_nodes()
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        if self._mt_publish and self._mt_nodeinfo_interval > 0:
            self._mt_announce_task = asyncio.create_task(self._mt_announce_loop(),
                                                         name=f"{self.name}-nodeinfo")

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        if self._mt_announce_task:
            self._mt_announce_task.cancel()
            self._mt_announce_task = None
        log.info("MQTT %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """Publish a message: Meshtastic protobuf when meshtastic_publish is on, else raw text to publish_topic."""
        if self._mt_publish:
            return await self._mt_send(message)
        if not self._pub_topic:
            log.warning("MQTT %s: no publish_topic configured (set meshtastic_publish: true "
                        "to send Meshtastic channel text)", self.name)
            return False
        try:
            import aiomqtt
            username, password = await self._resolve_credentials()
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
                username, password = await self._resolve_credentials()
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
                        # Reconnect before JWT expiry to get a fresh token — applies whether
                        # the token was device-signed or locally-signed, so no longer gated
                        # on self._private_key being set.
                        if self._pubkey_auth:
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
        parts = topic.split("/")
        mi = parts.index("msh") if "msh" in parts else -1
        if mi >= 0 and "2" in parts[mi:]:
            i = parts.index("2", mi) + 1
            kind = parts[i] if i < len(parts) else ""
            if kind in ("e", "c", "map"):
                # e/c: channel traffic; map: gateways' MapReport (name + rough position)
                await self._handle_meshtastic_protobuf(topic, payload)
                return
            if kind == "json" and self._mt_enabled:
                # Plain-JSON uplink of the same packets (gateways with JSON output on).
                await self._handle_meshtastic_json(topic, payload)
                return
            if kind != "json":
                # stat (online/offline) - not chat traffic
                return
        try:
            raw_str = payload.decode("utf-8")
        except UnicodeDecodeError:
            # Binary payload with no known decoder - never enqueue it as text.
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

    # -- Meshtastic protobuf -------------------------------------------------
    def _mt_is_duplicate(self, from_num: int, packet_id: int) -> bool:
        """The same packet is uplinked by every gateway that heard it."""
        if not packet_id:
            return False
        now = time.monotonic()
        key = (from_num, packet_id)
        if key in self._mt_seen and now - self._mt_seen[key] < 600:
            return True
        self._mt_seen[key] = now
        self._mt_seen.move_to_end(key)
        while len(self._mt_seen) > 4000:
            self._mt_seen.popitem(last=False)
        return False

    @staticmethod
    def _mt_apply_identity(node: MeshNode, info: dict, source: str) -> None:
        if info.get("long_name"):
            node.display_name = info["long_name"]
            node.name_source = source
        if info.get("short_name"):
            node.short_name = info["short_name"]
            node.meta["short_name"] = info["short_name"]
        if info.get("hw_model"):
            node.hw_model = info["hw_model"]
            node.meta["hw_model"] = info["hw_model"]
        if info.get("role"):
            node.meta["node_type"] = info["role"]

    def _mt_persist(self, node: MeshNode) -> None:
        """Save name/position so they survive restarts (NodeInfo only comes every ~3 h)."""
        if self._db is None:
            return
        try:
            asyncio.get_running_loop().create_task(self._db.upsert_mesh_node(self.name, node))
        except Exception as exc:
            log.debug("MQTT %s: node persist failed: %s", self.name, exc)

    async def _mt_load_nodes(self) -> None:
        if self._db is None:
            return
        try:
            rows = await self._db.get_mesh_nodes(self.name)
        except Exception as exc:
            log.warning("MQTT %s: could not load saved nodes: %s", self.name, exc)
            return
        named = 0
        for row in rows:
            nid = row["node_id"]
            if nid in self._mt_nodes:
                continue
            lh = None
            if row.get("last_heard"):
                try:
                    lh = datetime.fromisoformat(row["last_heard"])
                except ValueError:
                    pass
            try:
                meta = json.loads(row["meta_json"]) if row.get("meta_json") else {}
            except ValueError:
                meta = {}
            node = MeshNode(node_id=nid, display_name=row["display_name"] or nid, first_seen=lh,
                            last_heard=lh, lat=row.get("lat"), lon=row.get("lon"),
                            short_name=meta.get("short_name", ""), hw_model=meta.get("hw_model", ""),
                            name_source="saved" if row["display_name"] and row["display_name"] != nid else "")
            node.meta.update(meta)
            node.meta.setdefault("via", "mqtt")
            node.meta["protocol"] = "meshtastic"
            self._mt_nodes[nid] = node
            named += node.display_name != nid
        log.info("MQTT %s: loaded %d saved Meshtastic nodes (%d named)", self.name, len(rows), named)

    def _mt_node(self, node_id: str) -> MeshNode:
        node = self._mt_nodes.get(node_id)
        now = datetime.now(timezone.utc)
        if node is None:
            node = MeshNode(node_id=node_id, display_name=node_id, first_seen=now)
            node.meta["via"] = "mqtt"
            node.meta["protocol"] = "meshtastic"
            self._mt_nodes[node_id] = node
        node.last_heard = now
        return node

    # -- channel table (same shape the native Meshtastic adapter exposes) ----
    @property
    def _channel_list(self) -> list[dict]:
        return [{"index": i, "name": n} for i, n in enumerate(self._mt_channel_names)]

    def set_tx_channel(self, idx: int) -> None:
        if 0 <= int(idx) < len(self._mt_channel_names):
            self._mt_tx_idx = int(idx)
            log.info("MQTT %s: TX channel -> %s", self.name, self._mt_channel_names[self._mt_tx_idx])

    def _mt_channel_idx(self, name: str) -> int:
        if name not in self._mt_channel_names:
            self._mt_channel_names.append(name)
            log.info("MQTT %s: discovered Meshtastic channel %r", self.name, name)
        return self._mt_channel_names.index(name)

    @staticmethod
    def _mt_topic_parts(topic: str) -> tuple[str, str]:
        """msh/US/ME/2/e/LongFast/!gw -> ("msh/US/ME", "LongFast")
        (a bridged mt/state/msh/US/ME/2/e/... keeps its prefix in the root)"""
        parts = topic.split("/")
        i = parts.index("2", parts.index("msh") if "msh" in parts else 0)
        return "/".join(parts[:i]), (parts[i + 2] if i + 2 < len(parts) else "")

    @staticmethod
    def _mt_source(topic: str) -> str:
        """Which upstream broker a bridged topic came from: mt/state/msh/... -> "state"."""
        parts = topic.split("/")
        mi = parts.index("msh") if "msh" in parts else 0
        return parts[mi - 1] if mi > 0 else ""

    async def _handle_meshtastic_protobuf(self, topic: str, payload: bytes) -> None:
        from ech.adapters import meshtastic_mqtt_codec as codec
        root, topic_channel = self._mt_topic_parts(topic)
        try:
            pkt = codec.decode_envelope(payload, self._mt_keys, self._mt_fallback)
        except Exception as exc:
            log.debug("MQTT %s: protobuf decode error on %s: %s", self.name, topic, exc)
            pkt = None
        if pkt is None:
            self._mt_stats["undecryptable"] += 1
            if topic_channel and topic_channel != "PKI":
                if topic_channel not in self._mt_undecodable:
                    log.info("MQTT %s: channel %r heard but no key decrypts it", self.name, topic_channel)
                self._mt_undecodable[topic_channel] = self._mt_undecodable.get(topic_channel, 0) + 1
            return
        channel = pkt["channel"] or topic_channel
        if channel and channel != "PKI" and channel not in self._mt_keys and pkt.get("key"):
            # A channel we have no key for by that exact name, opened by a fallback key
            # (e.g. "Bot" when only "bot" is configured). Meshtastic channel names are
            # case-sensitive (the name is part of the channel hash), so remember the key
            # to be able to reply on "Bot" itself rather than on our "bot".
            self._mt_learned_keys.setdefault(channel, pkt["key"])
        if channel and channel != "PKI":   # PKI = the DM topic, not a channel
            self._mt_roots.setdefault(channel, set()).add(root)
            self._mt_channel_idx(channel)
        if any(p._mt_is_own(pkt["from_num"]) for p in self._mt_peers()):
            return   # another ECH adapter's own gateway node (our posts relayed between brokers)
        if self._mt_is_own(pkt["from_num"]):
            # Our own publish coming back. From our own gateway id it's just the
            # broker echo; from anyone else's it was transmitted on RF and heard.
            gw = pkt["gateway_id"]
            if gw and gw != self._mt_gateway_id:
                seen = self._mt_own_heard.setdefault(pkt["packet_id"], set())
                if gw not in seen:
                    seen.add(gw)
                    self._mt_stats["own_heard"] += 1
                    log.info("MQTT %s: our packet %08x on %s was heard on RF and re-uplinked by %s",
                             self.name, pkt["packet_id"], channel, gw)
                while len(self._mt_own_heard) > 500:
                    self._mt_own_heard.popitem(last=False)
            return
        if self._mt_is_duplicate(pkt["from_num"], pkt["packet_id"]):
            self._mt_stats["duplicates"] += 1
            return
        self._mt_stats["decoded"] += 1

        node = self._mt_node(pkt["from_id"])
        port = pkt["portnum"]
        if port in ("NODEINFO_APP", "MAP_REPORT_APP"):
            info = (codec.parse_user if port == "NODEINFO_APP" else codec.parse_map_report)(pkt["payload"])
            if info:
                self._mt_apply_identity(node, info, "nodeinfo" if port == "NODEINFO_APP" else "map_report")
                if info.get("lat") is not None:
                    node.lat, node.lon = info["lat"], info["lon"]
                self._mt_persist(node)
        elif port == "POSITION_APP":
            pos = codec.parse_position(pkt["payload"])
            if pos:
                node.lat, node.lon = pos["lat"], pos["lon"]
                if pos["alt"] is not None:
                    node.meta["altitude"] = pos["alt"]
                self._mt_persist(node)
        elif port == "TEXT_MESSAGE_APP":
            await self._mt_text(topic, pkt, node)

    async def _handle_meshtastic_json(self, topic: str, payload: bytes) -> None:
        """msh/.../2/json/<channel>/<gateway>: the gateway's plain-JSON copy of a
        packet. Treated like a decrypted protobuf packet (same channel naming,
        node names, dedup), so it doesn't land as anonymous generic MQTT."""
        from ech.adapters import meshtastic_mqtt_codec as codec
        try:
            data = json.loads(payload.decode("utf-8"))
            from_num = int(data["from"]) & 0xFFFFFFFF
        except Exception:
            return
        if not isinstance(data, dict):
            return
        root, channel = self._mt_topic_parts(topic)
        if channel and channel != "PKI":   # PKI = the DM topic, not a channel
            self._mt_roots.setdefault(channel, set()).add(root)
            self._mt_channel_idx(channel)
        if self._mt_is_own(from_num) or any(p._mt_is_own(from_num) for p in self._mt_peers()):
            return
        pid = int(data.get("id") or 0) & 0xFFFFFFFF
        if self._mt_is_duplicate(from_num, pid):
            self._mt_stats["duplicates"] += 1
            return
        self._mt_stats["decoded"] += 1
        self._mt_stats["json"] = self._mt_stats.get("json", 0) + 1
        node = self._mt_node(codec.node_id_str(from_num))
        kind = str(data.get("type") or "")
        body = data.get("payload")
        if kind == "nodeinfo" and isinstance(body, dict):
            self._mt_apply_identity(node, {
                "long_name": body.get("longname") or body.get("long_name"),
                "short_name": body.get("shortname") or body.get("short_name"),
                "hw_model": str(body.get("hardware") or "") or None,
            }, "nodeinfo")
            self._mt_persist(node)
        elif kind == "position" and isinstance(body, dict):
            lat_i, lon_i = body.get("latitude_i"), body.get("longitude_i")
            if lat_i and lon_i:
                node.lat, node.lon = lat_i / 1e7, lon_i / 1e7
                if body.get("altitude") is not None:
                    node.meta["altitude"] = body["altitude"]
                self._mt_persist(node)
        elif kind == "text":
            text = body.get("text") if isinstance(body, dict) else body
            if not isinstance(text, str) or not text.strip():
                return
            to_num = int(data.get("to") or codec.BROADCAST) & 0xFFFFFFFF
            hops_away = data.get("hops_away")
            pkt = {
                "payload": text.encode("utf-8"), "channel": channel, "packet_id": pid,
                "from_num": from_num, "from_id": codec.node_id_str(from_num),
                "to_num": to_num, "to_id": codec.node_id_str(to_num),
                "is_broadcast": to_num == codec.BROADCAST,
                "gateway_id": data.get("sender") or "",
                # JSON gives hops_away, not start/limit; express it as a 0-based pair
                "hop_start": hops_away if isinstance(hops_away, int) else None,
                "hop_limit": 0 if isinstance(hops_away, int) else None,
                "rx_snr": data.get("snr"), "rx_rssi": data.get("rssi"),
            }
            await self._mt_text(topic, pkt, node)

    async def _mt_text(self, topic: str, pkt: dict, node: MeshNode, _deferred: bool = False) -> None:
        text = pkt["payload"].decode("utf-8", errors="replace").strip()
        if not text:
            return
        if self._mt_dedup_with and pkt["packet_id"] and self._mt_dedup_delay > 0 and not _deferred:
            # Hold it off the receive loop so the main broker's copy can land first.
            async def _later():
                await asyncio.sleep(self._mt_dedup_delay)
                await self._mt_text(topic, pkt, node, _deferred=True)
            t = asyncio.create_task(_later())
            self._mt_pending.add(t)
            t.add_done_callback(self._mt_pending.discard)
            return
        is_dm = not pkt["is_broadcast"]
        # Someone else's DM (some gateways republish decrypted DMs as JSON on .../PKI/).
        # Shown on its own "Other DMs" channel, addressed to its recipient, so it never
        # looks like a DM to us. It isn't a real Meshtastic channel: no index, so
        # bridges and compose never pick it.
        others_dm = is_dm and pkt.get("to_num") is not None and pkt["to_num"] != self._mt_node_num
        if others_dm:
            self._mt_stats["others_dm"] = self._mt_stats.get("others_dm", 0) + 1
            rcpt = self._mt_nodes.get(pkt["to_id"])
            rcpt_name = rcpt.display_name if rcpt else pkt["to_id"]
            channel, ch_idx = OTHER_DMS_CHANNEL, None
        else:
            channel = pkt["channel"] or "mqtt"
            # A DM's "channel" is just the topic it rode on (often PKI); don't list it
            ch_idx = None if is_dm and channel == "PKI" else self._mt_channel_idx(channel)
        hop_start, hop_limit = pkt["hop_start"], pkt["hop_limit"]
        hop_count = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None
        key = (pkt["from_num"], pkt["packet_id"])
        if self._mt_dedup_with and pkt["packet_id"]:
            now = time.monotonic()
            if any(now - p._mt_text_seen.get(key, -1e9) < 600 for p in self._mt_peers()):
                self._mt_stats["cross_duplicates"] += 1
                log.debug("MQTT %s: text %08x already delivered by %s", self.name, pkt["packet_id"],
                          ",".join(self._mt_dedup_with))
                return
        if pkt["packet_id"]:
            self._mt_text_seen[key] = time.monotonic()
            while len(self._mt_text_seen) > 2000:
                self._mt_text_seen.popitem(last=False)
        self._mt_stats["text"] += 1
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=OTHER_DMS_CHANNEL if others_dm else ("DM" if is_dm else channel),
            from_id=pkt["from_id"],
            from_display=node.display_name,
            to_id=pkt["to_id"] if is_dm else None,
            body=(f"[to {rcpt_name}] {text}" if others_dm else f"[DM] {text}" if is_dm else text),
            priority=_priority(text),
            hop_count=hop_count,
            hop_start=hop_start,
            via_mqtt=True,
            # Text packets carry no position; use the sender's last known one so the
            # message gets a map link (flagged in raw so the UI can say so).
            lat=node.lat,
            lon=node.lon,
            raw={
                "position": "last_known" if node.lat is not None else None,
                "topic": topic,
                "mqtt": True,
                "channel": channel,
                "channel_idx": ch_idx,
                "channel_name": channel,
                "gateway": pkt["gateway_id"],
                "source": self._mt_source(topic),
                "packet_id": pkt["packet_id"],
                "snr": pkt["rx_snr"],
                "rssi": pkt["rx_rssi"],
            },
        )
        await self._enqueue(msg)
        log.debug("MQTT %s: meshtastic text %s on %s via %s: %s",
                  self.name, node.display_name, channel, pkt["gateway_id"], text[:60])

    def _mt_peers(self) -> list["MQTTAdapter"]:
        return [p for n in self._mt_dedup_with
                if (p := _MT_REGISTRY.get(n)) is not None and p is not self]

    async def nodes(self) -> list[MeshNode]:
        return list(self._mt_nodes.values())

    # -- Meshtastic publish ---------------------------------------------------
    def _mt_resolve_channel(self, raw: dict) -> str | None:
        hinted = str(raw.get("channel_name") or "").strip()
        if hinted:
            # "ch1:MaineNet" (native label) / "#MaineNet" / "MaineNet"
            want = hinted.split(":", 1)[-1].lstrip("#")
            # Exact name first: "Bot" and "bot" are different Meshtastic channels.
            if want in self._mt_channel_names and self._mt_has_key(want):
                return want
            for n in self._mt_channel_names:
                if n.lower() == want.lower() and self._mt_has_key(n):
                    return n
        if raw.get("channel_idx") is not None:
            try:
                return self._mt_channel_names[int(raw["channel_idx"])]
            except (TypeError, ValueError, IndexError):
                log.warning("MQTT %s: no channel at index %r", self.name, raw.get("channel_idx"))
                return None
        if hinted:
            log.info("MQTT %s: channel %r not known here, using TX channel", self.name, hinted)
        if not self._mt_channel_names:
            return None
        return self._mt_channel_names[min(self._mt_tx_idx, len(self._mt_channel_names) - 1)]

    def _mt_has_key(self, channel: str) -> bool:
        return channel in self._mt_keys or channel in self._mt_learned_keys

    def _mt_key(self, channel: str) -> bytes | None:
        if channel in self._mt_keys:
            return self._mt_keys[channel]
        return self._mt_learned_keys.get(channel)

    def _mt_roots_for(self, channel: str) -> list[str]:
        return self._mt_publish_roots or sorted(self._mt_roots.get(channel, ())) or ["msh/US"]

    def _mt_is_own(self, num) -> bool:
        """Our gateway node or our bridge node."""
        return num is not None and (num == self._mt_node_num or num == self._mt_bridge_num)

    async def _mt_announce_bridge(self, channel: str) -> None:
        from meshtastic.protobuf import mesh_pb2
        from ech.adapters import meshtastic_mqtt_codec as codec
        if self._mt_bridge_num is None or self._client is None or not self._mt_has_key(channel):
            return
        user = mesh_pb2.User(id=codec.node_id_str(self._mt_bridge_num), long_name=self._mt_bridge_long,
                             short_name=self._mt_bridge_short, hw_model=255)
        await self._mt_publish_packet(channel, codec.BROADCAST, 4, user.SerializeToString(),
                                      from_num=self._mt_bridge_num)
        self._mt_bridge_announced.add(channel)
        log.info("MQTT %s: announced bridge NodeInfo %s %r on %s", self.name,
                 codec.node_id_str(self._mt_bridge_num), self._mt_bridge_long, channel)

    async def _mt_publish_packet(self, channel: str, to_num: int, portnum: int, payload: bytes,
                                 from_num: int | None = None) -> int | None:
        from ech.adapters import meshtastic_mqtt_codec as codec
        client = self._client
        if client is None:
            log.warning("MQTT %s: not connected to broker, can't publish", self.name)
            return None
        key = self._mt_key(channel)
        pid = random.getrandbits(32) or 1
        env = codec.encode_envelope(from_num=from_num or self._mt_node_num, to_num=to_num, packet_id=pid,
                                    channel=channel, key=key, gateway_id=self._mt_gateway_id,
                                    portnum=portnum, payload=payload)
        for root in self._mt_roots_for(channel):
            await client.publish(f"{root}/2/e/{channel}/{self._mt_gateway_id}", payload=env, qos=self._qos)
        return pid

    async def _mt_send(self, message: NormalizedMessage) -> bool:
        from ech.adapters import meshtastic_mqtt_codec as codec
        raw = message.raw or {}
        channel = self._mt_resolve_channel(raw)
        if not channel:
            return False
        if not self._mt_has_key(channel):
            log.warning("MQTT %s: no key configured for channel %r - add it to channel_keys", self.name, channel)
            return False
        to_num = codec.BROADCAST
        self.last_send_error = None
        if message.to_id and not self._mt_allow_dm:
            # Current firmware uses PKI for DMs (the receiving node's public key + our
            # private key); a channel-key DM injected via MQTT is dropped by most nodes,
            # and gateways often don't downlink it. Refuse rather than "succeed" silently.
            self.last_send_error = "DMs aren't possible over Meshtastic MQTT - reply on the channel"
            log.warning("MQTT %s: refused DM to %s (DMs not supported over MQTT)", self.name, message.to_id)
            return False
        if message.to_id:
            to_num = codec.parse_node_id(message.to_id)
            if to_num is None:
                log.warning("MQTT %s: DM target %r is not a Meshtastic node id", self.name, message.to_id)
                return False
        text = message.body.encode("utf-8")
        if len(text) > 200:
            text = text[:200].decode("utf-8", errors="ignore").encode("utf-8")
        # Bridged channel posts come from the bridge node, not the gateway, so the
        # gateway's own name isn't on every relayed message (operator, 2026-11-21).
        sender = None
        if raw.get("_bridged_by") and self._mt_bridge_num is not None and not message.to_id:
            sender = self._mt_bridge_num
            if channel not in self._mt_bridge_announced:
                try:
                    await self._mt_announce_bridge(channel)
                except Exception as exc:
                    log.warning("MQTT %s: bridge NodeInfo publish failed: %s", self.name, exc)
        try:
            pid = await self._mt_publish_packet(channel, to_num, 1, text, from_num=sender)   # 1 = TEXT_MESSAGE_APP
        except Exception as exc:
            log.error("MQTT %s: publish error: %s", self.name, exc)
            return False
        if pid is None:
            return False
        self._mt_stats["sent"] += 1
        message.source_channel = "DM" if message.to_id else channel
        message.raw = {**raw, "channel_idx": self._mt_channel_names.index(channel),
                       "channel_name": channel, "packet_id": pid, "mqtt": True}
        self._mark_tx(message)
        log.info("MQTT %s: published text %08x to %s on %s%s", self.name, pid,
                 "DM " + codec.node_id_str(to_num) if message.to_id else "channel", channel,
                 " as the bridge node" if sender else "")
        return True

    async def _mt_announce_loop(self) -> None:
        """Announce our NodeInfo so Meshtastic users see a name, not !ecxxxxxx."""
        from meshtastic.protobuf import mesh_pb2
        from ech.adapters import meshtastic_mqtt_codec as codec
        if self._mt_nodeinfo_interval <= 0:
            return                           # nodeinfo_interval: 0 = never announce
        try:
            await asyncio.sleep(20)          # let the connection come up and roots be learned
            while self._connected:
                # Every channel we hold a key for (= every channel we can post on), so
                # listeners on MaineNet learn the name too, not just the TX channel.
                channels = [c for c in self._mt_keys if c] if self._client is not None else []
                if channels:
                    user = mesh_pb2.User(id=self._mt_gateway_id, long_name=self._mt_long_name,
                                         short_name=self._mt_short_name, hw_model=255)  # PRIVATE_HW
                    try:
                        for channel in channels:
                            await self._mt_publish_packet(channel, codec.BROADCAST, 4, user.SerializeToString())
                            log.info("MQTT %s: announced NodeInfo %s %r on %s", self.name,
                                     self._mt_gateway_id, self._mt_long_name, channel)
                        for ch in sorted(self._mt_bridge_announced):
                            await self._mt_announce_bridge(ch)
                    except Exception as exc:
                        log.warning("MQTT %s: NodeInfo publish failed: %s", self.name, exc)
                    await asyncio.sleep(self._mt_nodeinfo_interval)
                else:
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

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
                source_channel="mqtt:position",
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
                from ech.adapters.meshcore import _pubkey_registry, _adapter_registry
                pk = _pubkey_registry.get(self._pubkey_auth, "")
                adapter = _adapter_registry.get(self._pubkey_auth)
                device_signing = bool(adapter and getattr(adapter, "_connected", False))
                auth = f"jwt:v1_{pk[:8]}…" if pk else "jwt:pubkey-pending"
                if device_signing:
                    auth += " sign:device"
                elif self._private_key:
                    auth += " sign:config-key (device not connected)"
                else:
                    auth += " (no signature available — connect the MeshCore adapter or set private_key)"
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
            **(self._mt_health() if self._mt_enabled else {}),
        }

    def _mt_health(self) -> dict:
        names = self._mt_channel_names
        tx = names[min(self._mt_tx_idx, len(names) - 1)] if names else None
        return {
            # Same keys the native Meshtastic adapter reports, so the compose
            # channel picker and Settings channel switcher work unchanged.
            "channels_list": [{"idx": i, "name": n} for i, n in enumerate(names)],
            "channel_labels": list(names),
            "channel_idx": self._mt_tx_idx,
            "channel_name": tx,
            "meshtastic": {
                **self._mt_stats,
                "nodes": len(self._mt_nodes),
                "node_id": self._mt_gateway_id,
                "publish": self._mt_publish,
                "channels": {n: {"key": n in self._mt_keys,
                                 "roots": sorted(self._mt_roots.get(n, ()))} for n in names},
                "undecodable_channels": dict(self._mt_undecodable),
            },
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
