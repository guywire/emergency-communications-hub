# ECH Requirements and Progress Tracker

Last updated: 2026-06-30  
Based on: `ECV_v3_fixes.txt`, `ECH ADAPTER ENHANCEMENT REQUIREMENT.txt`, real-world MeshCore session observations, meshcoretomqtt JWT auth analysis, live deployment integration testing (Asterisk AMI, ADS-B, AIS-catcher)

---

## New Requirements (2026-06-22)

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| N1 | Simulation/live toggle separates real vs mock adapters — sim ON pauses real adapters, live ON pauses mocks | DONE | `state.py` `set_simulation()` |
| N2 | Select All / Select None buttons in compose adapter picker | DONE | `index.html` compose panel |
| N3 | Filter APRS adapters out of anomaly detection | DONE | `anomaly.py` `_is_mesh_adapter()` |
| N4 | Meshtastic position packets hidden from message feed (map/anomaly only) | DONE | `models.py` `msg_type`, `meshtastic_adapter.py`, `index.html` |
| N5 | Sent message status: hourglass stuck — now shows ✈/✓✓/✗ | DONE | `router.py` `send_tracked()` |
| N6 | Meshtastic channel config from settings: TX channel, monitored channels, PSK push to device | DONE | `meshtastic_adapter.py`, `app.py`, `settings.html` |

---

## Session Issues (Real-World MeshCore Observations) — Triage

| # | Issue | Status | File(s) |
|---|-------|--------|---------|
| S1 | No anomaly for "36k feet" in message body | DONE | `ech/core/anomaly.py` |
| S2 | Timestamps show device clock, not receive time | DONE | `ech/adapters/meshcore.py` |
| S3 | Hex node IDs not resolving to names in node list | DONE | `ech/adapters/meshcore.py` |
| S4 | Hop badge / hourglass disappears on tab switch | DONE | `ech/core/database.py` |
| S5 | Transmitted message never shows delivery receipt | DONE | `ech/core/database.py`, `ech/core/router.py`, `index.html` |
| S6 | Enable/disable adapters through web interface | DONE | `ech/ui/templates/settings.html` (Pause/Resume button, was mock-only) |
| S7 | Select MeshCore channels like #testing from UI | DONE | `app.py` `/api/adapters/{name}/channel`, `settings.html` channel switcher |
| S8 | Not receiving discovery messages from MeshCore nodes | DONE | `ech/adapters/meshcore.py` — periodic discovery pulse every 300s + Discover button |
| S9 | Routing path should show hop count inline; click for relay detail | DONE | `index.html` — `N hop(s)` badge, detail panel with timestamps |
| S10 | Fix MQTT bridge: correct topics and format per each server's spec | DONE | `ech/core/meshcore_bridge.py` — letsmesh format, correct topic schema |
| S11 | Anomaly WS events not showing in UI | DONE | `index.html` — `handleAnomaly()` toast added |

---

## MeshCore Adapter — Feature Matrix

### Channel Management

| Feature | Implemented | Notes |
|---------|-------------|-------|
| Enumerate channels (0–7) via CMD_GET_CHANNEL | YES | startup init sequence |
| Current channel display in UI | YES | settings.html adapter row |
| Switch channel by name (#testing) | YES | `/api/adapters/{name}/channel` |
| Switch channel by index | YES | same endpoint |
| Channel validation / name resolution | YES | `_try_resolve_channel_name()` |
| Auto-switch to configured channel_name at startup | YES | post-handshake resolution |

### Node Discovery

| Feature | Implemented | Notes |
|---------|-------------|-------|
| PUSH_ADVERT parsing → node table | YES | `_dispatch_frame()` PUSH_ADVERT handler |
| Auto-register senders not yet in node table | YES | `_handle_channel_msg` / `_handle_contact_msg` |
| Name extraction from PUSH_ADVERT payload | YES | `_scan_ascii_name()` |
| Update node name when real name arrives | YES | PUSH_ADVERT handler updates display_name |
| last_heard tracking | YES | updated on every advert and message |
| Periodic discovery pulse (APP_START + DEVICE_QUERY) | YES | `_discovery_pulse()` every 300s |
| On-demand Discover button in settings UI | YES | settings.html → `/api/adapters/{name}/discover` |
| Detect node departures | DEFERRED | Companion Protocol doesn't push departure events; would need timeout-based inference |

### Neighbor / RF Information

| Feature | Implemented | Notes |
|---------|-------------|-------|
| SNR on received messages | PARTIAL | stored in raw_json; not yet surfaced in UI |
| RSSI on received messages | PARTIAL | stored in raw_json; not yet surfaced in UI |
| Hop count from path_len field | YES | `NormalizedMessage.hop_count` + UI badge |
| Hop start (max hops) | YES | `NormalizedMessage.hop_start` |
| Relay node names in path | DEFERRED | Protocol only provides path_length; individual relay node IDs not exposed by Companion Protocol |

### Presence

| Feature | Implemented | Notes |
|---------|-------------|-------|
| Online state | YES | `connected` flag in health |
| Last heard timestamp per node | YES | `MeshNode.last_heard` |
| Offline detection | DEFERRED | timeout-based; not yet implemented |

### Telemetry

| Feature | Implemented | Notes |
|---------|-------------|-------|
| Battery (CMD_GET_BATTERY) | PARTIAL | command exists, not polled periodically |
| GPS from structured packets | YES | lat/lon extracted to NormalizedMessage |
| GPS / altitude from free-text | YES | anomaly engine regex extraction |
| Voltage | PENDING | not implemented |
| Environmental sensors | PENDING | not in Companion Protocol spec |

### Message Features

| Feature | Implemented | Notes |
|---------|-------------|-------|
| Channel broadcast messages | YES | PACKET_CHANNEL_MSG_V3 |
| Direct messages | YES | PACKET_CONTACT_MSG_V3 |
| Send channel message | YES | CMD_SEND_CHANNEL_MSG |
| Delivery acknowledgement (PUSH_SEND_CONFIRMED) | YES | fires `_router_notify` → WS + DB persist |
| Delivery status survives navigation | YES | `db.update_delivery_status()`, history API returns it |
| Message received at ECH time (not device clock) | YES | `datetime.now(timezone.utc)` in handler |
| Device clock visible in message detail | YES | raw.packet_timestamp shown in path detail panel |

### MQTT Bridge (MeshCore → MeshMapper / LetsMesh)

| Feature | Implemented | Notes |
|---------|-------------|-------|
| Correct topic format `meshcore/{IATA}/{PUBKEY}/packets` | YES | `meshcore_bridge.py` |
| `/status` topic (retained) | YES | published on connect + every 300s |
| `/decoded` topic (alias) | YES | same payload as `/packets` |
| letsmesh packet JSON format | YES | origin, origin_id, timestamp, type, direction, etc. |
| Packet type inference (GRP_TXT/TXT_MSG/ADVERT) | YES | `_infer_packet_type()` |
| WebSocket transport support | YES | `transport="websockets"` via aiomqtt |
| TLS support | YES | aiomqtt TLSParameters |
| Username/password auth | YES | config keys mqtt_username/mqtt_password |
| JWT auth (LetsMesh / meshcoretomqtt scheme) | DONE | `mqtt_adapter.py` — Ed25519 JWT via pycryptodome; serial auto-retrieves privkey, TCP requires config |
| Auto-detect device name from PACKET_SELF_INFO | YES | `_sync_device_info()` |
| IATA code config | YES | `iata_code` config key |
| device_pubkey config | YES | falls back to MD5-hash of device name if not set |
| Bridge status API `/api/meshcore-bridge` | YES | `app.py` |
| Enable/disable bridge API | YES | `/api/meshcore-bridge/enable` |

### Health / Diagnostics

| Feature | Implemented | Notes |
|---------|-------------|-------|
| Connected state | YES | `health()` → `connected` |
| Node count | YES | `health_detail()` → `node_count` |
| Active channel | YES | `health_detail()` → `channel_name` |
| Hardware model | YES | from PACKET_SELF_INFO |
| Firmware version | YES | from PACKET_SELF_INFO |
| Discovery interval | YES | `health_detail()` → `discovery_interval` |
| Log channel changes | YES | INFO log |
| Log discovery events | YES | DEBUG log |
| Log protocol errors | YES | ERROR log |
| Last discovery timestamp | PENDING | not yet tracked and returned |
| Last message timestamp | PARTIAL | not in health endpoint, in messages table |

---

## UI Enhancements

| Feature | Status | Notes |
|---------|--------|-------|
| Adapter Control section loads on init | DONE | was never called; fixed in settings.html |
| Pause/Resume for all adapters (not just mocks) | DONE | settings.html |
| Channel switcher input for real MeshCore adapters | DONE | settings.html |
| "Discover" button to trigger discovery pulse | DONE | settings.html → `/api/adapters/{name}/discover` |
| Anomaly toast with severity icon on WS event | DONE | index.html `handleAnomaly()` |
| Hop badge shows N hop(s) | DONE | index.html |
| Path detail shows device vs. ECH timestamps | DONE | index.html |
| Clock diff indicator in path detail | DONE | index.html |
| Delivery status badge (live, from WS) | DONE | index.html (existing) |
| Delivery status badge (persisted, from history) | DONE | index.html reads `delivery_status` from DB |
| MQTT preset buttons: subscribe + publish sections | DONE | settings.html |
| Meshtastic topic format corrected | DONE | `msh/US/2/json/LongFast/#` |
| LetsMesh presets (US + EU) with correct topic format | DONE | `meshcore/+/+/packets` |
| MeshMapper bridge preset (publish via bridge) | DONE | writes `meshcore_bridge:` config |
| Mobile responsive layout | DONE | index.html — single-column stacking, slide-in sidebar/right-panel overlays at ≤768px |
| Mobile JS panel toggle functions | DONE | `mobileToggleSidebar()`, `mobileToggleNodes()`, `mobileClosePanels()` |
| Node names from GET_CONTACTS (not message text) | DONE | meshcore.py — `CMD_GET_CONTACTS` at init + discovery pulse; adv_name at contact record offset 99 |
| Encrypted channel message detection | DONE | meshcore.py — `_is_likely_encrypted()` UTF-8 replacement char ratio >20% → `[Encrypted channel message]` |
| Spurious node entries from polled message parsing | DONE | meshcore.py — 0x11/0x08 polled formats have no pubkey; sender only registered from 0x88 push |

---

## Adapter Enhancement — Other Adapters

### Meshtastic

| Feature | Status | Notes |
|---------|--------|-------|
| Text messages | DONE | meshtastic_adapter.py |
| Position/GPS | DONE | `_on_position()` handler |
| Node info (NodeInfo portnum) | PARTIAL | basic name extraction |
| Telemetry (DeviceMetrics, EnvironmentMetrics) | PENDING | not yet decoded |
| NeighborInfo | PENDING | not yet decoded |
| Traceroute | PENDING | not yet decoded |
| Waypoints | PENDING | not yet decoded |
| via_mqtt flag | DONE | `viaMqtt` field in packet |
| Hop count | DONE | `hopLimit` field |

### Reticulum

| Feature | Status | Notes |
|---------|--------|-------|
| LXMF message receive | DONE | reticulum_adapter.py |
| Announce handling | PARTIAL | basic |
| Identity management | PARTIAL | |
| Path requests / discovery | PENDING | |
| Delivery receipts | PENDING | |
| Propagation / transport node info | PENDING | |

### APRS-IS / APRS-KISS

| Feature | Status | Notes |
|---------|--------|-------|
| Message receive | DONE | aprs_is.py, aprs_kiss.py |
| Position decode | DONE | |
| Object/item decode | PARTIAL | |
| Weather decode | PARTIAL | |
| Telemetry decode | PENDING | |
| Station directory population | PARTIAL | |

### JS8Call

| Feature | Status | Notes |
|---------|--------|-------|
| Message receive | DONE | mock_js8call.py |
| Heartbeat / directed messages | PARTIAL | |
| Grid square / position | PENDING | |
| Station network topology | PENDING | |

### Pat Winlink

| Feature | Status | Notes |
|---------|--------|-------|
| Message send/receive | DONE | mock_pat_winlink.py |
| RMS discovery | PENDING | |
| Session status | PENDING | |
| Transfer progress | PENDING | |
| Inbox/outbox monitoring | PENDING | |

---

## Session Work Log — 2026-06-30 (Asterisk / ADS-B / AIS / Map)

| Fix | File(s) | Root Cause |
|-----|---------|------------|
| Asterisk AMI EOF on login | `asterisk_adapter.py` | `_send_action()` joined lines with `\r\n` but omitted the mandatory blank-line terminator; Asterisk waited for `\r\n\r\n`, timed out, closed socket → EOF. Fixed: `"\r\n".join(lines) + "\r\n"` |
| Services panel showing `?` for all statuses | `index.html` | `loadServicesPanel()` read `s.state` but API returns `s.active`. All states were `undefined` → fell through to `'?'` fallback. Fixed: use `s.active` |
| ADS-B / AIS adapters silently "connected" with no data | `adsb_adapter.py`, `ais_catcher_adapter.py` | `aiohttp` import check was inside `_run()` (fire-and-forget task); adapter showed green/connected but task exited immediately. Fixed: check at `connect()` time, raise `ConnectionError` with clear pip message |
| `Priority.LOW` AttributeError in both adapters | `adsb_adapter.py`, `ais_catcher_adapter.py` | `Priority` IntEnum has NORMAL/ELEVATED/EMERGENCY only — no LOW. `AttributeError("LOW")` stringified to "LOW" in the poll-error log. Fixed: use `Priority.NORMAL` |
| Aircraft / vessels not shown on map | `map.html` | No color entries for `adsb`/`ais` in `adapterColor()`; no distinct icons. Fixed: amber ✈ for ICAO nodes, teal ⚓ for MMSI nodes; added ✈ ADSB and ⚓ AIS toggle buttons; legend updated |
| README missing ADS-B, AIS, and full mesh bot config | `README.md` | New sections added: ADS-B/PiAware, AIS-catcher (incl. install cmd), full mesh_bot config with observer position, `overhead` path table by install type, `satpass` TLE targets and skyfield dependency |

---

## Adapter Enhancement — ADS-B

| Feature | Status | Notes |
|---------|--------|-------|
| Poll dump1090/PiAware aircraft.json | DONE | `adsb_adapter.py` — auto-detects /skyaware, /tar1090, /dump1090, /dump1090-fa paths |
| Aircraft on map as amber ✈ icon | DONE | `map.html` `makeNodeIcon()` — ICAO node_id prefix |
| ADS-B toggle button on map | DONE | `map.html` — ✈ ADSB button, grey when off |
| FlightAware / ADS-B Exchange popup links | DONE | `map.html` `popupContent()` — meta.icao branch |
| Fail fast if aiohttp missing | DONE | `connect()` raises ConnectionError with pip hint |
| Mesh bot `overhead` command | DONE | `mesh_bot.py` reads dump1090 JSON from local filesystem |

## Adapter Enhancement — AIS (AIS-catcher)

| Feature | Status | Notes |
|---------|--------|-------|
| Poll AIS-catcher HTTP vessel feed | DONE | `ais_catcher_adapter.py` — auto-detects /vessels.json, /ships.json, /json |
| Vessels on map as teal ⚓ icon | DONE | `map.html` `makeNodeIcon()` — MMSI node_id prefix |
| AIS toggle button on map | DONE | `map.html` — ⚓ AIS button, grey when off |
| VesselFinder / MarineTraffic popup links | DONE | `map.html` `popupContent()` — meta.mmsi branch |
| AtoN (aid-to-navigation) MMSI detection | DONE | MMSI starting 99 → OpenSeaMap link |
| Ship type / nav status / speed in popup | DONE | meta fields parsed from AIS-catcher JSON |
| Fail fast if aiohttp missing | DONE | `connect()` raises ConnectionError with pip hint |
| Dynamic vessel eviction (stale_sec) | DONE | `_ingest()` — evicts vessels not seen in last poll |

## Adapter Enhancement — Asterisk/PBX

| Feature | Status | Notes |
|---------|--------|-------|
| AMI login | DONE | `asterisk_adapter.py` `_send_action()` — correct `\r\n\r\n` termination |
| Call event logging (Newchannel/Hangup) | DONE | `_on_new_channel()`, `_on_hangup()` |
| Click-to-call (Originate) | DONE | `originate()` method |
| Page / announce | DONE | `page()` method |
| Active call count in health | DONE | `_health_detail()` |

---

## Open Issues / Deferred Work

| ID | Description | Priority | Complexity |
|----|-------------|----------|------------|
| O1 | Node departure detection (timeout-based offline inference) | P2 | Low |
| O2 | Individual relay node IDs in received channel msg path | P3 | Blocked — Companion Protocol does not carry relay IDs in PUSH_CHANNEL_MSG/CHANNEL_MSG_RECV_V3; only hop_count available; use SEND_TRACE_PATH (0x24) → TRACE_DATA (0x89) for explicit path probing |
| O3 | Battery polling on schedule (CMD_GET_BATTERY every N min) | P2 | Low |
| O4 | JWT auth for MeshMapper/LetsMesh (requires device private key) | P2 | High |
| O5 | Meshtastic telemetry decode (DeviceMetrics, EnvironmentMetrics) | P2 | Medium |
| O6 | Meshtastic NeighborInfo and Traceroute decode | P2 | Medium |
| O7 | Reticulum delivery receipts | P3 | Medium |
| O8 | Last discovery timestamp in health endpoint | P3 | Low |
| O9 | APRS telemetry decode | P3 | Medium |
| O10 | ADS-B aircraft heading/track shown as rotated icon on map | P3 | Low — `ac.track` field available in dump1090 JSON; rotate ✈ by track deg |
| O11 | AIS vessel COG shown as rotated icon on map | P3 | Low — `cog` / `course` field available |
| O12 | MeshCore device "Potato" still deaf to RF (PUSH_ADVERT = 0) | P1 | Hardware — power-cycle USB to clear firmware state after crash-loop reconnection storm |
| O13 | JS8Call grid/position decode | P3 | Low |
| O14 | Pat Winlink RMS discovery and session status | P3 | Medium |
| O15 | Mesh bot: `metar <KXXX>` — raw METAR aviation weather via aviationweather.gov API (no key) | P3 | Low — good for ARES/RACES pilots |
| O16 | Mesh bot: `alerts` — active NWS weather alerts for station grid (polygon intersection) | P2 | Medium — high value for emergency ops; api.weather.gov/alerts?point= |
| O17 | Mesh bot: `sun` — sunrise/sunset times for base location via USNO or sunrise-sunset.org | P3 | Low |
| O18 | Mesh bot: `nodes` — list active mesh nodes heard in last N minutes (from router node table) | P2 | Low — no external API; reads internal state |
| O19 | Mesh bot: `aprs <W1ABC>` — last APRS-IS position for a callsign via api.aprs.fi or findu.com | P3 | Low |
| O20 | Mesh bot: `grid [lat lon\|callsign]` — Maidenhead grid square encode/decode | P3 | Low — pure math, no external API |
| O21 | Mesh bot: `id` — ECH node info reply: callsign, version, uptime, base grid | P3 | Low |
| O22 | Mesh bot: `moon` — moon phase and rise/set via USNO or skyfield (already a dep) | P3 | Low — useful for EME, night ops |
| O23 | Mesh bot: `dxcc <callsign>` — DXCC entity lookup (cty.dat or clublog API) | P3 | Low — DX ops and EMCOMM international contacts |
| O24 | Mesh bot: `contest` — upcoming/active HF contest list from contest-calendar.com or HamAlert | P3 | Low |

---

## Security Issues

| ID | Severity | Issue | Status |
|----|----------|-------|--------|
| SEC-01 | P0 | Default admin/admin password | OPEN — forced password change on first login not yet implemented |
| SEC-02 | P0 | No HTTPS / TLS | OPEN — TLS cert generation not in install.sh; use reverse proxy for prod |
| SEC-03 | P1 | No role enforcement on /api/users, /api/adapter-config, /api/system | **DONE rc6** — middleware ADMIN_PREFIXES blocks operators on all sensitive endpoints |
| SEC-04 | P1 | No login brute-force protection | **DONE rc6** — 10 attempts / 60s per IP, HTTP 429 on breach |
| SEC-05 | P1 | Settings page accessible to operator accounts | **DONE rc6** — server-side 403 + nav link hidden for operators |
| SEC-06 | P2 | Session not invalidated on password change | OPEN |
| SEC-07 | P2 | WebSocket /ws fully unauthenticated | **DONE rc6** — session cookie checked before ws.accept(); close(4401) if invalid |
| SEC-08 | P2 | XSS: adapter health detail values injected raw into innerHTML | **DONE rc6** — both key and value pass through escHtml() |
| SEC-09 | P2 | Open redirect via unvalidated `next` parameter in login | **DONE rc6** — next validated as relative path only |
| SEC-10 | P2 | Session cookie missing Secure flag | OPEN — setting Secure=True breaks HTTP mesh deployments; handle via config |
| SEC-11 | P3 | /api/version exposes process PID | **DONE rc6** — PID removed from response |
| SEC-12 | P3 | /metrics (Prometheus) public | OPEN — intentional for scraping; restrict at network level if needed |

---

## PBX / Asterisk Integration

| Feature | Status | File(s) |
|---------|--------|---------|
| AsteriskAdapter (AMI TCP connect/login) | DONE | `ech/adapters/asterisk_adapter.py` |
| Inbound call logging → NormalizedMessage feed | DONE | same |
| Click-to-call (originate) via AMI | DONE | same |
| Page / announce (Page app or exten method) | DONE | same |
| Active call tracking (health shows live count) | DONE | same |
| Screen phone stubs (push_to_screen, xml_directory) | DONE (stub, no-op until URL set) | same |
| MockAsteriskAdapter (simulates calls on timer) | DONE | `ech/adapters/mock_asterisk.py` |
| Adapter registration in main.py | DONE | `ech/main.py` |
| `POST /api/pbx/call` — click-to-call | DONE | `ech/api/app.py` |
| `POST /api/pbx/announce` — page all | DONE | same |
| `GET /api/pbx/status` — adapter state + call log | DONE | same |
| `GET /api/phone/directory` — XML remote phone book | DONE (stub) | same |
| `POST /api/phone/push` — screen phone push | DONE (stub) | same |
| Calls tab in right panel (with live call log) | DONE | `ech/ui/templates/index.html` |
| 📞 call button on node cards | DONE | same |
| 📢 PBX page all button in sidebar footer | DONE | same |
| PBX section in settings with config preset | DONE | `ech/ui/templates/settings.html` |
| Screen phone IP phone integration (real push) | DEFERRED | Needs screen_push_url set in config |

---

## MeshCore Protocol Audit (ECH_meshcore_further_advice.txt)

Critical issues identified from protocol audit. Status as of 2026-06-21.

| # | Issue | Status | Notes |
|---|-------|--------|-------|
| CI-1 | Message format assumptions — no validation | PARTIAL | Wire format confirmed from real v1.15 captures. No explicit frame length/version validator added yet. |
| CI-2 | Node names extracted from message body ("Bob: hello") | OPEN | `_extract_msg_sender()` still used as last resort since v1.15 PUSH_ADVERT carries no name. Should be removed or renamed to make it explicit. |
| CI-3 | PUSH_ADVERT name scanning heuristic | DONE | Confirmed: PUSH_ADVERT payload is 32-byte pubkey only — no name field. Scanning removed. |
| CI-4 | ping() sends CMD_SEND_TRACEROUTE | **DONE** | ping() now sends CMD_SEND_TRACE_PATH (0x24); TRACE_DATA (0x89) handler added with per-hop SNR; "Traceroute" button added to message detail popup |
| CI-5 | PUSH_PATH (0x81) not parsed into route tables | **DONE** | 0x81 correctly parsed as PATH_UPDATE (32-byte pubkey notification, not relay IDs); TRACE_DATA (0x89) is the proper traceroute result |
| CI-6 | announce() not a persistent discovery database | DONE | Node registry with last_heard and 1-hour stale expiry implemented. |
| CI-7 | SELF_INFO name parsing heuristic | PARTIAL | Byte 57 offset confirmed from real capture. _scan_ascii_name fallback still present for older firmware. |
| CI-8 | Channel switching not verified after connect | DONE | CMD_SET_CHANNEL + post-connect verification added. |
| CI-9 | Node discovery depends on chat message traffic | DONE | Nodes created from PUSH_ADVERT and auto-registered from channel/contact messages. |
| CI-10 | Missing protocol features: routing, telemetry, GPS, battery | PARTIAL | Delivery/routing done. Battery polling not scheduled. Relay node IDs blocked by protocol. |
| DEBUG | Raw packet capture endpoint | DONE | `/api/adapters/{name}/packets` (last 500 frames) + `/api/meshcore/raw` |

**Acceptance tests from audit (ECH_meshcore_further_advice.txt):**
- [x] Node names come from protocol metadata (SELF_INFO, PUSH_ADVERT) — not message text when avoidable
- [ ] No names extracted from message text (CI-2 still open — _extract_msg_sender still present)
- [x] PUSH_ADVERT creates node entries
- [x] Silent nodes appear (discovery pulse every 300s + Discover button)
- [x] Traceroute produces route information via TRACE_DATA (0x89) with per-hop SNR (CI-5 done)
- [x] ping() sends CMD_SEND_TRACE_PATH (0x24), result arrives as TRACE_DATA (0x89) (CI-4 done)
- [x] Channel selection verified after startup
- [x] Node discovery survives periods with no chat traffic
- [ ] Routing tables visible in UI (blocked pending CI-5)
- [x] Raw packet capture available for debugging

---

## Ham Radio Logger

| Feature | Status | Notes |
|---------|--------|-------|
| Contest selector (ARRL-FIELD-DAY, POTA, SOTA, GENERAL) | DONE | hamlog.html header |
| Up to 5 stations per contest | DONE | station-sel dropdown |
| Dup detection on callsign entry | DONE | checkDup() per keystroke |
| Wildcard callsign search (* and ?) | DONE | showDupeDetail() wildcardToRegex(); Log btn disabled while wildcard active |
| Log entry with band, mode, freq, RST, exchange | DONE | logQSO() |
| Comments / notes field (all contest types) | DONE | f-notes always visible |
| Extended QSO fields: Name, Country, State/Prov, County, Grid, Power, Time Off | DONE | DB migration + form row-extra; all editable inline |
| Per-band/mode stats bar | DONE | updateStats() |
| Field Day score (PH=1pt, CW/DI=2pts + bonus total display) | DONE | score-display; bonus in localStorage |
| ARRL Field Day bonus activities panel | DONE | openBonusPanel() — 15 bonus categories, checkboxes + count inputs, live score update, saved to localStorage |
| Cabrillo CLAIMED-SCORE is QSO points only | DONE | Bonus activities submitted separately on ARRL website |
| QSO table with source badges | DONE | renderTable() |
| Inline edit row — all fields including State | DONE | startEdit/saveEdit/cancelEdit; State+Country share one cell with two inputs |
| Edit row columns align with headers | DONE | rc37 — Grid/Name/Dist order fixed; empty td for Dist column |
| Delete with confirmation dialog | DONE | confirmDelete() |
| Role-based edit (operator+admin) / delete (admin only) | DONE | userRole + PATCH endpoint |
| Optimistic insert (no duplicate on WS echo) | DONE | client-side UUID + dedup |
| Multi-station WS sync | DONE | qso / qso_deleted / qso_updated events |
| ADIF export with extended fields | DONE | NAME, TX_PWR, STATE, COUNTRY, CNTY, GRIDSQUARE, TIME_OFF |
| Cabrillo 3.0 export with CLAIMED-SCORE | DONE | format_cabrillo() |
| POTA CSV V2 export | DONE | format_pota_csv() |
| SOTA CSV V2 export | DONE | format_sota_csv() |
| Live upload to QRZ logbook | DONE | upload_qrz() |
| Live upload to Club Log | DONE | upload_clublog() |
| Live upload to POTA | DONE | upload_pota() |
| Live upload to SOTA | DONE | upload_sota() |
| Auto-import from Winlink/APRS ECH messages | DONE | import_from_messages() |
| Callsign lookup — US (callook.info) + international (hamdb.org) | DONE | GET /api/callsign/{cs}; auto-fills name/state/country/county/grid on blur |
| Auto-fill country/state from callsign prefix table | DONE | callsignLookup() CALL_PREFIXES; fires on input, only fills empty fields |
| Distance and bearing per contact | DONE | haversineKm() + bearingDeg(); shown in table and dupe detail |
| My station Maidenhead grid input in station bar | DONE | #my-grid input; saves to localStorage + PATCH /api/hamlog/config → config.yaml |
| PATCH /api/hamlog/config endpoint | DONE | operators can save grid/callsign/fd-class/section without full admin access |
| Band conditions (hamqsl.com propagation data) | DONE | GET /api/propagation → SFI/K/A/X-ray/bands; regex parser handles malformed XML and both band tag formats |
| DX cluster spots (DX Summit) | DONE | GET /api/dx/spots proxies dxsummit.fi; band filter, hide-worked, 3-min auto-refresh |
| Submit spot to cluster | DONE | POST /api/dx/spots stores locally; our spots shown in gold with [our] badge |
| Chat notification badge | DONE | Red numbered dot on Chat tab when messages arrive while tab hidden; clears on open |
| 4 color themes (Dark/Light/Blue/Green) | DONE | CSS data-theme attribute |
| High Contrast / ADA theme (WCAG 2.1 AAA) | DONE | data-theme="hc"; all pairs ≥7:1 contrast |
| Theme persisted to localStorage | DONE | ech_hamlog_theme key |
| Ham Log nav link (📻) in main page | DONE | index.html nav |
| SQLite qso_log table with migrations | DONE | database.py _migrate() adds new columns non-destructively |
| Field Day callsign from config (not personal callsign) | DONE | _hamlog_config() reads hamlog.callsign |

## Main Page Enhancements (rc6)

| Feature | Status | Notes |
|---------|--------|-------|
| Who's online indicator (👤 N) in header | DONE | GET /api/auth/sessions + pollWhoOnline() |
| Popover shows username, role, expiry | DONE | renderWhoList() |
| Settings ⚙ link hidden for operators | DONE | applyRoleUI() checks /api/auth/me |
| Simulation 🎭 link hidden for operators | DONE | same |
| Access-denied redirect toast | DONE | ?access=denied → toast then history.replaceState |

## MeshCore Node Discovery (rc6 improvements)

| Feature | Status | Notes |
|---------|--------|-------|
| Periodic CMD_GET_CONTACTS poll (lightweight, no announce) | DONE | every 30s default (was 120s) |
| contacts_poll_interval config key | DONE | meshcore.py |
| Auto-detect contact_path_size from first CONTACT record | DONE | tries 3/64/32/8 — fixes silent drop when device uses 3-byte paths |
| No double-poll at startup | DONE | last_contacts_poll/last_discovery initialized to now0 |
| Contacts refresh on new PUSH_ADVERT from unknown node | DONE | _contacts_refresh_pending flag |
| contact_path_size live-set from settings UI | DONE | POST /api/adapters/{name}/contact_path_size |
| live-set nodes_updated WS event | DONE | _router_notify_nodes callback |

---

## Ham Log — Multi-Station Features (rc25–rc27)

| Feature | Status | Notes |
|---------|--------|-------|
| Multi-station band map (right panel) | DONE | renderBandMap() — shows operator/band/mode per active station |
| Station registration bar (callsign + band/mode + grid + Set) | DONE | updateMyStation() → POST /api/hamlog/stations; grid saves to server |
| Station TTL (5 min active window) | DONE | database.py get_stations(active_minutes=5) |
| Multi-station WS sync for station updates | DONE | hamlog_station WS event |
| ARRL Sections tracker (right panel) | DONE | renderSections() — worked sections highlighted from rcvd_exch |
| Sections search filter | DONE | sec-search input |
| Sections context message for non-FD contests | DONE | shows instructions when not ARRL-FIELD-DAY |
| Inter-station chat (right panel) | DONE | /api/hamlog/chat POST/GET + WS broadcast hamlog_chat |
| Chat immediate display (no WS dependency) | DONE | rc25 — sendChat() shows from HTTP response; 30s poll fallback |
| Per-session UUID works on plain HTTP | DONE | rc25 — generateUUID() uses crypto.getRandomValues(), not crypto.randomUUID() |
| Logout button (header, both main and hamlog pages) | DONE | rc26 — shows username + Sign out; only visible when auth active |

---

## Optional Next Steps

### CAT Radio Control (Ham Log)

**Goal:** Operators at their own stations can have the ham log auto-fill Frequency and Band from their connected radio.

**Architecture:**
- Uses the browser **Web Serial API** (Chrome/Edge only — same API used by Meshtastic Web Client and ESP Web Tools)
- Each operator connects their radio via USB to their own laptop; no server cable needed
- Browser reads frequency and mode directly from the radio; updates Freq/Band fields in real time
- **Requires HTTPS** — Web Serial is restricted to secure contexts; ECH currently runs HTTP

**BLOCKER:** Web Serial API is gated by `SecureContext` — it is **completely unavailable** on plain HTTP regardless of browser version. ECH must run HTTPS before any browser CAT work is worth starting. Firefox does not support Web Serial at all; Chrome/Edge only.

**Implementation plan:**
1. **Add HTTPS to ECH** (~1 hr): generate self-signed cert on server, add `tls: true` config option to uvicorn startup, operators trust cert once in browser — **required first; all other steps blocked without this**
2. **Web Serial boilerplate** (~1 hr): "Connect Radio" button → browser port picker dialog, connection state indicator in ham log header
3. **Kenwood CAT protocol in JS** (~2 hr): `IF;` command returns freq + mode; covers Kenwood TS-series, Elecraft KX3/K3/K4, many SDR rigs
4. **Yaesu CAT2** (~1 hr): covers FT-991A, FT-DX series
5. **Icom CI-V** (~2 hr): binary protocol, covers IC-7300, IC-705, IC-9700
6. **Ham log integration** (~1 hr): auto-fill Freq + Band on VFO change, visual "CAT live" indicator, optional auto-set mode

**Effort:** ~8 hrs total; Kenwood + Yaesu alone covers most common radios (~4 hrs). **Cannot start until HTTPS is in place.**

---

### Server-Side HF Radio Control (rigctld)

**Goal:** ECH server connects to a station HF radio for automated Winlink sessions and JS8Call HF chat.

**Architecture:**
- `rigctld` (Hamlib) runs on the Pi as a service, connected to the HF radio via USB/serial
- ECH backend (`ech/adapters/cat_hamlib.py`) communicates with rigctld via TCP (localhost:4532)
- Exposes `/api/cat/status`, `/api/cat/set_freq`, `/api/cat/set_mode` to the web UI
- Broadcasts current freq/mode to all browsers via WS as `cat_update` events

**Implementation plan:**
1. Add `rigctld` to install.sh as optional dependency (`sudo apt install libhamlib-utils`)
2. Create `ech/adapters/cat_hamlib.py` — polls freq/mode every 2s, broadcasts via WS
3. Add `cat:` config block: `rig_model`, `port`, `baud` (or `rigctld_host/port`)
4. Ham log listens for `cat_update` WS events, auto-fills when admin view is open

**Effort:** ~3 hrs; separate from browser CAT (these are independent features)

---

### Ham Log — Future Enhancements

| Feature | Priority | Notes |
|---------|----------|-------|
| QRZ.com callsign lookup (XML plan) | P2 | callook.info + hamdb.org already provide free lookup; QRZ XML needs paid API key |
| LoTW upload | P2 | TQSL CLI or ADIF upload to arrl.org |
| Push spot to real DX cluster (telnet) | P3 | Local spots stored in-process; telnet connection to DXSpider/AR-Cluster needed for network-wide spotting |
| Satellite pass auto-fill (Doppler-corrected freq from TLE) | P3 | pyephem or skyfield |
| ARRL Field Day bonus printable summary sheet | P3 | Bonuses tracked in localStorage; separate printable/PDF export would help submission |
| Separate Ham Log microservice | P3 | clean split: own FastAPI app, own SQLite, own port; existing /api/hamlog/* already isolated |

---

## Release Checkpoints

| Version | Status | Deployed | Key Changes |
|---------|--------|----------|-------------|
| v1.0.0-rc1 | Released | Yes | Initial ECH feature set |
| v1.0.0-rc2 | Released | Yes (2026-06-20) | Auth/security fixes, adapter startup fix, rate limiting, location UX |
| v1.0.0-rc3 | Released | Yes | Simulation pause fix, clock/callsign UX, cold disaster scenario, ReferenceError fix |
| v1.0.0-rc4–rc29 | Released | Yes | MeshCore fixes, MQTT bridge, PBX, ham logger, security audit, multi-station features |
| v1.0.0-rc30 | Released | Yes (2026-06-22) | Ham log map layer (manual QSOs only, source=manual filter) |
| v1.0.0-rc31 | Released | Yes | Band conditions: replaced ElementTree with regex parser for malformed hamqsl.com XML |
| v1.0.0-rc32 | Released | Yes | DX spots, callsign lookup (callook.info/hamdb.org), distance/bearing, auto-fill location |
| v1.0.0-rc33 | Released | Yes (2026-06-22) | Delete confirmation, state/county CSS width fix, applyCallsignData empty-check, deterministic map jitter, Maidenhead grid field |
| v1.0.0-rc34 | Released | Yes (2026-06-24) | Map: STATE_COORDS + qsoLatLon() uses stored country/state; ARRL FD bonus panel; Cabrillo CLAIMED-SCORE QSO-only |
| v1.0.0-rc35 | Released | Yes | DX spot submission (POST /api/dx/spots), local spots shown gold [our]; propagation XML parser hardened (multi-format band regex, SFI/solarflux alias) |
| v1.0.0-rc36 | Released | Yes | Grid input in station bar; PATCH /api/hamlog/config endpoint; myLatLon() uses live grid input, no stale cache |
| v1.0.0-rc37 | Released | Yes | Edit row column alignment fixed (Grid before Dist before Name); chat unread badge (red dot with count) |
| v1.0.0-rc38 | Released | Yes | State editable in inline edit row; Country column shows State/Country combined |
| v1.0.0-rc39 | Released | Yes (2026-06-24) | Wildcard callsign search (* and ?) in dupe checker; Log button disabled while wildcard active |
