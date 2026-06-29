"""
ech/adapters/aredn_ami.py
--------------------------
AREDN mesh PBX integration via Asterisk Manager Interface (AMI).

ECH connects to an Asterisk PBX running on the AREDN mesh network
and surfaces call events (ringing, answered, hangup) as NormalizedMessages
in the unified inbox. This gives dispatch visibility into voice traffic
without ECH becoming a PBX itself.

The AMI is a TCP text protocol on port 5038 (default).
FreePBX, HamVOIP, AllStarLink, and raw Asterisk all support it.

Config keys:
  name          str     adapter name (default: aredn-pbx)
  host          str     Asterisk/PBX host on AREDN mesh (REQUIRED)
  port          int     AMI port (default: 5038)
  username      str     AMI username (REQUIRED) — set in /etc/asterisk/manager.conf
  secret        str     AMI secret (REQUIRED)
  events        list    AMI event types to surface (default: see below)

Setup on Asterisk side (/etc/asterisk/manager.conf):
  [general]
  enabled = yes
  port = 5038
  bindaddr = 0.0.0.0

  [ech]
  secret = your_secret_here
  read = call,system
  write = call

AllStarLink / HamVOIP: same config applies.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

DEFAULT_EVENTS = {
    "Newchannel", "Hangup", "Answer", "Bridge", "BridgeEnter",
    "BridgeLeave", "Dial", "AgentCalled", "AgentRingNoAnswer",
    "Hold", "Unhold", "MeetmeJoin", "MeetmeLeave",
}


class AREDNAMIAdapter(Adapter):
    """
    Asterisk AMI event monitor for AREDN mesh PBX.
    Surfaces call events as messages and supports click-to-call / page.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name            = config.get("name", "aredn-pbx")
        self._host           = config.get("host", "127.0.0.1")
        self._port           = int(config.get("port", 5038))
        self._username       = config.get("username", "admin")
        self._secret         = config.get("secret", "")
        self._events         = set(config.get("events", DEFAULT_EVENTS))
        self._local_ext      = str(config.get("local_extension", config.get("local_ext", "101")))
        self._page_target    = config.get("page_target", "")
        self._page_method    = config.get("page_method", "app")
        self._page_extension = config.get("page_extension", "")
        self._context        = config.get("context", "from-internal")
        self._caller_id      = config.get("caller_id", "ECH <100>")
        self._screen_url     = config.get("screen_push_url", "")
        self._screen_user    = config.get("screen_push_user", "")
        self._screen_pass    = config.get("screen_push_pass", "")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._run_task: asyncio.Task | None = None
        self._call_count  = 0
        self._pbx_version = ""
        self._active_calls: dict[str, dict] = {}
        self._call_log: list[dict] = []
        self._packet_log: list[dict] = []
        self._action_counter = 0

    async def connect(self) -> None:
        log.info("AREDN AMI %s: connecting to %s:%d", self.name, self._host, self._port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=10.0,
        )
        # Read AMI banner
        banner = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
        self._pbx_version = banner.decode().strip()
        log.info("AREDN AMI %s: connected — %s", self.name, self._pbx_version)

        # Login
        await self._send_action({
            "Action": "Login",
            "Username": self._username,
            "Secret": self._secret,
            "Events": "on",
        })
        # Read login response
        resp = await self._read_response()
        if resp.get("Response") != "Success":
            raise ConnectionError(f"AMI login failed: {resp.get('Message', 'unknown')}")

        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("AREDN AMI %s: authenticated, monitoring call events", self.name)

    async def disconnect(self) -> None:
        self._connected = False
        if self._writer:
            try:
                await self._send_action({"Action": "Logoff"})
            except Exception:
                pass
            self._writer.close()
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Originate a call or send a command via AMI.
        to_id should be an extension number on the PBX.
        """
        if not message.to_id or not self._connected:
            return False
        try:
            await self._send_action({
                "Action": "Originate",
                "Channel": f"SIP/{message.to_id}",
                "Context": "default",
                "Exten": message.to_id,
                "Priority": "1",
                "CallerID": "ECH <100>",
                "Timeout": "30000",
                "Async": "yes",
            })
            self._mark_tx(message)
            return True
        except Exception as exc:
            log.error("AREDN AMI %s: originate error: %s", self.name, exc)
            return False

    async def _run(self) -> None:
        """Read and dispatch AMI event stream."""
        log.debug("AREDN AMI %s: event loop started", self.name)
        try:
            while self._connected:
                event = await asyncio.wait_for(self._read_response(), timeout=60.0)
                if not event:
                    continue
                event_type = event.get("Event", "")
                if event_type in self._events:
                    await self._surface_event(event_type, event)
        except asyncio.TimeoutError:
            pass   # keepalive timeout, loop back
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self._connected:
                log.error("AREDN AMI %s: event loop error: %s", self.name, exc)
                self._connected = False

    async def _surface_event(self, event_type: str, event: dict) -> None:
        """Convert an AMI event to a NormalizedMessage and track active calls."""
        import time as _time
        # Log raw AMI event for diagnostics
        self._packet_log.append({
            "dir": "rx", "type": event_type,
            "fields": dict(event), "ts": _time.time(),
        })
        if len(self._packet_log) > 200:
            self._packet_log = self._packet_log[-200:]
        import time as _time
        self._call_count += 1
        channel     = event.get("Channel", "")
        caller_id   = event.get("CallerIDNum", "") or event.get("CallerID", "")
        caller_name = event.get("CallerIDName", caller_id)
        exten       = event.get("Exten", "") or event.get("Extension", "")
        uid         = event.get("Uniqueid", channel)

        # Track active calls
        if event_type == "Newchannel":
            self._active_calls[uid] = {
                "uid": uid, "channel": channel, "callerid": caller_id,
                "exten": exten, "direction": "inbound", "start": _time.monotonic(),
            }
        elif event_type == "Hangup":
            call = self._active_calls.pop(uid, None)
            if call:
                duration = int(_time.monotonic() - call["start"])
                answered = duration > 2
                rec = {
                    "call_uid": uid, "callerid": caller_id, "exten": exten,
                    "direction": "inbound", "duration_s": duration,
                    "status": "answered" if answered else "missed",
                    "cause": event.get("Cause-txt", event.get("Cause", "")),
                }
                self._call_log.insert(0, rec)
                if len(self._call_log) > 50:
                    self._call_log = self._call_log[:50]

        # Build human-readable body
        if event_type == "Newchannel":
            body = f"📞 INCOMING: {caller_name} ({caller_id}) → ext {exten}"
        elif event_type == "Answer":
            body = f"✅ ANSWERED: {caller_name} on {channel}"
        elif event_type == "Hangup":
            cause = event.get("Cause-txt", event.get("Cause", ""))
            body  = f"📵 HANGUP: {channel} — {cause}"
        elif event_type in ("Bridge", "BridgeEnter"):
            body = f"🔗 BRIDGE: {caller_name} bridged"
        elif event_type == "Dial":
            dest = event.get("Destination", "")
            body = f"📲 DIALING: {caller_name} → {dest}"
        elif event_type == "MeetmeJoin":
            conf = event.get("Meetme", "")
            body = f"🎙 CONFERENCE: {caller_name} joined room {conf}"
        elif event_type == "MeetmeLeave":
            conf = event.get("Meetme", "")
            body = f"🎙 CONFERENCE: {caller_name} left room {conf}"
        else:
            body = f"PBX {event_type}: {caller_name or channel}"

        priority = Priority.ELEVATED if event_type == "Newchannel" else Priority.NORMAL
        nm = NormalizedMessage(
            source_adapter=self.name,
            source_channel="AREDN PBX",
            from_id=caller_id or channel,
            from_display=caller_name or caller_id or channel,
            body=body,
            priority=priority,
            raw={k: v for k, v in event.items() if k not in ("Event", "Privilege")},
        )
        await self._enqueue(nm)
        log.debug("AREDN AMI %s: %s from %s", self.name, event_type, caller_name)

    # ── AMI protocol helpers ──────────────────────────────────────────────

    async def _send_action(self, fields: dict) -> None:
        """Send an AMI action block."""
        self._action_counter += 1
        hdr   = f"ActionID: ech-{self._action_counter}\r\n"
        lines = "\r\n".join(f"{k}: {v}" for k, v in fields.items())
        self._writer.write((hdr + lines + "\r\n\r\n").encode())
        await self._writer.drain()

    async def _read_response(self) -> dict:
        """Read one AMI event/response block (blank-line delimited)."""
        fields: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(self._reader.readline(), timeout=65.0)
            line = line.decode("utf-8", errors="replace").strip("\r\n")
            if not line:
                break
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip()] = val.strip()
        return fields

    # ── PBX actions ───────────────────────────────────────────────────────

    async def originate(self, destination: str, caller_extension: str | None = None) -> bool:
        """Click-to-call: ring local extension, then bridge to destination."""
        if not self._connected or not self._writer:
            return False
        src = caller_extension or self._local_ext
        if not destination.startswith(("SIP/", "PJSIP/")):
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
        """Page / announce to one or more extensions simultaneously."""
        if not self._connected or not self._writer:
            return False
        dest = target or self._page_target or f"SIP/{self._local_ext}"
        if self._page_method == "exten" and self._page_extension:
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
            await self._send_action({
                "Action":      "Originate",
                "Channel":     "Local/s@default",
                "Application": "Page",
                "Data":        dest,
                "CallerID":    self._caller_id,
                "Timeout":     "30000",
                "Async":       "true",
            })
        log.info("AMI Page: dest=%s", dest)
        return True

    async def push_to_screen(self, text: str) -> bool:
        if not self._screen_url:
            return False
        try:
            import urllib.request, urllib.parse, base64
            xml  = f"<YealinkIPPhoneTextScreen><Title>ECH</Title><Text>{text[:100]}</Text></YealinkIPPhoneTextScreen>"
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
        items = "\n".join(
            f'  <DirectoryEntry><Name>{c.get("display_name","")}</Name>'
            f'<Telephone>{c.get("aprs_callsign") or c.get("node_id","")}</Telephone></DirectoryEntry>'
            for c in contacts[:200]
        )
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<YealinkIPPhoneDirectory>\n{items}\n</YealinkIPPhoneDirectory>'

    def recent_calls(self) -> list[dict]:
        return list(self._call_log)

    def active_calls(self) -> list[dict]:
        return list(self._active_calls.values())

    def _health_detail(self) -> dict:
        return {
            "host":             f"{self._host}:{self._port}",
            "pbx_version":      self._pbx_version,
            "call_events":      self._call_count,
            "monitored_events": len(self._events),
            "active_calls":     len(self._active_calls),
            "recent_calls":     len(self._call_log),
            "local_ext":        self._local_ext,
        }


class MockAREDNAMIAdapter(Adapter):
    """Mock AREDN PBX AMI adapter."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "aredn-pbx-mock")
        self._interval = config.get("interval_sec", 60.0)
        self._run_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("%s: mock AREDN PBX connected", self.name)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: NormalizedMessage) -> bool:
        self._mark_tx(message)
        return True

    async def _run(self) -> None:
        import random
        events = [
            ("📞 INCOMING", "W1ABC (2075551234) → ext 101", Priority.ELEVATED),
            ("✅ ANSWERED", "W1ABC answered on SIP/101", Priority.NORMAL),
            ("📵 HANGUP",   "SIP/101 — Normal Clearing",  Priority.NORMAL),
            ("📲 DIALING",  "EOC calling shelter ext 203", Priority.NORMAL),
            ("🎙 CONFERENCE","W1PBR joined room 900",       Priority.NORMAL),
        ]
        try:
            while self._connected:
                if self.is_paused():
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(self._interval + random.uniform(-15, 15))
                label, body, priority = random.choice(events)
                nm = NormalizedMessage(
                    source_adapter=self.name,
                    source_channel="AREDN PBX",
                    from_id="pbx",
                    from_display="AREDN PBX",
                    body=body,
                    priority=priority,
                    raw={"mock": True},
                )
                await self._enqueue(nm)
        except asyncio.CancelledError:
            pass

    def _health_detail(self) -> dict:
        return {"host": "mock", "mode": "mock"}
