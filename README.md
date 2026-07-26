# SignalMatrix

**Version 1.0.0-rc185** (authoritative version is always the `VERSION` file — this banner is updated manually and can lag)

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
| **Web dashboard** | Messages, map, node list, anomaly alerts, adapter status, SKYWARN + strip reports (`/reports`), analytics charts (`/analytics`) — all in the browser |
| **Ham Radio Log** | Contest logging (Field Day, POTA, SOTA, General); ADIF/Cabrillo/CSV import; ADIF/Cabrillo/POTA/SOTA export |
| **CAT radio control** | Browser Web Serial (no software install) or server-side rigctld/Hamlib |
| **Anomaly detection** | Automatic alerts for unusual message patterns or node behaviour |
| **Simulation mode** | Built-in mock adapters let you train operators without live hardware |
| **Mesh bot** | 25 on-mesh commands — weather/alerts/METAR/tides/solar, aircraft & ship tracking, satellite passes, FCC/DXCC lookups, SKYWARN spotter report intake, SHARES Region 1 strip-report intake, trivia with scoreboards, text-adventure games (see [Mesh Bot](#mesh-bot)) |
| **SKYWARN & strip reports** | Guided report intake over the mesh (DM the bot `skywarn` or `strip`), auto-prefilled from the sending node's known position/callsign/temperature when available; combined `/reports` review page with map plotting, edit/complete/delete, and `net`/`inws`/`winlink` output formats for relaying to NWS |
| **Analytics** | `/analytics` — messages per hour per adapter, bot command usage, anomaly trends (24h/48h/7d) |
| **GPS time sync** | Optional NMEA receiver auto-sets system clock and base position |
| **Storage guard** | Warns when disk free falls below 1 GB or 5%; automatic message retention purge (configurable per adapter family) |

<img width="1885" height="916" alt="image" src="https://github.com/user-attachments/assets/bc31414c-9c4c-44c4-9f79-711742a525e2" />
Map

<img width="1873" height="928" alt="image" src="https://github.com/user-attachments/assets/0c2145e1-cb23-44fc-bfbc-29d0b29c642a" />
Message window

<img width="1877" height="908" alt="image" src="https://github.com/user-attachments/assets/03ef1ed9-7e22-4266-acc9-f755e4fa8d3b" />
Analytics

<img width="1882" height="917" alt="image" src="https://github.com/user-attachments/assets/12625145-10d2-4970-832c-959afa743bcb" />
Anomaly Detection

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

Every adapter is commented out by default. Find the block in `config.yaml` for the hardware you have, uncomment it, and fill in the port or host:

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

### How ECH handles certificates

SignalMatrix generates its own Certificate Authority (CA) the first time it starts with TLS enabled. Every subsequent start, it re-issues a server certificate that includes every IP address the server has at that moment. This means the certificate is always valid no matter which IP your contest-site DHCP assigns — you do not need to regenerate anything when you pack up and redeploy at a new site.

You trust the CA once. After that, every ECH deployment is automatically trusted.

### Enabling TLS

In `config.yaml`, uncomment and edit the `tls` block:

```yaml
tls:
  enabled: true
  https_port: 8766      # HTTPS runs alongside HTTP on 8765
  data_dir: "."         # CA and server cert/key files are written here
```

Restart SignalMatrix. It prints the CA cert path in the startup log:

```
INFO  TLS  CA cert: ./ech-ca.crt   server cert: ./ech-server.crt
```

### Trusting the CA certificate (one-time per device)

You need to do this once on every device that will open the HTTPS dashboard or use Web Serial.

**Getting the CA cert:** browse to `http://<server-ip>:8765/ca.crt` — the file downloads automatically.

#### Windows

1. Double-click `ech-ca.crt`.
2. Click **Install Certificate**.
3. Choose **Local Machine** (requires admin) or **Current User**.
4. Select **Place all certificates in the following store** → **Browse** → **Trusted Root Certification Authorities**.
5. Click **Finish**. Close and reopen the browser.

#### macOS

1. Double-click `ech-ca.crt` — Keychain Access opens.
2. Find `ECH Local CA` in the **System** or **Login** keychain.
3. Double-click the certificate → expand **Trust** → set **When using this certificate** to **Always Trust**.
4. Close the dialog (enter your password when prompted).
5. Reopen the browser.

#### Linux / Chrome or Chromium

1. Navigate to `chrome://settings/certificates`.
2. Click the **Authorities** tab.
3. Click **Import** and select `ech-ca.crt`.
4. Check **Trust this certificate for identifying websites**.
5. Click **OK**.

#### Firefox (any platform)

1. Open **Settings** → **Privacy & Security** → scroll to **Certificates** → **View Certificates**.
2. Click the **Authorities** tab → **Import**.
3. Select `ech-ca.crt`.
4. Check **Trust this CA to identify websites** → **OK**.

#### Android (Chrome)

1. Transfer `ech-ca.crt` to the device (email, USB, or ADB).
2. Open **Settings** → **Security** → **Encryption & credentials** → **Install a certificate** → **CA certificate**.
3. Tap **Install anyway** → select the file.

### Connecting via HTTPS

After trusting the CA, open:

```
https://<server-ip>:8766
```

The padlock icon should appear with no warnings. The Ham Log page now shows the **Connect Radio** button.

### Optional: mDNS (access by name instead of IP)

Install `zeroconf` and SignalMatrix advertises itself on the local network as `ech.local`:

```bash
pip install zeroconf
```

Then browse to `https://ech.local:8766` from any device on the same subnet, regardless of IP address.

### In-app TLS guide

SignalMatrix includes a built-in setup page at `/tls-setup` that shows these same instructions alongside the current server's IP addresses and a direct download link for the CA cert.

---

## CAT Radio Control

CAT (Computer Aided Transceiver) lets ECH read and set the frequency, band, and mode on your radio. There are two ways to do it depending on where the radio is physically connected.

### Method A: Web Serial (recommended for remote operators)

The radio connects to the **operator's laptop**, not the server. No drivers or software beyond Chrome or Edge are needed.

**Requirements:**
- HTTPS must be enabled (see above)
- Chrome or Edge browser (Firefox does not support Web Serial)

**Steps:**
1. Open the Ham Log page at `https://<server-ip>:8766/hamlog`.
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
```

The prefix (e.g. `aprs`, `meshcore`) is matched against the `source_adapter` column. Add any adapter name prefix to the `retention:` block to cover additional adapters.

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

If you have a USB or UART GPS receiver (u-blox or similar), ECH can set the system clock from GPS and broadcast the base position to all adapters. Uncomment and fill in the `gps:` block in `config.yaml`. Requires ECH to run as root (or with `CAP_SYS_TIME`) for clock sync.

### Firewall

Open ports on the ECH machine:

| Port | Protocol | Purpose |
|------|----------|---------|
| 8765 | TCP | HTTP dashboard |
| 8766 | TCP | HTTPS dashboard (if TLS enabled) |

No inbound ports are required for most adapters (they connect outward). Exception: JS8Call and Pat must be reachable on their respective ports if ECH runs on a different machine than those services.

---

## Adapter Quick-Reference

| Adapter type | config.yaml `type:` | External requirement |
|---|---|---|
| Meshtastic USB/TCP | `meshtastic` | `pip install meshtastic` |
| APRS Internet | `aprs_is` | `pip install aprslib` |
| APRS KISS TNC / Direwolf | `aprs_kiss` | Direwolf or hardware TNC |
| MeshCore serial/TCP | `meshcore` | `pip install pycryptodome pyserial-asyncio-fast` — custom protocol client, no `meshcore` PyPI package needed |
| LetsMesh MQTT | `mqtt` (with `pubkey_auth`) | `pip install aiomqtt pycryptodome` |
| MQTT generic | `mqtt` | `pip install aiomqtt` |
| JS8Call HF | `js8call` | JS8Call app running with TCP API on port 2442 |
| Winlink / Pat | `pat_winlink` | Pat running with HTTP API |
| SMS modem | `sms` | SIM800L / SIM7600 on USB serial |
| Reticulum / LXMF | `reticulum` | `pip install rns lxmf` |
| AREDN mesh | `aredn_ami` | `pip install aiohttp` |
| Asterisk / PBX | `asterisk` | Asterisk with AMI enabled — talks raw AMI over a TCP socket, no extra package needed |
| ADS-B / PiAware | `adsb` | dump1090, PiAware, or readsb on the LAN |
| AIS vessels (local SDR) | `ais_catcher` | AIS-catcher with HTTP server enabled |
| AIS vessels (AISHub) | `aishub` | Free aishub.net account + API key |
| AIS vessels (aisstream.io) | `aisstream` | Free aisstream.io API key |
| CW / Morse over sound card | `cw_audio` | `pip install sounddevice numpy` (+ `libportaudio2` on Linux); radio TX needs VOX or CAT PTT (`ptt: cat` uses CAT control instead) |
| RTTY over sound card | `rtty_audio` | Same as cw_audio (45.45 Bd Baudot, 2125/2295 Hz) |
| PSK31 over sound card | `psk31_audio` | Same as cw_audio (31.25 Bd BPSK varicode) |
| FT8/FT4 via WSJT-X | `wsjtx` | WSJT-X with "UDP Server" pointed at ECH (port 2237); RX-only |

All adapters also have mock equivalents (`mock_meshtastic`, `mock_aprs`, etc.) for simulation and training.

**Browser-hosted hardware:** a MeshCore node or radio audio plugged into the *operator's*
computer (not the server) can back an adapter remotely: open `/remote-hw` in Chrome/Edge
over HTTPS, connect the device (Web Serial) or radio audio (Web Audio), and configure the
matching adapter with `transport: browser` (MeshCore) or `input_device: browser`
(CW/RTTY/PSK31). Closing the tab disconnects the adapter.

**APRS-IS filter tip:** keep the radius in `filter: "r/<lat>/<lon>/<km>"` tight. A wide
radius (e.g. 250 km) pulls in the whole region's digipeater beacons and ship-AIS objects —
observed at 8,000+ messages/day — which bloats the database and drowns out mesh traffic.
60 km is plenty for local situational awareness.

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
