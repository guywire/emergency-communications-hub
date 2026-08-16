"""
ech/adapters/aredn_meshchat.py
-------------------------------
AREDN mesh text messaging via MeshChat (github.com/kn6plv/meshchat).

MeshChat is the de facto chat service for AREDN mesh networks — it runs as a
Lua CGI app on an AREDN node (or a LAN-attached Debian/Pi "service computer"),
syncs its message database across every mesh node running it, and is
accessed by humans through a plain web browser. This adapter talks to that
same CGI backend directly instead of the browser UI, over the existing AREDN
IP mesh — no new protocol, no new hardware, just pointing at a MeshChat host
reachable on the mesh (matches the Phase 13 roadmap note: this is "what's
the lightest-weight service to run over AREDN" rather than a new protocol).

CAUTION (added 2026-08-12, after this adapter was written): AREDN's own team
has since introduced "Raven" (github.com/kn6plv/Raven, same author as
MeshChat), explicitly as MeshChat's replacement — the two are NOT
compatible. Raven is alpha software as of this writing, with essentially no
exposed API documentation, and it already bridges AREDN to Meshtastic/
MeshCore/Winlink directly, which may make a separate ECH adapter unnecessary
depending on how that bridging works. Before investing further in this
MeshChat adapter (or wiring it into a live config), (re)check whether
MeshChat is even still the right target, or whether integrating via/against
Raven instead makes more sense. This file is left as-is (untested either
way) rather than guessed-and-rewritten against Raven's undocumented surface.

REST-ish CGI API (extracted from source, Aug 2026 —
  https://github.com/kn6plv/meshchat/blob/master/src/data/www/cgi-bin/meshchat
  — NOT live-tested: no AREDN mesh with a reachable MeshChat instance was
  available at implementation time. Verify against a real node before relying
  on this in production; the exact GET-vs-POST convention in particular is
  inferred, not confirmed, from the source):
  GET  {base_url}?action=config
    Returns {"version", "protocol_verison", "node", "zone", "default_channel", "debug"}
    — used as a cheap connectivity check.
  GET  {base_url}?action=messages&epoch={unix_ts}
    Returns new messages since epoch: [{"id","epoch","message","call_sign","node","platform","channel"}, ...]
  POST {base_url}?action=send_message  (form-encoded body)
    Params: message, call_sign, channel (optional), epoch (optional), id (optional)
    Returns {"status":200, "response":"OK"}

Config keys:
  name            str    adapter name (default: aredn-meshchat)
  base_url        str    MeshChat CGI URL, e.g. "http://localnode.local.mesh/cgi-bin/meshchat" (REQUIRED)
  call_sign       str    your callsign, used as MeshChat's sender identity (REQUIRED)
  channel         str    MeshChat channel/zone to post to (default: server's default_channel)
  poll_interval   int    seconds between message polls (default: 30 — mesh chat is near-real-time,
                          much shorter than e.g. Winlink's 300s default)
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

EMRG_WORDS = {"emergency", "mayday", "sos", "evacuate now"}
ELVT_WORDS = {"urgent", "priority", "immediate"}


def _priority(text: str) -> Priority:
    lower = text.lower()
    if any(w in lower for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in lower for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


class AREDNMeshChatAdapter(Adapter):
    """AREDN mesh text messaging via a MeshChat CGI backend."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "aredn-meshchat")
        self._base_url = config.get("base_url", "").rstrip("/")
        self._call_sign = config.get("call_sign", "").upper().strip()
        self._channel = config.get("channel", "")
        self._poll_interval = int(config.get("poll_interval", 30))

        if not self._base_url:
            raise ValueError("AREDNMeshChatAdapter: 'base_url' is required in config")
        if not self._call_sign:
            raise ValueError("AREDNMeshChatAdapter: 'call_sign' is required in config")

        self._client: httpx.AsyncClient | None = None
        self._run_task: asyncio.Task | None = None
        self._last_epoch: float = 0.0
        self._seen_ids: set[str] = set()
        self._rx_count = 0
        self._tx_count = 0
        self._last_error: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=10.0)
        try:
            resp = await self._client.get(self._base_url, params={"action": "config"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ConnectionError(
                f"AREDN MeshChat {self.name}: not reachable at {self._base_url}: {exc}"
            ) from exc

        # Skip everything already in the message database — only emit new
        # traffic from here on, same reasoning as pat_winlink's inbox seeding.
        self._last_epoch = time.time()
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-poll")
        log.info("AREDN MeshChat %s: ready (base_url=%s, call_sign=%s)",
                  self.name, self._base_url, self._call_sign)

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        log.info("AREDN MeshChat %s: disconnected", self.name)

    # ── Send ──────────────────────────────────────────────────────────────

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Post to the MeshChat channel. MeshChat has no direct-message concept
        — everyone on a channel sees every message — so message.to_id is
        ignored; use message.body for the text.
        """
        if not self._client or not self._connected:
            return False

        data = {"message": message.body, "call_sign": self._call_sign}
        if self._channel:
            data["channel"] = self._channel

        try:
            resp = await self._client.post(self._base_url, params={"action": "send_message"}, data=data)
            if resp.status_code != 200:
                self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                log.error("AREDN MeshChat %s: send failed: %s", self.name, self._last_error)
                return False
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            log.error("AREDN MeshChat %s: send error: %s", self.name, exc)
            return False

        self._tx_count += 1
        self._last_error = None
        self._mark_tx(message)
        return True

    # ── Receive ───────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(self._poll_interval)
                await self._poll_messages()
        except asyncio.CancelledError:
            pass

    async def _poll_messages(self) -> None:
        try:
            resp = await self._client.get(
                self._base_url, params={"action": "messages", "epoch": self._last_epoch}
            )
            resp.raise_for_status()
            messages = resp.json()
            self._last_error = None
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            log.warning("AREDN MeshChat %s: poll error: %s", self.name, exc)
            return
        except ValueError as exc:
            self._last_error = f"bad JSON response: {exc}"
            log.warning("AREDN MeshChat %s: %s", self.name, self._last_error)
            return

        if not isinstance(messages, list):
            return

        for m in messages:
            mid = str(m.get("id", ""))
            if mid and mid in self._seen_ids:
                continue
            if mid:
                self._seen_ids.add(mid)

            sender = (m.get("call_sign") or "").upper().strip()
            if sender == self._call_sign:
                continue   # our own message echoed back by the sync

            epoch = m.get("epoch")
            if isinstance(epoch, (int, float)) and epoch > self._last_epoch:
                self._last_epoch = epoch

            body = m.get("message", "")
            if not body:
                continue

            self._rx_count += 1
            msg = NormalizedMessage(
                source_adapter=self.name,
                source_channel=m.get("channel") or self._channel or "meshchat",
                from_id=sender or "unknown",
                from_display=sender,
                body=body,
                priority=_priority(body),
                raw={"node": m.get("node"), "platform": m.get("platform"), "id": mid},
            )
            await self._enqueue(msg)

    # ── Health ────────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        return {
            "base_url": self._base_url,
            "call_sign": self._call_sign,
            "channel": self._channel or "(default)",
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "last_error": self._last_error,
        }
