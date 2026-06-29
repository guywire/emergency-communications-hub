# ECH — External Dependencies

Last updated: 2026-06-21

This file tracks all external Python packages, GitHub projects, and protocol references
used by ECH. Update it whenever a new dependency is added or an existing one changes role.

---

## Python Packages

### Core / always required

| Package | PyPI name | Version pinned | Purpose |
|---------|-----------|---------------|---------|
| FastAPI | `fastapi` | no | HTTP API and WebSocket server |
| Uvicorn | `uvicorn[standard]` | no | ASGI server |
| PyYAML | `pyyaml` | no | config.yaml parsing |
| aiosqlite | `aiosqlite` | no | async SQLite (message store, key-value state) |

### Adapter — MeshCore

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| meshcore | `meshcore` | 2.3.7 (inspected) | Reference library; protocol format authority. ECH implements its own binary framing rather than using the library directly, but the library source was used to verify packet structures (CONTACT record offsets, SELF_INFO layout, PUSH_CHANNEL_MSG format, channel decryption). |
| pyserial-asyncio | `pyserial-asyncio-fast` | ≥0.16 | Async serial I/O for serial transport |
| pycryptodome | `pycryptodome` | any | Ed25519 JWT signing for LetsMesh auth (`Crypto.PublicKey.ECC`, `Crypto.Signature.eddsa`); AES channel decryption reference |

**GitHub:** https://github.com/fdlamotte/meshcore_py — meshcore Python library (v2.3.7)
- Used to audit packet field offsets (reader.py, meshcore_parser.py)
- Key discoveries: CONTACT record layout (pubkey 32B, adv_name at offset 99), SELF_INFO pubkey at bytes 3–34, PUSH_CHANNEL_MSG (0x88) sender pubkey at bytes 1–6, CHANNEL_MSG_V3 (0x11) has no sender pubkey

### Adapter — MQTT (MQTTAdapter + meshcore_bridge)

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aiomqtt | `aiomqtt` | any | Async MQTT client; used for all broker connections including LetsMesh WebSocket+TLS |

**Auth scheme reference:** https://github.com/Cisien/meshcoretomqtt
- `meshcoretomqtt` by Cisien — bridges MeshCore serial to MQTT with Ed25519 JWT auth
- ECH replicates the exact JWT format: `base64url(header).base64url(payload).HEX_SIGNATURE`
- Username format: `v1_{PUBKEY_64_HEX_UPPERCASE}`
- Password: JWT signed with device Ed25519 private key (64 bytes: seed||pubkey)
- Private key retrieved via text command `get prv.key\r\n` on serial transport (auto at startup)
- JWT claims: `publicKey`, `iat`, `exp`, `aud` (broker hostname)
- LetsMesh brokers: `mqtt-us-v1.letsmesh.net:443` and `mqtt-eu-v1.letsmesh.net:443` (WSS)

### Adapter — Meshtastic

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| meshtastic | `meshtastic` | any | Official Meshtastic Python library; serial/TCP/BLE connection and protobuf decoding |

**GitHub:** https://github.com/meshtastic/python — Meshtastic Python library

### Adapter — APRS

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aprslib | `aprslib` | any | APRS-IS connection and packet parsing |
| (none for KISS) | — | — | KISS TNC uses raw asyncio serial/TCP |

### Adapter — Reticulum

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| rns | `rns` | any | Reticulum Network Stack; required for reticulum_adapter.py |
| lxmf | `lxmf` | any | LXMF messaging over Reticulum |

**GitHub:** https://github.com/markqvist/Reticulum

### Adapter — AREDN

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aiohttp | `aiohttp` | any | Async HTTP client for AREDN node API polling |

### Adapter — Asterisk/PBX

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| panoramisk | `panoramisk` | any | Async Asterisk AMI (Manager Interface) client |

### Adapter — SMS

No additional packages beyond pyserial (via pyserial-asyncio-fast). Uses AT command protocol directly.

### Adapter — Pat Winlink

No additional packages. Uses Pat's HTTP REST API via stdlib `aiohttp` or `urllib`.

### Weather service

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aiohttp | `aiohttp` | any | api.weather.gov polling |

---

## Install command (server)

```bash
pip install fastapi "uvicorn[standard]" pyyaml aiosqlite \
    aiomqtt pyserial-asyncio-fast pycryptodome \
    meshtastic aprslib aiohttp \
    rns lxmf panoramisk meshcore
```

> **Note:** `meshcore` (the library) is not strictly required at runtime since ECH implements
> its own binary framing, but having it installed provides a useful reference and its
> dependencies (pycryptodome, pyserial-asyncio-fast) are needed by ECH directly.

---

## GitHub Projects / External Services

| Project | URL | Role in ECH |
|---------|-----|------------|
| meshcore_py | https://github.com/fdlamotte/meshcore_py | Protocol format authority; library source audited for field offsets |
| meshcoretomqtt | https://github.com/Cisien/meshcoretomqtt | Ed25519 JWT auth scheme for LetsMesh MQTT; ECH replicates auth_token.py logic |
| Meshtastic Python | https://github.com/meshtastic/python | Used directly via `import meshtastic` in meshtastic_adapter.py |
| Reticulum | https://github.com/markqvist/Reticulum | Used directly via `import RNS` in reticulum_adapter.py |
| Pat Winlink | https://github.com/la5nta/pat | External process; ECH calls its HTTP API |
| Direwolf | https://github.com/wb2osz/direwolf | External process; ECH connects to its KISS-over-TCP port |
| JS8Call | https://js8call.com | External process; ECH connects to its TCP API on port 2442 |

---

## LetsMesh / Community MQTT Services

| Service | Broker | Port | Transport | Auth |
|---------|--------|------|-----------|------|
| LetsMesh US | `mqtt-us-v1.letsmesh.net` | 443 | WebSocket+TLS | Ed25519 JWT (`v1_{pubkey}` / signed token) |
| LetsMesh EU | `mqtt-eu-v1.letsmesh.net` | 443 | WebSocket+TLS | Ed25519 JWT |
| Meshtastic public | `mqtt.meshtastic.org` | 1883 | TCP | Anonymous |

Topic format (LetsMesh): `meshcore/{IATA}/{PUBKEY_64HEX}/{packets|status|debug|raw}`

---

## Protocol References

| Protocol | Reference | ECH file |
|----------|-----------|----------|
| MeshCore Companion Protocol v1.15 | https://docs.meshcore.io/companion_protocol/ (and meshcore_py library source) | `ech/adapters/meshcore.py` |
| APRS IS | http://www.aprs-is.net/Connecting.aspx | `ech/adapters/aprs_is.py` |
| APRS KISS | https://www.ax25.net/kiss.aspx | `ech/adapters/aprs_kiss.py` |
| Meshtastic protobuf | https://meshtastic.org/docs/development/firmware/portnum/ | `ech/adapters/meshtastic_adapter.py` |
| MQTT 3.1.1 | https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/mqtt-v3.1.1.html | `ech/adapters/mqtt_adapter.py` |
| Reticulum / LXMF | https://reticulum.network/manual/ | `ech/adapters/reticulum_adapter.py` |
| Asterisk AMI | https://wiki.asterisk.org/wiki/display/AST/AMI+v2+Specification | `ech/adapters/asterisk_adapter.py` |
| AREDN node API | https://github.com/aredn/aredn/blob/develop/files/app/etc/api/ | `ech/adapters/aredn_ami.py` |
| JS8Call TCP API | https://groups.io/g/js8call (informal community docs) | `ech/adapters/mock_js8call.py` |

---

## Key Implementation Notes

### MeshCore private key retrieval
- **Serial transport**: ECH sends `get prv.key\r\n` as a text CLI command *before* `CMD_APP_START` switches to binary companion mode. Response contains 128-char hex key. Auto-retrieval fires at every adapter connect.
- **TCP transport (port 4403)**: binary-only — text CLI not available. User must run `get prv.key` once on the serial console and paste the result as `private_key:` in the MQTT adapter config. Keep this value in `/etc/ech/config.yaml` only (not in the repo).

### JWT token format (LetsMesh)
```
{base64url(header)}.{base64url(payload)}.{HEX_SIGNATURE}
header:  {"alg":"Ed25519","typ":"JWT"}
payload: {"publicKey":"<64HEX>","iat":<unix>,"exp":<unix>,"aud":"<broker-host>"}
signing: Ed25519 sign of "{header}.{payload}" bytes using device seed (privkey[:32])
```
Token is refreshed automatically before expiry (at 90% of `token_ttl`).

### pycryptodome Ed25519 usage
```python
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
seed = bytes.fromhex(privkey_128hex)[:32]   # first 32 bytes = seed
key  = ECC.construct(curve="Ed25519", seed=seed)
sig  = eddsa.new(key, "rfc8032").sign(message_bytes)
```
MeshCore stores a 64-byte extended private key (seed||pubkey); only the 32-byte seed is passed to pycryptodome.
