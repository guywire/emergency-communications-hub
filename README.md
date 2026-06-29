# ECH — Emergency Communications Hub

**Version 1.0.0-rc60**

ECH is a Python/FastAPI application that bridges multiple emergency-communications radio networks into a single web dashboard. It runs on a laptop, thin client, or Raspberry Pi at an incident command post, field site, or contest operation and lets operators monitor, log, and relay messages across all active links from a browser on the LAN.

## Who it is for

- ARES/RACES/RACES teams needing a common operating picture across Meshtastic mesh, APRS, and HF
- Served agencies that want radio traffic visible in a browser without installing amateur-radio software on every workstation
- Ham operators running ARRL Field Day, POTA, or SOTA activations who want integrated logging and CAT radio control
- Emergency management exercises where simulated traffic needs to flow through real comms gear

---

## Features at a glance

| Feature | Notes |
|---------|-------|
| **Multi-network bridging** | Meshtastic, APRS (IS + KISS TNC), MeshCore, JS8Call, Winlink/PAT, SMS (SIM7x00/SIM800L), MQTT, Reticulum/LXMF, AREDN, Asterisk/PBX |
| **Web dashboard** | Messages, map, node list, anomaly alerts, adapter status — all in the browser |
| **Ham Radio Log** | Contest logging (Field Day, POTA, SOTA, General); ADIF/Cabrillo/CSV import; ADIF/Cabrillo/POTA/SOTA export |
| **CAT radio control** | Browser Web Serial (no software install) or server-side rigctld/Hamlib |
| **Anomaly detection** | Automatic alerts for unusual message patterns or node behaviour |
| **Simulation mode** | Built-in mock adapters let you train operators without live hardware |
| **Mesh bot** | On-mesh commands: `ping`, `weather <zip>`, `overhead`, `satpass`, `solar`, `help` |
| **GPS time sync** | Optional NMEA receiver auto-sets system clock and base position |
| **Storage guard** | Warns when disk free falls below 1 GB or 5% — important on thin-client SSDs |

---

## Quick Start

### 1. Install Python dependencies

No `requirements.txt` is bundled; install the packages directly:

```bash
pip install fastapi "uvicorn[standard]" pyyaml aiosqlite \
    aiomqtt pyserial-asyncio-fast pycryptodome \
    meshtastic aprslib aiohttp \
    rns lxmf panoramisk meshcore
```

Minimum adapter-specific installs if you only need a subset:

| Adapter | Extra package(s) |
|---------|-----------------|
| Meshtastic | `meshtastic` |
| APRS-IS | `aprslib` |
| MeshCore / LetsMesh | `pycryptodome pyserial-asyncio-fast aiomqtt` |
| MQTT (generic) | `aiomqtt` |
| Reticulum | `rns lxmf` |
| AREDN / weather / Pat API | `aiohttp` |
| Asterisk AMI | `panoramisk` |
| mDNS (optional) | `zeroconf` |

### 2. Copy and edit the config

```bash
cp config.yaml /etc/ech/config.yaml   # or keep it local
nano /etc/ech/config.yaml
```

Set `operator: callsign` to your callsign. All adapters are disabled by default — enable the ones you need (see [Configuration](#configuration) below).

### 3. Start ECH

```bash
# Use local config.yaml in current directory:
python -m ech.main

# Or point to a specific config:
python -m ech.main --config /etc/ech/config.yaml
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

ECH generates its own Certificate Authority (CA) the first time it starts with TLS enabled. Every subsequent start, it re-issues a server certificate that includes every IP address the server has at that moment. This means the certificate is always valid no matter which IP your contest-site DHCP assigns — you do not need to regenerate anything when you pack up and redeploy at a new site.

You trust the CA once. After that, every ECH deployment is automatically trusted.

### Enabling TLS

In `config.yaml`, uncomment and edit the `tls` block:

```yaml
tls:
  enabled: true
  https_port: 8766      # HTTPS runs alongside HTTP on 8765
  data_dir: "."         # CA and server cert/key files are written here
```

Restart ECH. It prints the CA cert path in the startup log:

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

Install `zeroconf` and ECH advertises itself on the local network as `ech.local`:

```bash
pip install zeroconf
```

Then browse to `https://ech.local:8766` from any device on the same subnet, regardless of IP address.

### In-app TLS guide

ECH includes a built-in setup page at `/tls-setup` that shows these same instructions alongside the current server's IP addresses and a direct download link for the CA cert.

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

ECH is designed to run on a thin client or mini PC with an 8 GB SSD. Recommended minimum: 4-core x86-64, 4 GB RAM, 8 GB storage. A Raspberry Pi 4 (4 GB) also works for most adapter combinations.

### Storage warnings

ECH monitors free disk space and displays a banner warning when:
- Free space drops below 1 GB, **or**
- Free space drops below 5% of the partition

On an 8 GB SSD with the OS already installed this threshold can be reached within a few days of heavy message traffic. To keep the database small, set a retention policy or periodically archive and vacuum `ech.db`.

The database path is configurable:

```yaml
database:
  path: "/data/ech/ech.db"   # move to a larger partition if needed
```

### Running as a service (Linux)

Create `/etc/systemd/system/ech.service`:

```ini
[Unit]
Description=ECH Emergency Communications Hub
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
| MeshCore serial/TCP | `meshcore` | `pip install pycryptodome pyserial-asyncio-fast` |
| LetsMesh MQTT | `mqtt` (with `pubkey_auth`) | `pip install aiomqtt pycryptodome` |
| MQTT generic | `mqtt` | `pip install aiomqtt` |
| JS8Call HF | `js8call` | JS8Call app running with TCP API on port 2442 |
| Winlink / Pat | `pat_winlink` | Pat running with HTTP API |
| SMS modem | `sms` | SIM800L / SIM7600 on USB serial |
| Reticulum / LXMF | `reticulum` | `pip install rns lxmf` |
| AREDN mesh | `aredn_ami` | `pip install aiohttp` |
| Asterisk / PBX | `asterisk` | Asterisk with AMI enabled; `pip install panoramisk` |

All adapters also have mock equivalents (`mock_meshtastic`, `mock_aprs`, etc.) for simulation and training.

---

## Mesh Bot Commands

When `mesh_bot: enabled: true` in `config.yaml`, any node on the mesh can send these commands to the ECH node:

| Command | What it does |
|---------|-------------|
| `ping` | ECH replies with signal report (SNR, hops) |
| `weather 04101` | Current NWS conditions + forecast for US zip code |
| `overhead` | Closest aircraft from a local dump1090 instance |
| `satpass` | Next ISS pass visible from base location |
| `solar` | Current solar flux, sunspot number, K-index |
| `help` | Lists available commands |

No API key is required. The `weather` command uses api.weather.gov (US zip codes only). `satpass` requires `pip install skyfield`.

---

## License

ECH is provided for use by amateur radio operators and served emergency agencies. See LICENSE for terms.
