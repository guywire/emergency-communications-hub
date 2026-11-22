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


def test_bot_channel_answers_commands_not_chatter():
    # 2026-11-21: #bot got "Unrecognized command" under every other bot's post
    # and under other bots' commands ("Test", "Stats", "Cmd"). Unknown text is
    # only answered when it @-mentions us.
    bot, _, calls = _bot()
    _run(bot, "ch2:#bot", "hello is anyone there")
    _run(bot, "ch2:#bot", "Stats")
    _run(bot, "ch2:#bot", "tide")
    _run(bot, "ch2:#bot", "@[SignalMatrix] hello?")
    assert [c[0] for c in calls] == ["tide", "unknown"]


def _run_from(bot, channel, sender, body):
    async def go():
        await bot.handle(NormalizedMessage(source_adapter="meshcore-usb", source_channel=channel,
                                           from_id=sender, from_display=sender, body=body))
        await asyncio.sleep(0)
    asyncio.run(go())


def test_other_bots_are_ignored():
    # Real #bot traffic, 2026-11-19..21.
    bot, _, calls = _bot()
    _run_from(bot, "ch2:#bot", "EWR-BOT", "Pong!")
    _run_from(bot, "ch2:#bot", "Zippy_WX_BOT", "@[4JMF/m] found 2 unique path(s):")
    _run_from(bot, "ch2:#bot", "VTRPH Pingbot", "[8875] Magic 8-ball: Reply hazy")
    _run_from(bot, "ch2:#bot", "NoWeare.NH.PingBot\U0001F6F8", "Bot Stats (24h):\nCmds: 147\nTop cmd: test")
    _run_from(bot, "ch2:#bot", "Some Repeater", "E0C8: Unknown\n8C96: Unknown\nD910: Unknown")
    _run_from(bot, "ch2:#bot", "FleetLynx", "@[HotWheels498] switch your companion to 2 byte path weather")
    assert calls == []


def test_humans_with_bot_like_letters_still_answered():
    bot, _, calls = _bot()
    _run_from(bot, "ch2:#bot", "Abbott", "ping")
    _run_from(bot, "ch2:#bot", "Trenton-1", "Path")
    assert [c[0] for c in calls] == ["ping", "path"]


def test_ignore_senders_config():
    bot, _, calls = _bot(ignore_senders=["Lanka Base"], ignore_bot_names=False)
    _run_from(bot, "ch2:#bot", "lanka base", "ping")
    _run_from(bot, "ch2:#bot", "EWR-BOT", "ping")     # name check off
    assert [c[0] for c in calls] == ["ping"]


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


def test_max_hops_limits_channel_requests_to_the_local_mesh():
    bot, _, calls = _bot(max_hops=4)

    def run(hops, body, channel="ch2:#bot"):
        async def go():
            await bot.handle(NormalizedMessage(source_adapter="meshcore-usb", source_channel=channel,
                                               from_id="N0CALL-T1", body=body, hop_count=hops))
            await asyncio.sleep(0)
        asyncio.run(go())
    run(3, "wx 04841")
    run(13, "wx 12186")                       # far away: their own bots answer
    run(13, "@[SignalMatrix] wx 04841")       # a mention always works
    run(None, "tide")                         # unknown distance: answered
    assert [c[0] for c in calls] == ["weather", "weather", "tide"]


def test_command_word_mid_sentence_is_not_a_command_on_channels():
    # 2026-11-21 on Public: "@[SignalMatrix] does bridged mean internet? If so, we need
    # to keep mesh off grid." ran `grid` and broadcast the station position.
    bot, _, calls = _bot()
    _run(bot, "ch0:Public", "@[SignalMatrix] does bridged mean internet? If so, we need to keep mesh off grid.")
    _run(bot, "ch2:#bot", "is the weather nice")
    _run(bot, "ch2:#bot", "@[SignalMatrix] weather 04841")
    _run(bot, "ch2:#bot", "@[SignalMatrix], grid FN54")
    assert [c[0] for c in calls] == ["unknown", "weather", "grid"]


def test_grid_without_args_never_gives_coordinates():
    bot, _, _ = _bot(lat=39.7392, lon=-104.9903)
    r = bot._cmd_grid("")
    assert "DM79" in r and "39.7" not in r and "-104" not in r and "DM79mr" not in r


def test_help_has_two_pages_that_fit_one_message_and_cover_every_command():
    from ech.core.mesh_bot import _CMD_WORDS, _CMD_ALIASES
    bot, _, _ = _bot()
    p1, p2 = bot._cmd_help(""), bot._cmd_help("2")
    assert bot._cmd_help("more") == p2 and "help 2" in p1
    assert len(p1) <= 160 and len(p2) <= 160
    listed = set(p1.replace("|", " ").split()) | set(p2.split())
    canon = {_CMD_ALIASES.get(w.rstrip("\?"), w.rstrip("\?")) for w in _CMD_WORDS} - {"help", "weather"}
    missing = {c for c in canon if c not in listed}
    assert missing <= {"localnews"} or "localnews" in listed
    assert missing == set(), missing
