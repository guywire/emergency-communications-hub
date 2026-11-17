"""Mesh bot: who it answers on a channel, and where the answer goes.

2026-11-17: on Public, "@[SignalMatrix] what does that mean?" got no reply.
The MeshCore app mentions a node as "@[NodeName]", and the bot only knew its
configured mention name ("[ECH Bot]"), not the radio's own name.
"""
import asyncio

from ech.core.mesh_bot import MeshBot, _is_mentioned
from ech.core.models import NormalizedMessage


class _Radio:
    _device_name = "SignalMatrix"


class _Router:
    def __init__(self):
        self._adapters = {"meshcore-usb": _Radio()}
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


def _bot(**cfg):
    base = {"enabled": True, "channels": ["ch0", "ch2", "ch4"], "mention_required_channels": ["ch0", "ch4"],
            "mention_name": "[ECH Bot]", "respond_all_channels": ["#bot"], "reply_dm": False,
            "global_cooldown_sec": 0, "per_user_cooldown_sec": 0}
    base.update(cfg)
    router = _Router()
    bot = MeshBot({"mesh_bot": base}, router=router)
    bot._client = object()            # handle() needs a live HTTP client; commands are stubbed below
    calls = []

    async def fake_dispatch(msg, cmd, args, mention_required=False):
        calls.append((cmd, args))
    bot._dispatch = fake_dispatch
    return bot, router, calls


def _run(bot, channel, body):
    async def go():
        await bot.handle(NormalizedMessage(source_adapter="meshcore-usb", source_channel=channel,
                                           from_id="N0CALL-T1", body=body))
        await asyncio.sleep(0)
    asyncio.run(go())


def test_mention_forms():
    assert _is_mentioned("@[SignalMatrix] hi", ["SignalMatrix"])
    assert _is_mentioned("@SignalMatrix hi", "SignalMatrix")
    assert _is_mentioned("@[ECH Bot] help", "[ECH Bot]")
    assert not _is_mentioned("mail foo@smith.com", "SM")
    assert not _is_mentioned("@[SignalMatrixX] hi", "SignalMatrix")   # bracket form is exact


def test_radio_own_name_mention_is_answered_on_public():
    bot, _, calls = _bot()
    _run(bot, "ch0:Public", "@[SignalMatrix] what does that mean?")
    assert calls == [("unknown", "")]


def test_mentioned_command_on_public_runs():
    bot, _, calls = _bot()
    _run(bot, "ch0:Public", "@[SignalMatrix] weather 04841")
    assert calls and calls[0][0] == "weather"


def test_plain_chatter_on_public_is_ignored():
    bot, _, calls = _bot()
    _run(bot, "ch0:Public", "the weather is nice today")
    _run(bot, "ch0:Public", "@[Other Node] Got your test")
    assert calls == []


def test_bot_channel_answers_everything():
    bot, _, calls = _bot()
    _run(bot, "ch2:#bot", "hello is anyone there")
    _run(bot, "ch2:#bot", "tide")
    assert [c[0] for c in calls] == ["unknown", "tide"]


def test_reply_goes_back_on_the_channel_it_came_from():
    bot, router, _ = _bot()
    msg = NormalizedMessage(source_adapter="meshcore-usb", source_channel="ch2:#bot", from_id="N0CALL-T1", body="x")
    asyncio.run(bot._send(msg, "pong"))
    assert router.sent[0]["to_id"] is None and router.sent[0]["raw"] == {"channel_idx": 2}


def test_dm_reply_goes_back_as_dm():
    bot, router, _ = _bot()
    msg = NormalizedMessage(source_adapter="meshcore-usb", source_channel="DM", from_id="A1B2C3D4E5F6", body="x")
    asyncio.run(bot._send(msg, "pong"))
    assert router.sent[0]["to_id"] == "A1B2C3D4E5F6"
