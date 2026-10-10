"""
ech/core/adapter_schemas.py
------------------------------
O109 follow-up (operator feedback, 2026-10-09): the Settings adapter
editor let you add an adapter and a field, but gave zero guidance on
what field keys a given type actually takes or what a sane default looks
like — "+ field" was a blind text prompt. This module is the answer:
a curated schema per adapter type (field key, kind, required, default,
help text, and enum options where the field is one of a fixed set of
strings), sourced directly from each adapter module's own "Config keys:"
docstring rather than guessed, so "add a MeshCore adapter" can actually
pre-fill a working starting config instead of an empty {name, type}.

Deliberately NOT exhaustive — curated for the types operators are most
likely to add through the GUI. Anything not listed here still works in
the editor; it just falls back to the old blind "+ field" prompt and an
empty starting template, same as before this module existed. Extending
coverage later is just adding another dict entry here, no other code
changes needed.

Field dict shape:
  key       str    the config.yaml key
  kind      str    "string" | "number" | "boolean" | "select" | "list"
  required  bool   shown with a visual marker in the UI; not enforced
                    server-side (accepted without is still provider-
                    specific behavior, not a hard ECH requirement)
  default   any    pre-filled value when the field or whole adapter is added
  help      str    short description, shown as the field's tooltip and in
                    the "+ field" picker
  options   list   only for kind == "select" — valid string values
"""

from __future__ import annotations

ADAPTER_SCHEMAS: dict[str, dict] = {

    "meshcore": {
        "fields": [
            {"key": "transport", "kind": "select", "required": False, "default": "serial",
             "help": "serial = USB device on this server; tcp = node's WiFi server; "
                     "browser = node plugged into YOUR browser instead (open /remote-hw)",
             # "ble" is documented in meshcore.py's module docstring as a transport
             # option but is NOT implemented in _make_transport() (confirmed by
             # reading the code, 2026-10-09) — deliberately excluded here rather
             # than offered as a choice that will just raise ValueError at connect.
             "options": ["serial", "tcp", "browser"]},
            {"key": "port", "kind": "string", "required": False, "default": "auto",
             "help": "Serial device, e.g. /dev/ttyUSB0 — only used when transport=serial. "
                     "\"auto\" tries to auto-detect."},
            {"key": "baud", "kind": "number", "required": False, "default": 115200,
             "help": "Serial baud rate (transport=serial only)"},
            {"key": "host", "kind": "string", "required": False, "default": "",
             "help": "Node's IP address (transport=tcp only)"},
            {"key": "tcp_port", "kind": "number", "required": False, "default": 4403,
             "help": "Node's WiFi server port (transport=tcp only)"},
            {"key": "channel_idx", "kind": "number", "required": False, "default": 0,
             "help": "Channel index to send/monitor on"},
            {"key": "channel_name", "kind": "string", "required": False, "default": "",
             "help": "Named channel to send on, e.g. \"TAC-1\" — overrides channel_idx if set"},
            {"key": "poll_interval", "kind": "number", "required": False, "default": 2.0,
             "help": "Seconds between message polls"},
        ],
    },

    "meshtastic": {
        "fields": [
            {"key": "transport", "kind": "select", "required": False, "default": "serial",
             "help": "serial = USB device on this server; tcp = node's WiFi server; "
                     "ble = Bluetooth LE (experimental); browser = node plugged into "
                     "YOUR browser instead (open /remote-hw)",
             "options": ["serial", "tcp", "ble", "browser"]},
            {"key": "port", "kind": "string", "required": False, "default": "",
             "help": "Serial device, e.g. /dev/ttyUSB0 — blank = auto-detect (transport=serial)"},
            {"key": "host", "kind": "string", "required": False, "default": "",
             "help": "Node's IP address or hostname (transport=tcp only)"},
            {"key": "ble_address", "kind": "string", "required": False, "default": "",
             "help": "BLE MAC address — blank = first found (transport=ble only)"},
            {"key": "channel_idx", "kind": "number", "required": False, "default": 0,
             "help": "Channel index to send on (0 = primary)"},
            {"key": "channel_name", "kind": "string", "required": False, "default": "",
             "help": "Named channel to send on, e.g. \"LongFast\" — overrides channel_idx if found"},
            {"key": "node_id", "kind": "string", "required": False, "default": "",
             "help": "Destination node hex ID for DMs — blank = broadcast"},
        ],
    },

    "aprs_is": {
        "fields": [
            {"key": "callsign", "kind": "string", "required": True, "default": "",
             "help": "Your callsign + SSID, e.g. W1ABC-9"},
            {"key": "passcode", "kind": "number", "required": True, "default": -1,
             "help": "APRS-IS passcode — use -1 for receive-only"},
            {"key": "server", "kind": "string", "required": False, "default": "rotate.aprs2.net",
             "help": "APRS-IS server"},
            {"key": "port", "kind": "number", "required": False, "default": 14580,
             "help": "APRS-IS port"},
            {"key": "filter", "kind": "string", "required": False, "default": "r/44.1/-69.1/100",
             "help": "APRS-IS filter string — r/lat/lon/radius_km around your own position"},
            {"key": "beacon", "kind": "boolean", "required": False, "default": True,
             "help": "Send a login beacon on connect"},
        ],
    },

    "aprs_kiss": {
        "fields": [
            {"key": "transport", "kind": "select", "required": False, "default": "serial",
             "help": "serial = hardware TNC on a serial port; tcp = Direwolf KISS-over-TCP; "
                     "agwpe = Direwolf AGWPE interface",
             "options": ["serial", "tcp", "agwpe"]},
            {"key": "port", "kind": "string", "required": False, "default": "",
             "help": "Serial device, e.g. /dev/ttyUSB0 (transport=serial only)"},
            {"key": "baud", "kind": "number", "required": False, "default": 9600,
             "help": "Baud rate (transport=serial only)"},
            {"key": "host", "kind": "string", "required": False, "default": "localhost",
             "help": "Host for tcp/agwpe transport"},
            {"key": "tcp_port", "kind": "number", "required": False, "default": 8001,
             "help": "Port for tcp (default 8001) or agwpe (default 8000)"},
            {"key": "callsign", "kind": "string", "required": False, "default": "N0CALL-9",
             "help": "Your callsign + SSID for TX"},
            {"key": "tx_path", "kind": "string", "required": False, "default": "WIDE1-1,WIDE2-1",
             "help": "AX.25 digipeater path"},
        ],
    },

    "pat_winlink": {
        "fields": [
            {"key": "callsign", "kind": "string", "required": True, "default": "",
             "help": "Your Winlink callsign"},
            {"key": "pat_url", "kind": "string", "required": False, "default": "http://127.0.0.1:8080",
             "help": "Pat's HTTP API base URL"},
            {"key": "poll_interval", "kind": "number", "required": False, "default": 300,
             "help": "Seconds between inbox polls"},
            {"key": "auto_connect", "kind": "boolean", "required": False, "default": False,
             "help": "Trigger a Pat connect session on ECH startup"},
            {"key": "connect_alias", "kind": "select", "required": False, "default": "telnet",
             "help": "Pat connect alias to use", "options": ["telnet", "ardop", "ax25"]},
        ],
    },

    "asterisk": {
        "fields": [
            {"key": "ami_host", "kind": "string", "required": False, "default": "localhost",
             "help": "Asterisk Manager Interface hostname"},
            {"key": "ami_port", "kind": "number", "required": False, "default": 5038,
             "help": "AMI port"},
            {"key": "ami_username", "kind": "string", "required": False, "default": "admin",
             "help": "AMI username (from manager.conf)"},
            {"key": "ami_secret", "kind": "string", "required": True, "default": "",
             "help": "AMI secret/password (from manager.conf)"},
            {"key": "local_extension", "kind": "string", "required": False, "default": "101",
             "help": "ATA/phone extension used as the click-to-call source"},
            {"key": "channel_driver", "kind": "select", "required": False, "default": "PJSIP",
             "help": "Channel technology prefix for originate/page", "options": ["PJSIP", "SIP"]},
            {"key": "context", "kind": "string", "required": False, "default": "from-internal",
             "help": "Dialplan context"},
            {"key": "caller_id", "kind": "string", "required": False, "default": "ECH <100>",
             "help": "Outbound caller ID"},
        ],
    },

    "dapnet": {
        "fields": [
            {"key": "callsign", "kind": "string", "required": True, "default": "",
             "help": "Your DAPNET account callsign (HTTP Basic auth)"},
            {"key": "password", "kind": "string", "required": True, "default": "",
             "help": "Your DAPNET account password — live-server config only, never commit"},
            {"key": "transmitter_groups", "kind": "list", "required": True, "default": [],
             "help": "DAPNET transmitter group short names, e.g. [\"dl-all\"] — "
                     "found under \"Transmitter Groups\" on the DAPNET web UI"},
            {"key": "api_url", "kind": "string", "required": False, "default": "http://hampager.de/api",
             "help": "DAPNET API base URL"},
            {"key": "poll_interval", "kind": "number", "required": False, "default": 300,
             "help": "Seconds between reachability checks"},
        ],
    },

    "m17_reflector": {
        "fields": [
            {"key": "reflector_host", "kind": "string", "required": True, "default": "",
             "help": "M17 reflector hostname/IP, e.g. m17.openquad.net"},
            {"key": "reflector_port", "kind": "number", "required": False, "default": 17000,
             "help": "Reflector UDP port"},
            {"key": "module", "kind": "string", "required": True, "default": "A",
             "help": "Single letter A-Z — the reflector \"room\" to join"},
            {"key": "callsign", "kind": "string", "required": True, "default": "",
             "help": "Your callsign"},
            {"key": "listen_only", "kind": "boolean", "required": False, "default": False,
             "help": "Join as a listen-only client (never transmits)"},
        ],
    },

    "mqtt": {
        "fields": [
            {"key": "host", "kind": "string", "required": True, "default": "",
             "help": "Broker hostname"},
            {"key": "port", "kind": "number", "required": False, "default": 1883,
             "help": "Broker port"},
            {"key": "username", "kind": "string", "required": False, "default": "",
             "help": "Broker username (not needed if using pubkey_auth)"},
            {"key": "password", "kind": "string", "required": False, "default": "",
             "help": "Broker password (not needed if using pubkey_auth)"},
            {"key": "pubkey_auth", "kind": "string", "required": False, "default": "",
             "help": "Name of a MeshCore adapter to derive JWT credentials from "
                     "(for LetsMesh-style brokers) — leave blank for plain username/password"},
            {"key": "tls", "kind": "boolean", "required": False, "default": False,
             "help": "Enable TLS"},
            {"key": "topics", "kind": "list", "required": False, "default": ["#"],
             "help": "Topics to subscribe to"},
            {"key": "client_id", "kind": "string", "required": False, "default": "",
             "help": "MQTT client ID — blank = ech-{name}"},
        ],
    },

    "aredn_ami": {
        "fields": [
            {"key": "host", "kind": "string", "required": True, "default": "",
             "help": "Asterisk/PBX host on the AREDN mesh"},
            {"key": "port", "kind": "number", "required": False, "default": 5038,
             "help": "AMI port"},
            {"key": "username", "kind": "string", "required": False, "default": "admin",
             "help": "AMI username"},
            {"key": "secret", "kind": "string", "required": True, "default": "",
             "help": "AMI secret/password"},
            {"key": "local_extension", "kind": "string", "required": False, "default": "101",
             "help": "ATA/phone extension used as the click-to-call source"},
            {"key": "context", "kind": "string", "required": False, "default": "from-internal",
             "help": "Dialplan context"},
            {"key": "caller_id", "kind": "string", "required": False, "default": "ECH <100>",
             "help": "Outbound caller ID"},
        ],
    },

    "aredn_meshchat": {
        "fields": [
            {"key": "base_url", "kind": "string", "required": True, "default": "",
             "help": "MeshChat CGI URL, e.g. http://localnode.local.mesh/cgi-bin/meshchat"},
            {"key": "call_sign", "kind": "string", "required": True, "default": "",
             "help": "Your callsign, used as MeshChat's sender identity"},
            {"key": "channel", "kind": "string", "required": False, "default": "",
             "help": "MeshChat channel/zone to post to — blank = server default"},
            {"key": "poll_interval", "kind": "number", "required": False, "default": 30,
             "help": "Seconds between message polls"},
        ],
    },

    "ax25_bbs": {
        "fields": [
            {"key": "ax25_port", "kind": "string", "required": True, "default": "",
             "help": "Port name from /etc/ax25/axports"},
            {"key": "bbs_callsign", "kind": "string", "required": True, "default": "",
             "help": "Target BBS station, e.g. KA1ABC-1"},
            {"key": "poll_interval_sec", "kind": "number", "required": False, "default": 1800,
             "help": "Seconds between BBS connects — packet is slow, keep this gentle"},
        ],
    },

    "dmr_brandmeister": {
        "fields": [
            {"key": "master_host", "kind": "string", "required": True, "default": "",
             "help": "HBP master hostname/IP"},
            {"key": "master_port", "kind": "number", "required": True, "default": 54000,
             "help": "HBP master UDP port — no universal default, ask your master operator"},
            {"key": "passphrase", "kind": "string", "required": True, "default": "",
             "help": "Shared login passphrase / repeater password"},
            {"key": "radio_id", "kind": "number", "required": True, "default": 0,
             "help": "Repeater-level DMR ID (NOT a personal subscriber ID)"},
            {"key": "callsign", "kind": "string", "required": True, "default": "",
             "help": "Repeater callsign, max 8 chars"},
            {"key": "colorcode", "kind": "number", "required": False, "default": 1,
             "help": "DMR color code 0-15"},
            {"key": "sms_format", "kind": "select", "required": False, "default": "etsi_be",
             "help": "SMS framing for outbound text — most radios use etsi_be; some "
                     "(e.g. AnyTone with \"Motorola\" selected) need motorola",
             "options": ["etsi_be", "motorola"]},
        ],
    },
}


def get_schema(adapter_type: str) -> dict | None:
    return ADAPTER_SCHEMAS.get(adapter_type)
