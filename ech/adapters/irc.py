"""
ech/adapters/irc.py
-------------------
IRC (Internet Relay Chat) text adapter — O92.

Plain asyncio client, no extra packages. Joins one or more channels on a
single server; channel messages and private messages (to ECH's nick) come
in as normal feed messages, and anything ECH sends goes out as PRIVMSG.

Protocol (RFC 1459 / RFC 2812, plus the IRCv3 SASL PLAIN extension):
  Register:  [PASS <password>]  NICK <nick>  USER <user> 0 * :<realname>
             With sasl_password set: CAP REQ :sasl, AUTHENTICATE PLAIN,
             AUTHENTICATE base64("\\0nick\\0password"), wait 903, CAP END.
  001        RPL_WELCOME — registered; JOIN the configured channels now
  433        ERR_NICKNAMEINUSE — retry with "_" appended (up to 3 times)
  PING :x    must be answered with PONG :x or the server drops us
  PRIVMSG <target> :<text>   target is "#chan" (channel) or our nick (DM)
  Lines are capped at 512 bytes including CRLF, so outbound text is split.

Config keys:
  name               str        adapter name (default: irc)
  server             str        IRC server hostname (REQUIRED), e.g. irc.libera.chat
  port               int        default 6697 with TLS, 6667 without
  tls                bool       use TLS (default: true)
  tls_verify         bool       verify the server certificate (default: true)
  nick               str        nickname (REQUIRED) — use your callsign, e.g. KN0O-ECH
  username           str        ident/username (default: nick)
  realname           str        "real name" field (default: "SignalMatrix")
  password           str        server password (PASS) — live config only, never commit
  sasl_password      str        SASL PLAIN account password — live config only, never commit
  nickserv_password  str        fallback: /msg NickServ IDENTIFY after connect — never commit
  channels           list[str]  channels to join, e.g. ["#ech-test"] (REQUIRED, at least one)
  default_channel    str        where untargeted sends go (default: first of channels)
  send_interval      float      seconds between outbound lines — flood protection (default: 1.0)

Sending:
  message.to_id "#chan"   -> that channel
  message.to_id "nick"    -> private message to that nick
  message.raw["channel_name"] (the UI's channel picker) -> that channel
  otherwise               -> default_channel
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import ssl
import time

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

EMRG_WORDS = {"emergency", "mayday", "sos", "evacuate now"}
ELVT_WORDS = {"urgent", "priority", "immediate"}

MAX_LINE = 512           # RFC 2812 limit, including the trailing CRLF
REGISTER_TIMEOUT = 30.0  # seconds to reach 001 (welcome) after connecting
PING_IDLE = 120.0        # send our own PING if the server has been quiet this long
LINK_TIMEOUT = 300.0     # give up on the link if nothing at all arrives for this long

# mIRC colour/bold/italic/underline/reverse/reset control codes
_FORMAT_RE = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x11\x16\x1d\x1e\x1f]")
_CHAN_PREFIXES = "#&+!"


def _priority(text: str) -> Priority:
    lower = text.lower()
    if any(w in lower for w in EMRG_WORDS):
        return Priority.EMERGENCY
    if any(w in lower for w in ELVT_WORDS):
        return Priority.ELEVATED
    return Priority.NORMAL


def strip_formatting(text: str) -> str:
    return _FORMAT_RE.sub("", text)


def parse_line(line: str) -> tuple[dict, str, str, list[str]]:
    """Split one IRC line into (tags, prefix, command, params).
    The trailing ":param" (which may contain spaces) is the last param."""
    tags: dict = {}
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for t in raw_tags.split(";"):
            k, _, v = t.partition("=")
            tags[k] = v
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    elif line.startswith(":"):
        trailing, line = line[1:], ""
    parts = line.split()
    command = parts[0].upper() if parts else ""
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return tags, prefix, command, params


def split_text(target: str, text: str, nick: str) -> list[str]:
    """Break text into chunks that fit one PRIVMSG line once the server
    prefixes it with our full ":nick!user@host" when relaying it on."""
    # 63 bytes is the RFC's longest hostname; allow for ":nick!~user@host "
    overhead = len(f":{nick}!~{nick[:10]}@ PRIVMSG {target} :\r\n".encode()) + 63
    room = max(MAX_LINE - overhead, 64)
    chunks: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        para = para.strip(" ")
        while para:
            piece = para
            while len(piece.encode("utf-8")) > room:
                cut = piece.rfind(" ", 0, len(piece) - 1)
                piece = piece[:cut] if cut > room // 2 else piece[:-1]
            chunks.append(piece.rstrip(" "))
            para = para[len(piece):].lstrip(" ")
    return chunks


class IRCAdapter(Adapter):
    """Text messaging over a single IRC server connection."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = config.get("name", "irc")
        self._server = str(config.get("server", "")).strip()
        self._tls = bool(config.get("tls", True))
        self._tls_verify = bool(config.get("tls_verify", True))
        self._port = int(config.get("port") or (6697 if self._tls else 6667))
        self._want_nick = str(config.get("nick", "")).strip()
        self._username = str(config.get("username") or self._want_nick).strip()
        self._realname = str(config.get("realname") or "SignalMatrix")
        self._password = config.get("password") or ""
        self._sasl_password = config.get("sasl_password") or ""
        self._nickserv_password = config.get("nickserv_password") or ""
        chans = config.get("channels") or []
        if isinstance(chans, str):
            chans = [chans]
        self._channels = [c if c[:1] in _CHAN_PREFIXES else f"#{c}" for c in (s.strip() for s in chans) if c]
        self._default_channel = str(config.get("default_channel") or (self._channels[0] if self._channels else ""))
        self._send_interval = float(config.get("send_interval", 1.0))

        if not self._server or not self._want_nick:
            raise ValueError("IRCAdapter: 'server' and 'nick' are required in config")
        if not self._channels:
            raise ValueError("IRCAdapter: 'channels' needs at least one channel, e.g. [\"#ech-test\"]")

        self.tx_channel = self._default_channel
        self._nick = self._want_nick
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._run_task: asyncio.Task | None = None
        self._registered = asyncio.Event()
        self._sasl_done: asyncio.Event | None = None
        self._joined: set[str] = set()
        self._nick_retries = 0
        self._last_rx_line = 0.0
        self._last_tx_line = 0.0
        self._send_lock = asyncio.Lock()
        self._rx_count = 0
        self._tx_count = 0
        self._last_error: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        ctx = None
        if self._tls:
            ctx = ssl.create_default_context()
            if not self._tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        self._registered.clear()
        self._joined.clear()
        self._nick = self._want_nick
        self._nick_retries = 0
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._server, self._port, ssl=ctx), timeout=20.0)
        self._last_rx_line = time.monotonic()

        if self._sasl_password:
            self._sasl_done = asyncio.Event()
            await self._raw("CAP REQ :sasl")
        if self._password:
            await self._raw(f"PASS {self._password}", log_as="PASS ***")
        await self._raw(f"NICK {self._nick}")
        await self._raw(f"USER {self._username} 0 * :{self._realname}")

        # Read until 001 inline: connect() must fail loudly (router retries
        # with back-off) rather than report a link that never registered.
        try:
            await asyncio.wait_for(self._read_until_registered(), timeout=REGISTER_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError) as exc:
            self._close_transport()
            raise ConnectionError(
                f"IRC {self.name}: not registered on {self._server}:{self._port} within "
                f"{REGISTER_TIMEOUT:.0f}s — {self._last_error or exc or 'no welcome from server'}"
            ) from exc

        if self._nickserv_password and not self._sasl_password:
            await self._raw(f"PRIVMSG NickServ :IDENTIFY {self._nickserv_password}",
                            log_as="PRIVMSG NickServ :IDENTIFY ***")
        await self._raw(f"JOIN {','.join(self._channels)}")
        self._connected = True
        self._run_task = asyncio.create_task(self._run(), name=f"{self.name}-run")
        log.info("IRC %s: registered on %s:%d as %s, joining %s",
                 self.name, self._server, self._port, self._nick, ", ".join(self._channels))

    async def disconnect(self) -> None:
        self._connected = False
        if self._run_task and self._run_task is not asyncio.current_task():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        self._run_task = None
        if self._writer and not self._writer.is_closing():
            try:
                self._writer.write(b"QUIT :SignalMatrix signing off\r\n")
                await asyncio.wait_for(self._writer.drain(), timeout=2.0)
            except Exception:
                pass
        self._close_transport()
        log.info("IRC %s: disconnected", self.name)

    def _close_transport(self) -> None:
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = self._writer = None

    # ── Send ──────────────────────────────────────────────────────────────

    async def send(self, message: NormalizedMessage) -> bool:
        if not self._connected or not self._writer:
            return False
        target = (message.to_id or "").strip()
        if not target:
            hint = str((message.raw or {}).get("channel_name") or "").strip()
            target = (hint if hint[:1] in _CHAN_PREFIXES else f"#{hint}") if hint else self._default_channel
        if not target or any(c in target for c in " ,\r\n\x07"):
            log.error("IRC %s: bad send target %r", self.name, target)
            return False
        body = strip_formatting(message.body or "").strip()
        if not body:
            return False
        try:
            for chunk in split_text(target, body, self._nick):
                await self._raw(f"PRIVMSG {target} :{chunk}", paced=True)
        except (ConnectionError, OSError) as exc:
            self._last_error = f"send failed: {exc}"
            log.error("IRC %s: %s", self.name, self._last_error)
            self._connected = False
            return False
        message.source_channel = target if target[:1] in _CHAN_PREFIXES else "DM"
        self._tx_count += 1
        self._mark_tx(message)
        return True

    async def _raw(self, line: str, paced: bool = False, log_as: str | None = None) -> None:
        if not self._writer:
            raise ConnectionError("not connected")
        line = line.replace("\r", " ").replace("\n", " ")
        async with self._send_lock:
            if paced:
                wait = self._last_tx_line + self._send_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._writer.write(line.encode("utf-8")[:MAX_LINE - 2] + b"\r\n")
            await self._writer.drain()
            self._last_tx_line = time.monotonic()
        log.debug("IRC %s >> %s", self.name, log_as or line)

    # ── Receive ───────────────────────────────────────────────────────────

    async def _readline(self, timeout: float) -> str:
        data = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        if not data:
            raise ConnectionError("server closed the connection")
        self._last_rx_line = time.monotonic()
        return data.decode("utf-8", errors="replace").rstrip("\r\n")

    async def _read_until_registered(self) -> None:
        while not self._registered.is_set():
            await self._handle_line(await self._readline(REGISTER_TIMEOUT))

    async def _run(self) -> None:
        try:
            while self._connected and self._reader:
                try:
                    line = await self._readline(PING_IDLE)
                except asyncio.TimeoutError:
                    if time.monotonic() - self._last_rx_line > LINK_TIMEOUT:
                        raise ConnectionError(f"nothing from server in {LINK_TIMEOUT:.0f}s")
                    await self._raw(f"PING :{self._server}")
                    continue
                await self._handle_line(line)
        except asyncio.CancelledError:
            pass
        except (ConnectionError, OSError) as exc:
            self._last_error = str(exc)
            log.warning("IRC %s: link lost — %s", self.name, exc)
            self._connected = False

    async def _handle_line(self, line: str) -> None:
        if not line:
            return
        _tags, prefix, cmd, params = parse_line(line)
        sender = prefix.split("!", 1)[0]

        if cmd == "PING":
            await self._raw(f"PONG :{params[-1] if params else self._server}")
        elif cmd == "001":
            if params:
                self._nick = params[0]
            self._registered.set()
        elif cmd == "433" and not self._registered.is_set():
            self._nick_retries += 1
            if self._nick_retries > 3:
                self._last_error = f"nick {self._want_nick} and 3 alternates are in use"
                raise ConnectionError(self._last_error)
            self._nick = f"{self._want_nick}{'_' * self._nick_retries}"
            await self._raw(f"NICK {self._nick}")
        elif cmd == "CAP" and len(params) >= 3 and params[1].upper() in ("ACK", "NAK"):
            if params[1].upper() == "ACK" and "sasl" in params[-1].lower().split():
                await self._raw("AUTHENTICATE PLAIN")
            else:
                self._last_error = "server does not offer SASL"
                await self._raw("CAP END")
        elif cmd == "AUTHENTICATE" and params and params[0] == "+":
            token = base64.b64encode(
                f"\0{self._want_nick}\0{self._sasl_password}".encode()).decode()
            await self._raw(f"AUTHENTICATE {token}", log_as="AUTHENTICATE ***")
        elif cmd in ("903", "904", "905", "906", "907"):
            if cmd != "903":
                self._last_error = f"SASL login failed ({cmd}: {params[-1] if params else ''})"
                log.warning("IRC %s: %s", self.name, self._last_error)
            await self._raw("CAP END")
        elif cmd == "JOIN" and sender == self._nick and params:
            self._joined.add(params[0].lower())
            log.info("IRC %s: joined %s", self.name, params[0])
        elif cmd in ("PART", "KICK") and params:
            who = params[1] if cmd == "KICK" and len(params) > 1 else sender
            if who == self._nick:
                self._joined.discard(params[0].lower())
                log.warning("IRC %s: left %s (%s)", self.name, params[0], cmd)
        elif cmd in ("471", "473", "474", "475", "477") and len(params) >= 2:
            self._last_error = f"cannot join {params[1]}: {params[-1]}"
            log.warning("IRC %s: %s", self.name, self._last_error)
        elif cmd == "NICK" and sender == self._nick and params:
            self._nick = params[0]
        elif cmd == "ERROR":
            self._last_error = params[-1] if params else "server ERROR"
            raise ConnectionError(self._last_error)
        elif cmd in ("PRIVMSG", "NOTICE") and len(params) >= 2 and sender:
            await self._on_message(cmd, sender, prefix, params[0], params[1])

    async def _on_message(self, cmd: str, sender: str, prefix: str, target: str, text: str) -> None:
        if "." in sender and "!" not in prefix:
            return   # server notices ("*** Looking up your hostname")
        if text.startswith("\x01"):
            ctcp = text.strip("\x01")
            if not ctcp.upper().startswith("ACTION "):
                if cmd == "PRIVMSG" and ctcp.upper() == "VERSION":
                    await self._raw(f"NOTICE {sender} :\x01VERSION SignalMatrix ECH IRC adapter\x01")
                return
            text = f"* {sender} {ctcp[7:]}"
        text = strip_formatting(text).strip()
        if not text:
            return
        is_channel = target[:1] in _CHAN_PREFIXES
        # Network services (NickServ, ChanServ, SaslServ "Last login from" ...) are
        # account chatter, not traffic — log them, keep them out of the feed.
        if cmd == "NOTICE" and not is_channel and (
                sender.lower().endswith("serv") or prefix.split("@", 1)[-1].startswith("services.")):
            log.info("IRC %s: %s: %s", self.name, sender, text)
            return
        self._rx_count += 1
        await self._enqueue(NormalizedMessage(
            source_adapter=self.name,
            source_channel=target if is_channel else "DM",
            from_id=sender,
            from_display=sender,
            to_id=None if is_channel else self._nick,
            body=text,
            priority=_priority(text),
            raw={"channel_name": target if is_channel else None, "irc_command": cmd,
                 "prefix": prefix},
        ))

    # ── Health ────────────────────────────────────────────────────────────

    def _health_detail(self) -> dict:
        return {
            "server": f"{self._server}:{self._port}",
            "tls": self._tls,
            "nick": self._nick,
            "channels": self._channels,
            "default_channel": self._default_channel,
            "joined": sorted(self._joined),
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "last_error": self._last_error,
        }
