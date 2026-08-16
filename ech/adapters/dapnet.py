"""
ech/adapters/dapnet.py
-----------------------
DAPNET (Decentralized Amateur Paging Network, hampager.de) adapter.

DAPNET is a POCSAG paging network — it is send-only from ECH's perspective.
Pagers are receive-only hardware with no talkback channel, so there is no
inbound traffic to normalize; this adapter's `_run()` loop only does a
lightweight periodic reachability check for the health panel.

REST API (confirmed against the official docs, Aug 2026):
  https://hampager.de/dokuwiki/doku.php?id=dapnetapisendcall
  https://dapnet-core.github.io/api-docs/
  POST {api_url}/calls   — send a page
    Body: {"text": str, "callSignNames": [str], "transmitterGroupNames": [str], "emergency": bool}
    Auth: HTTP Basic (DAPNET account callsign + password)
    Max message length: 80 chars (DAPNET truncates server-side; we also warn locally)
    "If one callsign fails or a group is invalid, the whole call is discarded" — no partial delivery.
    Confirmed live (Aug 2026) against a real account: returns 201 on success.
  GET  {api_url}/calls   — CONFIRMED FORBIDDEN (403 "No permission for this request") for an
    ordinary non-admin account even when POST /calls to the same account works fine — DAPNET
    gates "list all calls" separately from send permission. Do NOT use this for a connectivity
    probe (it will make the adapter falsely report itself unreachable forever).
  GET  {api_url}/transmitters — public, unauthenticated-friendly (200 regardless of account
    validity). Used for a bare "is the host up" check only; it does NOT validate credentials
    or send permission — those can only be proven by an actual POST /calls.

Config keys:
  name                 str        adapter name (default: dapnet)
  callsign             str        your DAPNET account callsign, used for HTTP Basic auth (REQUIRED)
  password             str        your DAPNET account password (REQUIRED) — live-server config only, never commit
  transmitter_groups   list[str]  DAPNET transmitter group short names to route pages through (REQUIRED)
                                  — found under "Transmitter Groups" on the DAPNET web UI, e.g. ["dl-all"]
  api_url              str        API base URL (default: "http://hampager.de/api")
  poll_interval        int        seconds between reachability checks (default: 300)

Sending a page:
  Set message.to_id to the recipient's DAPNET-registered callsign (no SSID).
  message.priority == EMERGENCY maps to DAPNET's "emergency" flag (repeated
  transmission + distinct tone on the receiving pager).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

MAX_LEN = 80  # DAPNET truncates anything longer server-side


class DAPNETAdapter(Adapter):
    """Send-only adapter for the DAPNET amateur paging network."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "dapnet")
        self._callsign = config.get("callsign", "").upper().strip()
        self._password = config.get("password", "")
        self._transmitter_groups = config.get("transmitter_groups", [])
        self._api_url = config.get("api_url", "http://hampager.de/api").rstrip("/")
        self._poll_interval = int(config.get("poll_interval", 300))

        if not self._callsign or not self._password:
            raise ValueError("DAPNETAdapter: 'callsign' and 'password' are required in config")
        if not self._transmitter_groups:
            raise ValueError(
                "DAPNETAdapter: 'transmitter_groups' is required — see 'Transmitter Groups' "
                "on the DAPNET web UI for the short names covering your area"
            )

        self._client: httpx.AsyncClient | None = None
        self._run_task: asyncio.Task | None = None
        self._tx_count = 0
        self._last_error: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._api_url,
            auth=(self._callsign, self._password),
            timeout=10.0,
        )
        # No pre-flight credential probe: GET /calls (confirmed live against a
        # real account) returns 403 for ordinary non-admin accounts even
        # though POST /calls (actually sending) works fine — DAPNET restricts
        # the "list all calls" endpoint separately from send permission. There
        # is no other authenticated endpoint that validates credentials
        # without side effects (a public GET like /transmitters returns 200
        # regardless of whether *this* account's credentials are valid), so
        # we optimistically mark connected and let send() surface real
        # auth/permission errors on the first actual page.
        try:
            resp = await self._client.get("/transmitters")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ConnectionError(f"DAPNET {self.name}: not reachable at {self._api_url}: {exc}") from exc

        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-health")
        log.info("DAPNET %s: ready (callsign=%s, groups=%s)",
                  self.name, self._callsign, self._transmitter_groups)

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
        log.info("DAPNET %s: disconnected", self.name)

    # ── Send ──────────────────────────────────────────────────────────────

    async def send(self, message: NormalizedMessage) -> bool:
        """
        Page message.to_id (a DAPNET-registered callsign) with message.body.
        DAPNET is one-way — pagers cannot reply, so there is no delivery
        confirmation beyond the HTTP response accepting the call.
        """
        if not message.to_id:
            log.warning("DAPNET %s: to_id (recipient callsign) required", self.name)
            return False
        if not self._client or not self._connected:
            return False

        body = message.body
        if len(body) > MAX_LEN:
            log.warning("DAPNET %s: message is %d chars, DAPNET truncates at %d",
                         self.name, len(body), MAX_LEN)
            body = body[:MAX_LEN]

        payload = {
            "text": body,
            "callSignNames": [message.to_id.upper().strip()],
            "transmitterGroupNames": self._transmitter_groups,
            "emergency": message.priority == Priority.EMERGENCY,
        }
        try:
            resp = await self._client.post("/calls", json=payload)
            if resp.status_code not in (200, 201):
                self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                log.error("DAPNET %s: send failed: %s", self.name, self._last_error)
                return False
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            log.error("DAPNET %s: send error: %s", self.name, exc)
            return False

        self._tx_count += 1
        self._last_error = None
        self._mark_tx(message)
        log.info("DAPNET %s: paged %s via %s", self.name, message.to_id, self._transmitter_groups)
        return True

    # ── Health-only background loop (no inbound traffic — see module docstring) ──

    async def _run(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(self._poll_interval)
                try:
                    resp = await self._client.get("/transmitters")
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self._last_error = str(exc)
                    log.warning("DAPNET %s: reachability check failed: %s", self.name, exc)
        except asyncio.CancelledError:
            pass

    # ── Health ────────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        return {
            "callsign": self._callsign,
            "transmitter_groups": self._transmitter_groups,
            "tx_count": self._tx_count,
            "last_error": self._last_error,
        }
