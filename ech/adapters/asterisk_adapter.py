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
  channel_driver  str     Channel technology prefix for originate/page — "PJSIP" or "SIP"
                          (default: PJSIP; use "SIP" only if this box still runs chan_sip)
  page_target     str     Channels to page, e.g. "PJSIP/101&PJSIP/102"
                          or a Page group name.  If blank, pages local_extension only.
  page_method     str     "app" = AMI Originate + Page app
                          "exten" = dial page_extension in context (default: app)
  page_extension  str     Dialplan extension to dial for paging (page_method=exten)
  context         str     Dialplan context        (default: from-internal)
  caller_id       str     Outbound caller ID      (default: ECH <100>)
  log_inbound     bool    Log inbound calls       (default: true)
  log_outbound    bool    Log ECH-originated calls (default: true)
  vm_context      str     Voicemail context (mailboxes are "{ext}@{vm_context}")
                          (default: default)
  vm_spool_dir    str     Voicemail spool directory override — defaults to
                          /var/spool/asterisk/voicemail/{vm_context}. ECH reads
                          this directly (no AMI equivalent exists) to list and
                          play back messages; the ech user needs read access.

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
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

_MAILBOX_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MSGID_RE   = re.compile(r"^msg\d{4}$")


class AsteriskAdapter(Adapter):

    # Overridden below (True) — this box can send real SIP MESSAGE text to
    # endpoints whose client supports it, in addition to voice.
    send_enabled: bool = True

    def __init__(self, config: dict):
        super().__init__(config)
        self.name            = config.get("name", "pbx")
        self._host           = config.get("ami_host", "localhost")
        self._port           = int(config.get("ami_port", 5038))
        self._username       = config.get("ami_username", "admin")
        self._secret         = config.get("ami_secret", "")
        self._local_ext      = str(config.get("local_extension", "101"))
        self._channel_driver = config.get("channel_driver", "PJSIP").strip().upper()
        self._page_target    = config.get("page_target", "")
        self._page_method    = config.get("page_method", "app")
        self._page_extension = config.get("page_extension", "")
        self._context        = config.get("context", "from-internal")
        self._caller_id      = config.get("caller_id", "ECH <100>")
        self._log_inbound    = bool(config.get("log_inbound", True))
        self._log_outbound   = bool(config.get("log_outbound", True))
        self._vm_context     = config.get("vm_context", "default")
        self._vm_spool_dir   = config.get("vm_spool_dir") or f"/var/spool/asterisk/voicemail/{self._vm_context}"
        self._vm_counts: dict[str, tuple[int, int]] = {}   # mailbox -> (new, old)
        self._endpoint_status: dict[str, dict] = {}    # extension -> {device_state, online, contacts}
        self._endpoint_scratch: dict[str, dict] = {}   # accumulator during a refresh cycle
        self._endpoint_poll_task: asyncio.Task | None = None
        self._endpoint_poll_sec = float(config.get("endpoint_poll_sec", 20.0))
        self.auto_page_on_emergency = bool(config.get("auto_page_on_emergency", True))

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
        self._run_task: asyncio.Task | None = None

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
        # Without this, the AMI event stream (Hangup call-logging, MessageWaiting
        # voicemail alerts, inbound-text UserEvent, endpoint status) is never read —
        # only the direct request/response actions (originate/page/send) work.
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-ami-events")
        self._endpoint_poll_task = asyncio.create_task(
            self._endpoint_poll_loop(), name=f"{self.name}-endpoint-poll"
        )
        log.info("Asterisk AMI: logged in as %s @ %s:%d", self._username, self._host, self._port)

    async def disconnect(self) -> None:
        self._connected = False
        for task in (self._run_task, self._endpoint_poll_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._writer:
            try:
                await self._send_action({"Action": "Logoff"})
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        log.info("Asterisk AMI: disconnected")

    async def send(self, message: NormalizedMessage) -> bool:
        """Send a real SIP MESSAGE (RFC 3428) to a PJSIP endpoint via AMI.

        Delivery depends on the far-end SIP client supporting instant
        messaging (most modern softphones do — Zoiper, Linphone, Grandstream
        Wave, Bria). AMI accepts the action without a synchronous
        confirmation (same limitation as originate()/page() below — reading
        a response here would race the event-loop's own packet reads on
        this single AMI connection), so a True return means "handed to
        Asterisk", not "received by the phone".
        """
        if not self._connected or not message.to_id:
            return False
        to_ext = message.to_id.strip()
        if not to_ext:
            return False
        await self._send_action({
            "Action": "MessageSend",
            "To":     f"pjsip:{to_ext}",
            "From":   f"sip:{self._local_ext}@{self._host}",
            "Body":   message.body,
        })
        log.info("AMI MessageSend: %s -> %s", self._local_ext, to_ext)
        return True

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
                elif event == "MessageWaiting":
                    await self._on_message_waiting(pkt)
                elif event == "UserEvent" and pkt.get("UserEvent") == "ECHTextMessage":
                    await self._on_text_message(pkt)
                elif event == "EndpointList":
                    self._on_endpoint_list_item(pkt)
                elif event == "EndpointListComplete":
                    self._endpoint_status = self._endpoint_scratch
                    self._endpoint_scratch = {}
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
        lines.append("")   # blank line terminates action (AMI needs \r\n\r\n)
        payload = "\r\n".join(lines) + "\r\n"
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
        if self._router_notify:
            await self._router_notify(self.name, msg.id, "direct" if answered else "failed", status)
        self._call_log.insert(0, msg.raw | {"body": body, "timestamp": msg.timestamp.isoformat()})
        if len(self._call_log) > 50:
            self._call_log = self._call_log[:50]
        log.info("AMI Hangup: %s from %s duration=%s", status, call["callerid"], dur_str)

    async def _on_message_waiting(self, pkt: dict) -> None:
        mailbox_full = pkt.get("Mailbox", "")   # e.g. "101@default"
        mailbox = mailbox_full.split("@")[0].strip()
        if not mailbox:
            return
        try:
            new_count = int(pkt.get("New", 0))
        except ValueError:
            new_count = 0
        try:
            old_count = int(pkt.get("Old", 0))
        except ValueError:
            old_count = 0
        prev_new, _ = self._vm_counts.get(mailbox, (0, 0))
        self._vm_counts[mailbox] = (new_count, old_count)
        if new_count > prev_new:
            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel="voicemail",
                from_id=f"vm:{mailbox}",
                from_display=f"Voicemail {mailbox}",
                body=f"📧 New voicemail on ext {mailbox} ({new_count} new)",
                priority=Priority.NORMAL,
                raw={"mailbox": mailbox, "new_count": new_count, "old_count": old_count},
            )
            await self._enqueue(msg)
            log.info("AMI MessageWaiting: mailbox=%s new=%d old=%d", mailbox, new_count, old_count)

    async def _on_text_message(self, pkt: dict) -> None:
        """Inbound SIP MESSAGE, delivered via the [messages] dialplan context's
        UserEvent (see extensions.conf) — required because AMI has no direct
        'message received' event of its own."""
        from_raw = pkt.get("From", "")
        to_ext   = pkt.get("To", "").strip()
        body     = pkt.get("Body", "")
        m = re.search(r"[Ss][Ii][Pp]s?:(\+?\w+)@", from_raw)
        from_ext = m.group(1) if m else from_raw.strip()
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel="DM",
            from_id=from_ext,
            from_display=f"Ext {from_ext}",
            to_id=to_ext or None,
            body=body,
            priority=Priority.NORMAL,
            raw={"from_uri": from_raw, "to_ext": to_ext},
        )
        await self._enqueue(msg)
        log.info("AMI inbound MESSAGE: %s -> %s: %s", from_ext, to_ext, body[:80])

    # ── Endpoint status (periodic poll — AMI has no push subscription for this) ─

    async def _endpoint_poll_loop(self) -> None:
        while self._connected:
            try:
                self._endpoint_scratch = {}
                await self._send_action({"Action": "PJSIPShowEndpoints"})
            except Exception as exc:
                log.debug("%s: endpoint status poll failed: %s", self.name, exc)
            await asyncio.sleep(self._endpoint_poll_sec)

    def _on_endpoint_list_item(self, pkt: dict) -> None:
        ext = pkt.get("ObjectName", "")
        if not ext:
            return
        device_state = pkt.get("DeviceState", "")
        self._endpoint_scratch[ext] = {
            "extension":    ext,
            "device_state": device_state,
            "online":       device_state not in ("", "Unavailable", "Invalid"),
            "contacts":     pkt.get("Contacts", ""),
        }

    def list_endpoint_status(self) -> list[dict]:
        return sorted(self._endpoint_status.values(), key=lambda e: e["extension"])

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
                "Channel":  f"{self._channel_driver}/{src}",
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
                "Channel":     f"{self._channel_driver}/{src}",
                "Application": "Dial",
                "Data":        destination,
                "CallerID":    self._caller_id,
                "Timeout":     "30000",
                "Async":       "true",
            })
        log.info("AMI Originate: %s → %s", src, destination)
        return True

    async def speak(self, extension: str, text: str) -> bool:
        """Text-to-speech call: ring `extension` and play synthesized speech.

        Renders locally with espeak-ng (offline, no API key or internet
        needed — matches ECH's degraded-connectivity design), resampled by
        sox to 8kHz/16-bit/mono — Asterisk's format_wav player requires
        exactly that telephony rate; espeak-ng's native 22050Hz output is
        silently unplayable (Playback() can't match it to any format it
        knows and just gives up, leaving the caller on dead air/dialtone).
        Requires espeak-ng + sox on THIS host and Asterisk able to read
        /tmp — both true in the supported deployment (ECH and Asterisk on
        the same box).
        """
        if not self._connected or not text.strip():
            return False
        text = text.strip()[:500]
        stem = f"/tmp/ech_tts_{uuid.uuid4().hex}"
        wav_path = f"{stem}.wav"
        try:
            espeak_proc = await asyncio.create_subprocess_exec(
                "espeak-ng", "--stdout", "-s", "150", text,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            sox_proc = await asyncio.create_subprocess_exec(
                "sox", "-t", "wav", "-", "-r", "8000", "-c", "1", "-b", "16", wav_path,
                stdin=espeak_proc.stdout, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            espeak_proc.stdout.close()   # let espeak see SIGPIPE if sox exits early
            await asyncio.wait_for(asyncio.gather(espeak_proc.wait(), sox_proc.wait()), timeout=15)
        except FileNotFoundError as exc:
            log.error("%s: TTS tool not installed (%s) — apt install espeak-ng sox", self.name, exc)
            return False
        except asyncio.TimeoutError:
            log.warning("%s: TTS render timed out", self.name)
            return False
        if espeak_proc.returncode != 0 or sox_proc.returncode != 0 or not Path(wav_path).is_file():
            log.warning("%s: TTS render failed (espeak rc=%s, sox rc=%s)",
                        self.name, espeak_proc.returncode, sox_proc.returncode)
            return False
        Path(wav_path).chmod(0o644)

        await self._send_action({
            "Action":      "Originate",
            "Channel":     f"{self._channel_driver}/{extension}",
            "Application": "Playback",
            "Data":        stem,   # Playback wants the path without extension
            "CallerID":    self._caller_id,
            "Timeout":     "30000",
            "Async":       "true",
        })
        log.info("AMI TTS Originate: ext %s: %r", extension, text[:60])
        asyncio.create_task(self._cleanup_tts_file(wav_path))
        return True

    async def _cleanup_tts_file(self, path: str, delay: float = 90.0) -> None:
        await asyncio.sleep(delay)
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    async def page(self, target: str | None = None) -> bool:
        """
        Page / announce: simultaneously ring one or more extensions.
        target overrides config page_target (e.g. "PJSIP/101&PJSIP/102").
        """
        if not self._connected:
            return False
        dest = target or self._page_target or f"{self._channel_driver}/{self._local_ext}"

        if self._page_method == "exten" and self._page_extension:
            # Dial a page extension in the dialplan (FreePBX page groups, etc.)
            await self._send_action({
                "Action":   "Originate",
                "Channel":  f"{self._channel_driver}/{self._local_ext}",
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

    # ── Voicemail (read directly from the spool — AMI has no listing action) ─

    def list_voicemail_mailboxes(self) -> dict[str, int]:
        """Return {mailbox: message_count} for every mailbox with mail waiting."""
        root = Path(self._vm_spool_dir)
        if not root.is_dir():
            return {}
        out: dict[str, int] = {}
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            inbox = sub / "INBOX"
            if not inbox.is_dir():
                continue
            n = len(list(inbox.glob("msg*.txt")))
            if n:
                out[sub.name] = n
        return out

    def list_voicemail(self, mailbox: str) -> list[dict]:
        """Return message metadata (caller ID, duration, time) for one mailbox."""
        if not _MAILBOX_RE.match(mailbox):
            return []
        inbox = Path(self._vm_spool_dir) / mailbox / "INBOX"
        if not inbox.is_dir():
            return []
        out = []
        for txt in sorted(inbox.glob("msg*.txt")):
            msg_id = txt.stem
            info: dict[str, str] = {}
            try:
                for line in txt.read_text(errors="replace").splitlines():
                    if "=" in line:
                        k, _, v = line.partition("=")
                        info[k.strip()] = v.strip()
            except OSError:
                pass
            wav = inbox / f"{msg_id}.wav"
            out.append({
                "id":        msg_id,
                "callerid":  info.get("callerid", ""),
                "duration":  int(info.get("duration") or 0),
                "origtime":  int(info.get("origtime") or 0),
                "has_audio": wav.is_file(),
            })
        return out

    def voicemail_audio_path(self, mailbox: str, msg_id: str) -> Path | None:
        """Path to a message's playable .wav, or None if invalid/missing."""
        if not _MAILBOX_RE.match(mailbox) or not _MSGID_RE.match(msg_id):
            return None
        p = Path(self._vm_spool_dir) / mailbox / "INBOX" / f"{msg_id}.wav"
        return p if p.is_file() else None

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
