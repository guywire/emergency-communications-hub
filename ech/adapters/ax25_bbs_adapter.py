"""
ech/adapters/ax25_bbs_adapter.py
----------------------------------
AX.25 packet radio BBS adapter — polls a classic connected-mode packet
bulletin board (W0RLI/FBB/MSYS-style), not Winlink. Winlink (even when it
rides over an AX.25/packet transport) is already handled by pat_winlink.py,
which talks to Pat's HTTP API; Pat owns the Winlink protocol (B2F, CMS auth)
regardless of which transport carries it. This adapter is for classic BBS
message forwarding, which is a different protocol entirely — a plain
terminal-style command dialect (L/R/S/B) once connected.

Integration model:
  This machine already has the Linux AX.25 stack configured: the `ax25`
  kernel module loaded, `kissattach` bound to a KISS TNC (e.g. Direwolf)
  defining a port in /etc/ax25/axports, and ax25-apps installed. ECH does
  NOT configure any of that — it's a prerequisite, same as Asterisk already
  running for the `asterisk` adapter, or Pat already running for
  `pat_winlink`.

  Rather than reimplementing the AX.25 connected-mode protocol (sequence
  numbers, retransmission timers, etc. — a real correctness risk to get
  subtly wrong), this adapter drives the standard `axcall` (aka `call`)
  program from ax25-apps as a subprocess: a full-duplex terminal session
  over stdin/stdout that already correctly implements connected-mode AX.25
  via the kernel stack. `-W` keeps it running until the far end disconnects
  even after we close stdin, which is exactly the shape needed for a
  scripted list/read/bye exchange.

  axcall usage (confirmed from ax25-apps documentation):
    axcall [-s mycall] [-W] <port> <bbs-callsign>
  A session with a packet BBS is not persistent like AMI/HTTP — each poll
  spawns a fresh connect/list/read/bye session and exits.

  BBS command dialect varies by software (W0RLI vs FBB vs MSYS vs JNOS).
  The defaults below (L / R <n> / B, body ended by a line "/EX") follow the
  original W0RLI convention most packet BBS software still accepts; adjust
  list_command/read_command_fmt/send_end_marker in config if your BBS uses a
  different dialect.

Config keys:
  name                str   adapter name (default: packet-bbs)
  ax25_port           str   port name from /etc/ax25/axports (REQUIRED)
  bbs_callsign        str   target BBS station, e.g. "KA1ABC-1" (REQUIRED)
  mycall               str   override 'mycall' passed to axcall (-s); usually
                             unnecessary — the port's axports entry already
                             defines the default mycall for that port.
  axcall_path          str   binary to invoke (default: "axcall", falls back
                             to "call" if axcall isn't found on PATH)
  poll_interval_sec    int   seconds between BBS connects (default: 1800 = 30min
                             — packet is slow and a connect ties up the channel,
                             so this shouldn't be aggressive like an IP poll)
  connect_timeout_sec  int   seconds to wait for the session to complete
                             (default: 120)
  list_command          str   BBS command to list new mail (default: "L")
  read_command_fmt      str   BBS command template to read message N,
                             {n} substituted (default: "R {n}")
  bye_command           str   BBS command to disconnect (default: "B")
  send_end_marker       str   line that ends outgoing message body
                             (default: "/EX")

Setup (prerequisite, outside ECH):
  1. Configure a KISS TNC (e.g. Direwolf) and note its KISS TCP/serial port.
  2. sudo apt install ax25-apps ax25-tools; sudo modprobe ax25
  3. Define the port in /etc/ax25/axports, e.g.:
       radio   MYCALL-1   1200   255   7    Packet radio via Direwolf
  4. kissattach /dev/pts/N radio   (or the appropriate KISS device)
  5. Set type: ax25_bbs in ECH config.yaml with ax25_port: "radio"
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import datetime, timezone

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

# Typical BBS listing row: "  123  N  1024 W1AW    KA1ABC   Subject text here"
# Columns vary by BBS software — this pulls a leading message number and
# treats the rest of the line as free text rather than assuming exact columns,
# since guessing a rigid column layout is more likely to break than a loose match.
_LISTING_ROW_RE = re.compile(r"^\s*(\d{1,6})\s+(\S.*)$")

EMRG_WORDS = {"emergency", "mayday", "sos", "urgent help", "life safety", "evacuate now"}
ELVT_WORDS = {"urgent", "priority", "immediate", "standby all", "resource request"}


def _priority(text: str) -> Priority:
    t = text.lower()
    if any(w in t for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in t for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


class AX25BBSAdapter(Adapter):
    """
    Polls a classic AX.25 packet BBS via the `axcall` connected-mode client.
    Receive-capable and send-capable (posts outgoing mail to the BBS).
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "packet-bbs")
        self._ax25_port    = config.get("ax25_port", "")
        self._bbs_callsign = config.get("bbs_callsign", "").upper()
        self._mycall       = config.get("mycall", "").upper()
        self._axcall_path  = config.get("axcall_path", "")
        self._poll_interval    = int(config.get("poll_interval_sec", 1800))
        self._connect_timeout  = int(config.get("connect_timeout_sec", 120))
        self._list_command     = config.get("list_command", "L")
        self._read_command_fmt = config.get("read_command_fmt", "R {n}")
        self._bye_command      = config.get("bye_command", "B")
        self._send_end_marker  = config.get("send_end_marker", "/EX")

        if not self._ax25_port:
            raise ValueError("AX25BBSAdapter: 'ax25_port' is required (name from /etc/ax25/axports)")
        if not self._bbs_callsign:
            raise ValueError("AX25BBSAdapter: 'bbs_callsign' is required")

        self._run_task: asyncio.Task | None = None
        self._seen_msg_ids: set[str] = set()
        self._rx_count = 0
        self._tx_count = 0
        self._poll_count = 0
        self._last_session_ok = False
        self._last_session_error = ""
        self._last_poll: datetime | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._axcall_path = (
            self._axcall_path
            or shutil.which("axcall")
            or shutil.which("call")
        )
        if not self._axcall_path:
            raise ConnectionError(
                "AX25BBSAdapter: 'axcall' (or 'call') not found on PATH — "
                "install ax25-apps (sudo apt install ax25-apps) and configure "
                "/etc/ax25/axports + kissattach before enabling this adapter"
            )
        self._connected = True
        log.info("AX25BBS %s: using %s, port=%s, bbs=%s",
                  self.name, self._axcall_path, self._ax25_port, self._bbs_callsign)
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-poll")

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        log.info("AX25BBS %s: disconnected", self.name)

    async def send(self, message: NormalizedMessage) -> bool:
        """Post an outgoing message to the BBS, addressed to message.to_id."""
        if not message.to_id:
            log.warning("AX25BBS %s: to_id (destination callsign/bulletin) required", self.name)
            return False
        subject = message.body[:60].replace("\n", " ")
        script = [
            f"S {message.to_id}",
            subject,
            message.body,
            self._send_end_marker,
            self._bye_command,
        ]
        try:
            ok, _output = await self._run_session(script)
        except Exception as exc:
            log.error("AX25BBS %s: send session error: %s", self.name, exc)
            return False
        if ok:
            self._tx_count += 1
            self._mark_tx(message)
            log.info("AX25BBS %s: posted message to %s", self.name, message.to_id)
        return ok

    # ── Poll loop ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            while self._connected:
                await self._poll_bbs()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            pass

    async def _poll_bbs(self) -> None:
        self._poll_count += 1
        self._last_poll = datetime.now(timezone.utc)
        try:
            ok, output = await self._run_session([self._list_command, self._bye_command])
            self._last_session_ok = ok
            self._last_session_error = "" if ok else "session did not complete cleanly"
            if not ok:
                log.warning("AX25BBS %s: list session failed: %s", self.name, output[-300:] if output else "(no output)")
                return

            new_ids = self._parse_listing(output)
            if not new_ids:
                return
            log.info("AX25BBS %s: %d new message(s) in listing", self.name, len(new_ids))

            for msg_id in new_ids:
                read_cmd = self._read_command_fmt.format(n=msg_id)
                ok2, body_output = await self._run_session([read_cmd, self._bye_command])
                if not ok2:
                    log.warning("AX25BBS %s: read session failed for msg %s", self.name, msg_id)
                    continue
                await self._emit_message(msg_id, body_output)
        except Exception as exc:
            self._last_session_error = str(exc)
            log.error("AX25BBS %s: poll error: %s", self.name, exc)

    def _parse_listing(self, output: str) -> list[str]:
        """Extract message numbers from a BBS listing that aren't already seen."""
        found = []
        for line in output.splitlines():
            m = _LISTING_ROW_RE.match(line)
            if not m:
                continue
            msg_id = m.group(1)
            if msg_id in self._seen_msg_ids:
                continue
            self._seen_msg_ids.add(msg_id)
            found.append(msg_id)
        return found

    async def _emit_message(self, msg_id: str, raw_body: str) -> None:
        self._rx_count += 1
        # Strip the echoed command and BBS prompt noise a real terminal
        # session picks up — keep it simple rather than over-fitting to one
        # BBS software's exact banner format.
        lines = [l for l in raw_body.splitlines() if l.strip()]
        body = "\n".join(lines)[:1000] if lines else "(empty message)"

        nm = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"BBS:{self._bbs_callsign}",
            from_id=self._bbs_callsign,
            from_display=self._bbs_callsign,
            body=f"[msg {msg_id}] {body}"[:500],
            timestamp=datetime.now(timezone.utc),
            priority=_priority(body),
            raw={"msg_id": msg_id, "bbs": self._bbs_callsign},
        )
        await self._enqueue(nm)
        log.debug("AX25BBS %s: emitted msg %s from %s", self.name, msg_id, self._bbs_callsign)

    # ── axcall subprocess session ─────────────────────────────────────────

    async def _run_session(self, script_lines: list[str]) -> tuple[bool, str]:
        """
        Spawn axcall, feed script_lines to stdin (one per line, CRLF-terminated),
        collect stdout until the process exits or connect_timeout_sec elapses.
        Returns (completed_cleanly, combined_output).
        """
        args = [self._axcall_path, "-W"]
        if self._mycall:
            args += ["-s", self._mycall]
        args += [self._ax25_port, self._bbs_callsign]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            script = "".join(f"{line}\r\n" for line in script_lines).encode("utf-8", errors="replace")
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(input=script), timeout=self._connect_timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False, "(session timed out)"
            output = out.decode("utf-8", errors="replace") if out else ""
            return proc.returncode == 0, output
        finally:
            if proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass

    # ── Overrides ─────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        return {
            "ax25_port": self._ax25_port,
            "bbs_callsign": self._bbs_callsign,
            "poll_count": self._poll_count,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "last_session_ok": self._last_session_ok,
            "last_session_error": self._last_session_error or None,
            "seen_messages": len(self._seen_msg_ids),
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
        }
