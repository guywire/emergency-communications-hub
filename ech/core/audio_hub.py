"""Live audio for the web UI (O191): listen to radio/PBX sources and a browser phone.

Everything is 8 kHz, 16-bit, mono PCM (what Asterisk and Codec2 both speak):

* AudioHub - named sources ("allstar", "m17:<adapter>") with subscriber queues.
  A source's start() runs when its first listener arrives and stop() when the
  last one leaves, so nothing is decoded or bridged while nobody listens.
* AudioSocketServer - a loopback TCP server for Asterisk's AudioSocket()
  (https://docs.asterisk.org/Configuration/Channel-Driver/AudioSocket/):
  frames are type(1) + length(2, big-endian) + payload; 0x01 UUID, 0x10 audio,
  0x03 DTMF, 0x00 hangup, 0xff error.
* AllStar listening - an AMI Originate of Local/allstar@ech-audio (which runs
  Rpt(<node>,P), phone mode, never keyed) whose other leg runs AudioSocket
  back to us.
* WebPhoneManager - browser phone on a PBX extension: outbound calls are AMI
  Originates of Local/<number>@from-internal into AudioSocket; inbound calls
  reach the extension's dialplan, which raises UserEvent(ECHWebPhone) with the
  caller ID and then AudioSocket()s here, where the call rings every browser
  that is "available". Ringback is generated while it rings.
* Codec2Decoder - libcodec2 via ctypes for M17 voice (Codec2 3200 / 1600).
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import math
import re
import struct
import time
import uuid as uuidlib
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

RATE = 8000
FRAME_BYTES = 320            # 20 ms of 8 kHz s16le
KIND_HANGUP, KIND_UUID, KIND_DTMF, KIND_AUDIO, KIND_ERROR = 0x00, 0x01, 0x03, 0x10, 0xFF


# ── Hub ───────────────────────────────────────────────────────────────────

class _Source:
    def __init__(self, key: str, label: str, kind: str,
                 start: Callable[[], Awaitable[None]] | None = None,
                 stop: Callable[[], Awaitable[None]] | None = None, tx=None):
        self.key, self.label, self.kind = key, label, kind
        self.start, self.stop = start, stop
        # tx: an object with can_transmit(), voice_start(owner, dst) -> error | None,
        # voice_push(owner, pcm), voice_end(owner) (the M17 adapter); None = receive only
        self.tx = tx
        self.subs: set[asyncio.Queue] = set()
        self.talker = ""
        self.talker_at = 0.0


class AudioHub:
    def __init__(self):
        self.sources: dict[str, _Source] = {}

    def register(self, key: str, label: str, kind: str, start=None, stop=None, tx=None) -> None:
        old = self.sources.get(key)
        src = _Source(key, label, kind, start, stop, tx)
        if old:
            src.subs = old.subs
        self.sources[key] = src

    def unregister(self, key: str) -> None:
        self.sources.pop(key, None)

    def listing(self) -> list[dict]:
        now = time.time()
        return [{"key": s.key, "label": s.label, "kind": s.kind, "listeners": len(s.subs),
                 "talker": s.talker if now - s.talker_at < 3 else "",
                 "tx": bool(s.tx and s.tx.can_transmit())}
                for s in self.sources.values()]

    def set_talker(self, key: str, name: str) -> None:
        s = self.sources.get(key)
        if s:
            s.talker, s.talker_at = name, time.time()

    def talker(self, key: str, window: float = 2.0) -> str:
        s = self.sources.get(key)
        return s.talker if s and time.time() - s.talker_at < window else ""

    def tx_for(self, key: str):
        s = self.sources.get(key)
        return s.tx if s and s.tx and s.tx.can_transmit() else None

    def has_listeners(self, key: str) -> bool:
        s = self.sources.get(key)
        return bool(s and s.subs)

    def publish(self, key: str, pcm: bytes, talker: str | None = None) -> None:
        s = self.sources.get(key)
        if not s or not s.subs:
            return
        if talker is not None:
            s.talker, s.talker_at = talker, time.time()
        for q in list(s.subs):
            if q.full():
                try:
                    q.get_nowait()          # drop the oldest: a slow browser must not grow memory
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(pcm)

    async def subscribe(self, key: str) -> asyncio.Queue:
        s = self.sources.get(key)
        if not s:
            raise KeyError(key)
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        s.subs.add(q)
        if len(s.subs) == 1 and s.start:
            try:
                await s.start()
            except Exception as e:
                log.warning("audio source %s failed to start: %s", key, e)
        return q

    async def unsubscribe(self, key: str, q: asyncio.Queue) -> None:
        s = self.sources.get(key)
        if not s or q not in s.subs:
            return
        s.subs.discard(q)
        if not s.subs and s.stop:
            try:
                await s.stop()
            except Exception as e:
                log.warning("audio source %s failed to stop: %s", key, e)


hub = AudioHub()


# ── AudioSocket ───────────────────────────────────────────────────────────

class AudioSocketSession:
    """One Asterisk channel connected to us."""

    def __init__(self, uid: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.uid, self.reader, self.writer = uid, reader, writer
        self.closed = False
        self.on_audio: Callable[[bytes], None] | None = None
        self.on_dtmf: Callable[[str], None] | None = None
        self.on_close: Callable[[], None] | None = None
        self.last_tx = time.monotonic()

    KEEPALIVE_S = 0.5   # Asterisk 22's AudioSocket() hangs up after 2 s with no audio either way

    def send_audio(self, pcm: bytes) -> None:
        if self.closed or not pcm:
            return
        self.last_tx = time.monotonic()
        for i in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[i:i + FRAME_BYTES]
            self.writer.write(bytes([KIND_AUDIO]) + struct.pack(">H", len(chunk)) + chunk)

    async def hangup(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.write(bytes([KIND_HANGUP, 0, 0]))
            await self.writer.drain()
            self.writer.close()
        except Exception:
            pass

    async def _keepalive(self) -> None:
        """Send 20 ms of silence whenever we have been quiet for KEEPALIVE_S."""
        # No drain() here: several tasks write this socket, and concurrent drain()
        # raises on Python 3.11 (it ended this loop and the call dropped 2 s later).
        while not self.closed:
            await asyncio.sleep(self.KEEPALIVE_S / 2)
            if not self.closed and time.monotonic() - self.last_tx >= self.KEEPALIVE_S:
                try:
                    self.send_audio(bytes(FRAME_BYTES))
                except Exception as e:
                    log.debug("AudioSocket %s keepalive: %s", self.uid[:8], e)

    async def run(self) -> None:
        self.last_tx = time.monotonic()
        ka = asyncio.ensure_future(self._keepalive())
        try:
            while True:
                head = await self.reader.readexactly(3)
                kind, length = head[0], struct.unpack(">H", head[1:])[0]
                body = await self.reader.readexactly(length) if length else b""
                if kind == KIND_AUDIO and self.on_audio:
                    self.on_audio(body)
                elif kind == KIND_DTMF and self.on_dtmf and body:
                    self.on_dtmf(body[:1].decode("ascii", "replace"))
                elif kind in (KIND_HANGUP, KIND_ERROR):
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed = True
            ka.cancel()
            try:
                self.writer.close()
            except Exception:
                pass
            if self.on_close:
                self.on_close()


class AudioSocketServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 9093):
        self.host, self.port = host, port
        self.pending: dict[str, Callable[[AudioSocketSession], None]] = {}
        self.inbound: Callable[[AudioSocketSession], None] | None = None
        self.listen_requests: dict[str, tuple[str, str]] = {}   # uuid -> (hub source, phone channel)
        self._server: asyncio.base_events.Server | None = None
        self.get_pbx: Callable | None = None        # for the phone "who is talking" display

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("AudioSocket server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()

    def note_listen(self, uid: str, source: str, channel: str = "") -> None:
        self.listen_requests[uid] = (source, channel)
        while len(self.listen_requests) > 50:
            self.listen_requests.pop(next(iter(self.listen_requests)))

    async def _serve_listen(self, s: "AudioSocketSession", source: str, channel: str = "") -> None:
        """A desk phone dialed a live-audio extension: play a hub source into the call."""
        key = source if source in hub.sources else None
        if key is None and "#" in source:                    # "m17#1" = second M17 source by name
            kind, _, idx = source.partition("#")
            keys = sorted(k for k in hub.sources if k.split(":")[0] == kind)
            key = keys[int(idx)] if idx.isdigit() and int(idx) < len(keys) else None
        elif key is None:
            key = next((k for k in sorted(hub.sources) if k.split(":")[0] == source), None)
        log.info("AudioSocket %s: phone listening to %s", s.uid[:8], key or f"{source} (no such source)")
        if not key:
            await s.hangup()
            return
        q = await hub.subscribe(key)
        keyer = PhoneKeyer(s, key)

        async def pump():
            while not s.closed:
                try:
                    pcm = await q.get()
                    if not keyer.keyed:            # no receive audio over your own transmission
                        s.send_audio(pcm)
                except Exception:
                    break
        async def show_talker():
            """Who is talking, as the call's name on the phone screen (connected-line update)."""
            pbx = self.get_pbx() if self.get_pbx else None
            if not channel or not pbx or not hasattr(pbx, "set_connected_name"):
                return
            idle = "M17 (quiet)" if key.startswith("m17") else "AllStar (quiet)"
            kind = "M17" if key.startswith("m17") else "AllStar"
            shown = None
            while not s.closed:
                who = hub.talker(key, 2.5)
                want = "You are transmitting" if keyer.keyed else (f"{kind}: {who}" if who else idle)
                if want != shown:
                    shown = want
                    try:
                        await pbx.set_connected_name(channel, want)
                    except Exception as e:
                        log.debug("talker display: %s", e)
                await asyncio.sleep(0.5)
        task = asyncio.ensure_future(pump())
        shower = asyncio.ensure_future(show_talker())
        try:
            await s.run()
        finally:
            task.cancel()
            shower.cancel()
            keyer.release()
            await hub.unsubscribe(key, q)

    def expect(self, uid: str, handler: Callable[[AudioSocketSession], None]) -> None:
        self.pending[uid] = handler

    def service(self) -> str:
        return f"{self.host}:{self.port}"

    async def _handle(self, reader, writer) -> None:
        try:
            head = await asyncio.wait_for(reader.readexactly(3), 10)
            if head[0] != KIND_UUID:
                writer.close()
                return
            raw = await reader.readexactly(struct.unpack(">H", head[1:])[0])
            uid = str(uuidlib.UUID(bytes=raw[:16])) if len(raw) >= 16 else raw.decode("ascii", "replace")
        except Exception:
            writer.close()
            return
        s = AudioSocketSession(uid, reader, writer)
        req = self.listen_requests.pop(uid, None)
        if req is not None:
            await self._serve_listen(s, *req)
            return
        handler = self.pending.pop(uid, None) or self.inbound
        if not handler:
            await s.hangup()
            return
        handler(s)
        await s.run()


def tone(freq: float, seconds: float, level: float = 0.25) -> bytes:
    n = int(seconds * RATE)
    return b"".join(struct.pack("<h", int(level * 32767 * math.sin(2 * math.pi * freq * i / RATE)))
                    for i in range(n))


class PhoneKeyer:
    """Desk-phone push-to-talk on a live-audio call, the same keys as AllStar
    phone mode: *99 keys up, # unkeys. Needs a source with a transmitter (M17);
    on a receive-only source *99 gets a low "no" tone."""

    def __init__(self, s: AudioSocketSession, key: str):
        self.s, self.key = s, key
        self.digits = ""
        self.keyed = False
        s.on_dtmf = self.on_dtmf
        s.on_audio = self.on_audio

    def on_dtmf(self, d: str) -> None:
        if d == "#":
            self.digits = ""
            if self.keyed:
                self.release()
                self.beep(tone(660, 0.12) + bytes(400) + tone(440, 0.12))
            return
        self.digits = (self.digits + d)[-3:]
        if self.digits == "*99" and not self.keyed:
            self.digits = ""
            tx = hub.tx_for(self.key)
            err = tx.voice_start(self.s.uid, "") if tx else "this source is receive-only"
            if err:
                log.info("AudioSocket %s: PTT refused on %s: %s", self.s.uid[:8], self.key, err)
                self.beep(tone(300, 0.5))
                return
            self.keyed = True
            log.info("AudioSocket %s: phone keyed up on %s", self.s.uid[:8], self.key)
            self.beep(tone(1000, 0.15))      # go-ahead beep: start talking after it

    def on_audio(self, pcm: bytes) -> None:
        if self.keyed:
            tx = hub.tx_for(self.key)
            if not tx or tx.voice_push(self.s.uid, pcm) is False:
                self.keyed = False                    # timed out / link dropped
                self.beep(tone(300, 0.5))

    def beep(self, pcm: bytes) -> None:
        """Play a prompt tone at real-time pace (a burst of frames comes out garbled)."""
        async def go():
            for i in range(0, len(pcm), FRAME_BYTES):
                if self.s.closed:
                    return
                self.s.send_audio(pcm[i:i + FRAME_BYTES])
                await asyncio.sleep(0.02)
        asyncio.ensure_future(go())

    def release(self) -> None:
        if self.keyed:
            self.keyed = False
            tx = hub.tx_for(self.key)
            if tx:
                tx.voice_end(self.s.uid)
            log.info("AudioSocket %s: phone unkeyed on %s", self.s.uid[:8], self.key)


# ── AllStar listening ─────────────────────────────────────────────────────

def talker_from_xnode(out: str, node: str, name_for=None) -> str:
    """'rpt xnode' output -> who is talking: the keyed linked node(s), or a local
    phone user keyed on our own node (RPT_RXKEYED=1). '' = nobody."""
    vals = dict(l.strip().split("=", 1) for l in (out or "").splitlines() if "=" in l)
    keyed = []
    for item in (vals.get("RPT_ALINKS") or "").split(",")[1:]:
        m = re.match(r"^\s*([0-9]{3,7})[A-Z]K\s*$", item)
        if m:
            n = m.group(1)
            extra = (name_for(n) if name_for else "") or ""
            keyed.append(f"{n} {extra}".strip())
    if keyed:
        return ", ".join(keyed)[:60]
    if vals.get("RPT_RXKEYED") == "1":
        return f"local user on {node}"
    return ""

class AllStarListener:
    """Hub source 'allstar': bridges node audio in while anyone listens."""

    def __init__(self, server: AudioSocketServer, get_pbx: Callable, node: str):
        self.server, self.get_pbx, self.node = server, get_pbx, str(node)
        self.session: AudioSocketSession | None = None

    def register(self) -> None:
        hub.register("allstar", f"AllStar node {self.node}", "allstar", self.start, self.stop)

    POLL_S = 1.0
    name_for: Callable[[str], str] | None = None      # node -> "W1PBR Hope, Maine" (directory)

    async def _watch_keyed(self) -> None:
        """While someone listens: which linked node is keyed (rpt xnode RPT_ALINKS,
        e.g. "2,620860TK,521771TU": K = keyed), shown as the talker."""
        while self.session and not self.session.closed:
            pbx = self.get_pbx()
            try:
                out = await pbx.ami_command(f"rpt xnode {self.node}") if pbx and hasattr(pbx, "ami_command") else ""
                who = talker_from_xnode(out, self.node, self.name_for)
                if who:
                    hub.set_talker("allstar", who)
            except Exception as e:
                log.debug("AllStar talker poll: %s", e)
            await asyncio.sleep(self.POLL_S)

    async def start(self) -> None:
        pbx = self.get_pbx()
        if not pbx or not getattr(pbx, "_connected", False):
            raise RuntimeError("PBX not connected")
        uid = str(uuidlib.uuid4())

        def attach(s: AudioSocketSession):
            self.session = s
            s.on_audio = lambda pcm: hub.publish("allstar", pcm)
            s.on_close = lambda: setattr(self, "session", None)
            asyncio.ensure_future(self._watch_keyed())
        self.server.expect(uid, attach)
        await pbx.originate_app("Local/allstar@ech-audio", "AudioSocket", f"{uid},{self.server.service()}",
                                caller_id=f"ECH listen <{self.node}>")

    async def stop(self) -> None:
        if self.session:
            await self.session.hangup()
            self.session = None


# ── Web phone ─────────────────────────────────────────────────────────────

def ringback(seconds: float, phase: float = 0.0) -> bytes:
    """North American ringback (440+480 Hz, 2 s on / 4 s off) starting at `phase` seconds."""
    out = bytearray()
    n = int(seconds * RATE)
    for i in range(n):
        t = phase + i / RATE
        on = (t % 6.0) < 2.0
        v = 0.0
        if on:
            v = 0.18 * (math.sin(2 * math.pi * 440 * t) + math.sin(2 * math.pi * 480 * t))
        out += struct.pack("<h", int(v * 32767 * 0.5))
    return bytes(out)


class WebPhoneCall:
    def __init__(self, cid: str, direction: str, peer: str):
        self.id, self.direction, self.peer = cid, direction, peer
        self.state = "ringing" if direction == "in" else "calling"
        self.session: AudioSocketSession | None = None
        self.client = None                     # the browser handling it
        self.started = time.time()
        self.chan_id = ""                      # uniqueid of our Local ;1 leg (outbound), for DTMF


class WebPhoneManager:
    """Browser phone on extension `ext`. Clients are objects with
    `async send_json(dict)` and `send_audio(bytes)` (see app.py)."""

    RING_SECONDS = 25
    CALL_SETUP_SECONDS = 60

    def __init__(self, server: AudioSocketServer, get_pbx: Callable, ext: str = "111"):
        self.server, self.get_pbx, self.ext = server, get_pbx, str(ext)
        self.clients: set = set()          # browsers that are "available"
        self.calls: dict[str, WebPhoneCall] = {}
        self.caller_ids: dict[str, str] = {}   # inbound uuid -> caller ID (from UserEvent)
        server.inbound = self._inbound

    # presence -> Asterisk DB key the dialplan checks before ringing us
    async def _presence(self) -> None:
        pbx = self.get_pbx()
        if pbx and getattr(pbx, "_connected", False):
            await pbx.db_put("ech", "webphone", "1" if self.clients else "0")

    async def set_available(self, client, on: bool) -> None:
        (self.clients.add if on else self.clients.discard)(client)
        await self._presence()

    async def drop_client(self, client) -> None:
        self.clients.discard(client)
        for c in list(self.calls.values()):
            if c.client is client:
                await self.hangup(c.id)
        await self._presence()

    def note_caller(self, uid: str, caller: str) -> None:
        self.caller_ids[uid] = caller.strip(" <>") and caller or "unknown caller"
        if len(self.caller_ids) > 50:
            self.caller_ids.pop(next(iter(self.caller_ids)))

    async def call(self, client, number: str) -> WebPhoneCall:
        pbx = self.get_pbx()
        if not pbx or not getattr(pbx, "_connected", False):
            raise RuntimeError("PBX not connected")
        uid = str(uuidlib.uuid4())
        c = WebPhoneCall(uid, "out", number)
        c.client = client
        self.calls[uid] = c
        self.server.expect(uid, lambda s: self._connected(c, s))
        c.chan_id = f"echwp-{uid}"
        await pbx.originate_app(f"Local/{number}@from-internal", "AudioSocket", f"{uid},{self.server.service()}",
                                caller_id=f"Web phone <{self.ext}>", timeout_ms=self.CALL_SETUP_SECONDS * 1000,
                                channel_id=c.chan_id)
        asyncio.ensure_future(self._setup_timeout(c))
        return c

    async def _setup_timeout(self, c: WebPhoneCall) -> None:
        await asyncio.sleep(self.CALL_SETUP_SECONDS + 2)
        if c.state == "calling":
            self.server.pending.pop(c.id, None)
            await self._end(c, "no answer")

    def _connected(self, c: WebPhoneCall, s: AudioSocketSession) -> None:
        c.session, c.state = s, "connected"
        s.on_audio = lambda pcm: c.client and c.client.send_audio(pcm)
        s.on_close = lambda: asyncio.ensure_future(self._end(c, "ended"))
        if c.client:
            asyncio.ensure_future(c.client.send_json({"type": "call", "id": c.id, "state": "connected", "peer": c.peer}))

    def _inbound(self, s: AudioSocketSession) -> None:
        c = WebPhoneCall(s.uid, "in", self.caller_ids.pop(s.uid, "unknown caller"))
        c.session = s
        self.calls[c.id] = c
        s.on_close = lambda: asyncio.ensure_future(self._end(c, "caller hung up"))
        asyncio.ensure_future(self._ring(c))

    async def _ring(self, c: WebPhoneCall) -> None:
        for cl in list(self.clients):
            await cl.send_json({"type": "ring", "id": c.id, "from": c.peer})
        t0, phase = time.time(), 0.0
        while c.state == "ringing" and time.time() - t0 < self.RING_SECONDS and not c.session.closed:
            c.session.send_audio(ringback(0.2, phase))
            phase += 0.2
            await asyncio.sleep(0.2)
        if c.state == "ringing":
            await self._end(c, "not answered")

    async def answer(self, client, cid: str) -> bool:
        c = self.calls.get(cid)
        if not c or c.state != "ringing":
            return False
        c.state, c.client = "connected", client
        c.session.on_audio = lambda pcm: c.client and c.client.send_audio(pcm)
        for cl in list(self.clients):
            if cl is not client:
                await cl.send_json({"type": "call", "id": cid, "state": "taken"})
        await client.send_json({"type": "call", "id": cid, "state": "connected", "peer": c.peer})
        return True

    DTMF_GAP_S = 0.15

    async def dtmf(self, client, cid: str, digits: str) -> bool:
        """Key presses into a connected outbound call (IVRs, AllStar *99 / #).
        Inbound calls have no addressable channel here, so they can't."""
        c = self.calls.get(cid)
        pbx = self.get_pbx()
        if (not c or c.client is not client or c.state != "connected" or not c.chan_id
                or not pbx or not hasattr(pbx, "play_dtmf")):
            return False
        for d in digits[:8]:
            if d in "0123456789*#ABCD":
                await pbx.play_dtmf(c.chan_id, d)
                await asyncio.sleep(self.DTMF_GAP_S)
        return True

    def mic(self, client, pcm: bytes) -> None:
        for c in self.calls.values():
            if c.client is client and c.state == "connected" and c.session:
                c.session.send_audio(pcm)

    async def hangup(self, cid: str) -> None:
        c = self.calls.get(cid)
        if c:
            await self._end(c, "hung up")

    async def _end(self, c: WebPhoneCall, why: str) -> None:
        if self.calls.pop(c.id, None) is None:
            return
        prev = c.state
        c.state = "ended"
        if c.session:
            await c.session.hangup()
        targets = [c.client] if c.client else (list(self.clients) if prev == "ringing" else [])
        for cl in targets:
            try:
                await cl.send_json({"type": "call", "id": c.id, "state": "ended", "reason": why})
            except Exception:
                pass


# ── Codec2 (M17 voice) ────────────────────────────────────────────────────

class Codec2Decoder:
    MODE_3200, MODE_1600 = 0, 2
    _lib = None

    @classmethod
    def available(cls) -> bool:
        if cls._lib is None:
            name = ctypes.util.find_library("codec2") or "libcodec2.so.1.0"
            try:
                lib = ctypes.CDLL(name)
                lib.codec2_create.restype = ctypes.c_void_p
                lib.codec2_create.argtypes = [ctypes.c_int]
                lib.codec2_destroy.argtypes = [ctypes.c_void_p]
                lib.codec2_samples_per_frame.argtypes = [ctypes.c_void_p]
                lib.codec2_bytes_per_frame.argtypes = [ctypes.c_void_p]
                lib.codec2_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_short), ctypes.c_char_p]
                lib.codec2_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_short)]
                cls._lib = lib
            except OSError:
                cls._lib = False
        return bool(cls._lib)

    def __init__(self, mode: int):
        if not self.available():
            raise RuntimeError("libcodec2 not installed")
        self.mode = mode
        self._c2 = self._lib.codec2_create(mode)
        self.samples = self._lib.codec2_samples_per_frame(self._c2)
        self.nbytes = self._lib.codec2_bytes_per_frame(self._c2)
        self._buf = (ctypes.c_short * self.samples)()

    def decode(self, bits: bytes) -> bytes:
        """Decode as many whole Codec2 frames as `bits` holds; returns s16le PCM."""
        out = bytearray()
        for i in range(0, len(bits) - self.nbytes + 1, self.nbytes):
            self._lib.codec2_decode(self._c2, self._buf, bytes(bits[i:i + self.nbytes]))
            out += bytes(self._buf)
        return bytes(out)

    def close(self) -> None:
        if self._c2:
            self._lib.codec2_destroy(self._c2)
            self._c2 = None


class Codec2Encoder(Codec2Decoder):
    """s16le 8 kHz PCM -> Codec2 bits (M17 voice uses 3200: 160 samples -> 8 bytes)."""

    def encode(self, pcm: bytes) -> bytes:
        """Encode as many whole frames as `pcm` holds (callers pass exact multiples)."""
        out = bytearray()
        step = self.samples * 2
        bits = ctypes.create_string_buffer(self.nbytes)
        for i in range(0, len(pcm) - step + 1, step):
            ctypes.memmove(self._buf, pcm[i:i + step], step)
            self._lib.codec2_encode(self._c2, bits, self._buf)
            out += bits.raw
        return bytes(out)


def parse_m17_stream(data: bytes) -> dict | None:
    """M17-over-IP stream frame (54 bytes): 'M17 ' + stream id + LSF (dst, src,
    type, meta) + frame number + 16-byte payload + CRC."""
    if len(data) < 54 or data[:4] != b"M17 ":
        return None
    typ = struct.unpack(">H", data[18:20])[0]     # after magic(4) + stream id(2) + dst(6) + src(6)
    fn = struct.unpack(">H", data[34:36])[0]
    return {"sid": data[4:6].hex(), "dst": data[6:12], "src": data[12:18], "type": typ,
            "data_type": (typ >> 1) & 0x3,          # 2 = voice (3200), 3 = voice+data (1600)
            "fn": fn & 0x7FFF, "last": bool(fn & 0x8000), "payload": data[36:52]}


# ── Wiring ────────────────────────────────────────────────────────────────

state: dict = {"server": None, "webphone": None, "allstar": None, "options": {}}


async def setup(cfg: dict, router) -> None:
    """Start the AudioSocket server and register the PBX-backed sources (main.py)."""
    from ech.core.pbx_streams import audio_options
    opts = audio_options(cfg)
    state["options"] = opts
    if not opts["enabled"]:
        return

    def get_pbx():
        for a in router._adapters.values():
            if a.__class__.__name__ == "AsteriskAdapter":
                return a
        return None

    server = AudioSocketServer("127.0.0.1", opts["port"])
    server.get_pbx = get_pbx
    try:
        await server.start()
    except OSError as e:
        log.warning("AudioSocket server not started (%s): web listening/phone disabled", e)
        return
    state["server"] = server
    if opts["webphone_ext"]:
        wp = WebPhoneManager(server, get_pbx, opts["webphone_ext"])
        state["webphone"] = wp
        pbx = get_pbx()
        if pbx:
            pbx.on_webphone_caller = wp.note_caller
    pbx = get_pbx()
    if pbx:
        pbx.on_listen_request = server.note_listen
    if opts["allstar_node"]:
        al = AllStarListener(server, get_pbx, opts["allstar_node"])
        al.register()
        state["allstar"] = al
