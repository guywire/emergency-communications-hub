"""
ech/adapters/pat_winlink.py
----------------------------
Winlink adapter via Pat (https://getpat.io) — open-source, Linux-native
Winlink client written in Go by LA5NTA.

Integration model:
  Pat runs as a sidecar systemd service on the same machine as ECH.
  ECH talks to Pat's HTTP API on localhost:8080 using httpx (async HTTP).
  Pat handles all Winlink protocol complexity: B2F forwarding, CMS
  authentication, transport negotiation (ARDOP / AX.25 / telnet).

Pat HTTP API (localhost:8080 by default):
  GET  /api/mailbox/{folder}          list messages  (folder: in|out|sent|archive)
  GET  /api/mailbox/{folder}/{mid}    read one message
  POST /api/mailbox/out               compose/post to outbox
  POST /api/connect                   trigger a connect session
  POST /api/disconnect                abort current session
  GET  /api/status                    station info + connection state
  WS   /ws                            live event stream (new mail, progress)

Pat mailbox directory (fallback when API is unavailable):
  ~/.local/share/pat/mailbox/CALL/in/   inbound .b2f files
  ~/.local/share/pat/mailbox/CALL/out/  outbound .b2f files

Pat WebSocket events (JSON):
  {"Lines": ["..."], "Preamble": "..."}  — connect progress
  {"NewMessages": N}                     — new mail notification

Config keys:
  name            str     adapter name (default: winlink)
  callsign        str     your Winlink callsign (REQUIRED)
  pat_url         str     Pat HTTP base URL (default: http://127.0.0.1:8080)
  poll_interval   int     seconds between inbox polls (default: 300 = 5 min)
  auto_connect    bool    trigger Pat connect on startup (default: False)
  connect_alias   str     Pat connect alias to use (default: telnet)
                          e.g. "telnet", "ardop", "ax25"
  mailbox_path    str     path to Pat mailbox dir (for file-watch fallback)
                          default: ~/.local/share/pat/mailbox/{callsign}/in

Setup:
  1. Install Pat: https://getpat.io
     wget https://github.com/la5nta/pat/releases/latest/download/pat_linux_arm64.tar.gz
     sudo install pat /usr/local/bin/
  2. Configure Pat:  pat configure  (set callsign + password)
  3. Run Pat HTTP server:
     pat http --listen 127.0.0.1:8080
     (or add to systemd — see scripts/pat.service in this repo)
  4. Set  type: pat_winlink  in ECH config.yaml

First-time Winlink registration:
  Connect via telnet once to register your callsign and receive password:
    pat connect telnet
  Your password will arrive in the first inbox message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

EMRG_WORDS = {"emergency", "mayday", "sos", "urgent help", "life safety", "evacuate now"}
ELVT_WORDS = {"urgent", "priority", "immediate", "standby all", "resource request"}


def _priority(subject: str, body: str) -> Priority:
    text = (subject + " " + body).lower()
    if any(w in text for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in text for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


def _parse_pat_date(s: str) -> datetime:
    """Parse Pat's ISO-8601 date strings, falling back to now."""
    if not s:
        return datetime.now(timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(s.rstrip("Z"), fmt.rstrip("Z"))
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


class PatWinlinkAdapter(Adapter):
    """
    Winlink adapter via Pat HTTP API.
    Requires Pat running locally with --listen on pat_url.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name          = config.get("name", "winlink")
        self._callsign     = config.get("callsign", "").upper()
        self._pat_url      = config.get("pat_url", "http://127.0.0.1:8080").rstrip("/")
        self._poll_interval = int(config.get("poll_interval", 300))
        self._auto_connect  = bool(config.get("auto_connect", False))
        self._connect_alias = config.get("connect_alias", "telnet")
        self._mailbox_path  = config.get("mailbox_path", None)

        if not self._callsign:
            raise ValueError("PatWinlinkAdapter: 'callsign' is required in config")

        self._client: httpx.AsyncClient | None = None
        self._run_task: asyncio.Task | None = None
        self._ws_task: asyncio.Task | None = None
        self._seen_mids: set[str] = set()    # message IDs already processed
        self._pat_version: str = ""
        self._pat_status: dict = {}
        self._rx_count = 0
        self._tx_count = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._pat_url,
            timeout=10.0,
        )
        # Verify Pat is reachable
        try:
            resp = await self._client.get("/api/status")
            resp.raise_for_status()
            self._pat_status = resp.json()
            self._pat_version = self._pat_status.get("PatVersion", "")
            log.info(
                "Pat Winlink %s: connected to Pat %s, callsign %s",
                self.name, self._pat_version, self._callsign,
            )
        except (httpx.ConnectError, httpx.HTTPError) as exc:
            raise ConnectionError(
                f"Pat not reachable at {self._pat_url} — "
                f"is Pat running? ('pat http --listen 127.0.0.1:8080')  ({exc})"
            ) from exc

        self._connected = True

        # Initial inbox drain (mark existing as seen without emitting them)
        await self._mark_existing_seen()

        # Auto-connect to fetch any waiting messages
        if self._auto_connect:
            await self._trigger_connect()

        # Start poll loop and WebSocket watcher
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-poll")
        self._ws_task  = asyncio.create_task(self._watch_ws(), name=f"{self.name}-ws")

    async def disconnect(self) -> None:
        self._connected = False
        for task in (self._run_task, self._ws_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._client:
            await self._client.aclose()
        log.info("Pat Winlink %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Post a message to Pat's outbox via POST /api/mailbox/out.
        message.to_id should be a Winlink callsign or email address.
        Sending the message to the CMS requires a subsequent connect().
        """
        if not message.to_id:
            log.warning("Pat Winlink %s: to_id (callsign/email) required", self.name)
            return False
        if not self._client or not self._connected:
            return False

        # Pat v1.0.0 compose payload
        # Required fields confirmed from Pat source: to, date, subject, body
        date_str = message.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "to":      message.to_id,
            "cc":      "",
            "date":    date_str,
            "subject": message.body[:60],
            "body":    message.body,
        }
        try:
            # Try v1.0.0 endpoint first
            resp = await self._client.post("/api/mailbox/out", json=payload)
            log.debug("Pat Winlink %s: POST /api/mailbox/out → %d: %s",
                      self.name, resp.status_code, resp.text[:200])

            if resp.status_code == 404:
                # Try alternate endpoint used in some Pat versions
                resp = await self._client.post("/mailbox/out", json=payload)
                log.debug("Pat Winlink %s: POST /mailbox/out → %d: %s",
                          self.name, resp.status_code, resp.text[:200])

            if resp.status_code not in (200, 201, 204):
                log.error("Pat Winlink %s: outbox POST failed %d: %s",
                          self.name, resp.status_code, resp.text[:300])
                # Fall back to CLI compose
                return await self._send_via_cli(message)

            self._tx_count += 1
            self._mark_tx(message)
            try:
                mid = resp.json().get("mid", "")
            except Exception:
                mid = "(no mid)"
            log.info("Pat Winlink %s: posted to outbox → %s (mid=%s)",
                     self.name, message.to_id, mid)

            if self._auto_connect:
                asyncio.create_task(self._trigger_connect())

            return True
        except Exception as exc:
            log.error("Pat Winlink %s: send error: %s", self.name, exc)
            return await self._send_via_cli(message)

    # ── Internal receive loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        """Periodic inbox poll — catches new messages every poll_interval seconds."""
        log.debug("Pat Winlink %s: poll loop started (every %ds)", self.name, self._poll_interval)
        try:
            while self._connected:
                await asyncio.sleep(self._poll_interval)
                await self._poll_inbox()
                await self._refresh_status()
        except asyncio.CancelledError:
            pass

    async def _watch_ws(self) -> None:
        """
        Connect to Pat's WebSocket for live event notifications.
        Reconnects automatically on disconnect.
        Handles {"NewMessages": N} events to trigger immediate inbox poll.
        """
        import websockets

        ws_url = self._pat_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        backoff = 2.0

        while self._connected:
            try:
                async with websockets.connect(ws_url, ping_interval=20) as ws:
                    backoff = 2.0
                    log.debug("Pat Winlink %s: WebSocket connected to %s", self.name, ws_url)
                    async for raw in ws:
                        if not self._connected:
                            break
                        await self._handle_ws_event(raw)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._connected:
                    log.debug("Pat Winlink %s: WS disconnected (%s), retry in %.0fs",
                              self.name, exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def _handle_ws_event(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return

        if "NewMessages" in event and event["NewMessages"] > 0:
            log.debug("Pat Winlink %s: WS new-mail event (%d)", self.name, event["NewMessages"])
            await self._poll_inbox()

        elif "Lines" in event:
            # Connect progress lines — surface as a status message if interesting
            lines = event.get("Lines", [])
            if lines:
                log.debug("Pat Winlink %s: connect progress: %s", self.name, lines[-1])

    # ── Mailbox operations ────────────────────────────────────────────────

    async def _mark_existing_seen(self) -> None:
        """
        On startup, fetch the current inbox and mark all existing messages
        as seen so we don't re-emit them on first poll.
        """
        try:
            msgs = await self._fetch_inbox()
            for m in msgs:
                self._seen_mids.add(m.get("mid", ""))
            log.debug("Pat Winlink %s: marked %d existing messages as seen", self.name, len(msgs))
        except Exception as exc:
            log.debug("Pat Winlink %s: could not pre-seed seen set: %s", self.name, exc)

    async def _poll_inbox(self) -> None:
        """Fetch inbox, emit any messages not yet seen."""
        try:
            msgs = await self._fetch_inbox()
            new_count = 0
            for msg_meta in msgs:
                mid = msg_meta.get("mid", "")
                if mid in self._seen_mids:
                    continue
                self._seen_mids.add(mid)

                # Fetch full message body
                full = await self._fetch_message("in", mid)
                if full:
                    await self._emit_message(full)
                    new_count += 1

            if new_count:
                log.info("Pat Winlink %s: %d new message(s) in inbox", self.name, new_count)
        except Exception as exc:
            log.debug("Pat Winlink %s: inbox poll error: %s", self.name, exc)

    async def _fetch_inbox(self) -> list[dict]:
        """GET /api/mailbox/in — returns list of message metadata dicts."""
        resp = await self._client.get("/api/mailbox/in")
        resp.raise_for_status()
        data = resp.json()
        # Pat returns either a list directly, or {"Messages": [...]}
        if isinstance(data, list):
            return data
        return data.get("Messages", []) or []

    async def _fetch_message(self, folder: str, mid: str) -> dict | None:
        """GET /api/mailbox/{folder}/{mid} — full message with body."""
        try:
            resp = await self._client.get(f"/api/mailbox/{folder}/{mid}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.debug("Pat Winlink %s: fetch message %s/%s error: %s", self.name, folder, mid, exc)
            return None

    async def _emit_message(self, msg: dict) -> None:
        """Convert a Pat JSON message dict to NormalizedMessage and enqueue."""
        self._rx_count += 1
        mid     = msg.get("mid", "")
        subject = msg.get("subject", "").strip()
        body    = msg.get("body", "").strip()
        from_   = msg.get("from", "").strip()
        date_s  = msg.get("date", "")

        # Combine subject + body for display (Winlink is email-style)
        display_body = f"[{subject}] {body}" if subject else body
        if not display_body.strip():
            display_body = f"[{subject}]" if subject else "(empty)"

        ts = _parse_pat_date(date_s)
        priority = _priority(subject, body)

        nm = NormalizedMessage(
            source_adapter=self.name,
            source_channel="Winlink",
            from_id=from_,
            from_display=from_,
            body=display_body[:500],
            timestamp=ts,
            priority=priority,
            raw={
                "mid": mid,
                "subject": subject,
                "attachments": [a.get("name", "") for a in msg.get("files", [])],
                "transport": self._pat_status.get("ActiveListeners", []),
            },
        )
        await self._enqueue(nm)
        log.debug("Pat Winlink %s: emitted mid=%s from=%s subj=%r", self.name, mid, from_, subject)

    async def _trigger_connect(self) -> None:
        """POST /api/connect — tell Pat to connect and exchange messages."""
        try:
            resp = await self._client.post(
                "/api/connect",
                json={"transport": self._connect_alias},
                timeout=5.0,
            )
            resp.raise_for_status()
            log.info("Pat Winlink %s: connect triggered via %s", self.name, self._connect_alias)
        except Exception as exc:
            log.warning("Pat Winlink %s: connect trigger failed: %s", self.name, exc)

    async def _refresh_status(self) -> None:
        try:
            resp = await self._client.get("/api/status", timeout=3.0)
            if resp.status_code == 200:
                self._pat_status = resp.json()
        except Exception:
            pass

    # ── Required abstract stub ────────────────────────────────────────────

    async def _send_via_cli(self, message) -> bool:
        """
        Fallback: write message directly to Pat's mailbox directory.
        Pat stores outbox messages as .b2f files in:
          ~/.local/share/pat/mailbox/{CALLSIGN}/out/
        We write a minimal Winlink message file that Pat will pick up.
        """
        import os, uuid
        from pathlib import Path

        callsign = self._callsign.upper()
        # Try common Pat mailbox locations
        candidates = [
            Path(f"/home/ech/.local/share/pat/mailbox/{callsign}/out"),
            Path(f"/root/.local/share/pat/mailbox/{callsign}/out"),
            Path(os.path.expanduser(f"~/.local/share/pat/mailbox/{callsign}/out")),
        ]
        mailbox_out = None
        for path in candidates:
            if path.parent.parent.exists():
                path.mkdir(parents=True, exist_ok=True)
                mailbox_out = path
                break

        if not mailbox_out:
            log.error("Pat Winlink %s: cannot find Pat mailbox directory. "
                      "Tried: %s", self.name, [str(p) for p in candidates])
            return False

        mid = str(uuid.uuid4())[:8].upper()
        filename = mailbox_out / f"{mid}.b2f"
        # Winlink B2F message format that Pat reads from its mailbox directory
        # Date must be in the format Pat expects: YYYY/MM/DD HH:MM
        body_bytes = message.body.encode("utf-8")
        content = (
            f"Mid: {mid}\r\n"
            f"Date: {message.timestamp.strftime('%Y/%m/%d %H:%M')}\r\n"
            f"From: {callsign}\r\n"
            f"To: {message.to_id}\r\n"
            f"Subject: {message.body[:60]}\r\n"
            f"Mbo: {callsign}\r\n"
            f"Body: {len(body_bytes)}\r\n"
            f"\r\n"
            f"{message.body}\r\n"
        )
        try:
            filename.write_text(content)
            log.info("Pat Winlink %s: wrote message to mailbox file %s",
                     self.name, filename)
            self._tx_count += 1
            self._mark_tx(message)
            return True
        except Exception as exc:
            log.error("Pat Winlink %s: mailbox write error: %s", self.name, exc)
            return False

    async def _run(self) -> None:
        """Implemented above as the poll loop."""
        pass

    # ── Overrides ─────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        listeners = self._pat_status.get("ActiveListeners", [])
        connected_to = self._pat_status.get("ConnectedTo", "")
        return {
            "pat_url": self._pat_url,
            "callsign": self._callsign,
            "pat_version": self._pat_version,
            "active_listeners": listeners,
            "connected_to": connected_to,
            "connect_alias": self._connect_alias,
            "seen_messages": len(self._seen_mids),
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
        }
