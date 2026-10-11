"""
tests/test_bridge_engine.py
---------------------------
O117: bridges link ONE endpoint per side (mesh channel ↔ APRS addressee),
with explicit direction, type filtering, rate limiting, loop/echo and
multi-path de-dupe, text shaping, callsign gating and dry-run.
"""

import pytest

from ech.core.bridge import BridgeConfigError, BridgeEngine
from ech.core.models import NormalizedMessage


class FakeMeshtasticAdapter:
    def __init__(self, name):
        self.name = name
        self._connected = True
        self._paused = False
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)
        return True


class FakeAPRSAdapter(FakeMeshtasticAdapter):
    pass


def mesh_msg(text, ch=2, sender="Bob", to_id=None, msg_type="text", src="mesh"):
    return NormalizedMessage(source_adapter=src, source_channel=f"ch{ch}", from_id="!abcd1234",
                             from_display=sender, body=text, to_id=to_id, msg_type=msg_type,
                             raw={"channel": ch})


def aprs_msg(text, addressee="EMCOMM", sender="W1ABC-7", fmt="message"):
    raw = {"format": fmt}
    if fmt == "message":
        raw.update(addressee=addressee, message_text=text)
    return NormalizedMessage(source_adapter="aprs", source_channel="144.390 (IS)", from_id=sender,
                             from_display=sender, body=f"MSG {sender}→{addressee}: {text}", raw=raw)


RULE = {"name": "net", "a": {"adapter": "mesh", "channel": 2},
        "b": {"adapter": "aprs", "to": "EMCOMM"}, "direction": "both", "rate_per_min": 60}


def setup(rule=None):
    eng = BridgeEngine()
    eng.load([rule or RULE])
    adapters = {"mesh": FakeMeshtasticAdapter("mesh"), "aprs": FakeAPRSAdapter("aprs")}
    return eng, adapters


@pytest.mark.asyncio
async def test_only_the_configured_channel_crosses_with_attribution():
    eng, ad = setup()
    await eng.apply(mesh_msg("net check in", ch=2), ad)
    await eng.apply(mesh_msg("other channel chatter", ch=0), ad)
    assert [m.body for m in ad["aprs"].sent] == ["Bob: net check in"]
    assert ad["aprs"].sent[0].to_id == "EMCOMM"


@pytest.mark.asyncio
async def test_aprs_side_only_messages_to_the_addressee():
    eng, ad = setup()
    await eng.apply(aprs_msg("hello mesh"), ad)
    await eng.apply(aprs_msg("not for the net", addressee="KN0O"), ad)
    await eng.apply(aprs_msg("", fmt="position"), ad)
    assert [m.body for m in ad["mesh"].sent] == ["W1ABC-7: hello mesh"]
    assert ad["mesh"].sent[0].raw["channel_idx"] == 2


@pytest.mark.asyncio
async def test_direction_is_respected():
    eng, ad = setup({**RULE, "direction": "a_to_b"})
    await eng.apply(aprs_msg("should not cross"), ad)
    await eng.apply(mesh_msg("should cross"), ad)
    assert ad["mesh"].sent == [] and len(ad["aprs"].sent) == 1


@pytest.mark.asyncio
async def test_types_positions_and_dms_stay_home_unless_opted_in():
    eng, ad = setup()
    await eng.apply(mesh_msg("pos", msg_type="position"), ad)
    await eng.apply(mesh_msg("private", to_id="!ffff"), ad)
    assert ad["aprs"].sent == []
    eng2, ad2 = setup({**RULE, "types": ["text", "dm"]})
    await eng2.apply(mesh_msg("private", to_id="!ffff"), ad2)
    assert len(ad2["aprs"].sent) == 1


@pytest.mark.asyncio
async def test_no_ping_pong_and_echo_suppressed():
    eng, ad = setup()
    await eng.apply(mesh_msg("road closed at route 1 bridge"), ad)
    out = ad["aprs"].sent[0]
    # The forwarded copy itself must never be bridged again
    await eng.apply(out, ad)
    # …nor the network echoing our text back on the APRS side
    await eng.apply(aprs_msg(out.body, sender="KN0O"), ad)
    assert ad["mesh"].sent == []
    assert eng.rules[0].counters["dropped_loop"] == 1


@pytest.mark.asyncio
async def test_short_genuine_reply_not_mistaken_for_echo():
    eng, ad = setup()
    await eng.apply(mesh_msg("ok thanks all"), ad)
    await eng.apply(aprs_msg("ok"), ad)          # a real, different short reply
    assert [m.body for m in ad["mesh"].sent] == ["W1ABC-7: ok"]


@pytest.mark.asyncio
async def test_multipath_duplicate_forwarded_once():
    eng, ad = setup()
    await eng.apply(mesh_msg("same text twice"), ad)
    await eng.apply(mesh_msg("same text twice"), ad)    # heard again via MQTT/another path
    assert len(ad["aprs"].sent) == 1
    assert eng.rules[0].counters["dropped_duplicate"] == 1


@pytest.mark.asyncio
async def test_rate_limit():
    eng, ad = setup({**RULE, "rate_per_min": 3})
    for i in range(6):
        await eng.apply(mesh_msg(f"burst message number {i}"), ad)
    assert len(ad["aprs"].sent) == 3
    assert eng.rules[0].counters["dropped_rate"] == 3


@pytest.mark.asyncio
async def test_truncates_to_aprs_limit():
    eng, ad = setup()
    await eng.apply(mesh_msg("x" * 300), ad)
    body = ad["aprs"].sent[0].body
    assert len(body) == 67 and body.endswith("…")


@pytest.mark.asyncio
async def test_require_callsign_and_dry_run():
    eng, ad = setup({**RULE, "require_callsign": True})
    await eng.apply(mesh_msg("hi", sender="Bob"), ad)
    await eng.apply(mesh_msg("hi there", sender="KN0O Guy"), ad)
    assert [m.body for m in ad["aprs"].sent] == ["KN0O Guy: hi there"]

    eng, ad = setup({**RULE, "dry_run": True})
    await eng.apply(mesh_msg("test"), ad)
    assert ad["aprs"].sent == [] and eng.rules[0].counters["would_forward"] == 1
    assert eng.status()["log"][-1]["outcome"].startswith("dry run")


def test_validation_and_legacy_rules():
    with pytest.raises(BridgeConfigError):
        BridgeEngine.validate([{**RULE, "direction": "sideways"}])
    with pytest.raises(BridgeConfigError):
        BridgeEngine.validate([{**RULE, "types": ["position"]}])
    with pytest.raises(BridgeConfigError):
        BridgeEngine.validate([{"a": {"adapter": "x"}, "b": {"adapter": "x"}}])
    (legacy,) = BridgeEngine.validate([{"from_adapter": "mesh", "to_adapter": "aprs"}])
    d = legacy.to_dict()
    assert d["direction"] == "a_to_b" and d["types"] == ["text"]
    assert d["a"] == {"adapter": "mesh"} and d["b"] == {"adapter": "aprs"}


@pytest.mark.asyncio
async def test_aprs_target_without_addressee_does_not_send():
    eng, ad = setup({**RULE, "b": {"adapter": "aprs"}, "direction": "a_to_b"})
    await eng.apply(mesh_msg("hello"), ad)
    assert ad["aprs"].sent == []
    assert "needs a 'to' addressee" in eng.status()["log"][-1]["outcome"]
