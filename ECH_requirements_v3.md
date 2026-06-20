# Emergency Communications Hub (ECH) — Requirements v3.0
# Prepared for Claude Code handoff

## Current version: v1.0.0-rc3 (local) / v1.0.0-rc2 (live server as of 2026-06-20)
## See WORK_STATUS.md for deployment status and resume instructions

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

## 5. Bug Tracker

### Fixed in v1.0.0-rc1 / v0.9.x

**SIM-1: Simulation pause doesn't stop mock messages**
- Status: ✅ Fixed v0.9.9 — all mock adapters check `if self._paused: continue` at top of loop

**SIM-2: Edit simulation nodes doesn't work**
- Status: ✅ Fixed v0.9.9 — nodes stored in `_nodeStore` dict, buttons use `editNodeById(id)`

**SIM-3: Mock nodes default to ocean (0,0)**
- Status: ✅ Fixed v0.9.9 — mock adapters accept `base_lat`/`base_lon` config keys

**UI-1 thru UI-5:** ✅ Fixed v0.9.9 (clear display, OP callsign, UTC/LOCAL toggle, colors, favicon)

**SVC-1: Service status shows "Failed to load"**
- Status: ✅ Fixed v0.9.9 — uses /bin/systemctl full path. rc3 improves error message to show HTTP status code.
- If still failing: `sudo usermod -aG systemd-journal ech`

**AUTH-1, WL-1, MAP-1:** ✅ Fixed in earlier versions

**WISH-1/2/3:** ✅ PSKReporter, map colors, MQTT status added v0.9.9

### Fixed in v1.0.0-rc2 (deployed 2026-06-20)

**ADAPT-1: Real adapter crashes entire startup**
- Root cause: `import serial_asyncio` at module level in meshcore.py, aprs_kiss.py, at_engine.py
- Fix: build_adapter() uses `importlib.import_module()` (lazy). serial_asyncio moved inside connect()
- Files: ech/main.py, ech/adapters/meshcore.py, ech/adapters/aprs_kiss.py, ech/adapters/at_engine.py

**AUTH-2: Login sets cookie but redirect loops back to /login**
- Root cause: JS fetch() + window.location pattern unreliable with SameSite=Lax across some browsers
- Fix: Login form uses HTML form POST → HTTP 303 + Set-Cookie in one response
- Files: ech/ui/templates/login.html, ech/api/app.py

**PSK-1: PSKReporter no rate limiting + JSON parse error**
- Root cause: No caching; also PSKReporter returns XML not JSON; encap=1 wrapped in CDATA
- Fix: Module-level 5-min TTL cache; switched to xml.etree.ElementTree parsing
- File: ech/api/app.py

**LOC-1: base_lat/base_lon requires config.yaml edit**
- Fix: "BASE LOCATION" section in Settings page with GPS detect + save buttons
- Files: ech/ui/templates/settings.html, ech/api/app.py

**SVC-2: Service restart buttons fail (sudo requires password)**
- Fix: install.sh writes /etc/sudoers.d/ech-services (passwordless whitelist)
- File: scripts/install.sh

### Fixed in v1.0.0-rc3 (local only, NOT YET deployed — see WORK_STATUS.md)

**SIM-PAUSE: Simulation state not propagated on restart**
- Root cause: state.py init() loaded simulation_enabled from DB but never called set_simulation(),
  so adapters defaulted to _paused=False even when simulation was disabled.
- Fix: init() iterates router._adapters and sets _paused=True if simulation_enabled is False
- File: ech/core/state.py

**CLOCK: UTC clock doesn't switch to local time**
- Root cause: tick() hardcoded UTC without checking _showLocal flag
- Fix: tick() branches on _showLocal, uses Intl.DateTimeFormat for timezone name
- File: ech/ui/templates/index.html

**OP-CS: Operator callsign uses browser prompt() (unreliable)**
- Root cause: promptOperator() used window.prompt() which can be blocked by browsers
- Fix: Inline input field appears in header span on click; saves on Enter/blur; Escape cancels
- File: ech/ui/templates/index.html

**SVC-ERR: Service load failure shows generic message**
- Fix: loadServices() checks r.ok and shows actual HTTP status code in error message
- File: ech/ui/templates/settings.html

**MOCK-DISASTER: Simulation messages generic, not scenario-relevant**
- Fix: All 6 mock adapters updated with cold weather disaster scenario
  (frozen pipes, warming centers, trees down from storm, Canadian lobster poachers)
- Also: base_lat: 44.1059 / base_lon: -69.1128 added to all mock adapters in config.yaml
- Files: all ech/adapters/mock_*.py, config.yaml

### Open / Next Sprint

**SEC-01 P0: Default admin password** — force password change on first login
**SEC-02 P0: No HTTPS** — add TLS cert generation to install.sh
**SEC-03 P1: No role enforcement** on /api/users, /api/adapter-config, service restart
**SEC-04 P1: No login brute-force protection**
**SEC-08 P2: DBLogHandler asyncio loop param** — crashes Python 3.12+
**MQTT-TEST: Real adapter MQTT posting** — not yet tested on live hardware
**MEM-RELOAD: /api/base-location** should update live adapter positions without restart

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
