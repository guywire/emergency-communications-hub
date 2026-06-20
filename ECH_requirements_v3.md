# Emergency Communications Hub (ECH) — Requirements v3.0
# Prepared for Claude Code handoff

## Current version: v0.9.8 → v1.0.0-rc1

---

## 1. Architecture Summary

**Stack:** Python 3.11+, FastAPI, asyncio, SQLite (aiosqlite), Uvicorn, HTMX-style vanilla JS UI  
**Target:** Debian 12 minimal, Raspberry Pi 4/5, AMD G-T48E thin client, Proxmox LXC  
**Entry point:** `ech/main.py` → `create_app()` in `ech/api/app.py`  
**UI:** Server-side HTML templates in `ech/ui/templates/`, no npm build step  
**Config:** `/etc/ech/config.yaml` (system) or `config.yaml` (dev)  
**Database:** SQLite at path from config, default `ech.db`  

## 2. Adapter Registry

All adapters inherit from `ech/adapters/base.py:Adapter`. Registered in `ech/main.py:build_adapter()`.

| Config type | Module | Status |
|---|---|---|
| `meshtastic` | `adapters/meshtastic_adapter.py` | Real (requires pyserial + meshtastic lib) |
| `mock_meshtastic` | `adapters/mock_meshtastic.py` | Mock — supports `base_lat`, `base_lon`, `node_count` config keys for concentric node positions |
| `meshcore` | `adapters/meshcore.py` | Real (serial/TCP) |
| `mock_meshcore` | `adapters/mock_meshcore.py` | Mock — supports `base_lat`, `base_lon` |
| `aprs_is` | `adapters/aprs_is.py` | Real (internet) |
| `mock_aprs` | `adapters/mock_aprs.py` | Mock |
| `aprs_kiss` | `adapters/aprs_kiss.py` | Real (TNC) |
| `js8call` | `adapters/js8call.py` | Real (localhost API) |
| `mock_js8call` | `adapters/mock_js8call.py` | Mock |
| `sms` | `adapters/sms.py` | Real (AT modem) |
| `mock_sms` | `adapters/mock_sms.py` | Mock |
| `pat_winlink` | `adapters/pat_winlink.py` | Real (Pat HTTP API on :8080) |
| `mock_pat_winlink` | `adapters/mock_pat_winlink.py` | Mock |
| `reticulum` | `adapters/reticulum_adapter.py` | Real (requires `pip install rns lxmf`) |
| `mock_reticulum` | `adapters/reticulum_adapter.py:MockReticulumAdapter` | Mock |
| `mqtt` | `adapters/mqtt_adapter.py` | Real (aiomqtt, multiple brokers) |
| `mock_mqtt` | `adapters/mqtt_adapter.py:MockMQTTAdapter` | Mock |
| `aredn_ami` | `adapters/aredn_ami.py` | Real (Asterisk AMI TCP) |
| `mock_aredn_ami` | `adapters/aredn_ami.py:MockAREDNAMIAdapter` | Mock |

## 3. Core Modules

| Module | Purpose |
|---|---|
| `core/database.py` | SQLite — messages, nodes, sessions, users, anomalies, kv_store, sim_nodes/messages, logs |
| `core/router.py` | Async fan-in, dedup, WS broadcast, bridge rules, metrics hooks, meshcore bridge hook |
| `core/auth.py` | bcrypt sessions, admin/operator roles, session cookie middleware |
| `core/state.py` | Operational mode (standard/emergency), simulation pause/resume, incident name, operator callsign |
| `core/anomaly.py` | RF anomaly detection engine (high altitude, impossible jump, MQTT injection, ducting, stale position) |
| `core/weather.py` | NWS CAP alert polling (api.weather.gov, no key), time sync, scheduled broadcasts |
| `core/metrics.py` | Prometheus metrics (`/metrics` endpoint) |
| `core/meshcore_bridge.py` | MeshCore → MeshMapper MQTT republisher |

## 4. Web Pages

| URL | Template | Purpose |
|---|---|---|
| `/` | `index.html` | Main dashboard — unified inbox, compose, node panel, system stats bar |
| `/login` | `login.html` | Session auth |
| `/anomalies` | `anomalies.html` | RF anomaly monitor, acknowledge, broadcast |
| `/map` | `map.html` | Leaflet node map, PSKReporter overlay, Maidenhead grid |
| `/logs` | `logs.html` | Live log viewer, severity filter, download |
| `/settings` | `settings.html` | Service status, mode, weather, adapters, users, MQTT bridge |
| `/simulation` | `simulation.html` | Mock node/message editor, simulation pause, inject |

## 5. Known Bugs To Fix (Claude Code)

### High priority

**SIM-1: Simulation pause doesn't stop mock messages**
- Root cause: state.py was cancelling _run_task which mock adapters restart; now uses `_paused` flag on base Adapter
- Status: Fixed in v0.9.9 — all mock adapters check `if self._paused: continue` at top of loop
- Test: Toggle simulation off → verify no new messages appear in inbox for 30s

**SIM-2: Edit simulation nodes doesn't work**
- Root cause: JSON.stringify in onclick attribute gets corrupted by HTML escaping
- Status: Fixed in v0.9.9 — nodes stored in `_nodeStore` dict, buttons use `editNodeById(id)`
- Test: Add a node, click Edit, verify form populates

**SIM-3: Mock nodes default to ocean (0,0)**
- Root cause: Removed hardcoded positions without adding configurable defaults
- Status: Fixed in v0.9.9 — mock adapters accept `base_lat`/`base_lon` config keys and generate concentric circle positions
- Action needed: Operator configures base location once in config.yaml

**UI-1: Clear message display does nothing**
- Root cause: clearMessageDisplay() wasn't clearing `state.messages` array, only the DOM
- Status: Fixed in v0.9.9

**UI-2: OP: W1ABC hardcoded in header**
- Root cause: Hardcoded default string, operator callsign not loaded from state on page load
- Status: Fixed in v0.9.9 — click the OP: display to set callsign, persisted in DB

**UI-3: Timezone always UTC, no option for local**
- Status: Fixed in v0.9.9 — UTC/LOCAL toggle button in header, persisted in localStorage

**UI-4: Meshtastic and MeshCore same color on map**
- Status: Fixed in v0.9.9 — Meshtastic=green (#3fb950), MeshCore=orange (#f78c6c)

**UI-5: Favicon missing from map, logs, simulation pages**
- Status: Fixed in v0.9.9

**SVC-1: Service status shows "Failed to load"**
- Root cause: systemctl not in uvicorn's PATH; also ech user needs systemd read permission
- Status: Fixed in v0.9.9 — uses /bin/systemctl full path
- May still need: `sudo usermod -aG systemd-journal ech`

**PKG-1: `{ech` directory appearing in tarballs**
- Root cause: tar glob `ech/**` was picking up a literal `{ech` directory at the same level
- Status: Fixed in v0.9.9 — `{ech` directory deleted, tar exclude patterns tightened

### Medium priority

**AUTH-1: Login page appears but unauthenticated requests weren't redirected**
- Status: Fixed in v0.9.8 — FastAPI middleware added

**WL-1: Winlink send returns 400 "Missing date value"**  
- Status: Fixed in v0.9.4 — `date` field added to outbox POST payload

**MAP-1: Map doesn't show nodes because positions removed**
- Status: Fixed in v0.9.9 — nodes use concentric positions when base_lat/base_lon configured

### Low priority / Wishlist

**WISH-1: PSKReporter/GridTracker callsign spots on map**
- Status: Added in v0.9.9 — PSK button on map fetches PSKReporter spots via backend proxy; GRID button shows Maidenhead grid squares

**WISH-2: Different colors per adapter type on map**
- Status: Fixed in v0.9.9

**WISH-3: MQTT settings visible in web UI**
- Status: Added in v0.9.9 — Settings page shows MQTT adapter status and MeshMapper bridge health

## 6. Configuration Reference

### Mock adapter base location (for map display)

```yaml
adapters:
  - type: mock_meshtastic
    name: meshtastic-mock
    interval_sec: 8.0
    node_count: 5
    base_lat: 44.105     # your location decimal degrees
    base_lon: -69.112    # nodes arranged in concentric circles around this
```

### MeshCore → MeshMapper MQTT bridge

```yaml
meshcore_bridge:
  enabled: true
  mqtt_host: localhost       # or mqtt.letsmesh.com for EastMesh
  mqtt_port: 1883
  topic_prefix: meshcore     # MeshMapper subscribes to meshcore/#
  adapter_name: meshcore-usb # ECH MeshCore adapter to mirror
  publish_decoded: true
```

### MQTT adapter (multiple broker support)

```yaml
  - type: mqtt
    name: meshtastic-mqtt
    host: mqtt.meshtastic.org
    port: 1883
    topics: ["msh/US/ME/#"]    # customize for your region

  - type: mqtt
    name: letsmesh-us
    host: mqtt.letsmesh.com
    port: 1883
    topics: ["meshcore/+/packets"]
```

### Operator / incident (set via UI or config)

```yaml
operator:
  callsign: W1ABC     # also settable by clicking OP: in header
  incident: EXERCISE  # also settable in Settings page
```

## 7. Installation

```bash
# Standard install
sudo bash scripts/install.sh

# Asterisk VoIP (builds from source, ~25 min)
sudo bash scripts/install_asterisk.sh

# Mosquitto MQTT broker (if not installed by main script)
sudo apt install mosquitto mosquitto-clients -y
sudo systemctl enable mosquitto && sudo systemctl start mosquitto

# Pat Winlink client
wget https://github.com/la5nta/pat/releases/download/v1.0.0/pat_1.0.0_linux_amd64.tar.gz
tar -xzf pat_1.0.0_linux_amd64.tar.gz && sudo install pat /usr/local/bin/
sudo -u ech pat configure
sudo cp systemd/pat.service /etc/systemd/system/
sudo systemctl enable pat && sudo systemctl start pat
```

## 8. Service Status

| Service | Port | Check | Install |
|---|---|---|---|
| ECH | 8765 | `systemctl status ech` | `bash scripts/install.sh` |
| Pat | 8080 | `systemctl status pat` | see above |
| Asterisk | 5060/5038 | `systemctl status asterisk` | `bash scripts/install_asterisk.sh` |
| Mosquitto | 1883 | `systemctl status mosquitto` | `apt install mosquitto` |
| Prometheus | 9090 | `systemctl status prometheus` | see install script |
| avahi | mdns | `systemctl status avahi-daemon` | installed by main script |

## 9. Phase Roadmap

### Phase 9 — Stability (current)
- Fix all bugs listed in section 5
- Auth enforcement throughout
- Simulation fully controllable from UI

### Phase 10 — Operational features
- ICS-309 communications log auto-export (PDF/CSV)
- Net control check-in tracker (who has checked in, overdue stations)
- AIS vessel position integration (coastal SAR use case)
- SAME/EAS decoder via RTL-SDR soundcard input
- Offline map tiles (MBTiles + local tile server)

### Phase 11 — Intelligence
- Message triage/classification with local Ollama (Llama 3.2 3B)
- Predictive RF anomaly (statistical baseline per node)
- Voice-to-message transcription (Whisper.cpp on Pi 5)

### Phase 12 — Federation
- ECH-to-ECH relay over LXMF propagation nodes or Winlink
- Matrix/Element bridge for served-agency internet channel
- AREDN mesh IP tunnel between EOC sites
