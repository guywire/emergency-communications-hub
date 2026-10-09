# ECH — External Dependencies

Last updated: 2026-07-08

This file tracks all external Python packages, GitHub projects, and protocol references
used by ECH. Update it whenever a new dependency is added or an existing one changes role.
The machine-readable authority is `pyproject.toml` `[project.dependencies]`; this file adds
the *why* and the external-process/protocol context that pyproject can't carry.

---

## Python Packages

### Core / always required

| Package | PyPI name | Version pinned | Purpose |
|---------|-----------|---------------|---------|
| FastAPI | `fastapi` | no | HTTP API and WebSocket server |
| Uvicorn | `uvicorn[standard]` | no | ASGI server |
| PyYAML | `pyyaml` | no | config.yaml parsing |
| aiosqlite | `aiosqlite` | no | async SQLite (message store, key-value state) |
| bcrypt | `bcrypt` | no | password hashing for web-UI auth |
| httpx | `httpx` | no | async HTTP client (weather, mesh bot lookups) |
| Jinja2 | `jinja2` | no | HTML template rendering |
| prometheus-client | `prometheus-client` | no | `/metrics` scrape endpoint (raw counters only; the in-app `/analytics` page charts from the SQLite DB instead) |
| psutil | `psutil` | no | disk/CPU stats for storage guard and health |
| croniter | `croniter` | no | scheduled-task cron expressions |
| smspdudecoder | `smspdudecoder` | no | SMS PDU decoding for the SMS adapter |
| numpy | `numpy` | **==1.26.4 on deploy** | transitive dep; install.sh pins <2 because numpy 2.x uses AVX2 and SIGILLs on the deployment host's SSE2-only CPU |

### Audio / sound card (cw_audio adapter)

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| sounddevice | `sounddevice` | ≥0.4 | PortAudio bindings: sound-card enumeration, capture, playback |

**System dependency:** `libportaudio2` (Debian) — the Linux pip wheel does not bundle
PortAudio; install.sh has a check-and-install block. CW DSP itself (`ech/core/cw.py`)
is pure numpy and has no audio-device dependency.

### Mesh bot features

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| adventure | `adventure` | ≥1.7 | Colossal Cave Adventure engine for the `mud` command (install.sh has an explicit install check — the deploy tarball doesn't carry pyproject.toml) |
| skyfield | `skyfield` | optional | `satpass` satellite pass prediction; TLE data downloaded from CelesTrak on first use |

### Adapter — MeshCore

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| meshcore | `meshcore` | 2.3.7 (inspected) | Reference library; protocol format authority. ECH implements its own binary framing rather than using the library directly, but the library source was used to verify packet structures (CONTACT record offsets, SELF_INFO layout, PUSH_CHANNEL_MSG format, channel decryption). |
| pyserial-asyncio | `pyserial-asyncio` | ≥0.6 | Async serial I/O for serial transport (corrected 2026-10-09 — `-fast` doesn't exist on PyPI at the version previously listed; the code only ever imports plain `serial_asyncio`, confirmed live) |
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
- Password: JWT signed with the device's own Ed25519 identity key — **on-device**, via the
  companion protocol's `CMD_SIGN_START/DATA/FINISH` (0x21/0x22/0x23; confirmed present and
  NOT gated behind a firmware flag, unlike `CMD_EXPORT_PRIVATE_KEY`). The raw key never
  leaves the radio and this works over serial, TCP, or BLE alike. A `private_key:` config
  fallback (manually exported via the MeshCore app) exists only for when the MeshCore
  adapter is disconnected at JWT-refresh time. (Superseded 2026-08-15/16 — there is no
  serial-only auto-retrieval of the raw key; that never actually worked, since companion
  firmware has no ASCII CLI at all.)
- JWT claims: `publicKey`, `iat`, `exp`, `aud` (broker hostname — LetsMesh's own example
  config sets `audience` to the exact broker hostname), `client` (always sent by the
  reference client; ECH was missing this until 2026-08-16), and optional `owner`/`email`
  (only sent when `tls: true`, matching the reference client's own guard — link an observer
  to a letsmesh.net dashboard account; confirmed cosmetic, not required for auth, per
  LetsMesh's own MQTT-observer docs)
- LetsMesh brokers: `mqtt-us-v1.letsmesh.net:443` and `mqtt-eu-v1.letsmesh.net:443` (WSS)
- **Known open issue (O75):** even with the corrected claim set, LetsMesh's broker still
  rejects ECH's JWT with `[code:135] Not authorized` as of 2026-08-16 — root cause not yet
  found; see `ECH_REQUIREMENTS_AND_PROGRESS.md`.

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

### Adapter — DMR/BrandMeister (Homebrew Repeater Protocol)

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| dmr_utils3 | `dmr_utils3` | ≥0.1.29 | BPTC(196,96) interleave/encode matrix primitives (`bptc.interleave_19696`/`encode_19696`, reused directly for data-block FEC, not just voice LC) and 3/4-byte DMR ID packing (`utils.bytes_3`/`bytes_4`) |
| bitarray | `bitarray` | ≥2.3.5 | Bit-level manipulation for BPTC matrix operations (a transitive dep of dmr_utils3/hblink3, used directly too) |
| libscrc | `libscrc` | any | CRC-16/GSM (`gsm16`) and CRC-32/POSIX (`posix`) — DMR data header and CSBK checksums (ETSI TS 102 361-1) |

**Protocol reference:** `HBLink-org/hblink3` (`hblink.py`, `const.py`) — the Homebrew/DMRplus
login handshake and DMRD frame layout were extracted directly from this source and confirmed
live against a real hblink3 master (2026-08-16), not re-derived from docs. See
`ech/adapters/dmr_brandmeister.py`'s module docstring for the exact byte layout.

**SMS codec reference:** `kf7eel/hbnet` (`data_gateway.py`, GPL-3.0) — the SMS/short-data
payload framing (data header, CSBK preamble, BTF/POC fragmentation, and hbnet's own extended
BPTC(196,96) decode that recovers a full 192-bit data block rather than just the 96-bit Link
Control subset dmr_utils3 exposes for voice) was vendored and adapted, with attribution, into
`ech/adapters/dmr_sms_codec.py`. Simplified vs. hbnet: always ETSI UTF-16BE (no per-destination
format learning) and a hand-rolled IPv4+UDP header instead of adding `scapy` as a dependency.
Verified via round-trip self-tests (`tests/test_dmr_sms_codec.py`) and a manual real
over-the-wire test — two `DMRBrandmeisterAdapter` instances relaying an SMS through a live
hblink3 master (2026-08-16) — since there's no live DMR SMS traffic available to check against
otherwise.

**Test infrastructure:** a private hblink3 master on an operator-provided Ubuntu 22.04 box
(separate from the live ECH server), used to validate this adapter before ever touching a real
DMR network (TGIF, BrandMeister). See `ECH_REQUIREMENTS_AND_PROGRESS.md`'s DMR section.

### Adapter — ADS-B (PiAware / dump1090 / tar1090 / readsb)

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aiohttp | `aiohttp` | any | HTTP polling of dump1090 `aircraft.json` feed (auto-detects /skyaware, /tar1090, /dump1090 paths) |

**External process:** Any dump1090-compatible receiver (PiAware, dump1090-fa, tar1090, readsb) reachable on the LAN via HTTP. ECH polls `aircraft.json` — no SDR or ADS-B decoding happens inside ECH.

**Mesh bot `overhead` command:** reads `aircraft.json` from local filesystem path (`dump1090_path` in `mesh_bot:` config); does not use aiohttp. Default path: `/run/dump1090-fa/aircraft.json`.

### Adapter — AIS (AIS-catcher)

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aiohttp | `aiohttp` | any | HTTP polling of AIS-catcher vessel JSON API |

**External process:** [AIS-catcher](https://github.com/jvde-github/AIS-catcher) with `--server` / HTTP output mode enabled (default port 8100). ECH polls `/vessels.json` (or auto-detected path). No SDR decoding happens inside ECH.

### Adapter — AIS (internet feeds: AISHub / aisstream.io)

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aiohttp | `aiohttp` | any | `aishub` adapter: REST polling of aishub.net; `aisstream` adapter: WebSocket stream from aisstream.io |

**External services:** both need a free account/API key (config `api_key:`). Keep keys in `/etc/ech/config.yaml` only — never in the repo.

### Adapter — AREDN

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| aiohttp | `aiohttp` | any | Async HTTP client for AREDN node API polling |

### Adapter — Asterisk/PBX

| Package | PyPI name | Version | Purpose |
|---------|-----------|---------|---------|
| panoramisk | `panoramisk` | any | Async Asterisk AMI (Manager Interface) client |

### Adapter — SMS

No additional packages beyond pyserial (via pyserial-asyncio). Uses AT command protocol directly.

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
    aiomqtt pyserial-asyncio pycryptodome \
    meshtastic aprslib aiohttp \
    rns lxmf panoramisk meshcore
```

> **Note:** `meshcore` (the library) is not strictly required at runtime since ECH implements
> its own binary framing, but having it installed provides a useful reference and its
> dependencies (pycryptodome, pyserial-asyncio) are needed by ECH directly.

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
| AIS-catcher | https://github.com/jvde-github/AIS-catcher | External process; ECH polls its HTTP JSON API for vessel positions |
| dump1090-fa / PiAware | https://github.com/flightaware/dump1090 | External process; ECH polls aircraft.json for ADS-B positions and mesh bot `overhead` |
| tar1090 / readsb | https://github.com/wiedehopf/tar1090 | Alternative dump1090-compatible ADS-B feed; same aircraft.json format |

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
