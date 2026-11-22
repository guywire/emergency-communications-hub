"""Live audio core (O191): hub fan-out, AudioSocket protocol, web phone call flow, M17 parsing."""
import asyncio
import struct
import uuid

import pytest

from ech.core import audio_hub as ah


def _frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + struct.pack(">H", len(payload)) + payload


class FakePBX:
    """Stands in for the Asterisk adapter: records originates, can 'be' Asterisk."""
    _connected = True

    def __init__(self):
        self.originates = []
        self.db = {}

    async def originate_app(self, channel, app, data, caller_id="", timeout_ms=30000, channel_id=""):
        self.originates.append((channel, app, data, caller_id))
        self.channel_ids = getattr(self, "channel_ids", []) + [channel_id]
        return True

    async def play_dtmf(self, channel, digit, duration_ms=120):
        self.dtmf = getattr(self, "dtmf", []) + [(channel, digit)]
        return True

    async def db_put(self, fam, key, val):
        self.db[(fam, key)] = val


async def _asterisk_connect(port: int, uid: str):
    """Open an AudioSocket connection the way Asterisk does (UUID first)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(_frame(ah.KIND_UUID, uuid.UUID(uid).bytes))
    await w.drain()
    return r, w


async def _read_frame(r):
    head = await asyncio.wait_for(r.readexactly(3), 3)
    n = struct.unpack(">H", head[1:])[0]
    return head[0], (await r.readexactly(n) if n else b"")


async def _read_until(r, kind, skip_silence=True):
    """Next frame of `kind`, skipping keepalive silence (and other audio when waiting for hangup)."""
    for _ in range(500):
        k, body = await _read_frame(r)
        if k == kind and not (skip_silence and k == ah.KIND_AUDIO and not any(body)):
            return k, body
    raise AssertionError(f"no frame of kind {kind}")


class FakeClient:
    def __init__(self):
        self.json, self.audio = [], []

    async def send_json(self, obj):
        self.json.append(obj)

    def send_audio(self, pcm):
        self.audio.append(pcm)


@pytest.mark.asyncio
async def test_hub_starts_on_first_listener_and_stops_on_last():
    hub = ah.AudioHub()
    calls = []

    async def start():
        calls.append("start")

    async def stop():
        calls.append("stop")
    hub.register("x", "X", "test", start, stop)
    q1 = await hub.subscribe("x")
    q2 = await hub.subscribe("x")
    hub.publish("x", b"\x01\x00" * 160, talker="KN0O")
    assert (await q1.get()) == (await q2.get())
    assert hub.listing()[0]["talker"] == "KN0O" and hub.listing()[0]["listeners"] == 2
    await hub.unsubscribe("x", q1)
    assert calls == ["start"]
    await hub.unsubscribe("x", q2)
    assert calls == ["start", "stop"]
    hub.publish("x", b"\x00\x00")          # nobody listening: dropped, no error


@pytest.mark.asyncio
async def test_slow_listener_queue_drops_oldest():
    hub = ah.AudioHub()
    hub.register("x", "X", "test")
    q = await hub.subscribe("x")
    for i in range(150):
        hub.publish("x", bytes([i % 256, 0]))
    assert q.qsize() == 100
    assert (await q.get())[0] == 50


@pytest.mark.asyncio
async def test_allstar_listener_bridges_audio_socket_into_hub():
    server = ah.AudioSocketServer("127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    server.port = port
    pbx = FakePBX()
    al = ah.AllStarListener(server, lambda: pbx, "63018")
    al.register()
    try:
        q = await ah.hub.subscribe("allstar")
        chan, app, data, _ = pbx.originates[0]
        assert chan == "Local/allstar@ech-audio" and app == "AudioSocket" and data.endswith(f"127.0.0.1:{port}")
        uid = data.split(",")[0]
        r, w = await _asterisk_connect(port, uid)
        w.write(_frame(ah.KIND_AUDIO, b"\x10\x00" * 160))
        await w.drain()
        assert len(await asyncio.wait_for(q.get(), 3)) == 320
        await ah.hub.unsubscribe("allstar", q)      # last listener -> we hang the channel up
        kind, _ = await _read_until(r, ah.KIND_HANGUP)
        assert kind == ah.KIND_HANGUP
        w.close()
    finally:
        ah.hub.unregister("allstar")
        await server.stop()


@pytest.mark.asyncio
async def test_webphone_outbound_and_inbound_calls():
    server = ah.AudioSocketServer("127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    server.port = port
    pbx = FakePBX()
    wp = ah.WebPhoneManager(server, lambda: pbx, "111")
    wp.RING_SECONDS = 3
    a, b = FakeClient(), FakeClient()
    try:
        await wp.set_available(a, True)
        await wp.set_available(b, True)
        assert pbx.db[("ech", "webphone")] == "1"

        # outbound: browser a calls 101
        c = await wp.call(a, "101")
        chan, app, data, cid = pbx.originates[-1]
        assert chan == "Local/101@from-internal" and app == "AudioSocket" and cid == "Web phone <111>"
        r, w = await _asterisk_connect(port, data.split(",")[0])
        for _ in range(50):
            if c.state == "connected":
                break
            await asyncio.sleep(0.02)
        assert c.state == "connected" and {"type": "call", "id": c.id, "state": "connected", "peer": "101"} in a.json
        w.write(_frame(ah.KIND_AUDIO, b"\x22\x00" * 160))      # far end -> browser
        await w.drain()
        await asyncio.sleep(0.1)
        assert a.audio and len(a.audio[0]) == 320
        wp.mic(a, b"\x33\x00" * 160)                            # browser mic -> far end
        kind, body = await _read_until(r, ah.KIND_AUDIO)
        assert kind == ah.KIND_AUDIO and body == b"\x33\x00" * 160
        await wp.hangup(c.id)
        assert (await _read_until(r, ah.KIND_HANGUP))[0] == ah.KIND_HANGUP
        w.close()

        # inbound: caller ID arrives by UserEvent, then the AudioSocket connects
        uid = str(uuid.uuid4())
        wp.note_caller(uid, "KN0O <101>")
        r, w = await _asterisk_connect(port, uid)
        for _ in range(50):
            if any(j.get("type") == "ring" for j in b.json):
                break
            await asyncio.sleep(0.02)
        ring = next(j for j in b.json if j.get("type") == "ring")
        assert ring["from"] == "KN0O <101>"
        kind, _ = await _read_frame(r)                          # caller hears ringback while it rings
        assert kind == ah.KIND_AUDIO
        assert await wp.answer(b, uid)
        assert {"type": "call", "id": uid, "state": "taken"} in a.json
        w.close()

        # unanswered inbound call ends after RING_SECONDS
        uid2 = str(uuid.uuid4())
        r2, w2 = await _asterisk_connect(port, uid2)
        await asyncio.sleep(wp.RING_SECONDS + 0.6)
        assert uid2 not in wp.calls
        w2.close()

        await wp.drop_client(a)
        await wp.drop_client(b)
        assert pbx.db[("ech", "webphone")] == "0"
    finally:
        await server.stop()


def test_ringback_cadence():
    pcm = ah.ringback(3.0)
    assert len(pcm) == 3 * 8000 * 2
    on = struct.unpack("<1000h", pcm[:2000])
    off = struct.unpack("<1000h", pcm[2 * 8000 * 2 + 2000: 2 * 8000 * 2 + 4000])
    assert max(abs(x) for x in on) > 1000 and max(abs(x) for x in off) == 0


def test_parse_m17_stream_frame():
    from ech.adapters.m17_reflector import encode_callsign
    typ = (2 << 1) | 1                                       # stream, voice 3200
    frame = (b"M17 " + bytes([0xAB, 0xCD]) + bytes([255]) * 6 + encode_callsign("KN0O")
             + struct.pack(">H", typ) + bytes(14) + struct.pack(">H", 0x8005) + bytes(range(16)) + b"\x00\x00")
    f = ah.parse_m17_stream(frame)
    assert f["sid"] == "abcd" and f["data_type"] == 2 and f["fn"] == 5 and f["last"] is True
    assert f["payload"] == bytes(range(16))
    assert ah.parse_m17_stream(b"M17P" + bytes(50)) is None


@pytest.mark.skipif(not ah.Codec2Decoder.available(), reason="libcodec2 not installed")
def test_codec2_decodes_two_3200_frames():
    d = ah.Codec2Decoder(ah.Codec2Decoder.MODE_3200)
    pcm = d.decode(bytes(16))
    assert len(pcm) == 2 * 160 * 2
    d.close()


@pytest.mark.asyncio
async def test_desk_phone_listens_to_a_hub_source():
    server = ah.AudioSocketServer("127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    ah.hub.register("m17:test-ref", "M17 test", "m17")
    try:
        uid = str(uuid.uuid4())
        server.note_listen(uid, "m17")              # UserEvent(ECHListen, Source: m17)
        r, w = await _asterisk_connect(port, uid)
        for _ in range(50):
            if ah.hub.has_listeners("m17:test-ref"):
                break
            await asyncio.sleep(0.02)
        assert ah.hub.has_listeners("m17:test-ref")
        ah.hub.publish("m17:test-ref", bytes(640), talker="KN0O")
        kind, body = await _read_frame(r)
        assert kind == ah.KIND_AUDIO and len(body) == 320
        w.write(_frame(ah.KIND_HANGUP))
        await w.drain()
        w.close()
        for _ in range(50):
            if not ah.hub.has_listeners("m17:test-ref"):
                break
            await asyncio.sleep(0.02)
        assert not ah.hub.has_listeners("m17:test-ref")
    finally:
        ah.hub.unregister("m17:test-ref")
        await server.stop()


@pytest.mark.asyncio
async def test_quiet_session_sends_keepalive_silence():
    """Asterisk 22 drops an AudioSocket after 2 s of no audio; we must fill silence."""
    server = ah.AudioSocketServer("127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    try:
        uid = str(uuid.uuid4())
        server.expect(uid, lambda s: None)          # a session nobody feeds
        r, w = await _asterisk_connect(port, uid)
        kind, body = await asyncio.wait_for(_read_frame(r), 2)
        assert kind == ah.KIND_AUDIO and body == bytes(320)
        w.close()
    finally:
        await server.stop()


# ── Push-to-talk (2026-11-21) ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webphone_dtmf_goes_to_our_local_channel():
    server = ah.AudioSocketServer("127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    server.port = port
    pbx = FakePBX()
    wp = ah.WebPhoneManager(server, lambda: pbx, "111")
    wp.DTMF_GAP_S = 0
    a, other = FakeClient(), FakeClient()
    try:
        c = await wp.call(a, "750")
        assert pbx.channel_ids[-1] == f"echwp-{c.id}"
        assert not await wp.dtmf(a, c.id, "*99")              # not connected yet
        r, w = await _asterisk_connect(port, pbx.originates[-1][2].split(",")[0])
        for _ in range(50):
            if c.state == "connected":
                break
            await asyncio.sleep(0.02)
        assert not await wp.dtmf(other, c.id, "*99")          # someone else's call
        assert await wp.dtmf(a, c.id, "*9x9")
        assert pbx.dtmf == [(c.chan_id, "*"), (c.chan_id, "9"), (c.chan_id, "9")]
        w.close()
    finally:
        await server.stop()


def test_m17_stream_frame_layout_and_crc():
    from ech.adapters.m17_reflector import build_stream_frame, encode_callsign, _crc16_m17, BROADCAST_ADDR
    f = build_stream_frame(0xABCD, BROADCAST_ADDR, encode_callsign("KN0O"), 7, bytes(range(16)), last=True)
    assert len(f) == 54
    p = ah.parse_m17_stream(f)
    assert p["sid"] == "abcd" and p["type"] == 0x0005 and p["data_type"] == 2
    assert p["fn"] == 7 and p["last"] and p["payload"] == bytes(range(16))
    assert int.from_bytes(f[52:], "big") == _crc16_m17(f[:52])


class _FakeEnc:
    MODE_3200 = 0
    samples, nbytes = 160, 8

    def __init__(self, mode=0):
        pass

    @staticmethod
    def available():
        return True

    def encode(self, pcm):
        return bytes([pcm[0]]) * (len(pcm) // 40)        # 640 bytes -> 16 "bits"

    def close(self):
        pass


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, b):
        self.sent.append(b)


def _m17_adapter(monkeypatch, listen_only=False):
    from ech.adapters import m17_reflector as m
    monkeypatch.setattr(ah, "Codec2Encoder", _FakeEnc)
    a = m.M17ReflectorAdapter({"name": "m17t", "reflector_host": "x", "module": "A",
                               "callsign": "KN0O", "listen_only": listen_only})
    a._connected, a._transport = True, _FakeTransport()
    return a


@pytest.mark.asyncio
async def test_m17_voice_tx_paces_frames_and_ends_with_last_flag(monkeypatch):
    a = _m17_adapter(monkeypatch)
    assert a.can_transmit()
    assert a.voice_start("web-1", "#PARROT") is None
    assert a.voice_start("web-2") == "another ECH user is transmitting on this link"
    for i in range(1, 6):
        assert a.voice_push("web-1", bytes([i]) * 320 + bytes([i]) * 320)   # 5 x 40 ms
    a.voice_end("web-1")
    for _ in range(100):
        if a._vtx is None:
            break
        await asyncio.sleep(0.02)
    frames = [ah.parse_m17_stream(b) for b in a._transport.sent]
    assert [f["payload"][0] for f in frames[:5]] == [1, 2, 3, 4, 5]
    assert [f["fn"] for f in frames] == list(range(len(frames)))
    assert frames[-1]["last"] and not any(f["last"] for f in frames[:-1])
    assert len({f["sid"] for f in frames}) == 1
    from ech.adapters.m17_reflector import encode_callsign
    assert a._transport.sent[0][6:12] == encode_callsign("#PARROT")


@pytest.mark.asyncio
async def test_m17_voice_tx_refusals(monkeypatch):
    assert _m17_adapter(monkeypatch, listen_only=True).voice_start("w") == "this M17 link is listen-only"
    a = _m17_adapter(monkeypatch)
    import time as _t
    a._rx_voice_at, a._rx_voice_who = _t.monotonic(), "W1ABC"
    assert a.voice_start("w") == "channel busy: W1ABC is talking"


@pytest.mark.asyncio
async def test_desk_phone_star99_keys_m17_and_hash_unkeys():
    class Tx:
        def __init__(self):
            self.log = []

        def can_transmit(self):
            return True

        def voice_start(self, owner, dst):
            self.log.append("start" + (":" + dst if dst else ""))

        def voice_push(self, owner, pcm):
            self.log.append(len(pcm))
            return True

        def voice_end(self, owner):
            self.log.append("end")
    tx = Tx()
    server = ah.AudioSocketServer("127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    ah.hub.register("m17:test-tx", "M17 test", "m17", tx=tx)
    try:
        uid = str(uuid.uuid4())
        server.note_listen(uid, "m17")
        r, w = await _asterisk_connect(port, uid)
        w.write(_frame(ah.KIND_AUDIO, bytes(320)))            # not keyed: not sent
        for d in b"*99":
            w.write(_frame(ah.KIND_DTMF, bytes([d])))
        w.write(_frame(ah.KIND_AUDIO, b"\x01\x00" * 160))
        w.write(_frame(ah.KIND_DTMF, b"#"))
        w.write(_frame(ah.KIND_AUDIO, bytes(320)))            # unkeyed again
        await w.drain()
        for _ in range(50):
            if "end" in tx.log:
                break
            await asyncio.sleep(0.02)
        assert tx.log == ["start", 320, "end"]
        w.close()
    finally:
        ah.hub.unregister("m17:test-tx")
        await server.stop()


def test_allstar_talker_from_rpt_xnode():
    out = "RPT_NUMLINKS=2\nRPT_ALINKS=2,620860TK,521771TU\nRPT_RXKEYED=0\n"
    assert ah.talker_from_xnode(out, "63018", lambda n: {"620860": "W1PBR Hope, Maine"}.get(n, "")) == "620860 W1PBR Hope, Maine"
    assert ah.talker_from_xnode("RPT_ALINKS=1,620860TU\nRPT_RXKEYED=1\n", "63018") == "local user on 63018"
    assert ah.talker_from_xnode("RPT_ALINKS=\nRPT_RXKEYED=0\n", "63018") == ""


@pytest.mark.asyncio
async def test_phone_shows_who_is_talking():
    class Pbx:
        names = []

        async def set_connected_name(self, chan, name, number=""):
            self.names.append((chan, name))
    pbx = Pbx()
    server = ah.AudioSocketServer("127.0.0.1", 0)
    server.get_pbx = lambda: pbx
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    ah.hub.register("m17:test-show", "M17 test", "m17")
    try:
        uid = str(uuid.uuid4())
        server.note_listen(uid, "m17", "PJSIP/101-0000001")
        r, w = await _asterisk_connect(port, uid)
        await asyncio.sleep(0.2)
        ah.hub.publish("m17:test-show", bytes(320), talker="W1ABC")
        for _ in range(40):
            if any("W1ABC" in n for _, n in pbx.names):
                break
            await asyncio.sleep(0.05)
        assert pbx.names[0] == ("PJSIP/101-0000001", "M17 (quiet)")
        assert ("PJSIP/101-0000001", "M17: W1ABC") in pbx.names
        w.close()
    finally:
        ah.hub.unregister("m17:test-show")
        await server.stop()


@pytest.mark.asyncio
async def test_desk_phone_star43_is_an_echo_test():
    class Tx:
        dsts = []

        def can_transmit(self):
            return True

        def voice_start(self, owner, dst):
            self.dsts.append(dst)

        def voice_push(self, owner, pcm):
            return True

        def voice_end(self, owner):
            pass
    tx = Tx()
    server = ah.AudioSocketServer("127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    ah.hub.register("m17:test-echo", "M17 test", "m17", tx=tx)
    try:
        uid = str(uuid.uuid4())
        server.note_listen(uid, "m17")
        r, w = await _asterisk_connect(port, uid)
        for d in b"*43":
            w.write(_frame(ah.KIND_DTMF, bytes([d])))
        await w.drain()
        for _ in range(50):
            if tx.dsts:
                break
            await asyncio.sleep(0.02)
        assert tx.dsts == ["#PARROT"]
        w.close()
    finally:
        ah.hub.unregister("m17:test-echo")
        await server.stop()


def test_voice_tx_level_text():
    from ech.adapters.m17_reflector import _VoiceTx
    v = _VoiceTx("o", bytes([0xFF]) * 6, None)
    assert v.level_text() == "no audio"
    v.level(bytes(640))
    assert "silent" in v.level_text()
    v.level(struct.pack("<2h", 16384, -16384))
    assert v.level_text().startswith("peak -6 dBFS")


def test_leveller_turns_hot_audio_down_and_leaves_normal_audio_alone():
    import math
    from ech.adapters.m17_reflector import Leveller
    tone = lambda amp: struct.pack("<320h", *[int(amp * math.sin(2 * math.pi * 400 * i / 8000)) for i in range(320)])
    rms = lambda b: math.sqrt(sum(x * x for x in struct.unpack("<320h", b)) / 320)
    normal = Leveller()
    pcm = tone(4000)                                   # about -21 dBFS RMS
    assert normal.process(pcm) == pcm and normal.text() == "no level change"
    hot = Leveller()
    for _ in range(20):
        out = hot.process(tone(32000))                 # clipping-hot, like the 2026-11-21 phone
    assert max(abs(x) for x in struct.unpack("<320h", out)) <= 32768 * 10 ** (-3 / 20) + 1
    assert 20 * math.log10(rms(out) / 32768) < -15
    assert hot.text().startswith("turned down")


def test_playout_buffer_waits_for_cushion_and_rebuffers():
    b = ah.PlayoutBuffer()
    b.push(bytes(640))                     # 40 ms: not enough to start
    assert b.pop() is None
    b.push(bytes(320) * 14)                # now 320 ms
    frames = [b.pop() for _ in range(16)]
    assert all(f is not None and len(f) == 320 for f in frames)
    assert b.pop() is None and b.underruns == 1 and not b.playing
    b.push(bytes(320) * 200)               # 4 s burst: capped at MAX
    assert len(b.data) == int(b.MAX * 8000) * 2


def test_grid_square_roundtrip():
    from ech.core.grid import latlon_to_grid, grid_to_latlon
    assert latlon_to_grid(44.104, -69.125) == "FN54kc"
    assert latlon_to_grid(44.104, -69.125, 4) == "FN54"
    assert latlon_to_grid(*grid_to_latlon("FN54kc")) == "FN54kc"
    assert latlon_to_grid(-33.86, 151.21) == "QF56od"


def test_gnss_meta_matches_libm17_layout():
    from ech.adapters.m17_reflector import gnss_meta
    m = gnss_meta(45.0, -90.0)
    assert len(m) == 14 and m[0] == 0x00 and m[1] == 0x80 and m[2] == 0
    assert int.from_bytes(m[3:6], "big") == int(0.5 * 8388607)              # +45 deg = half of 90
    lo = int.from_bytes(m[6:9], "big")
    assert lo - (1 << 24) == int(-0.5 * 8388607)                           # -90 deg = minus half of 180
    assert m[9:] == bytes(5)


@pytest.mark.asyncio
async def test_voice_tx_carries_grid_centre_position(monkeypatch):
    from ech.core.grid import latlon_to_grid
    a = _m17_adapter(monkeypatch)
    a.set_base_location(40.7128, -74.0060)                  # an exact position (New York)
    lat, lon = a.position()
    assert latlon_to_grid(lat, lon) == "FN20xr" and (lat, lon) != (40.7128, -74.0060)
    assert a.voice_start("w") is None
    a.voice_push("w", bytes(640))
    a.voice_end("w")
    for _ in range(100):
        if a._vtx is None:
            break
        await asyncio.sleep(0.02)
    f = a._transport.sent[0]
    assert int.from_bytes(f[18:20], "big") == 0x0025 and f[20:22] == b"\x00\x80"
    assert ah.parse_m17_stream(f)["data_type"] == 2                         # still voice to receivers
    a._position_mode = "off"
    assert a.position() is None
