"""
ech/adapters/sms.py
-------------------
SMS adapter for USB AT-command GSM/LTE modems.

Tested hardware targets:
  SIM800L / SIM800C     — classic 2G, common on Pi projects
  SIM7600 (Waveshare)   — 4G LTE, multiple serial interfaces
  Huawei E3372          — 4G USB stick (hilink mode may need AT mode switch)
  Any Hayes-compatible USB modem with AT+CMGF and AT+CNMI support

Architecture:
  Uses ATEngine (at_engine.py) for all serial I/O.
  Inbound SMS is driven by URCs (+CMTI, +CMT) — no polling.
  A fallback poll loop runs every `poll_interval` seconds to catch
  any messages missed by URC (carrier/modem quirks).
  Outbound uses AT+CMGS in text mode (CMGF=1).
  PDU mode is supported via `smspdudecoder` for modems that misbehave
  in text mode (rare but seen on some Huawei sticks).

Contact list import:
  `import_vcf(path)` and `import_csv(path)` populate the ECH contact
  store from phone address books. Called from the API or CLI.

Config keys:
  name            str     adapter name (default: sms)
  port            str     serial port, e.g. /dev/ttyUSB2  (REQUIRED)
  baud            int     baud rate (default: 115200)
  pdu_mode        bool    use PDU mode instead of text mode (default: False)
  poll_interval   int     seconds between message poll sweeps (default: 60)
  delete_on_read  bool    delete SMS from SIM after reading (default: True)
  pin             str     SIM PIN code if required (default: None)
  smsc            str     override SMS service center number (default: None)

Hardware note — SIM7600 exposes multiple serial ports:
  /dev/ttyUSB0  AT commands + SMS  ← use this one
  /dev/ttyUSB1  GPS NMEA
  /dev/ttyUSB2  diagnostic
  /dev/ttyUSB3  modem PPP
Run `ls -la /dev/ttyUSB*` after plugging in to confirm.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ech.adapters.at_engine import ATEngine
from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

# Regex patterns for URC and CMGL/CMGR response parsing
RE_CMTI  = re.compile(r'\+CMTI:\s*"?(\w+)"?,(\d+)')
RE_CMGR  = re.compile(r'\+CMGR:\s*"([^"]+)","([^"]+)"[^,]*,\s*"([^"]*)"')
RE_CMGL  = re.compile(r'\+CMGL:\s*(\d+),"([^"]+)","([^"]+)"[^,]*,\s*"([^"]*)"')
RE_CMGS  = re.compile(r'\+CMGS:\s*(\d+)')
RE_CSQ   = re.compile(r'\+CSQ:\s*(\d+),')
RE_CREG  = re.compile(r'\+CREG:\s*\d*,?\s*(\d)')
RE_COPS  = re.compile(r'\+COPS:[^,]*,[^,]*,"([^"]+)"')

# Priority keywords (same logic as JS8Call adapter)
EMRG_WORDS = {"911", "emergency", "mayday", "sos", "help!", "fire", "flood"}
ELVT_WORDS = {"urgent", "priority", "immediate", "evacuate"}


def _assess_priority(text: str) -> Priority:
    lower = text.lower()
    if any(w in lower for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in lower for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


class SMSAdapter(Adapter):
    """
    SMS adapter for USB AT-command modems.
    Supports text mode (default) and PDU mode.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name            = config.get("name", "sms")
        self._port           = config["port"]      # required
        self._baud           = int(config.get("baud", 115200))
        self._pdu_mode       = bool(config.get("pdu_mode", False))
        self._poll_interval  = int(config.get("poll_interval", 60))
        self._delete_on_read = bool(config.get("delete_on_read", True))
        self._pin            = config.get("pin", None)
        self._smsc           = config.get("smsc", None)

        self._at = ATEngine(self._port, self._baud, urc_handler=self._on_urc)
        self._run_task: asyncio.Task | None = None
        self._signal_rssi: int | None = None
        self._operator: str = ""
        self._reg_status: int = 0
        self._tx_count = 0
        self._rx_count = 0
        # Buffer for multi-line URC (CMT direct delivery)
        self._urc_cmt_header: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("SMS %s: opening %s @ %d baud", self.name, self._port, self._baud)
        await self._at.connect()
        await self._init_modem()
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("SMS %s: ready (operator=%s, signal=%s)",
                 self.name, self._operator, self._signal_rssi)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        await self._at.disconnect()
        log.info("SMS %s: disconnected", self.name)

    # ── Modem initialisation ──────────────────────────────────────────────

    async def _init_modem(self) -> None:
        """Full modem bring-up sequence."""
        # Basic sanity
        await self._at.cmd("AT")
        await self._at.cmd("ATE0")          # disable echo
        await self._at.cmd("AT+CMEE=1")     # verbose error codes

        # SIM PIN
        pin_resp = await self._at.cmd("AT+CPIN?")
        pin_status = next((l for l in pin_resp if "+CPIN" in l), "")
        if "SIM PIN" in pin_status:
            if self._pin:
                resp = await self._at.cmd(f'AT+CPIN="{self._pin}"')
                if not any("OK" in l for l in resp):
                    raise RuntimeError(f"SMS {self.name}: SIM PIN rejected")
                log.info("SMS %s: SIM PIN accepted", self.name)
            else:
                raise RuntimeError(f"SMS {self.name}: SIM requires PIN but none configured")
        elif "READY" not in pin_status and "+CPIN" in pin_status:
            raise RuntimeError(f"SMS {self.name}: unexpected SIM state: {pin_status}")

        # Wait for network registration (up to 30s)
        await self._wait_registered()

        # SMS text/PDU mode
        mode = 0 if self._pdu_mode else 1
        await self._at.cmd(f"AT+CMGF={mode}")

        # Message storage: prefer SIM ("SM") then modem ("ME")
        await self._at.cmd('AT+CPMS="SM","SM","SM"')

        # URC mode: +CMTI for new messages (store+notify), or +CMT for direct delivery
        # Mode 2,1 = notify+store; gives us +CMTI URC with index
        await self._at.cmd("AT+CNMI=2,1,0,0,0")

        # SMSC override if configured
        if self._smsc:
            await self._at.cmd(f'AT+CSCA="{self._smsc}"')

        # Get operator name and signal strength
        await self._refresh_status()

        # Drain any messages already in storage from before we connected
        await self._poll_messages()

    async def _wait_registered(self, timeout: float = 30.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            resp = await self._at.cmd("AT+CREG?")
            for line in resp:
                m = RE_CREG.search(line)
                if m:
                    status = int(m.group(1))
                    self._reg_status = status
                    if status in (1, 5):   # 1=registered home, 5=roaming
                        return
            await asyncio.sleep(2.0)
        log.warning("SMS %s: network registration timeout (status=%d)", self.name, self._reg_status)

    async def _refresh_status(self) -> None:
        try:
            csq = await self._at.cmd("AT+CSQ")
            for line in csq:
                m = RE_CSQ.search(line)
                if m:
                    rssi_raw = int(m.group(1))
                    # Convert 0-31 raw RSSI to dBm
                    self._signal_rssi = -113 + (rssi_raw * 2) if rssi_raw < 99 else None
        except Exception:
            pass

        try:
            cops = await self._at.cmd("AT+COPS?")
            for line in cops:
                m = RE_COPS.search(line)
                if m:
                    self._operator = m.group(1)
        except Exception:
            pass

    # ── Send ──────────────────────────────────────────────────────────────

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Send an SMS to message.to_id (E.164 phone number or local number).
        If to_id is None, logs a warning and returns False — SMS requires
        an explicit recipient.
        """
        if not message.to_id:
            log.warning("SMS %s: to_id required for SMS send", self.name)
            return False
        if not self._connected:
            return False

        number = message.to_id.strip()
        body   = message.body[:160]   # single SMS max; TODO: multipart

        try:
            if self._pdu_mode:
                return await self._send_pdu(number, body, message)
            else:
                return await self._send_text(number, body, message)
        except Exception as exc:
            log.error("SMS %s: send error to %s: %s", self.name, number, exc)
            return False

    async def _send_text(self, number: str, body: str, message: NormalizedMessage) -> bool:
        resp = await self._at.cmd_prompt(
            f'AT+CMGS="{number}"',
            body.encode("utf-8"),
        )
        ok = any("+CMGS" in l or "OK" in l for l in resp)
        if ok:
            self._tx_count += 1
            self._mark_tx(message)
            log.debug("SMS %s: sent to %s", self.name, number)
        else:
            log.error("SMS %s: send failed: %s", self.name, resp)
        return ok

    async def _send_pdu(self, number: str, body: str, message: NormalizedMessage) -> bool:
        """PDU mode send — encode message as GSM 7-bit PDU."""
        try:
            pdu, pdu_len = self._encode_pdu(number, body)
        except Exception as exc:
            log.error("SMS %s: PDU encode error: %s", self.name, exc)
            return False

        resp = await self._at.cmd_prompt(
            f"AT+CMGS={pdu_len}",
            pdu.encode("ascii"),
        )
        ok = any("+CMGS" in l or "OK" in l for l in resp)
        if ok:
            self._tx_count += 1
            self._mark_tx(message)
        return ok

    # ── Receive — URC-driven ──────────────────────────────────────────────

    async def _on_urc(self, line: str) -> None:
        """
        Called by ATEngine for every unsolicited result code.
        Handles +CMTI (message stored at index) and +CMT (direct delivery).
        """
        # New message stored notification
        m = RE_CMTI.match(line)
        if m:
            mem   = m.group(1)
            index = int(m.group(2))
            log.debug("SMS %s: +CMTI %s idx %d", self.name, mem, index)
            asyncio.create_task(self._read_message(index))
            return

        # Direct delivery header (text mode, 2-line URC)
        # +CMT: "+12075551234",,"24/05/10,14:30:00+00"
        if line.startswith("+CMT:"):
            self._urc_cmt_header = line
            return

        # Second line of +CMT direct delivery (the message body)
        if self._urc_cmt_header is not None:
            await self._handle_cmt_body(self._urc_cmt_header, line)
            self._urc_cmt_header = None
            return

    async def _handle_cmt_body(self, header: str, body: str) -> None:
        """Parse a 2-line +CMT direct-delivery URC."""
        # +CMT: "<number>",,"<timestamp>"
        parts = header.split(",", 3)
        number = parts[0].replace("+CMT:", "").strip().strip('"')
        timestamp_str = parts[2].strip('"') if len(parts) > 2 else ""
        ts = _parse_sms_timestamp(timestamp_str)

        await self._enqueue_sms(number, body.strip(), ts)

    async def _read_message(self, index: int) -> None:
        """Fetch message at `index` from modem storage via AT+CMGR."""
        try:
            resp = await self._at.cmd(f"AT+CMGR={index}")
            if self._pdu_mode:
                await self._parse_cmgr_pdu(resp, index)
            else:
                await self._parse_cmgr_text(resp, index)
        except Exception as exc:
            log.error("SMS %s: read_message(%d) error: %s", self.name, index, exc)

    async def _parse_cmgr_text(self, resp: list[str], index: int) -> None:
        """
        Parse AT+CMGR response in text mode.
        +CMGR: "REC UNREAD","+12075551234",,"24/05/10,14:30:00+00"
        <body text>
        OK
        """
        header_line = None
        body_lines  = []
        for line in resp:
            if line.startswith("+CMGR:"):
                header_line = line
            elif header_line and line not in ("OK", "ERROR") and not line.startswith("+"):
                body_lines.append(line)

        if not header_line:
            return

        m = RE_CMGR.search(header_line)
        if not m:
            return

        # status = m.group(1)   # "REC UNREAD" etc.
        number    = m.group(2)
        timestamp_str = m.group(3)
        body = " ".join(body_lines).strip()
        ts = _parse_sms_timestamp(timestamp_str)

        if body:
            await self._enqueue_sms(number, body, ts)
            if self._delete_on_read:
                asyncio.create_task(self._delete_message(index))

    async def _parse_cmgr_pdu(self, resp: list[str], index: int) -> None:
        """Parse AT+CMGR response in PDU mode using smspdudecoder."""
        # Response: "+CMGR: 0,,<length>" then a PDU hex string, then OK
        pdu_lines = [l for l in resp if l and not l.startswith("+CMGR") and l != "OK"]
        if not pdu_lines:
            return
        pdu_hex = pdu_lines[0].strip()
        try:
            from smspdudecoder.elements import SMS
            sms_obj = SMS.decode(pdu_hex)
            number  = str(sms_obj.sender)
            body    = str(sms_obj.content)
            ts      = getattr(sms_obj, "date", None) or datetime.now(timezone.utc)
            if hasattr(ts, "astimezone"):
                ts = ts.astimezone(timezone.utc)
        except Exception as exc:
            log.warning("SMS %s: PDU decode error: %s", self.name, exc)
            return

        if body:
            await self._enqueue_sms(number, body, ts)
            if self._delete_on_read:
                asyncio.create_task(self._delete_message(index))

    async def _enqueue_sms(self, number: str, body: str, ts: datetime) -> None:
        self._rx_count += 1
        priority = _assess_priority(body)
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel="SMS",
            from_id=number,
            from_display=number,   # resolved to contact name by router if contact exists
            body=body,
            timestamp=ts,
            priority=priority,
            raw={"number": number},
        )
        await self._enqueue(msg)
        log.debug("SMS %s: RX from %s: %s", self.name, number, body[:60])

    async def _delete_message(self, index: int) -> None:
        try:
            await self._at.cmd(f"AT+CMGD={index}")
            log.debug("SMS %s: deleted message %d", self.name, index)
        except Exception as exc:
            log.debug("SMS %s: delete(%d) error: %s", self.name, index, exc)

    # ── Poll loop (fallback) ──────────────────────────────────────────────

    async def _run(self) -> None:
        """Background loop: periodic message poll + signal refresh."""
        tick = 0
        try:
            while self._connected:
                await asyncio.sleep(self._poll_interval)
                tick += 1
                # Signal refresh every 5 ticks
                if tick % 5 == 0:
                    await self._refresh_status()
                # Full message poll (catches anything missed by URCs)
                await self._poll_messages()
        except asyncio.CancelledError:
            pass

    async def _poll_messages(self) -> None:
        """Read all unread messages from storage via AT+CMGL."""
        try:
            if self._pdu_mode:
                resp = await self._at.cmd('AT+CMGL=0')   # 0 = unread in PDU mode
            else:
                resp = await self._at.cmd('AT+CMGL="REC UNREAD"')

            await self._parse_cmgl(resp)
        except Exception as exc:
            log.debug("SMS %s: poll error: %s", self.name, exc)

    async def _parse_cmgl(self, resp: list[str]) -> None:
        """Parse AT+CMGL response (potentially multiple messages)."""
        if self._pdu_mode:
            # PDU: pairs of header + PDU lines
            i = 0
            while i < len(resp):
                line = resp[i]
                if line.startswith("+CMGL:"):
                    parts = line.split(",")
                    index = int(parts[0].split(":")[1].strip())
                    if i + 1 < len(resp):
                        pdu_hex = resp[i + 1].strip()
                        await self._parse_cmgr_pdu([pdu_hex], index)
                    i += 2
                else:
                    i += 1
            return

        # Text mode: +CMGL header followed by body line(s)
        current_index = None
        current_meta  = None
        body_lines: list[str] = []

        for line in resp:
            m = RE_CMGL.search(line)
            if m:
                # Flush previous message if any
                if current_meta and body_lines:
                    number    = current_meta["number"]
                    timestamp = current_meta["timestamp"]
                    body = " ".join(body_lines).strip()
                    if body:
                        await self._enqueue_sms(number, body, timestamp)
                        if self._delete_on_read and current_meta["index"] is not None:
                            asyncio.create_task(self._delete_message(current_meta["index"]))
                    body_lines = []

                current_index = int(m.group(1))
                number    = m.group(3)
                timestamp = _parse_sms_timestamp(m.group(4))
                current_meta = {"index": current_index, "number": number, "timestamp": timestamp}

            elif current_meta and line not in ("OK", "ERROR") and not line.startswith("+CMGL"):
                body_lines.append(line)

        # Flush last
        if current_meta and body_lines:
            body = " ".join(body_lines).strip()
            if body:
                await self._enqueue_sms(current_meta["number"], body, current_meta["timestamp"])
                if self._delete_on_read and current_meta["index"] is not None:
                    asyncio.create_task(self._delete_message(current_meta["index"]))

    # ── PDU encoding (text → GSM 7-bit PDU hex) ──────────────────────────

    def _encode_pdu(self, number: str, text: str) -> tuple[str, int]:
        """
        Minimal PDU encoder for SMS-SUBMIT (MT=1).
        Returns (hex_string, pdu_length_excluding_smsc).
        For production use, consider the `smspdudecoder` library's encoder.
        """
        # SMSC length 0 (use modem default)
        smsc = "00"

        # SMS-SUBMIT header: MTI=01 (SUBMIT), MR=0, VP absent
        # PDU type byte: 0x11 = TP-MTI=01 (SUBMIT) + TP-VPF=10 (relative)
        pdu_type = 0x11
        mr   = 0x00    # message reference
        pid  = 0x00    # protocol identifier
        dcs  = 0x00    # data coding: GSM 7-bit default

        # Encode destination number
        number_clean = re.sub(r"[^\d+]", "", number)
        international = number_clean.startswith("+")
        digits = number_clean.lstrip("+")
        ton_npi = 0x91 if international else 0x81
        if len(digits) % 2:
            digits += "F"
        addr_hex = "".join(digits[i+1] + digits[i] for i in range(0, len(digits), 2))
        addr_len = len(number_clean.lstrip("+"))

        # VP: relative, 0xAA = 4 days (standard default)
        vp = 0xAA

        # Encode text as GSM 7-bit
        text_encoded = _gsm7_encode(text[:160])
        text_len = len(text)   # character count, not byte count
        ud_hex = text_encoded.hex().upper()

        pdu = (
            f"{pdu_type:02X}"
            f"{mr:02X}"
            f"{addr_len:02X}"
            f"{ton_npi:02X}"
            f"{addr_hex}"
            f"{pid:02X}"
            f"{dcs:02X}"
            f"{vp:02X}"
            f"{text_len:02X}"
            f"{ud_hex}"
        )
        pdu_len = len(pdu) // 2 - 1   # bytes excluding SMSC
        full = smsc + pdu
        return full, pdu_len

    # ── Health ────────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        return {
            "port": self._port,
            "operator": self._operator,
            "signal_dbm": self._signal_rssi,
            "registered": self._reg_status in (1, 5),
            "pdu_mode": self._pdu_mode,
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
        }


# ── Contact import utilities ──────────────────────────────────────────────

def import_vcf(path: str | Path) -> list[dict]:
    """
    Parse a vCard 3.0/4.0 file and return a list of contact dicts
    compatible with the ECH contact store schema.
    """
    contacts = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    cards = re.split(r"END:VCARD", text, flags=re.IGNORECASE)

    for card in cards:
        if "BEGIN:VCARD" not in card.upper():
            continue
        contact: dict = {"sms_number": None, "aprs_callsign": None,
                         "meshtastic_id": None, "meshcore_id": None,
                         "tags": "", "notes": ""}

        # Full name
        fn_m = re.search(r"^FN[;:][^\r\n]+", card, re.MULTILINE | re.IGNORECASE)
        if fn_m:
            contact["display_name"] = fn_m.group().split(":", 1)[-1].strip()
        else:
            n_m = re.search(r"^N[;:][^\r\n]+", card, re.MULTILINE | re.IGNORECASE)
            if n_m:
                parts = n_m.group().split(":", 1)[-1].split(";")
                contact["display_name"] = " ".join(p.strip() for p in reversed(parts) if p.strip())
            else:
                continue

        # Phone number — prefer CELL, then any TEL
        tel_match = re.search(
            r"^TEL[^:]*TYPE[^:]*CELL[^:]*:([^\r\n]+)", card,
            re.MULTILINE | re.IGNORECASE
        )
        if not tel_match:
            tel_match = re.search(r"^TEL[^:]*:([^\r\n]+)", card, re.MULTILINE | re.IGNORECASE)
        if tel_match:
            raw_tel = re.sub(r"[^\d+]", "", tel_match.group(1).strip())
            if raw_tel:
                contact["sms_number"] = raw_tel

        # Notes — check for amateur radio callsign
        note_m = re.search(r"^NOTE[;:][^\r\n]+", card, re.MULTILINE | re.IGNORECASE)
        if note_m:
            note_text = note_m.group().split(":", 1)[-1].strip()
            contact["notes"] = note_text
            call_m = re.search(r"\b([A-Z]{1,2}\d[A-Z]{1,3}(?:-\d{1,2})?)\b", note_text.upper())
            if call_m:
                contact["aprs_callsign"] = call_m.group(1)

        if contact.get("display_name"):
            contacts.append(contact)

    log.info("VCF import: parsed %d contacts from %s", len(contacts), path)
    return contacts


def import_csv(path: str | Path, column_map: dict | None = None) -> list[dict]:
    """
    Parse a CSV contact file and return contact dicts.
    Default column map: {"name": 0, "phone": 1, "callsign": 2, "notes": 3}
    Pass column_map to override (0-indexed or header name strings).
    """
    import csv
    contacts = []
    col = column_map or {"name": 0, "phone": 1, "callsign": 2, "notes": 3}

    def get_col(row, key):
        c = col.get(key)
        if c is None:
            return ""
        if isinstance(c, int):
            return row[c].strip() if c < len(row) else ""
        return row.get(c, "").strip() if isinstance(row, dict) else ""

    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        # Auto-detect header vs no-header
        sample = f.read(1024)
        f.seek(0)
        has_header = any(isinstance(v, str) for v in col.values())

        if has_header or csv.Sniffer().has_header(sample):
            reader = csv.DictReader(f)
        else:
            reader = csv.reader(f)

        for row in reader:
            name  = get_col(row, "name")
            phone = re.sub(r"[^\d+]", "", get_col(row, "phone"))
            if not name:
                continue
            contacts.append({
                "display_name": name,
                "sms_number":   phone or None,
                "aprs_callsign": get_col(row, "callsign") or None,
                "meshtastic_id": None,
                "meshcore_id":   None,
                "tags":  get_col(row, "tags"),
                "notes": get_col(row, "notes"),
            })

    log.info("CSV import: parsed %d contacts from %s", len(contacts), path)
    return contacts


# ── GSM 7-bit encoding ────────────────────────────────────────────────────

# GSM 7-bit default alphabet (basic character set)
_GSM7 = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_MAP = {c: i for i, c in enumerate(_GSM7)}


def _gsm7_encode(text: str) -> bytes:
    """Pack text into GSM 7-bit septets, returned as bytes."""
    septets = []
    for ch in text:
        if ch in _GSM7_MAP:
            septets.append(_GSM7_MAP[ch])
        elif ch == "[":   septets += [0x1B, 0x3C]
        elif ch == "]":   septets += [0x1B, 0x3E]
        elif ch == "{":   septets += [0x1B, 0x28]
        elif ch == "}":   septets += [0x1B, 0x29]
        elif ch == "\\":  septets += [0x1B, 0x2F]
        elif ch == "^":   septets += [0x1B, 0x14]
        elif ch == "|":   septets += [0x1B, 0x40]
        elif ch == "~":   septets += [0x1B, 0x3D]
        else:
            septets.append(ord("?"))   # replace unsupported chars

    # Pack 7-bit septets into 8-bit bytes
    result = bytearray()
    bit_buf = 0
    bit_count = 0
    for s in septets:
        bit_buf |= (s & 0x7F) << bit_count
        bit_count += 7
        while bit_count >= 8:
            result.append(bit_buf & 0xFF)
            bit_buf >>= 8
            bit_count -= 8
    if bit_count:
        result.append(bit_buf & 0xFF)
    return bytes(result)


def _parse_sms_timestamp(ts: str) -> datetime:
    """Parse AT+CMGR/CMGL timestamp format: YY/MM/DD,HH:MM:SS±TZ"""
    try:
        # "24/05/10,14:30:00+00"
        ts_clean = re.sub(r"[+\-]\d+$", "", ts.strip())
        return datetime.strptime(ts_clean, "%y/%m/%d,%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)
