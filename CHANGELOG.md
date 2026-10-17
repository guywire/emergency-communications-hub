# Changelog

All notable changes to SignalMatrix (ECH) are documented here, newest first.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

Full engineering detail (root causes, file locations, live-verification
notes) lives in `ECH_REQUIREMENTS_AND_PROGRESS.md`; `git log` has the
complete commit history. This file is the user-facing summary, starting
from v1.0.0-rc198 — earlier history predates this file.

## v1.1.9 (2026-10-16)

### Fixed
- MeshCore: a trace path drawn on the map that started at ECH's own node never left the radio.
  The node was sent as the first hop, and a node never repeats its own packet. ECH now treats its
  own node as the starting point and leaves it out of the hop list.

## v1.1.8 (2026-10-16)

### Changed
- MeshCore: ECH's node now adverts once an hour instead of every 5 minutes. The contact list still
  refreshes every 5 minutes. The interval is set by `advert_interval`, in seconds; 0 turns automatic
  adverts off, leaving only the Announce button.

## v1.1.7 (2026-10-16)

### Added
- MeshCore: when a second MeshCore receiver on the LAN sends its packets to ECH, the trace packets
  it hears now count toward trace and sweep progress. ECH then learns how many hops a probe made
  even when its own radio missed a hop's retransmission.

## v1.1.6 (2026-10-16)

### Changed
- Spoken intercom now waits 1.5 s after the phone auto-answers, then plays a soft two-note chime
  before the speech. The first words were being lost under the phone's own intercom tone.
  Both are configurable (`intercom_lead_s`, `intercom_chime`).

### Fixed
- Intercom calls no longer appear in the Messages pane as "[INBOUND]". They are listed only in
  the Calls panel log, as "[INTERCOM]". Calls ECH places (click-to-call, Ring + speak) are now
  labelled "[OUTBOUND]" instead of "[INBOUND]".
- A text-to-speech failure now reports an error. Before, it could send a call with no audio.

## v1.1.5 (2026-10-16)

### Fixed
- Calls panel: the 🔊 Intercom button was cut off the right edge of the panel. Extension and text
  now sit on one row with "📞 Ring + speak" and "🔊 Intercom" as equal buttons underneath.

## v1.1.4 (2026-10-16)

### Fixed
- The MeshCore link evidence behind the "one-way link" verdicts and the longer repeater IDs is
  now saved with the route map, so a restart or update no longer wipes it (1.1.3 kept it in
  memory only).

## v1.1.3 (2026-10-16)

### Changed
- **MeshCore traces use longer repeater IDs** (2 bytes instead of 1) whenever every hop can be
  pinned to one specific repeater, so a trace no longer gets picked up by a different repeater
  that happens to share the same 1-byte ID.
- **Trace and sweep results now say when a link is one-way**: e.g. "We hear LandOfLucy directly
  (… packets, avg SNR +4.6 dB) but it has not answered a trace from us", which points at your
  node's antenna/height/placement rather than the route.
- Calls panel buttons renamed: "📞 Ring & speak" rings normally; "🔊 Intercom" auto-answers on the
  phone's loudspeaker.

### Fixed
- MeshCore route map treated the same repeater as several when the mesh used 2- or 3-byte IDs,
  splitting known routes apart.
- Traces to repeaters no longer wait 15 s for a route discovery that repeaters don't answer.

## v1.1.2 (2026-10-16)

### Added
- **Calls: choose your phone.** A "📞 My phone" picker on the Calls panel sets which extension
  rings first when you dial from ECH (it then connects you to the number). Previously this was
  always ext 101. Extensions with no phone connected can't be picked.
- **Intercom ("🔊 On speaker")**: the chosen phone(s) answer automatically on the loudspeaker.
  Type text to have it spoken, or leave the text empty and your own phone rings so you can talk
  live; hanging up ends it. Works with Yealink phones (Intercom allowed, which is the default).
  The extension box takes one extension, a list (102,103) or "all".

### Security
- Extensions typed into Calls are checked to be plain extension names before they reach the
  phone system.

## v1.1.1 (2026-10-16)

### Added
- **MeshCore trace sweep** ("Sweep" button next to Trace in a MeshCore node's map popup): tries
  every likely route to the node one hop at a time and posts a single summary to Messages saying
  which link breaks (for example "✓ us → RKDRPTMON, ✗ RKDRPTMON → LandOfLucy"). Hops already
  proven aren't re-probed; at most 8 probes, one at a time. Takes a few minutes.

## v1.1.0 (2026-10-16)

First release numbered without a release-candidate suffix: from here on each release bumps the
third number (1.1.1, 1.1.2, ...). 1.1.0 is v1.0.0-rc291; everything below it is the rc series.

### Added
- **M17 module picker in the compose bar**: with an M17 network selected, choose the reflector
  module (room, A-Z) next to the message box. Switching relinks in a few seconds and is
  remembered until you change it, including across restarts. If the reflector refuses the new
  module, the link stays on the old one and says so.

## v1.0.0-rc290 (2026-10-16)

### Security
- Log masking also covers API keys that sit in a URL path (NASA FIRMS) rather than a `key=`
  parameter: any secret value from the config is masked wherever it appears in a log line.

## v1.0.0-rc289 (2026-10-16)

### Fixed
- **USGS DYFI reports would not send**: the form is ~1,200 characters and the compose box
  capped every message at 900. Messages going only to Winlink now allow far longer bodies
  (radio networks keep the 900 limit). The callsign is filled in automatically from the
  station's Winlink callsign, as Winlink's own form does.
- **MeshCore trace routes**: guessed routes are limited to two relays and every hop must be
  within a plausible radio distance, so traces no longer go out through same-ID repeaters
  hundreds of km away or as 9-hop round trips. (Seeing a repeater twice in a trace is
  normal: a trace returns along the path it went out on.)

### Security
- Log lines now mask `key=`, `token=`, `password=` and similar values, which were appearing
  in the server log in request URLs.

## v1.0.0-rc288 (2026-10-16)

### Added
- **USGS "Did You Feel It?" (DYFI) Winlink template** (USGS group): same JSON format, address and
  subject as Winlink's standard DYFI form, so USGS's automated mailbox can ingest it. Covers the
  main questionnaire and the building-damage checklist (tick all that apply); the estimated
  intensity is computed the same way the official form does. Send through an Internet-connected
  gateway, and mark drills as EXERCISE.

### Fixed
- **Map: 24-hour APRS tracks.** Stations that haven't beaconed in the last hour drop off the live
  map, and their recorded tracks disappeared with them. Tracks now stay visible while their layer
  is on, drawn dashed when the station is no longer live.
- **MeshCore trace** now asks the mesh for the real route to a repeater (path discovery, as the
  official MeshCore apps do) before falling back to a route guessed from overheard traffic.

### Changed
- M17: received messages record the sender's frame type/metadata, to compare with what we send
  when replies go unseen.

## v1.0.0-rc287 (2026-10-16)

### Changed
- **Messages header** (UI refresh, phase 3): fits at 1440 px with full nav labels again
  (it was dropping to icons only). The "live" text is gone (the connection dot shows the
  state, with a tooltip); the clock itself now switches UTC / local / both (the separate
  UTC button is gone); the version number moved to the right end of the system bar; the
  STANDARD mode badge only appears when the hub is in a non-standard mode (e.g. EMERGENCY).
- Header status chips (sessions, bot, operator) share one height and style; Sign out is
  an icon button on every page that has one (Messages, Ham Log).

## v1.0.0-rc286 (2026-10-15)

### Changed
- **Ham Log header** (UI refresh, phase 2): the top bar now holds only the shared nav, clock,
  connection dot and sign-out. Contest/station/operator, QSO count + score + bonuses, CAT /
  Connect Radio, My station (callsign, grid, band, mode, Set) and the colour theme moved
  into a grouped, labelled station toolbar that wraps cleanly on narrow screens. Fits at
  1440 px with full nav labels (previously cut off on the right).
- Clicking the SignalMatrix logo/page name in the header now reloads the page you are on
  instead of jumping to Messages.

## v1.0.0-rc285 (2026-10-15)

### Changed
- **New app shell (UI refresh, phase 1):** every page now shares one header: SignalMatrix
  brand + page title, a labelled grouped nav (Messages, Map, Radio, Incident, Monitor,
  Settings) in place of the 12 unlabelled emoji icons, and a live "links" pill showing how
  many network adapters are connected. Shared stylesheet `/static/ech-ui.css` sharpens
  colours, tables, inputs, tab bars and map popups site-wide. Busy headers degrade to an
  icon-only nav instead of overflowing.
- **Map tracks** are drawn in the adapter's colour (no longer the speed colour); the speed
  glow stays on the moving marker. Each point on a track is a clickable dot showing the
  time, position, average speed since the previous point, and what the node reported there
  (altitude, speed, heading, SNR, battery...). Stored with each server-side track point.
- **PSK Reporter** map layer: listed in the map key; one marker per transmitting station
  with band/mode label; popup shows distance and bearing from you, grid, bands/modes, how
  many stations heard it (farthest, best SNR), recent receptions, and draws lines to the
  receivers while open.

### Fixed
- Map: aircraft/vessels (and other broadcast-only sources) no longer show a Message button.
- Map: on open, layers that are off in your default profile (e.g. ADS-B) no longer flash
  their markers/tracks before clearing - the profile is applied before the first draw, and
  tracks are only drawn for markers actually shown.

### Notes
- Rollback point is tag `v1.0.0-rc284` (local source archive `ECH_rc284_backup.tar.gz`).
- DB: `node_positions` gains an `info` column (auto-migrated).

## v1.0.0-rc284 (2026-10-14)

### Added
- **My layer profiles** (map > Layers, top group): save the layers you have switched on as
  named profiles, pick one from a list, mark one as your default so it loads whenever you
  open the map. Stored per login on the server (follows your account across browsers) with a
  local cache; viewers can keep their own. `GET/PUT /api/user/map-profiles`.

### Fixed
- Layers menu: the new Satellites button shared an id with "Satellite IR" (GOES imagery);
  renamed so both work independently.

## v1.0.0-rc283 (2026-10-14)

### Added
- **Signed release provenance:** the SLSA workflow now builds a real release tarball from
  `VERSION`/`git archive`, signs SLSA L3 provenance (generator v2.1.0) and attaches both to
  the GitHub Release; README gains "Verifying a release" with the `slsa-verifier` command.
- README: **Legal, licensing and interference** section (licence/ID, automatic control,
  bridging onto amateur RF, Part 15/ISM limits, PSTN gateways, service terms, receive-only
  data, privacy, emergency-use limits); the installer prints a short reminder.


### Fixed
- Satellites: retired NOAA 15/18/19 replaced by Meteor-M2 3/4 (LRPT) and GOES-19/18; a
  failed CelesTrak download is retried after 2 h (CelesTrak answers 403 to repeat downloads).

## v1.0.0-rc282 (2026-10-14)

### Added
- **Satellites map layer** (Layers > Satellites): ISS, amateur satellites with CW beacons
  (AO-7, FO-29, AO-73, RS-44, CAS-9, JO-97, LilacSat-2), QO-100 (FT8 narrow-band
  transponder, GEO), NOAA weather satellites, plus **Starlink trains** (a fresh launch still
  in a line) with their next passes over the base. Click a satellite for its ground track.
  Elements from CelesTrak (cached 12 h on disk); positions via vectorised SGP4.
- Layers > Panels: hide the **Moon status** panel and the **Map key** entirely (remembered).

## v1.0.0-rc281 (2026-10-14)

### Added
- **Movement tracks on the map:** a trail is drawn behind anything that moves (aircraft,
  vessels, mobile stations). New "Movement tracks" group in the Layers menu: on/off toggle
  and a length selector (5 min - 6 h); applies to APRS, MeshCore, Meshtastic, aircraft and
  vessels, with a speed glow on the marker and trail (cyan walking, green/yellow vehicle,
  orange highway, red/magenta aircraft). Positions are recorded on the server (new `node_positions`
  table, `track_history` config: enabled/keep_days) and kept 7 days, so trails exist the
  moment the map opens; selectable 5 min - 7 days. Fixed stations (GPS jitter under ~30 m)
  draw nothing. `GET /api/map/tracks?hours=N`.
- **Scheduled backups to a second disk:** `scripts/setup_backup.sh <device|dir>` (and
  `install.sh --backup-disk X`) mounts a USB stick/disk by UUID (`nofail`), installs
  `ech-backup` and a daily timer (03:30) that snapshots the database (consistent SQLite
  backup), `/etc/ech` and `/etc/asterisk`, keeping 14 archives. Never formats a disk and
  skips (rather than filling the root disk) if the stick is missing.
- Map: Winlink RMS stations have their own toggle in "Node types".
- Messages page > Nodes tab: each adapter chip shows its node count (shown/total when
  searching).

### Changed
- POTA and SOTA map layers now show activators worldwide (the 500 km filter is gone);
  `?radius_km=N` still works on the API. Legend entries shortened ("Parks on the Air",
  "WWFF IOTA BOTA").
- Untyped MeshCore/Meshtastic nodes (such as this hub's own radio) count as Companion.

### Fixed
- **AQI layer showed only Rhode Island:** AirNow posts the current hour state by state;
  ECH only fell back to the previous hour when the response was empty. It now merges the
  previous and current hour, newest report per station winning.
- **ADS-B/AIS markers reappeared with the layer switched off:** live position updates
  from the WebSocket bypassed the layer toggles.

## v1.0.0-rc280 (2026-10-13)

### Added
- **Update-available notification:** a bell icon with an amber dot appears in the top
  navigation (admins only) when GitHub's `VERSION` is newer than the running install.
  Hover for the versions; click to open Settings -> Software update. The check is cached
  server-side for 6 hours (retried after 15 minutes on failure) and reads only the raw
  VERSION file, so it costs one small request per 6 h however many pages are open.

## v1.0.0-rc279 (2026-10-13)

### Changed
- **Messages sidebar:** data-input-only adapters (ADS-B, OpenSky, AIS, AISHub, AISstream)
  and the passive compare adapters no longer appear in the channel list. They feed the
  map and node list, not the message pane. Their alerts still show under "All".

## v1.0.0-rc278 (2026-10-13)

### Changed
- **APRS page, Objects tab** is now two panes: your objects and the heard-objects table
  (with search, live/killed filter and a live/killed/mine count) on the left, and a
  persistent **New object** form on the right with remaining-character counters and a
  **live packet preview** showing exactly what will be transmitted. "My beacon position"
  fills the coordinates; "Clear" resets the form.
- Messages and Beacon tabs get status badges (messages waiting for an ack / without one,
  and how many APRS adapters can transmit).
- Passive compare adapters are labelled with their own name in the channel list instead of
  a generic "LetsMesh (compare)", so a leftover one isn't mistaken for a removed adapter.

## v1.0.0-rc277 (2026-10-13)

### Changed
- **Installer:** mDNS (Avahi) and Prometheus are now **optional**. `scripts/install.sh`
  no longer installs/enables Avahi by default; pass `--with-mdns` for `<hostname>.local`
  and `--with-prometheus` for a local Prometheus server. The Prometheus option writes a
  scrape config that works (HTTPS to 127.0.0.1:8765); the hand-made one on the live server
  pointed at `host.docker.internal` over HTTP and never collected anything. ECH does not
  need either; the app's own `/metrics` endpoint is unchanged.

### Fixed
- Settings -> Software update: the **"Update & Restart ECH" button was stuck on
  "Restarting..." (disabled) on every page load** whenever an old update log existed, even
  though nothing was updating. An idle status now leaves the button usable.

## v1.0.0-rc276 (2026-10-12)

### Fixed
- Meshtastic messages you send now show the **channel** they went out on (e.g.
  `ch1:test`, or `DM`) in the Messages window. They were stored as "outbound", a label
  the UI hides. This includes messages the bridge or bots send on your behalf.

## v1.0.0-rc275 (2026-10-12)

### Fixed
- Meshtastic **Configure channel on device** (Settings) always failed with
  `'Node' object has no attribute 'setChannel'`. It now edits the radio's channel
  table and writes the entry with `writeChannel`.

## v1.0.0-rc274 (2026-10-12)

### Fixed
- Meshtastic: the device's channel list was read **once** at connect, so channels the
  radio hadn't finished sending (or that you added afterwards, e.g. from the phone app)
  never appeared and only channel 0 showed. It is now re-read 4 s and 15 s after connect
  and on every status poll, so the Settings channel pickers track the device.

## v1.0.0-rc273 (2026-10-12)

### Added
- README screenshots of the map weather layers and the new APRS page.

### Fixed
- Meshtastic: messages from a node the radio hadn't learned yet showed a **null sender**
  (the library sets `fromId` to None, and the fallback only applied to a missing key). The
  sender is now taken from the numeric node number (`!aabbccdd`) and shown until its name
  arrives; same fix for the destination, so direct messages are no longer mistaken for broadcasts.
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
