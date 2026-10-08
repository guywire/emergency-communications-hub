# Changelog

All notable changes to SignalMatrix (ECH) are documented here, newest first.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

Full engineering detail (root causes, file locations, live-verification
notes) lives in `ECH_REQUIREMENTS_AND_PROGRESS.md`; `git log` has the
complete commit history. This file is the user-facing summary, starting
from v1.0.0-rc198 — earlier history predates this file.

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
