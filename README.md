# SignalMatrix

**Version 1.0.0-rc255** (from the `VERSION` file — synced automatically on every deploy by `deploy/build_and_scp.ps1`)

SignalMatrix is a Python/FastAPI application that bridges multiple emergency-communications radio networks into a single web dashboard. It runs on a laptop, thin client, or Raspberry Pi at an incident command post, field site, or contest operation and lets operators monitor, log, and relay messages across all active links from a browser on the LAN.

## Who it is for

- ARES/RACES teams needing a common operating picture across Meshtastic mesh, APRS, and HF
- Served agencies that want radio traffic visible in a browser without installing amateur-radio software on every workstation
- Ham operators running ARRL Field Day, POTA, or SOTA activations who want integrated logging and CAT radio control
- Emergency management exercises where simulated traffic needs to flow through real comms gear

---

## Features at a glance

| Feature | Notes |
|---------|-------|
| **Multi-network bridging** | Meshtastic, APRS (IS + KISS TNC), MeshCore, JS8Call, Winlink/PAT, SMS (SIM7x00/SIM800L), MQTT, Reticulum/LXMF, AREDN, Asterisk/PBX |
| **Web dashboard** | Messages, map, node list, anomaly alerts, adapter status, SKYWARN + strip reports (`/reports`), analytics charts (`/analytics`) — all in the browser. The compose box shows a per-send MeshCore/Meshtastic channel picker and a Winlink subject field only when a relevant adapter is selected. |
| **Ham Radio Log** | Contest logging (Field Day, POTA, SOTA, General); ADIF/Cabrillo/CSV import; ADIF/Cabrillo/POTA/SOTA export |
| **CAT radio control** | Browser Web Serial (no software install) or server-side rigctld/Hamlib |
| **Sound-card digital modes** | CW, RTTY and PSK31 decode/encode — from a sound card on the server or from radio audio on the operator's own computer via the browser (one audio stream feeds all three decoders). AFC finds signals anywhere in the passband; noise rejection; PTT keyed over CAT/RTS/DTR around each transmission; live tuning bar in the message window (see [Sound-card digital modes](#sound-card-digital-modes-cw--rtty--psk31)) |
| **Anomaly detection** | Automatic alerts for unusual message patterns or node behaviour |
| **Simulation mode** | Built-in mock adapters let you train operators without live hardware |
| **Mesh bot** | 25 on-mesh commands — weather/alerts/METAR/tides/solar, aircraft & ship tracking, satellite passes, FCC/DXCC lookups, SKYWARN spotter report intake, SHARES Region 1 strip-report intake, trivia with scoreboards, text-adventure games (see [Mesh Bot](#mesh-bot)) |
| **SKYWARN & strip reports** | Guided report intake over the mesh (DM the bot `skywarn` or `strip`), auto-prefilled from the sending node's known position/callsign/temperature when available; combined `/reports` review page with map plotting, edit/complete/delete, and `net`/`inws`/`winlink` output formats for relaying to NWS |
| **Analytics** | `/analytics` — messages per hour per adapter, bot command usage, anomaly trends (24h/48h/7d/30d), with per-series show/hide toggles on the time charts |
| **Winlink templates** | ICS-213, ARRL Radiogram, SITREP, Winlink AI Query, and Catalog Request (weather/radar/propagation/satellite bulletins) — fill-in-the-blank, addresses and subjects itself correctly |
| **GPS time sync** | Optional NMEA receiver auto-sets system clock and base position |
| **Storage guard** | Warns when disk free falls below 1 GB or 5%; automatic message retention purge (configurable per adapter family) |

Map
<img width="1885" height="916" alt="image" src="https://github.com/user-attachments/assets/bc31414c-9c4c-44c4-9f79-711742a525e2" />

Message window
<img width="1873" height="928" alt="image" src="https://github.com/user-attachments/assets/0c2145e1-cb23-44fc-bfbc-29d0b29c642a" />

Analytics
<img width="1877" height="908" alt="image" src="https://github.com/user-attachments/assets/03ef1ed9-7e22-4266-acc9-f755e4fa8d3b" />

Anomaly Detection
<img width="1882" height="917" alt="image" src="https://github.com/user-attachments/assets/12625145-10d2-4970-832c-959afa743bcb" />

---

## Quick Start

### 1. Install Python dependencies

```bash
git clone https://github.com/guywire/emergency-communications-hub.git
cd emergency-communications-hub
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

`pyproject.toml` declares everything the core app and every built-in adapter need
(MeshCore, Meshtastic, APRS, Reticulum, the mesh bot's `satpass`, CW/RTTY/PSK31
audio modes, etc.) — `pip install -e .` is the complete install; nothing else to
pick and choose unless you're deliberately trimming it down. This also gives you
an `ech` console command (equivalent to `python -m ech.main`).

One system-level package audio modes need that pip can't provide — install it
first if you'll use `cw_audio`/`rtty_audio`/`psk31_audio`:

```bash
# Debian/Ubuntu/Raspberry Pi OS
sudo apt-get install -y libportaudio2
```

If you'd rather not pull in everything, the per-adapter table below lists the
minimum package(s) each one needs — install those individually instead of running
`pip install -e .`.

### 2. Copy and edit the config

```bash
cp config.yaml /etc/ech/config.yaml   # or keep it local
nano /etc/ech/config.yaml
```

Set `operator: callsign` to your callsign. All adapters are disabled by default — enable the ones you need (see [Configuration](#configuration) below).

### 3. Start SignalMatrix

```bash
# Use local config.yaml in current directory:
ech
# (equivalent to: python -m ech.main)

# Or point to a specific config:
ech --config /etc/ech/config.yaml
```

Open a browser to `http://<server-ip>:8765`. That is the dashboard.

To run a simulation-only demo with no hardware (uses the bundled `config-sim.yaml`):

```bash
python -m ech.main --config config-sim.yaml
```

---

## Configuration

All settings live in `config.yaml`. The file is heavily commented — read it top to bottom before deploying. Key sections:

```yaml
server:
  host: "0.0.0.0"
  port: 8765          # HTTP dashboard port

database:
  path: "ech.db"      # SQLite file; put on a path with room to grow

operator:
  callsign: "W1ABC"   # your station callsign

incident:
  name: "EXERCISE"    # shown on the dashboard header
```

**Do not commit `/etc/ech/config.yaml` to git** — it contains API keys and passwords. The `config.yaml` in the repository uses `N0CALL` placeholders only.

### Enabling adapters

**GUI method (recommended):** Settings → Adapter Configuration has an editable
card per adapter — add one, pick its type, fill in fields with real inputs
(including a dropdown of actually-attached serial devices for any
`port`/`device` field), enable/disable, and save. Changes still need an ECH
restart to take effect, same as editing the file by hand.

**Manual method:** every adapter is commented out by default in
`config.yaml`. Find the block for the hardware you have, uncomment it, and
fill in the port or host:

```yaml
adapters:
  - type: meshtastic
    name: meshtastic-usb
    transport: serial
    port: /dev/ttyUSB0    # Windows: COM3, etc.
    channel_idx: 0
```

Mock (simulated) adapters are named `mock_meshtastic`, `mock_aprs`, `mock_meshcore`, etc. Use them to test the dashboard without hardware.

---

## HTTPS / TLS Setup

### Why you need HTTPS

ECH's browser-side CAT radio control uses the **Web Serial API**. The Web Serial API is only available in a **Secure Context** — meaning the page must be served over HTTPS. Without HTTPS, the "Connect Radio" button does not appear.

HTTPS also encrypts operator credentials on the LAN, which matters at large events where the Wi-Fi may be shared.

### Recommended: reverse proxy with Caddy

Run ECH in plain HTTP (the default — leave `tls:` disabled in `config.yaml`) and put [Caddy](https://caddyserver.com/) in front of it to handle HTTPS. This is the recommended setup: `caddy trust` installs Caddy's local CA into the OS trust store **in one command**, instead of clicking through a per-OS certificate-import wizard by hand (the previous `/ca.crt` download-and-install flow below still works, but is fiddlier and easy to get stuck on, especially on mobile).

**Install Caddy** (see [caddyserver.com/docs/install](https://caddyserver.com/docs/install) for other platforms):

```bash
# Debian/Ubuntu/Raspberry Pi OS
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

**Caddyfile** (`/etc/caddy/Caddyfile`) — reverse-proxies HTTPS to ECH's plain-HTTP port:

```caddyfile
# LAN-only deployment (no public domain) — Caddy issues its own locally-trusted cert
https://<server-ip-or-hostname> {
    tls internal
    reverse_proxy localhost:8765
}
```

If the ECH box has a real, internet-reachable domain name instead (less common for a field/incident deployment, but applicable for a fixed station), drop the `tls internal` line and give Caddy a real domain — it automatically gets a publicly-trusted Let's Encrypt certificate, and **no device needs to trust anything at all**:

```caddyfile
ech.example.org {
    reverse_proxy localhost:8765
}
```

**Serving ECH under a sub-path instead of its own host/port** (e.g. `https://host/ech/`, alongside other services on the same domain) needs one extra step on each side — ECH has to know its own external prefix so its generated links/API calls/WebSocket connections include it:

```caddyfile
example.org {
    handle_path /ech/* {      # handle_path strips the /ech prefix before forwarding
        reverse_proxy localhost:8765
    }
}
```

```yaml
# config.yaml
server:
  base_path: "/ech"    # must match the Caddyfile's handle_path prefix exactly
```

Restart Caddy, then trust its local CA once on the server itself:

```bash
sudo systemctl restart caddy
sudo caddy trust
```

For every *other* device (laptops, tablets, phones) that will open the dashboard: grab Caddy's root certificate from the server —

```bash
# on the ECH server, Caddy's local CA root lives here when run as a systemd service:
sudo cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
```

— copy it to the other device as `caddy-root.crt`, and install it the same way as any root CA:

| Platform | Steps |
|---|---|
| **Windows** | Double-click the file → **Install Certificate** → **Local Machine** (admin) or **Current User** → **Place all certificates in the following store** → **Trusted Root Certification Authorities** → **Finish** |
| **macOS** | Double-click → Keychain Access opens → find the Caddy cert → double-click it → expand **Trust** → set **Always Trust** |
| **Linux / Chrome** | `chrome://settings/certificates` → **Authorities** → **Import** → check **Trust this certificate for identifying websites** |
| **Firefox** | **Settings** → **Privacy & Security** → **Certificates** → **View Certificates** → **Authorities** → **Import** → check **Trust this CA to identify websites** |
| **Android (Chrome)** | Transfer the file to the device → **Settings** → **Security** → **Encryption & credentials** → **Install a certificate** → **CA certificate** → **Install anyway** |

Then browse to `https://<server-ip-or-hostname>` (port 443, Caddy's default — no port number needed in the URL). The padlock appears with no warnings, and the Ham Log page shows the **Connect Radio** button.

### Alternative: ECH's built-in self-signed CA

If you'd rather not run a separate reverse proxy, ECH can generate and serve its own CA directly — no Caddy install required, but each device needs its own manual certificate-import steps (same per-OS dance as above, pointed at `ech-ca.crt` instead of Caddy's root).

In `config.yaml`:

```yaml
tls:
  enabled: true
  https_port: 8766      # HTTPS runs alongside HTTP on 8765
  data_dir: "."         # CA and server cert/key files are written here
```

Restart SignalMatrix — it prints the CA cert path in the startup log and re-issues a server certificate covering every IP the server currently has, so nothing needs regenerating when you redeploy at a new site with a different DHCP-assigned IP.

Download the CA cert by browsing to `http://<server-ip>:8765/ca.crt` on each device, then follow the same per-OS import steps as the Caddy table above (substitute `ech-ca.crt` for the Caddy root cert). Connect via `https://<server-ip>:8766`.

### Optional: mDNS (access by name instead of IP)

Install `zeroconf` and SignalMatrix advertises itself on the local network as `ech.local`:

```bash
pip install zeroconf
```

With Caddy in front, point the Caddyfile's site address at `ech.local` instead of an IP; with the built-in CA, browse to `https://ech.local:8766`. Either way this works from any device on the same subnet regardless of IP address.

### In-app TLS guide

SignalMatrix includes a built-in setup page at `/tls-setup` that shows the built-in-CA instructions above alongside the current server's IP addresses and a direct download link for `ech-ca.crt` — useful if you're going the no-Caddy route.

---

## CAT Radio Control

CAT (Computer Aided Transceiver) lets ECH read and set the frequency, band, and mode on your radio. There are two ways to do it depending on where the radio is physically connected.

### Method A: Web Serial (recommended for remote operators)

The radio connects to the **operator's laptop**, not the server. No drivers or software beyond Chrome or Edge are needed.

**Requirements:**
- HTTPS must be enabled (see above)
- Chrome or Edge browser (Firefox does not support Web Serial)

**Steps:**
1. Open the Ham Log page over HTTPS — `https://<server-ip-or-hostname>/hamlog` behind Caddy (recommended, port 443), or `https://<server-ip>:8766/hamlog` with ECH's built-in CA.
2. Click the **Connect Radio** button in the header.
3. Select your **protocol**:
   - **Icom CI-V** — for Icom radios and Xiegu G90/G106/X6100
   - **Kenwood text** — for Elecraft (K3/K4/KX3), Kenwood (TS-590/TS-2000), and Yaesu FT-991A
4. Select the CI-V address if using Icom CI-V:
   - Xiegu G90: `0x70`
   - Icom IC-7300: `0x94`
   - Icom IC-705: `0x91`
   - Icom IC-9700: `0x98`
5. Select the **baud rate**:
   - Xiegu G90: 19200 (default)
   - Most Icom: 9600 (default)
   - Elecraft K3/K4: 57600
6. Grant the browser permission to use the serial port when prompted.

Once connected, frequency, band, and mode auto-fill in the log form. Use the **→ Radio** button to send the log form's frequency and mode back to the radio.

### Method B: Server-side rigctld (Hamlib)

Use this when the radio is physically connected (USB or serial) to the machine running ECH — for example, an IC-7300 on the ops desk connected to the ECH thin client.

#### Step 1: Install Hamlib

```bash
# Debian/Ubuntu/Raspberry Pi OS
sudo apt install libhamlib-utils

# Or download from https://hamlib.sourceforge.net
```

Check available rig model numbers:

```bash
rigctl -l | grep -i "xiegu\|icom\|yaesu\|elecraft\|kenwood"
```

#### Step 2: Start rigctld for your radio

Replace `/dev/ttyUSB0` with your actual port (Windows: `COM3`, etc.).

```bash
# Xiegu G90 (CI-V, 19200 baud)
rigctld -m 3083 -r /dev/ttyUSB0 -s 19200 -t 4532

# Icom IC-7300 (9600 baud)
rigctld -m 3073 -r /dev/ttyUSB0 -s 9600 -t 4532

# Icom IC-705
rigctld -m 3085 -r /dev/ttyUSB0 -s 9600 -t 4532

# Icom IC-9700
rigctld -m 3081 -r /dev/ttyUSB0 -t 4532

# Yaesu FT-991A (38400 baud)
rigctld -m 1035 -r /dev/ttyUSB0 -s 38400 -t 4532

# Yaesu FT-817/818 (9600 baud)
rigctld -m 1039 -r /dev/ttyUSB0 -s 9600 -t 4532

# Yaesu FT-DX10 (38400 baud)
rigctld -m 1043 -r /dev/ttyUSB0 -s 38400 -t 4532

# Elecraft K3/K4 (38400 baud)
rigctld -m 2029 -r /dev/ttyUSB0 -s 38400 -t 4532

# No radio attached (dummy — for testing)
rigctld -m 1 -t 4532
```

Run this in a terminal before starting ECH, or add it to a systemd unit so it starts automatically.

#### Step 3: Enable CAT in config.yaml

```yaml
cat:
  enabled: true
  rigctld_host: localhost
  rigctld_port: 4532
  poll_interval: 2.0      # seconds between freq/mode polls
  auto_fill_hamlog: true  # push updates to ham log via WebSocket
```

When ECH connects to rigctld, the Ham Log header shows a green **CAT** pill. Frequency, band, and mode update in the log form every two seconds.

---

## Ham Radio Log

The Ham Log (`/hamlog`) supports contest, portable, and general operating.

### Supported contests

| Contest | `contest:` value |
|---------|-----------------|
| ARRL Field Day | `ARRL-FIELD-DAY` |
| Parks on the Air | `POTA` |
| Summits on the Air | `SOTA` |
| General / casual | `GENERAL` |

Configure in `config.yaml`:

```yaml
hamlog:
  callsign: "W1ABC"
  operator: "W1ABC"
  grid: "FN42"
  power: "LOW"
  contest: "ARRL-FIELD-DAY"
  field_day_class: "2A"
  field_day_section: "ME"
```

### Import formats

Upload an existing log from the **Import** button:
- **ADIF** (`.adi` or `.adif`) — from any logging software
- **Cabrillo** (`.cbr` or `.log`)
- **CSV** — column headers must include `callsign`, `freq`, `mode`, `date`, `time`

### Export formats

From the **Export** menu:
- **ADIF** — for upload to QRZ, eQSL, LoTW
- **Cabrillo** — for contest submission
- **POTA CSV** — for pota.app upload
- **SOTA CSV** — for sota.org upload

### Live upload (optional)

Add API credentials to `/etc/ech/config.yaml` for automatic log uploads:

```yaml
hamlog:
  qrz_api_key: ""           # QRZ.com XML-plan logbook key
  clublog_api_key: ""
  clublog_email: ""
  pota_username: ""
  pota_password: ""
  sota_username: ""
  sota_password: ""
```

---

## Deployment Notes

### Target hardware

SignalMatrix is designed to run on a thin client or mini PC with an 8 GB SSD. Recommended minimum: 4-core x86-64, 4 GB RAM, 8 GB storage. A Raspberry Pi 4 (4 GB) also works for most adapter combinations.

### Storage warnings

SignalMatrix monitors free disk space and displays a banner warning when:
- Free space drops below 1 GB, **or**
- Free space drops below 5% of the partition

On an 8 GB SSD with the OS already installed this threshold can be reached within a few days of heavy message traffic.

### Automatic message retention (purge)

SignalMatrix runs an hourly purge that deletes messages older than a configurable threshold. Configure in `config.yaml`:

```yaml
retention:
  enabled: true
  aprs: 12          # purge APRS messages older than 12 hours
  meshcore: 36      # purge MeshCore messages older than 36 hours
  meshtastic: 36    # purge Meshtastic messages older than 36 hours
  archive_days: 30  # purged messages are archived (restorable) before deletion
```

The prefix (e.g. `aprs`, `meshcore`, `meshtastic`) is matched against the `source_adapter` column. Add any adapter name prefix to the `retention:` block to cover additional adapters.

Before a message is purged, it's archived to a dated, restorable file
(`message_archive/YYYY-MM-DD.jsonl` next to the database) rather than being
deleted outright. Archive files are themselves deleted once older than
`archive_days`. Restore a given day's messages from **Settings → Data
Retention**.

Retention settings can also be adjusted live from the **Settings → Data Retention** section without restarting ECH.

### Moving the database to a larger drive

If the root partition is full, move the database to a USB or secondary drive:

```bash
# Stop ECH
sudo systemctl stop ech

# Move the database
sudo mv /var/lib/ech/ech.db /mnt/usb/ech.db

# Symlink it back so the existing config still works
sudo ln -s /mnt/usb/ech.db /var/lib/ech/ech.db

# Make sure the ECH service user owns the new directory
sudo chown ech:ech /mnt/usb

# Restart
sudo systemctl start ech
```

Alternatively, update `config.yaml` to point directly to the new path:

```yaml
database:
  path: "/mnt/usb/ech.db"
```

SQLite needs to create `-wal` and `-shm` journal files alongside the database. Make sure the `ech` service user has write permission to the directory (`chown ech:ech /mnt/usb`).

### Running as a service (Linux)

Create `/etc/systemd/system/ech.service`:

```ini
[Unit]
Description=ECH SignalMatrix
After=network.target

[Service]
User=ech
WorkingDirectory=/opt/ech
ExecStart=/opt/ech/venv/bin/python -m ech.main --config /etc/ech/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ech
```

### GPS time sync

If you have a USB or UART GPS receiver (u-blox or similar), ECH can set the system clock from GPS and broadcast the base position to all adapters. Configure it from **Settings → Base Location & GPS** — it lists attached serial devices to pick from, no need to find the port name yourself. (Equivalently, you can still hand-edit the `gps:` block in `config.yaml`.) Requires ECH to run as root (or with `CAP_SYS_TIME`) for clock sync; either way takes effect after a restart.

### Firewall

Open ports on the ECH machine:

| Port | Protocol | Purpose |
|------|----------|---------|
| 8765 | TCP | HTTP dashboard |
| 8766 | TCP | HTTPS dashboard (if using ECH's built-in self-signed CA) |
| 443  | TCP | HTTPS dashboard (if using the recommended Caddy reverse proxy) |

No inbound ports are required for most adapters (they connect outward). Exception: JS8Call and Pat must be reachable on their respective ports if ECH runs on a different machine than those services.

---

## Adapter Quick-Reference

| Adapter type | config.yaml `type:` | External requirement |
|---|---|---|
| Meshtastic USB/TCP | `meshtastic` | `pip install meshtastic` |
| APRS Internet | `aprs_is` | `pip install aprslib` |
| APRS KISS TNC / Direwolf | `aprs_kiss` | Direwolf or hardware TNC |
| MeshCore serial/TCP | `meshcore` | `pip install pycryptodome pyserial-asyncio` — custom protocol client, no `meshcore` PyPI package needed |
| LetsMesh MQTT | `mqtt` (with `pubkey_auth`) | `pip install aiomqtt pycryptodome` |
| LetsMesh packet comparison | `letsmesh_compare` | `pip install aiomqtt pycryptodome` — merges path data from other LetsMesh observers into a local MeshCore adapter's topology graph; no messages added to the feed |
| MQTT generic | `mqtt` | `pip install aiomqtt` |
| JS8Call HF | `js8call` | JS8Call app running with TCP API on port 2442 |
| Winlink / Pat | `pat_winlink` | Pat running with HTTP API |
| SMS modem | `sms` | SIM800L / SIM7600 on USB serial |
| Reticulum / LXMF | `reticulum` | `pip install rns lxmf` |
| AREDN mesh (PBX bridge) | `aredn_ami` | `pip install aiohttp` |
| AREDN mesh (MeshChat) | `aredn_meshchat` | `pip install httpx` — talks to a MeshChat CGI backend reachable on the mesh |
| AX.25 packet BBS | `ax25_bbs` | `axcall` (Linux AX.25 tools) + a configured AX.25 port |
| Asterisk / PBX | `asterisk` | Asterisk with AMI enabled — talks raw AMI over a TCP socket, no extra package needed |
| M17 digital voice reflector | `m17_reflector` | None — software-only UDP client to an M17 reflector (e.g. mrefd), no radio hardware required |
| DAPNET paging | `dapnet` | `pip install httpx` — DAPNET account (hampager.de) |
| DMR via BrandMeister | `dmr_brandmeister` | `pip install dmr_utils3 bitarray libscrc` — Homebrew/DMRplus protocol to a BrandMeister-compatible master |
| ADS-B / PiAware | `adsb` | dump1090, PiAware, or readsb on the LAN |
| Aircraft tracking (OpenSky) | `opensky` | `pip install aiohttp` — free OpenSky Network account |
| AIS vessels (local SDR) | `ais_catcher` | AIS-catcher with HTTP server enabled |
| AIS vessels (AISHub) | `aishub` | Free aishub.net account + API key |
| AIS vessels (aisstream.io) | `aisstream` | Free aisstream.io API key |
| CW / Morse over sound card | `cw_audio` | `pip install sounddevice numpy` (+ `libportaudio2` on Linux) for a server sound card; `numpy` only for browser audio. See [Sound-card digital modes](#sound-card-digital-modes-cw--rtty--psk31) |
| RTTY over sound card | `rtty_audio` | Same as cw_audio (45.45 Bd Baudot, 170 Hz shift, AFC, reverse) |
| PSK31 over sound card | `psk31_audio` | Same as cw_audio (31.25 Bd BPSK varicode, AFC carrier lock) |
| FT8/FT4 via WSJT-X | `wsjtx` | WSJT-X with "UDP Server" pointed at ECH (port 2237); RX-only |

All adapters also have mock equivalents (`mock_meshtastic`, `mock_aprs`, etc.) for simulation and training.

**Browser-hosted hardware:** a MeshCore node or radio audio plugged into the *operator's*
computer (not the server) can back an adapter remotely: open `/remote-hw` in Chrome/Edge
over HTTPS, connect the device (Web Serial) or radio audio (Web Audio), and configure the
matching adapter with `transport: browser` (MeshCore) or `input_device: browser`
(CW/RTTY/PSK31). The bridge lives in that tab — keep it open (while connected, links
open in a new tab and closing asks for confirmation); adapters re-attach automatically
when the tab reconnects.

**Bridges (`bridge_rules`)** link ONE endpoint on each side — a mesh channel ↔ APRS
messages addressed to a group such as `EMCOMM` — not whole adapters. Each bridge sets
its direction (one-way or bidirectional), what crosses (channel text; DMs opt-in —
positions/telemetry are not bridged), a per-direction rate limit, sender attribution
(`Bob: …`), the far side's length limit (APRS 67 chars), and optionally "callsign
senders only". Bridged text is never re-bridged, network echoes are dropped and
multi-path duplicates are forwarded once. Build bridges in **Settings → Bridge rules**
(new bridges start in *dry run*: they log what they would forward until you press
*Go live*); live counters and a decision log are shown there.

```yaml
bridge_rules:
  - name: mesh-aprs-emcomm
    a: {adapter: meshtastic-usb, channel: 2}
    b: {adapter: aprs-internet, to: EMCOMM}   # APRS: send to and listen for EMCOMM
    direction: both                           # a_to_b | b_to_a | both
    types: [text]                             # add dm to include direct messages
    rate_per_min: 6
```

**APRS-IS filter tip:** keep the radius in `filter: "r/<lat>/<lon>/<km>"` tight. A wide
radius (e.g. 250 km) pulls in the whole region's digipeater beacons and ship-AIS objects —
observed at 8,000+ messages/day — which bloats the database and drowns out mesh traffic.
60 km is plenty for local situational awareness.

---

## Sound-card digital modes (CW / RTTY / PSK31)

SignalMatrix decodes and sends CW, RTTY (45.45 Bd, 170 Hz) and PSK31 itself — no fldigi
needed. Decoded transmissions land in the message feed; sending one is a normal send from
the compose box with the CW/RTTY/PSK adapter selected.

### Where the audio comes from

* **Server sound card** — `input_device: "USB Audio"` (name substring, index, or `null`
  for the default). Needs `sounddevice` + PortAudio on the server.
* **The operator's computer, via the browser** — `input_device: browser`. Open
  `/remote-hw` in Chrome/Edge over HTTPS, pick the radio's audio input/output, and click
  *Connect radio audio*. Give all three modem adapters the same `browser_session` and
  one browser stream feeds every decoder at once:

```yaml
adapters:
  - type: cw_audio
    name: cw-remote
    input_device: browser
    browser_session: radio-audio   # the session name entered on /remote-hw
    wpm: 20                        # TX speed
  - type: rtty_audio
    name: rtty-remote
    input_device: browser
    browser_session: radio-audio
  - type: psk31_audio
    name: psk31-remote
    input_device: browser
    browser_session: radio-audio
```

The `/remote-hw` page shows an input level meter (aim for roughly −30 to −10 dBFS with
signal present; it flags *silent* and *clipping*).

### Receive behaviour

* **AFC** (`auto_tune: true`, default): PSK31 finds and locks its carrier anywhere in the
  passband; RTTY finds the strongest tone pair at the configured shift; CW retunes to each
  transmission's pitch. Tune the radio anywhere sensible — no need to hit an exact audio
  frequency. RTTY `reverse: true` for the inverted (USB) tone sense.
* **Noise rejection**: static crashes and noise-only bursts are dropped (CW rejects
  impossible speeds above `max_wpm`, lone E/T noise blips, and glitch marks; PSK31
  rejects anything that isn't real BPSK varicode, so it ignores CW/RTTY on a shared stream).
* **Sensitivity 1–5** (`sensitivity`, default 3): one knob for every mode — 1 is strict,
  3 is the measured zero-false-decode default, 4–5 dig for weak signals at the cost of
  occasional junk.

### Message-window modem bar

Select a CW/RTTY/PSK adapter in the compose bar and a tuning row appears: pitch/mark/
carrier, AFC, TX wpm (CW), shift/baud/reverse (RTTY), sensitivity, the last copy's
measured speed/frequency/SNR, **⇆ Match** (CW: set your TX speed and pitch to the station
you just copied; RTTY/PSK31: lock the tracked frequency) and **💾 Save** (write to
`config.yaml`). Changes apply instantly. Same settings via `GET/POST
/api/adapters/<name>/modem`.

### Transmit and PTT

With browser audio, **the `/remote-hw` page keys the radio** over the CAT serial port
(connect the CAT card on the same page) around each transmission: PTT on → TX delay →
audio → tail → PTT off. Choose the keying method under *Transmit keying (PTT)*:
CI-V, Kenwood/Elecraft `TX;`/`RX;`, Yaesu `TX1;`/`TX0;`, the RTS or DTR line (DigiRig and
most USB interfaces use RTS), or VOX. Safety: 3-minute stuck-key watchdog, unkey on
disconnect or page close, CAT polling paused while transmitting, and received audio is
not decoded while keyed. Use **Test PTT (2 s tone)** into a dummy load to set the
computer's output level so ALC barely moves — overdriven PSK31/RTTY is distorted and wide.

With a server sound card, `ptt: cat` keys through the server's rigctld CAT controller;
otherwise the radio's VOX keys itself.

FT-817/818/857/897 use 5-byte binary CAT, which the browser CAT card does not speak — use
RTS or VOX for PTT on those.

---

## ADS-B / Aircraft Tracking (PiAware / dump1090)

The `adsb` adapter polls a local [PiAware](https://www.flightaware.com/ware/piaware/), [dump1090-fa](https://github.com/flightaware/dump1090), [tar1090](https://github.com/wiedehopf/tar1090), or [readsb](https://github.com/wiedehopf/readsb) JSON feed and shows aircraft as map nodes. No messages are added to the text inbox — all entries have `msg_type=position`.

### Setup

PiAware / dump1090 must already be running and reachable from the ECH machine. No additional Python packages are needed.

### Configuration

```yaml
adapters:
  - type: adsb
    name: adsb
    host: 192.168.6.5       # IP of the PiAware / dump1090 device
    port: 80                # HTTP port (default 80)
    # path: /skyaware/data/aircraft.json   # auto-detected if omitted
    poll_interval: 10       # seconds between polls (default 10)
    stale_sec: 120          # remove aircraft not seen for this long (default 120)
```

**Auto-detection:** If `path` is omitted, ECH tries these paths in order until one responds:
- `/skyaware/data/aircraft.json` (PiAware / dump1090-fa)
- `/tar1090/data/aircraft.json` (tar1090)
- `/dump1090/data/aircraft.json` (classic dump1090)
- `/dump1090-fa/data/aircraft.json`

For the **mesh bot `overhead` command**, the bot reads dump1090's JSON directly from the local filesystem (faster than HTTP). Set `dump1090_path` in the `mesh_bot:` block — see [Mesh Bot](#mesh-bot) below.

---

## AIS Vessel Tracking (AIS-catcher)

The `ais_catcher` adapter polls a local [AIS-catcher](https://github.com/jvde-github/AIS-catcher) HTTP server and shows vessels as map nodes. No messages are added to the text inbox.

### Setup

Install and start AIS-catcher with its HTTP server enabled:

```bash
# Install
sudo apt install ais-catcher          # or build from source

# Start with HTTP server on port 8100
ais-catcher -v 2 -H 0.0.0.0 8100 RTLSDR
```

To run persistently, create a systemd unit or add to `/etc/rc.local`. AIS-catcher must be reachable from the ECH machine on its HTTP port.

### Configuration

```yaml
adapters:
  - type: ais_catcher
    name: ais
    host: 192.168.6.5       # IP of the AIS-catcher device (or localhost)
    port: 8100              # AIS-catcher HTTP port (default 8100)
    # path: /vessels.json   # auto-detected if omitted
    poll_interval: 30       # seconds between polls (default 30)
    stale_sec: 300          # remove vessels not updated for this long (default 300 = 5 min)
```

**Auto-detection:** If `path` is omitted, ECH tries `/vessels.json`, `/ships.json`, `/json`, and `/` in order.

---

## Mesh Bot

When `mesh_bot: enabled: true`, any node on the mesh can send text commands to the SignalMatrix node. ECH replies by DM (default) or channel broadcast.

```yaml
mesh_bot:
  enabled: true
  channels: ["ch0", "ch2", "ch4"]  # channels to listen on ("ch2", "2", "ch2:name", or "*")
  mention_required_channels: ["ch0", "ch4"]  # see "Mention gating" below
  mention_name: "SM"            # name the bot answers to when @-mentioned
  adapters: []                  # [] = all adapters; ["meshcore-1"] = one adapter only
  reply_dm: true                # true = DM sender; false = reply to channel
  per_user_cooldown_sec: 5      # rate-limit per sender (all commands share this)
  global_cooldown_sec: 2        # minimum gap between any two bot replies (flood guard)
  max_reply_len: 160            # hard cap — keeps reply to one LoRa payload
```

### Mention gating on busy channels

Several command words are ordinary English (`weather`, `help`, `sun`, `ping`, …), so on a
general-conversation channel a sentence like *"nice weather today"* would trigger the bot.
Any channel listed in `mention_required_channels` requires an explicit `@<mention_name>`
(e.g. `@SM weather`) before **any** command fires; channels not listed (e.g. a dedicated
bot channel) respond to bare command words. Replies to something the bot itself just asked
(a trivia answer, a category pick) never need the mention. DMs always work without it.

### Commands

| Command | What it does |
|---------|-------------|
| `ping` | SignalMatrix replies with signal report (SNR, hops) |
| `weather 04101` / `wx 04101` | Current NWS conditions + forecast for a US zip code |
| `overhead` | Closest aircraft within radius from a local dump1090 instance |
| `satpass [name]` | Next pass of ISS or a named satellite visible from base position |
| `solar` | Current solar flux (SFI), sunspot number, K-index from hamqsl.com |
| `tide` | Today's NOAA high/low tide times and heights (requires `tide_station` config) |
| `metar [ICAO]` | METAR aviation weather for a configured or specified ICAO station |
| `alerts` | Active NWS weather alerts for the configured area |
| `sun` | Sunrise, sunset, and solar noon for base position |
| `nodes` | Number of known mesh nodes and last-heard times |
| `aprs` | Recent APRS messages from the APRS-IS adapter |
| `anomalies` | Any active anomaly alerts |
| `ships` | Nearby AIS vessels (requires an AIS adapter) |
| `fcc <callsign>` | FCC license lookup |
| `dxcc <prefix>` | DXCC entity lookup |
| `contest` | Upcoming ham radio contests |
| `grid [locator or lat,lon]` | Base-position grid square, or convert between grid and coordinates |
| `moon` | Moon phase, rise and set times |
| `id` | Bot version and identity |
| `path` | The relay route YOUR message took to reach the bot, decoded to repeater names |
| `trace <node name>` | Fire a real active trace probe at a named node and reply with its measured hop path (unlike `path`, which only reports your own last message's route) |
| `dad` | A dad joke |
| `skywarn` | SKYWARN spotter reports — see below |
| `strip` | SHARES Region 1 "Response Creator" strip reports — see below |
| `trivia [category]` | Multiple-choice trivia; auto-continues, `trivia stop` to end |
| `score` / `lb` | Your trivia score / the leaderboard |
| `mud` | Text-adventure games (TinyMUD, Colossal Cave Adventure, Derelict). DM = private game; on a dedicated bot channel = one shared game anyone can drive. Not available on mention-required channels, where it would swallow normal chat |
| `help` | Lists available commands |

No API key is required for any command (except `aprs_fi_key` for some APRS features).

### `skywarn` — spotter report intake

DMing the bot `skywarn` starts a guided form (callsign, spotter ID, location, event type,
temperature, wind, hail size, precipitation, notes). Reports are **logged locally only —
never auto-submitted to NWS** (there is no public NWS submission API), and the bot's
confirmation says so. They appear on the `/reports` dashboard page (live via WebSocket)
with edit / complete / delete actions. Relay helpers:

| Sub-command | Output |
|---|---|
| `skywarn <callsign> <report>` | One-line quick report, skips the guided form |
| `skywarn last` | Your most recent logged report |
| `skywarn net` | Last report phrased for reading to a SKYWARN net |
| `skywarn inws` | Last report in NWS Local-Storm-Report style for manual iNWS entry |
| `skywarn winlink` | Emails the last report via the Winlink adapter (`skywarn_winlink_to` config) |

### `strip` — SHARES Region 1 strip reports

DMing the bot `strip` (or naming a template directly, e.g. `strip skywarn`) starts a
guided form built from the SHARES Region 1 "Response Creator" RI strip templates: GYX CAR
SKYWARN, LOCALWX, SITREP, HURRICANE REPORT, and WXOBS. You can also paste a complete
slash-delimited strip in one message instead of stepping through the guided form.

Fields the bot can determine from the sending node's own shared position/telemetry —
call sign, MGRS grid, and temperature — are pre-filled automatically and skipped in the
guided flow; everything else is asked one field at a time. To keep the guided back-and-forth
out of the main message feed, session prompts and answers are tagged as bot-session traffic
and shown instead as a live "active" indicator in the dashboard header's bot-status
popover (`/api/bot/sessions`).

On completion the bot logs a plain-language summary (all answered fields) to the message
log, saves the report (WXOBS reports are additionally radiogram/MARS-encoded), and — if the
report includes MGRS or lat/lon — plots it on the map (purple = pending, green = sent).
Ask to relay it via Winlink or skip. Reports appear on the same `/reports` dashboard page as
SKYWARN reports, filterable by kind and status.

### Observer position

`overhead` and `satpass` both need to know where you are. Set this once in the `mesh_bot:` block. If not set here, ECH falls back to the coordinates in `weather_service:` (the NWS weather section).

```yaml
mesh_bot:
  lat: 44.1059    # decimal degrees, positive = North
  lon: -69.1128   # decimal degrees, negative = West
```

### `overhead` — aircraft within range

Reads dump1090's aircraft JSON directly from the local filesystem (no HTTP round-trip). The default path matches a standard PiAware / dump1090-fa install:

```yaml
mesh_bot:
  dump1090_path: "/run/dump1090-fa/aircraft.json"   # default
  overhead_radius_nm: 20                            # search radius in nautical miles
```

Common paths by install type:

| Install | aircraft.json path |
|---------|-------------------|
| PiAware / dump1090-fa | `/run/dump1090-fa/aircraft.json` |
| dump1090 (classic) | `/run/dump1090/aircraft.json` |
| readsb | `/run/readsb/aircraft.json` |
| tar1090 | `/run/tar1090/aircraft.json` |

If ECH runs on a **different machine** than dump1090, mount the path via NFS/sshfs or switch to the `adsb` adapter and let `overhead` use the same JSON over HTTP — set `dump1090_path` to a URL instead (e.g., `http://192.168.6.5/skyaware/data/aircraft.json`).

### `tide` — NOAA tide predictions

Returns today's high/low tide times and heights for a configured NOAA tide station.

```yaml
mesh_bot:
  tide_station: "8418150"   # NOAA CO-OPS station ID (Portland ME = 8418150)
```

Find your station ID at [tidesandcurrents.noaa.gov](https://tidesandcurrents.noaa.gov). Reply format: `TIDES Portland: H06:12(10.2ft) L12:31(0.4ft) H18:45(9.8ft) L01:02(0.8ft)`

No API key is required.

### `satpass` — next satellite pass

Requires `pip install skyfield`. On first use, ECH downloads TLE data from CelesTrak and caches it locally. Configure which satellites to track:

```yaml
mesh_bot:
  tle_targets:
    - "ISS (ZARYA)"   # default
    - "NOAA 19"       # default
    - "NOAA 18"       # default
    - "NOAA 15"
    - "ARISS"
```

Names must match the TLE catalog name (case-insensitive). Use `satpass iss`, `satpass noaa 19`, etc. to query a specific satellite. Without an argument, SignalMatrix picks the soonest pass among all configured targets.

Install skyfield:

```bash
pip install skyfield
```

---

## License

SignalMatrix is provided for use by amateur radio operators and served emergency agencies. See LICENSE for terms.
