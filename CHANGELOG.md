# Changelog

All notable changes to SignalMatrix (ECH) are documented here, newest first.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

Full engineering detail (root causes, file locations, live-verification
notes) lives in `ECH_REQUIREMENTS_AND_PROGRESS.md`; `git log` has the
complete commit history. This file is the user-facing summary, starting
from v1.0.0-rc198 — earlier history predates this file.

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
