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

    async def fake_tts(text):
        return "/tmp/ech_tts_test"

    a._render_tts = fake_tts
    res = asyncio.run(a.intercom(["102"], text="Net check in five minutes"))
    assert res["ok"] and res["mode"] == "speak"
    blk = _actions(a)[0]
    assert "Application: Playback" in blk and "Data: /tmp/ech_tts_test" in blk
    assert "answer-after=0" in blk


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
