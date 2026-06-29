"""
ech/adapters/asterisk_adapter.py
---------------------------------
Asterisk PBX integration via AMI (Asterisk Manager Interface).

Capabilities
------------
- Inbound call logging → NormalizedMessage in the ECH message feed
- Click-to-call: originate(channel, destination) via AMI Originate
- Page / announce: page(target) rings one or more extensions simultaneously
- Active call tracking (health endpoint shows live call count)

Screen phone hooks (stubs — no-op until a screen phone is added)
-----------------------------------------------------------------
- push_to_screen(text): HTTP notify to Yealink/Polycom screen
- /api/phone/directory: XML directory served by app.py
- /api/phone/status: ECH status for idle screen display

Config keys (under adapters: in config.yaml)
--------------------------------------------
  type            asterisk
  name            pbx  (or any label)
  ami_host        str     AMI hostname            (default: localhost)
  ami_port        int     AMI port                (default: 5038)
  ami_username    str     AMI username            (default: admin)
  ami_secret      str     AMI secret/password
  local_extension str     ATA/phone extension for click-to-call source  (default: 101)
  page_target     str     Channels to page, e.g. "SIP/101&SIP/102"
                          or a Page group name.  If blank, pages local_extension only.
  page_method     str     "app" = AMI Originate + Page app
                          "exten" = dial page_extension in context (default: app)
  page_extension  str     Dialplan extension to dial for paging (page_method=exten)
  context         str     Dialplan context        (default: from-internal)
  caller_id       str     Outbound caller ID      (default: ECH <100>)
  log_inbound     bool    Log inbound calls       (default: true)
  log_outbound    bool    Log ECH-originated calls (default: true)

  # Screen phone — leave blank until you add one
  screen_extension  str   IP phone extension number
  screen_push_url   str   HTTP URL for XML push (Yealink: http://{ip}/CGI/Execute)
  screen_push_user  str   HTTP Basic auth username for screen push
  screen_push_pass  str   HTTP Basic auth password for screen push

Example config
--------------
  - type: asterisk
    name: pbx
    ami_host: 192.168.1.10
    ami_port: 5038
    ami_username: ech
    ami_secret: changeme
    local_extension: "101"
    page_target: "SIP/101&SIP/102"
    context: from-internal
    caller_id: "ECH <100>"
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)


class AsteriskAdapter(Adapter):

    def __init__(self, config: dict):
        super().__init__(config)
        self.name            = config.get("name", "pbx")
        self._host           = config.get("ami_host", "localhost")
        self._port           = int(config.get("ami_port", 5038))
        self._username       = config.get("ami_username", "admin")
        self._secret         = config.get("ami_secret", "")
        self._local_ext      = str(config.get("local_extension", "101"))
        self._page_target    = config.get("page_target", "")
        self._page_method    = config.get("page_method", "app")
        self._page_extension = config.get("page_extension", "")
        self._context        = config.get("context", "from-internal")
        self._caller_id      = config.get("caller_id", "ECH <100>")
        self._log_inbound    = bool(config.get("log_inbound", True))
        self._log_outbound   = bool(config.get("log_outbound", True))

        # Screen phone stubs
        self._screen_ext     = config.get("screen_extension", "")
        self._screen_url     = config.get("screen_push_url", "")
        self._screen_user    = config.get("screen_push_user", "")
        self._screen_pass    = config.get("screen_push_pass", "")

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._active_calls: dict[str, dict] = {}   # uniqueid → call info
        self._call_log: list[dict] = []             # recent completed calls (last 50)
        self._action_counter = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )
        # Read AMI banner
        banner = await self._reader.readline()
        log.info("Asterisk AMI: %s", banner.decode().strip())

        # Login — Action MUST be first header per AMI convention
        log.info("Asterisk AMI: authenticating as %r @ %s:%d",
                 self._username, self._host, self._port)
        await self._send_action({
            "Action": "Login",
            "Username": self._username,
            "Secret": self._secret,
        })
        resp = await self._read_packet()
        if resp.get("Response") != "Success":
            raise ConnectionError(
                f"AMI login failed for user '{self._username}' @ {self._host}:{self._port} — "
                f"Asterisk says: {resp.get('Message', resp)}. "
                f"Check manager.conf: section name=[{self._username}], secret={self._secret[:2]}***"
            )
        self._connected = True
        log.info("Asterisk AMI: logged in as %s @ %s:%d", self._username, self._host, self._port)

    async def disconnect(self) -> None:
        self._connected = False
        if self._writer:
            try:
                await self._send_action({"Action": "Logoff"})
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        log.info("Asterisk AMI: disconnected")

    async def send(self, message: NormalizedMessage) -> bool:
        # Sending a text message via AMI is not standard; treat as originate to body
        return False

    async def _run(self) -> None:
        log.debug("Asterisk AMI: event loop started")
        try:
            while self._connected:
                pkt = await self._read_packet()
                if not pkt:
                    continue
                event = pkt.get("Event", "")
                if event == "Newchannel":
                    self._on_new_channel(pkt)
                elif event == "Hangup":
                    await self._on_hangup(pkt)
        except ConnectionError as exc:
            log.error("Asterisk AMI: connection lost: %s", exc)
            self._connected = False
        except asyncio.CancelledError:
            pass

    # ── AMI wire protocol ─────────────────────────────────────────────────

    async def _read_packet(self) -> dict:
        """Read one AMI packet (blank-line delimited key:value block)."""
        pkt = {}
        while True:
            try:
                raw = await self._reader.readline()
            except Exception as exc:
                raise ConnectionError(f"AMI read error: {exc}") from exc
            if not raw:
                raise ConnectionError("AMI EOF")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                break
            if ":" in line:
                key, _, val = line.partition(":")
                pkt[key.strip()] = val.strip()
        return pkt

    async def _send_action(self, fields: dict) -> None:
        self._action_counter += 1
        lines = []
        for k, v in fields.items():
            lines.append(f"{k}: {v}")
        lines.append(f"ActionID: ech-{self._action_counter}")
        lines.append("")   # blank line terminates action
        payload = "\r\n".join(lines)
        self._writer.write(payload.encode())
        await self._writer.drain()

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_new_channel(self, pkt: dict) -> None:
        uid      = pkt.get("Uniqueid", "")
        channel  = pkt.get("Channel", "")
        callerid = pkt.get("CallerIDNum", "unknown")
        exten    = pkt.get("Exten", "")
        direction = "inbound" if not channel.startswith("Local/") else "internal"
        self._active_calls[uid] = {
            "uid":       uid,
            "channel":   channel,
            "callerid":  callerid,
            "exten":     exten,
            "direction": direction,
            "start":     time.monotonic(),
            "start_dt":  datetime.now(timezone.utc),
        }
        log.debug("AMI Newchannel: uid=%s from=%s exten=%s", uid[:8], callerid, exten)

    async def _on_hangup(self, pkt: dict) -> None:
        uid  = pkt.get("Uniqueid", "")
        call = self._active_calls.pop(uid, None)
        if not call:
            return

        duration = int(time.monotonic() - call["start"])
        dur_str  = f"{duration // 60}m {duration % 60}s" if duration >= 60 else f"{duration}s"
        cause    = pkt.get("Cause-txt", pkt.get("Cause", ""))
        answered = duration > 2

        direction = call["direction"]
        if not self._log_inbound and direction == "inbound":
            return
        if not self._log_outbound and direction != "inbound":
            return

        status = "answered" if answered else "missed"
        icon   = "📞" if answered else "📵"
        body   = (
            f"{icon} [{direction.upper()}] {call['callerid']} → ext {call['exten']} "
            f"| {status} | {dur_str}"
        )
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel="voice",
            from_id=call["callerid"],
            from_display=call["callerid"],
            body=body,
            timestamp=call["start_dt"],
            priority=Priority.NORMAL,
            raw={
                "call_uid":   uid,
                "direction":  direction,
                "callerid":   call["callerid"],
                "exten":      call["exten"],
                "duration_s": duration,
                "status":     status,
                "cause":      cause,
            },
        )
        await self._enqueue(msg)
        self._call_log.insert(0, msg.raw | {"body": body, "timestamp": msg.timestamp.isoformat()})
        if len(self._call_log) > 50:
            self._call_log = self._call_log[:50]
        log.info("AMI Hangup: %s from %s duration=%s", status, call["callerid"], dur_str)

    # ── Public PBX actions ────────────────────────────────────────────────

    async def originate(self, destination: str, caller_extension: str | None = None) -> bool:
        """
        Click-to-call: ring caller_extension (default: local_extension config),
        then bridge to destination when answered.
        destination can be a SIP extension ("SIP/102"), a number ("5551234"),
        or a dialplan exten string.
        """
        if not self._connected:
            return False
        src = caller_extension or self._local_ext
        # If destination looks like a bare number/extension, dial via dialplan
        if not destination.startswith("SIP/") and not destination.startswith("PJSIP/"):
            await self._send_action({
                "Action":   "Originate",
                "Channel":  f"SIP/{src}",
                "Context":  self._context,
                "Exten":    destination,
                "Priority": "1",
                "CallerID": self._caller_id,
                "Timeout":  "30000",
                "Async":    "true",
            })
        else:
            # Direct SIP channel bridge
            await self._send_action({
                "Action":      "Originate",
                "Channel":     f"SIP/{src}",
                "Application": "Dial",
                "Data":        destination,
                "CallerID":    self._caller_id,
                "Timeout":     "30000",
                "Async":       "true",
            })
        log.info("AMI Originate: %s → %s", src, destination)
        return True

    async def page(self, target: str | None = None) -> bool:
        """
        Page / announce: simultaneously ring one or more extensions.
        target overrides config page_target (e.g. "SIP/101&SIP/102").
        """
        if not self._connected:
            return False
        dest = target or self._page_target or f"SIP/{self._local_ext}"

        if self._page_method == "exten" and self._page_extension:
            # Dial a page extension in the dialplan (FreePBX page groups, etc.)
            await self._send_action({
                "Action":   "Originate",
                "Channel":  f"SIP/{self._local_ext}",
                "Context":  self._context,
                "Exten":    self._page_extension,
                "Priority": "1",
                "CallerID": self._caller_id,
                "Timeout":  "30000",
                "Async":    "true",
            })
        else:
            # Use Asterisk Page application directly
            await self._send_action({
                "Action":      "Originate",
                "Channel":     f"Local/s@default",
                "Application": "Page",
                "Data":        dest,
                "CallerID":    self._caller_id,
                "Timeout":     "30000",
                "Async":       "true",
            })
        log.info("AMI Page: dest=%s method=%s", dest, self._page_method)
        return True

    # ── Screen phone stubs (no-op until screen_push_url is set) ──────────

    async def push_to_screen(self, text: str) -> bool:
        """Push a text notification to the screen phone display (Yealink/Polycom)."""
        if not self._screen_url:
            return False
        try:
            import urllib.request, urllib.parse, base64
            xml = f"<YealinkIPPhoneTextScreen><Title>ECH</Title><Text>{text[:100]}</Text></YealinkIPPhoneTextScreen>"
            data = f"XML={urllib.parse.quote(xml)}".encode()
            req  = urllib.request.Request(self._screen_url, data=data, method="POST")
            if self._screen_user:
                creds = base64.b64encode(f"{self._screen_user}:{self._screen_pass}".encode()).decode()
                req.add_header("Authorization", f"Basic {creds}")
            urllib.request.urlopen(req, timeout=3)
            return True
        except Exception as exc:
            log.debug("Screen push failed: %s", exc)
            return False

    def xml_directory(self, contacts: list[dict]) -> str:
        """Return a Yealink-compatible XML remote phone book for the screen phone."""
        items = "\n".join(
            f'  <DirectoryEntry>'
            f'<Name>{c.get("display_name","")}</Name>'
            f'<Telephone>{c.get("aprs_callsign") or c.get("node_id","")}</Telephone>'
            f'</DirectoryEntry>'
            for c in contacts[:200]
        )
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<YealinkIPPhoneDirectory>\n{items}\n</YealinkIPPhoneDirectory>'

    # ── Health ────────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        d = {
            "ami_host":     f"{self._host}:{self._port}",
            "local_ext":    self._local_ext,
            "active_calls": len(self._active_calls),
            "recent_calls": len(self._call_log),
            "screen_phone": bool(self._screen_url),
        }
        if self._active_calls:
            d["calls"] = [
                {"from": c["callerid"], "exten": c["exten"], "direction": c["direction"]}
                for c in self._active_calls.values()
            ]
        return d

    def recent_calls(self) -> list[dict]:
        return list(self._call_log)

    def active_calls(self) -> list[dict]:
        return list(self._active_calls.values())
