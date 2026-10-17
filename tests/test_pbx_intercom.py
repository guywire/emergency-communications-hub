"""O154 (choose which of your phones rings for click-to-call) and O155
(intercom: auto-answer on the target phone's loudspeaker), 2026-10-16."""

import asyncio

from ech.adapters.asterisk_adapter import AsteriskAdapter, valid_extension


class _Writer:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass


def _adapter():
    a = AsteriskAdapter({"name": "pbx", "ami_secret": "x", "local_extension": "101"})
    a._writer = _Writer()
    a._connected = True
    return a


def _actions(a):
    return [blk for blk in a._writer.buf.decode().split("\r\n\r\n") if blk.strip()]


def test_valid_extension_blocks_channel_injection():
    assert valid_extension("102") and valid_extension("kn0o_ext-1")
    for bad in ("", "101&PJSIP/102", "Local/s@default", "102,x", "1 2", "x" * 21):
        assert not valid_extension(bad)


def test_click_to_call_rings_the_chosen_phone():
    a = _adapter()
    assert asyncio.run(a.originate("3194", "103"))
    assert "Channel: PJSIP/103" in _actions(a)[0]
    assert not asyncio.run(a.originate("3194", "101&PJSIP/102"))


def test_intercom_talk_auto_answers_targets_into_quiet_room_then_rings_caller():
    a = _adapter()
    res = asyncio.run(a.intercom(["102", "103"], caller_extension="104"))
    assert res["ok"] and res["mode"] == "talk"
    acts = _actions(a)
    assert len(acts) == 3
    for blk, ext in zip(acts[:2], ("102", "103")):
        assert f"Channel: PJSIP/{ext}" in blk and "Application: ConfBridge" in blk
        assert "Variable: PJSIP_HEADER(add,Call-Info)=<sip:intercom@127.0.0.1>;answer-after=0" in blk
        assert "Variable: PJSIP_HEADER(add,Alert-Info)=<http://127.0.0.1>;info=alert-autoanswer" in blk
        assert "Variable: CONFBRIDGE(user,wait_marked)=yes" in blk
        assert "Variable: CONFBRIDGE(user,timeout)=120" in blk
    room = acts[0].split("Data: ")[1].split("\r\n")[0]
    assert room.startswith("ech-intercom-") and f"Data: {room}" in acts[2]
    assert "Channel: PJSIP/104" in acts[2] and "CONFBRIDGE(user,marked)=yes" in acts[2]
    assert "answer-after" not in acts[2]                 # the caller's own phone rings normally


def test_intercom_rejects_own_phone_and_bad_targets():
    a = _adapter()
    assert not asyncio.run(a.intercom(["101"]))["ok"]          # default caller is 101
    assert not asyncio.run(a.intercom(["102&PJSIP/103"]))["ok"]
    assert not asyncio.run(a.intercom([]))["ok"]


def test_intercom_speak_plays_tts_with_auto_answer():
    a = _adapter()

    seen = {}

    async def fake_tts(text, lead_s=0.0, chime=False):
        seen.update(lead_s=lead_s, chime=chime)
        return "/tmp/ech_tts_test"

    a._render_tts = fake_tts
    res = asyncio.run(a.intercom(["102"], text="Net check in five minutes"))
    assert res["ok"] and res["mode"] == "speak"
    blk = _actions(a)[0]
    assert "Application: Playback" in blk and "Data: /tmp/ech_tts_test" in blk
    assert "answer-after=0" in blk
    assert seen == {"lead_s": 1.5, "chime": True}        # pause + chime before speech


def test_tts_lead_in_and_chime_sox_steps(monkeypatch):
    import ech.adapters.asterisk_adapter as mod
    cmds = []

    class _P:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        cmds.append(list(cmd))
        return _P()

    class _FakePath:
        def __init__(self, p):
            self.p = p

        def is_file(self):
            return True

        def unlink(self, missing_ok=False):
            pass

        def chmod(self, mode):
            pass

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(mod, "Path", _FakePath)
    stem = asyncio.run(_adapter()._render_tts("hello", lead_s=1.5, chime=True))
    assert stem and stem.startswith("/tmp/ech_tts_")
    joined = [" ".join(c) for c in cmds]
    assert cmds[0][0] == "espeak-ng"
    assert any("synth 0.35 sine 880" in j for j in joined) and any("synth 0.6 sine 660" in j for j in joined)
    final = cmds[-1]                                       # ding + dong + speech, 1.5 s silence first
    assert final[-2:] == ["pad", "1.50"] and final[-3] == stem + ".wav"
    assert final[1].endswith("_ding.wav") and final[2].endswith("_dong.wav") and final[3].endswith("_8k.wav")


def _pkt(uid, channel="PJSIP/102-0001", cid="100", exten=""):
    return {"Uniqueid": uid, "Channel": channel, "CallerIDNum": cid, "Exten": exten}


def _channel_id(a):
    return _actions(a)[0].split("ChannelId: ")[1].split("\r\n")[0]


def _queue(a):
    queued = []

    async def enq(msg):
        queued.append(msg)

    a._enqueue = enq
    a._router_notify = None
    return queued


def test_intercom_legs_go_to_call_log_not_messages():
    a = _adapter()
    queued = _queue(a)

    async def fake_tts(text, lead_s=0.0, chime=False):
        return "/tmp/ech_tts_test"

    a._render_tts = fake_tts
    asyncio.run(a.intercom(["102"], text="hello"))
    cid = _channel_id(a)
    assert cid.startswith("ech-intercom-")
    a._on_new_channel(_pkt(cid))
    a._active_calls[cid]["start"] -= 6
    asyncio.run(a._on_hangup({"Uniqueid": cid}))
    assert queued == []
    assert a._call_log[0]["direction"] == "intercom" and "INTERCOM" in a._call_log[0]["body"]
    assert "ext 102" in a._call_log[0]["body"]


def test_ech_placed_call_is_labelled_outbound():
    a = _adapter()
    queued = _queue(a)
    asyncio.run(a.originate("3194", "102"))
    cid = _channel_id(a)
    a._on_new_channel(_pkt(cid))
    asyncio.run(a._on_hangup({"Uniqueid": cid}))
    assert "[OUTBOUND]" in queued[0].body and "INBOUND" not in queued[0].body
    # a call nobody at ECH placed is still inbound
    a._on_new_channel(_pkt("1700000000.5"))
    asyncio.run(a._on_hangup({"Uniqueid": "1700000000.5"}))
    assert "[INBOUND]" in queued[1].body


def test_api_refuses_unregistered_phone_and_expands_all():
    from unittest.mock import MagicMock
    from starlette.testclient import TestClient
    from ech.api.app import create_app
    from ech.adapters.mock_asterisk import MockAsteriskAdapter
    pbx = MockAsteriskAdapter({"name": "pbx", "local_extension": "101"})
    pbx._connected = True
    router = MagicMock()
    router._adapters = {"pbx": pbx}
    with TestClient(create_app(router, MagicMock())) as c:
        d = c.post("/api/pbx/call", json={"destination": "3194", "caller_extension": "103"}).json()
        assert d["status"] == "error" and "no phone connected" in d["detail"]
        d = c.post("/api/pbx/call", json={"destination": "3194", "caller_extension": "102"}).json()
        assert d["status"] == "ok" and d["caller_extension"] == "102"
        d = c.post("/api/pbx/intercom", json={"targets": "all", "caller_extension": "101"}).json()
        assert d["status"] == "ok" and d["targets"] == ["102"] and d["mode"] == "talk"
        d = c.post("/api/pbx/intercom", json={"targets": ["104"], "text": "hi"}).json()
        assert d["status"] == "error" and "no phone connected" in d["detail"]
