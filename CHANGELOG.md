# Changelog

All notable changes to SignalMatrix (ECH) are documented here, newest first.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

Full engineering detail (root causes, file locations, live-verification
notes) lives in `ECH_REQUIREMENTS_AND_PROGRESS.md`; `git log` has the
complete commit history. This file is the user-facing summary, starting
from v1.0.0-rc198 — earlier history predates this file.

## v1.0.0-rc273 (2026-10-12)

### Added
- README screenshots of the map weather layers and the new APRS page.

### Fixed
- Settings → add adapter: **JS8Call** now offers its fields (host, port, callsign,
  keepalive, activity, filter-own) instead of an empty form; APRS KISS gained `tocall`
  and `beacon_interval_sec` in its field list.

## v1.0.0-rc272 (2026-10-12)

### Added
- **APRS page** (📍 in the top bar, `/aprs`) — APRS as a status system, not just messaging:
  - **Messages:** send/receive with **acks and automatic retries** (30 s / 60 s / 120 s);
    shows waiting / acked / no ack per message.
  - **Bulletins:** BLN bulletins and NWS bulletins heard on APRS.
  - **Objects:** objects and items other stations have set, plus **your own objects** —
    sent on APRS-IS and/or RF, re-sent every 10+ min until you kill them, optionally
    following a mesh node's live position. Manual only: nothing becomes an APRS object
    unless you press Send.
  - **Beacon:** position, symbol, comment and interval per APRS adapter, "Beacon now".
  - **Packets:** raw packet log (last 2000, filter by callsign/text/type/adapter).
  - **Weather:** APRS weather stations with temperature, humidity, wind, gust, pressure,
    rain and a temperature trend.
- **📡 APRS object** link on map popups (mesh nodes, aircraft, vessels, strip reports,
  emergency-status entries) — opens the object form pre-filled.

### Fixed
- Messages addressed to you on APRS are now **acknowledged** (they never were, so the
  sender's radio kept retrying and never saw delivery); retries of an already-delivered
  message are acked but not shown twice.
- APRS RF (KISS/Direwolf): frames were addressed to the first path element (`WIDE1-1`)
  instead of `APRS` via `WIDE1-1,WIDE2-1`; messages went out without the APRS message
  header; the receive loop stopped after 2 s of silence. All three fixed.
- APRS weather from position reports: wind speed is mph (was read as knots).

## v1.0.0-rc271 (2026-10-12)

### Added
- **Special Weather Statements on the map:** the ⛈ Weather warnings layer now shows every
  active NWS alert for your state, neighbouring states and nearby waters, not just alerts
  covering the hub (within 500 km, `map_alert_radius_km`). Zone-based alerts (most SPS, winter/wind/fog advisories) are drawn as
  their NWS zone/county outlines (dashed); SPS use NWS's moccasin colour. Zone outlines are
  cached on the server, so they still draw when the internet is down.
- **GOES from your own receiver (RTL-SDR):** new `goes_receiver` section. Point it at
  goestools/SatDump output and the 🛰 Satellite IR layer uses your own full-disk image
  (reprojected for your area) instead of the internet, and EMWIN text gives warnings and
  SPS with no internet. `auto` (default) uses the internet while it answers and your
  receiver otherwise; `local`/`goes`, `internet` and `both` force a source.
- **APRS weather bulletins:** NWS bulletins on APRS (NWS-WARN/WATCH/ADVIS) are drawn on the
  weather layer in violet with a dotted outline; GOES-received alerts are cyan. NWS / GOES /
  APRS buttons under the layer turn each source on or off. APRS-IS adds `g/NWS-*` to the
  filter automatically (`nws_bulletins: false` to opt out); bulletins don't enter the
  message log.
- **aprs.fi symbols for APRS stations** on the map (Hessu's open aprs.fi symbol set,
  bundled so it works offline), including overlay characters. RF (KISS) stations now
  appear on the map too.

## v1.0.0-rc269 (2026-10-11)

### Added
- **Multiple radios as one station** (e.g. a server MeshCore + one in an operator's browser):
  - one compose button per protocol (`MSHC×2`) sends through all of them; a channel picked
    in the picker is sent by name so each radio uses its own matching channel; DM replies
    go out on the radio that received them;
  - a channel message heard on several radios is one feed entry, marked
    "📡 heard on N radios";
  - one of your radios hearing another's transmission is no longer shown as incoming.
- Bridges between two radios of the same protocol skip a radio that already heard the
  message itself (no double transmissions where sites overlap).

## v1.0.0-rc268 (2026-10-11)

### Fixed
- **Browser-connected devices that silently did nothing:** `/remote-hw` now warns when no
  adapter will use the device you connected — wrong name, or the adapter isn't set to
  transport "browser" — and says exactly what to change.
- Meshtastic with a blank serial port now auto-detects instead of failing with
  "No such file or directory: ''"; the adapter editor no longer saves blank fields.
- Meshtastic nodes connected through the browser are picked up immediately (no up-to-a-
  minute retry gap).

## v1.0.0-rc267 (2026-10-11)

### Fixed
- **MeshCore traces:** the learned mesh topology never forgot anything — a node heard
  directly once (or a different node sharing its 1-byte hash) stayed "heard direct"
  forever, so traces were routed over links that no longer existed. Direct links now
  expire after 24 h and relay links after 72 h unless heard again (configurable:
  `topology_direct_ttl_hours`, `topology_edge_ttl_hours`); older saved topology without
  timestamps is relearned.
- Unanswered traces now post a **"no reply"** result to the feed (route tried, how recently
  the first hop was actually heard) instead of disappearing silently.
- Other observers' reported paths no longer mark nodes as directly heard by your radio,
  and their comma-separated path format is now read correctly.
- No more traceback when a log line races the database closing at shutdown.

## v1.0.0-rc266 (2026-10-11)

### Added
- Caddy configuration for remote MeshCore observers (`deploy/caddy/Caddyfile.observers`):
  MQTT over WebSocket + TLS on 443 for a dedicated hostname. README walks through it.

### Changed
- The observer broker's WebSocket listener is off by default: Mosquitto 2.0.x ignores the
  bind address on WebSocket listeners, so it must be firewalled to localhost before use
  (commands in the config file). The broker's SignalMatrix reader password is kept in a
  root-only file instead of being printed.

## v1.0.0-rc265 (2026-10-11)

### Added
- **Packet-level MeshCore comparison**: every packet this station hears is matched against
  what other observers (LetsMesh or your own observer broker) report, using the same
  path-independent packet hash observers use — showing heard by both / only us / only
  others, per packet type and per observer, and which packets we missed. Settings → MQTT
  and `GET /api/meshcore/compare`.

### Fixed
- The MeshCore MQTT bridge now publishes raw packets in the exact observer format with the
  real packet hash (it used an internal message ID that could never match other
  observers). Publishing decoded (decrypted) messages is now opt-in
  (`meshcore_bridge.publish_decoded`, default off) — it previously sent decrypted text,
  sender and channel to the configured broker.
- The bridge no longer disconnects and reconnects after every idle minute.

## v1.0.0-rc264 (2026-10-11)

### Added
- **Your own observer broker** for MeshCore packet comparison: Mosquitto config, an ACL
  that lets each observer publish only under its own public key (and read nothing), and
  `scripts/observer_broker.sh install|add|revoke|list`. Localhost-only until you choose
  to expose it. See README "Your own observer broker".

## v1.0.0-rc263 (2026-10-11)

### Added
- **💧 River gauges** map layer: NOAA National Water Prediction Service gauges within
  80 km — stage, flow and flood category now and forecast, colour-coded (green no
  flooding → yellow action → orange minor → red moderate → purple major), most severe
  first, with a warning toast when any gauge is at minor flooding or worse.
- **Water temperature** at the nearest NOAA sensor stations, shown on the 🌊 Tides layer.
- **🌧 Radar** (NEXRAD) and **🛰 Satellite IR** (GOES-East, day and night) overlays from the
  Iowa Environmental Mesonet — loaded directly by your browser, nothing stored on the
  server, refreshed every 5 minutes.

## v1.0.0-rc262 (2026-10-11)

### Added
- **More Winlink templates**, grouped in the picker:
  - **ICS-214A Individual Log** — activity log validated line by line (HHMM + activity).
  - **Local Weather Report** and **SHARES Region 1** strips — SKYWARN (GYX/CAR), SITREP,
    Hurricane Report, WXOBS — built from the same definitions as the SHARES Response
    Creator, so the output is byte-for-byte the strip that tool produces.
  - **On-the-Air Spot / Hunter Report** — POTA, SOTA, WWFF, IOTA, BOTA, castles,
    lighthouses…: spot, self-spot or hunter contact formatted like a cluster spot.
- **Field validation on every template**: callsigns, frequencies, UTC times, dates,
  state/NWS office codes, MGRS (or lat,lon converted), Y/N/Unk/NA answers, weather values
  in sensible ranges, length limits, program-specific references (e.g. a SOTA reference
  must look like W1/HA-126), and no "/" inside SHARES answers. Errors show next to each
  field; fixed lists are dropdowns.

## v1.0.0-rc261 (2026-10-11)

### Added
- **📻 On the Air map layer** (replaces the separate IOTA and BOTA buttons): every
  on-the-air activation spotted on the DX cluster in the last hour, worldwide — WWFF,
  IOTA, SOTA, Bunkers (BOTA), lighthouses (ILLW/ARLHS), castles (COTA), LLOTA, and
  Beaches on the Air / HSOTA / boats (/MM) if spotted. Each chip shows the program tags;
  the popup names each program and its reference. POTA keeps its own layer.

### Changed
- Moon panel moved to the bottom centre and can be collapsed to a small 🌙 chip
  (remembered per browser).
- SOTA button no longer sends you to the SOTAwatch website; it reports that SOTA spots
  are unavailable until the layer is updated to SOTA's current API.

## v1.0.0-rc259 (2026-10-11)

### Changed
- **Every install uses its own identity and agreements.** All outbound requests (NWS,
  NOAA, PSKReporter, OpenStreetMap, callsign lookups) send `(SignalMatrix/<your call>,
  <your email>)` from your own config instead of a placeholder; the Settings contact
  email now covers all of them (`operator.contact_email`).
- **Winlink RMS gateways come from your own Pat** (`/api/rmslist`) instead of calling
  Winlink with an access key that belonged to the Pat project.
- No more built-in default callsign in the Asterisk installer, PBX phone book or Winlink
  mail forwarder. README: new "Your own accounts, keys and agreements" section.

### Fixed
- JS8Call stations with 6-character grid squares were plotted ~75 km east / ~50 km
  north of their real location.
- `scripts/forward_winlink_mail.sh` had a bash syntax error and had never run.

## v1.0.0-rc258 (2026-10-11)

### Added
- **Contact email field** (Settings → PSKReporter): each operator enters their own
  address; it's sent to PSKReporter with each query so they can reach you instead of
  blocking you, applies immediately and is saved to `pskreporter.contact_email`.

### Changed
- Bridge-rule endpoints are admin-only, like other configuration changes.

## v1.0.0-rc257 (2026-10-11)

### Added
- **Tides layer** (🌊): nearest NOAA tide stations to base — rising/falling and the next
  high/low on the map, next four highs/lows in the popup. Station list and predictions
  cached in memory only (nothing written to disk).
- **Moon panel**: phase, % illuminated, moonrise/moonset at the base and next full/new
  moon — computed in the browser, no API.
- **Spot-layer legend**: every activity layer now has its own icon, colour and type tag
  (🏞 POTA, ⛰ SOTA, 🏝 IOTA, 🛡 BOTA, 📡 DX) plus a legend entry explaining it.

### Fixed
- **DX cluster / IOTA / BOTA placement**: dxsummit reports longitudes west-positive, so
  spots were plotted in the wrong hemisphere; and US stations were pinned at their FCC
  home address via callook.info. Now sign-corrected, no home-address lookups, the DX
  layer shows what spotters within 500 km of you are hearing, and IOTA/BOTA are
  worldwide. DX chips no longer reuse the MeshCore node colour.
- Spot text from public spotting networks is now HTML-escaped on the map.

## v1.0.0-rc256 (2026-10-11)

### Changed
- **SOTA map spots are off by default.** SOTA deprecated the spots API the layer used (it
  returned a single "DEPRECATED" placeholder, so the map showed nothing), and SOTA's API
  terms require their prior approval for AI-generated software. The ⛰ button now explains
  this and opens SOTAwatch; opt back in with `sota: {api_enabled: true}` only after
  clearing it with SOTA.

## v1.0.0-rc255 (2026-10-10)

### Added
- **Endpoint bridges**: a bridge now links ONE mesh channel (or APRS addressee such as
  `EMCOMM`) on each side, with explicit direction, text/DM selection, per-direction
  rate limit, sender attribution, length limits, optional callsign-only, and dry run.
  Echoes and re-bridging are blocked; duplicates forwarded once. New Settings editor
  with dropdowns, live counters and a decision log; `GET /api/bridge-rules/status`.
- Browser CAT card: Yaesu 5-byte binary CAT (FT-817/818/857/897) — frequency/mode
  readout and PTT.

### Fixed
- **Settings saves failed with "permission denied"** on installs where `/etc/ech` is
  root-owned (every save since rc247): the deploy now gives the service user the
  directory, and saves fall back to a safe in-place write if it still can't.
- MeshCore `transport: browser` now waits for the browser instead of failing into a
  back-off gap (same bug as browser CW in rc250).

## v1.0.0-rc249–rc254 — Sound-card digital modes over browser audio (2026-10-10)

### Added
- **RTTY and PSK31 over browser audio**, alongside CW. One `/remote-hw` audio stream
  now feeds all three decoders at once (`browser_session`, e.g. `radio-audio`).
- **AFC**: PSK31 finds and locks its carrier anywhere in the passband; RTTY finds the
  strongest tone pair at the configured shift; RTTY `reverse` for inverted tones.
- **Message-window modem bar** (appears when a CW/RTTY/PSK adapter is selected in
  compose): pitch/mark/carrier, AFC, TX wpm, shift/baud/reverse, a 1–5 sensitivity
  slider, last-copy readout, **⇆ Match** (copy the other station's speed/pitch) and
  **💾 Save**. API: `GET/POST /api/adapters/<name>/modem`.
- **PTT keyed by the browser** around each transmission over the CAT serial port —
  CI-V, Kenwood/Elecraft, Yaesu text, RTS, DTR or VOX — with TX delay/tail, a
  3-minute stuck-key watchdog, unkey on disconnect/page close, and a *Test PTT* tone.
- `/remote-hw`: audio input/output device pickers, input level meter, server error
  detail on disconnect; the page keeps its bridges alive (links open in a new tab).

### Fixed
- `/remote-hw` bridges closed instantly on every connect ("Server closed the bridge")
  — a missing import crashed the WebSocket handler (rc249).
- Browser CW never attached to the audio (10 s window + up to 60 s back-off) and never
  re-attached after a reconnect (rc250).
- Noise decoded as streams of E/T (CW), single letters (RTTY) and short junk (PSK31):
  glitch filtering, robust CW timing, speed/SNR limits, and per-mode "is this really
  the mode?" checks (CW short dit/dah groups, RTTY tone dominance, PSK31 BPSK
  phase/varicode/carrier checks) (rc251, rc254).
- RTTY tone detection snapped to a 182 Hz grid, so copy depended on where the signal
  sat; now measured at the exact frequency (rc254).
- TX audio over the browser played with no PTT (the server keyed only while queueing)
  and was upsampled crudely; now keyed around playback and resampled by Web Audio.

## v1.0.0-rc244–rc248 (2026-10-09)

### Added
- Emergency-status board entries plotted on the map (O93).
- NWS weather-warning polygons as a map layer (O94).
- Adapter editor field picker driven by each adapter's real config schema.

### Fixed
- Self-restart race and non-atomic `config.yaml` writes that could corrupt the file (O110).
- False "update failed" shown after every successful self-update (O111).
- LetsMesh JWT claim set rebuilt (O75 — broker access still unresolved).

## v1.0.0-rc243 (2026-10-09)

### Added
- **Settings now has a real GUI adapter editor** instead of only a raw JSON
  textarea — each adapter is an editable card (text/number/checkbox fields,
  add/delete fields, enable/disable, delete, "+ Add adapter" with a type
  picker). A serial-port dropdown (listing actually-attached devices) now
  backs any `port`/`device` field instead of requiring you to find and type
  a `/dev/ttyUSBn`/`COMn` path by hand.
- **GPS is now configurable from the GUI** — enable toggle, serial port
  picker, baud rate, minimum satellites, update interval, clock-sync toggle.
  Previously this required SSHing in and hand-editing `config.yaml`.
- The Settings page header now shows the running ECH version, matching the
  messaging page.

### Fixed
- Self-update: the progress log used to just stop updating with no
  indication of whether the update succeeded, failed, or was still running.
  Every outcome now shows an explicit ✅/❌/⚠ message. "Check for updates"
  now shows ECH's own version number (e.g. `v1.0.0-rc243`) instead of a raw
  git commit hash.
- Template/config file reads could throw a `UnicodeDecodeError` on
  Windows-based dev/test environments (harmless on the live Linux
  deployment, which already defaults to UTF-8) — all read/write sites now
  specify UTF-8 explicitly.
- `README.md`'s MeshCore adapter row referenced a nonexistent PyPI package
  (`pyserial-asyncio-fast`) — the real dependency is `pyserial-asyncio`.

## v1.0.0-rc237–rc241 — AREDN awareness, M17 protocol fixes, security audit (2026-10-08)

### Added
- AREDN-facing adapters now sanity-check their configured host against
  real AREDN addressing conventions (`*.local.mesh` hostnames, the actual
  10.x.x.x mesh range) and log a non-blocking warning if it doesn't look
  mesh-typical — catches e.g. a typo'd public IP.
- AREDN PBX/phone service discovery: query a local AREDN node for other
  phone/PBX services already advertised mesh-wide, surfaced in the PBX
  directory with click-to-call for any service with a structured `sip:`
  link.
- A tamper-evident audit log for logins, user management, and
  admin/service actions (Settings → Audit Log), with a one-click chain
  integrity check.

### Fixed
- M17 reflector adapter: the packet-mode TYPE field had a bug that wrongly
  flagged every outgoing message as "stream mode" to any spec-compliant
  receiver (the production reflector used for live testing never checked
  this field, which is why it went unnoticed). Also implemented the
  `#PARROT` self-test echo destination, previously left unimplemented
  rather than guessed at.
- A handful of flaky/stale test failures that had been carried as
  "known, unrelated" for a while — both had real root causes (a never-
  finished helper function in the MeshCore↔MQTT bridge, and an anomaly-
  detection rule unintentionally excluded for APRS).

## v1.0.0-rc230–rc236 — Caddy sub-path support, self-update, M17 picker, HOIP directory (2026-10-08)

### Added
- ECH can now run behind a reverse proxy mounted under a sub-path (e.g.
  `https://host/ech/`), not just a dedicated host/port — see the Caddy
  section below.
- **Self-update from GitHub** — Settings → System can now pull the latest
  code from a chosen branch and restart ECH, without needing the external
  Windows deploy pipeline.
- A local emergency-status board (hospital beds, shelters, vehicles, etc.)
  with a page, a mesh-bot `status` command, and manual entry.
- M17 reflector server/module can now be switched live from Settings
  instead of requiring a config edit + restart.
- The HamVOIP/AllStarLink/AREDN directory (Settings → PBX, and the
  Messages page's Calls tab) now includes HOIP's full published directory
  — ~150 real conference bridges, RF links, test numbers, and audio/
  dispatch-monitor feeds — and the same directory now also populates the
  Yealink phone's remote phonebook.
- Winlink messages can be marked read/unread from the inbox.

## v1.0.0-rc229 (2026-10-08)

### Fixed
- **AirNow AQI data was silently broken since 2026-10-01** — AirNow retired
  the old current-conditions endpoint; air quality polling has been failing
  on every cycle for a week. Switched to the real replacement endpoint.
- **Satellite pass predictions (`satpass`) lost one of three TLE sources** —
  CelesTrak changed its URL scheme; the mesh bot's small curated
  station/weather satellite feeds were 404ing (AMSAT and SatNOGS fallbacks
  kept it working, just with less coverage). Fixed.

### Added
- Winlink message templates: ICS-213, ARRL Radiogram, SITREP, an **AI Query**
  request (the real Winlink Express "Ask AI" feature — To: AIHELP, Subject:
  AI), and a **Catalog Request** template (weather bulletins, propagation,
  satellite imagery, US radar image, nets schedules, and more — pick `LIST`
  to have Winlink mail back its current full catalog). Available from a new
  "📋 Template" button in the Winlink tab.
- Reply / Forward buttons on the Winlink message reader.
- A compose-bar channel picker for MeshCore/Meshtastic — shown only when one
  of those adapters is selected (same pattern as the Winlink Subject field)
  — lets you target a specific channel for one message without changing the
  adapter's tuned default channel in Settings.
- A `trace <node name>` mesh bot command — fires a real active trace probe
  to a named node and replies with the measured hop path, distinct from the
  existing passive `path` command (which only reports the relay chain of
  your own last message).

### Changed
- Dark-themed form fields throughout the Winlink template picker and the
  new compose-bar Subject field — they were rendering with the browser's
  default white background against the app's dark theme.

## v1.0.0-rc228 (2026-10-07/08)

### Fixed
- Meshtastic message retention: there was no Meshtastic entry anywhere
  (reference config, Settings UI, or the save payload) — only APRS and
  MeshCore — so Meshtastic messages were kept forever regardless of the
  retention setting. Added.
- Mesh bot replies on Meshtastic could go out on the wrong channel: the
  adapter was ignoring the origin-channel hint and always sending on
  whichever channel it happened to be tuned to. Fixed (MeshCore already
  handled this correctly).
- Remote Hardware (Web Serial) reconnects could get locked out with an
  "already has a live session" error after a browser tab closed uncleanly
  (refresh, lost network). A new connection now replaces a stale one.
- Winlink's inline message reader always showed a blank subject/body/sender
  — the endpoint was returning Pat's raw API response instead of the
  normalized shape the UI expected.

### Added
- Purged messages (MeshCore/Meshtastic, per retention) are now archived to
  a restorable dated file before deletion, with their own longer retention
  (default 30 days) and a Settings UI to restore a given day.
- Winlink RMS gateway markers can now be restricted to a bounding box
  (`bounding_box:` in the adapter config) instead of showing all ~1,200
  gateways worldwide.
- Analytics: per-series show/hide toggles (with All/None) on both time
  charts, and a 30-day range option (previously capped at 7 days even
  though the underlying data was already retained that long).

## v1.0.0-rc209–rc219 — DMR/BrandMeister, M17, AREDN MeshChat, DAPNET (2026-08-12–16)

- Real `dmr_brandmeister` adapter against the Homebrew/DMRplus repeater
  protocol, with a working SMS payload codec verified over-the-wire against
  a live test rig (both ETSI and Motorola SMS framing).
- M17 reflector adapter — confirmed live against a production M17 reflector.
- AREDN MeshChat adapter.
- DAPNET paging adapter — confirmed live (real page sent and received).
- MQTT/LetsMesh JWT auth reworked to sign on-device (no exported private
  key needed); Pat Winlink RMS discovery fixed (wrong URL, then a missing
  required API key, both found and fixed against real traffic).
- Reply-to-name bug fixed at the root cause for polled MeshCore channel
  messages; `@mention` autocomplete added to the compose box.
- Automated daily database integrity check.

## v1.0.0-rc198–rc208 — UI/backend audit, CAT control, DB corruption guard (2026-08-12)

- Logs page Raw Packets/Encrypted tabs, map "last message", and the
  MeshCore "Reconnect" button were all silently broken — fixed.
- CAT (PTT + rig control) added to Settings and Remote Hardware (Web
  Serial), shared across Ham Log and other pages.
- Timezone toggle now actually affects per-message timestamps, not just
  the header clock.
- Critical file-descriptor exhaustion fixed: the router never released
  adapter resources on reconnect.

---

For anything older than rc198, see `git log` and the early sections of
`ECH_REQUIREMENTS_AND_PROGRESS.md`.
