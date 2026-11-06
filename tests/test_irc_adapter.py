"""
tests/test_irc_adapter.py
-------------------------
O92: ech/adapters/irc.py against a scripted in-process IRC server — line
parsing, registration (plain, nick collision, SASL PLAIN), PING/PONG,
channel + private messages in, and outbound PRIVMSG splitting/targeting.
"""

import asyncio
import base64

import pytest

from ech.adapters.irc import IRCAdapter, parse_line, split_text, strip_formatting
from ech.core.models import NormalizedMessage, Priority


# ── Pure helpers ──

def test_parse_line_prefix_trailing_and_tags():
    tags, prefix, cmd, params = parse_line("@time=x :bob!~b@h PRIVMSG #ech :hello there")
    assert tags == {"time": "x"}
    assert prefix == "bob!~b@h"
    assert cmd == "PRIVMSG"
    assert params == ["#ech", "hello there"]


def test_parse_line_no_prefix():
    assert parse_line("PING :irc.example.net")[2:] == ("PING", ["irc.example.net"])


def test_strip_formatting():
    assert strip_formatting("\x02bold\x02 \x0304,01red\x03 \x1fu\x0f") == "bold red u"


def test_split_text_respects_line_limit_and_words():
    chunks = split_text("#ech", "word " * 300, "KN0O")
    assert len(chunks) > 1
    for c in chunks:
        line = f":KN0O!~KN0O@{'h' * 63} PRIVMSG #ech :{c}\r\n"
        assert len(line.encode()) <= 512
        assert not c.startswith(" ") and not c.endswith(" ")
    assert " ".join(chunks).split() == ["word"] * 300


def test_split_text_newlines_become_separate_lines():
    assert split_text("#ech", "one\ntwo", "N") == ["one", "two"]


def test_config_validation():
    with pytest.raises(ValueError):
        IRCAdapter({"server": "x"})
    with pytest.raises(ValueError):
        IRCAdapter({"server": "x", "nick": "N"})
    a = IRCAdapter({"server": "x", "nick": "N", "channels": ["ech"], "tls": False})
    assert a._channels == ["#ech"] and a._port == 6667 and a.tx_channel == "#ech"


# ── Scripted server ──

class FakeServer:
    def __init__(self, taken_nicks=(), sasl=False):
        self.lines: list[str] = []
        self.taken = set(taken_nicks)
        self.sasl = sasl
        self.writer = None
        self.got = asyncio.Condition()

    async def start(self):
        self.srv = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.srv.sockets[0].getsockname()[1]

    def say(self, line):
        self.writer.write(line.encode() + b"\r\n")

    async def wait_for(self, pred, timeout=3.0):
        async def _w():
            async with self.got:
                await self.got.wait_for(lambda: any(pred(x) for x in self.lines))
        await asyncio.wait_for(_w(), timeout)

    async def _client(self, reader, writer):
        self.writer = writer
        nick = None
        while True:
            data = await reader.readline()
            if not data:
                break
            line = data.decode().rstrip("\r\n")
            async with self.got:
                self.lines.append(line)
                self.got.notify_all()
            cmd, _, rest = line.partition(" ")
            if cmd == "CAP" and self.sasl:
                self.say(":srv CAP * ACK :sasl")
            elif cmd == "AUTHENTICATE" and rest == "PLAIN":
                self.say("AUTHENTICATE +")
            elif cmd == "AUTHENTICATE":
                self.say(":srv 903 * :SASL authentication successful")
            elif cmd == "NICK":
                if rest in self.taken:
                    self.say(f":srv 433 * {rest} :Nickname is already in use")
                else:
                    nick = rest
            elif cmd == "USER" or (cmd == "CAP" and rest == "END"):
                pass
            elif cmd == "JOIN":
                for ch in rest.split(","):
                    if ch in getattr(self, "banned", ()):
                        self.say(f":srv 474 {nick} {ch} :Cannot join channel (+b) - you are banned")
                    else:
                        self.say(f":{nick}!~u@h JOIN {ch}")
            elif cmd == "PART":
                self.say(f":{nick}!~u@h PART {rest.split()[0]}")
            if cmd in ("USER", "NICK") and nick and not self.sasl or (cmd == "CAP" and rest == "END" and nick):
                if not getattr(self, "_welcomed", False):
                    self._welcomed = True
                    self.say(f":srv 001 {nick} :Welcome")
            await writer.drain()

    async def close(self):
        if self.writer:
            self.writer.close()
        self.srv.close()


async def _adapter(port, **extra):
    cfg = {"server": "127.0.0.1", "port": port, "tls": False, "nick": "KN0O",
           "channels": ["#ech", "#ares"], "send_interval": 0, **extra}
    a = IRCAdapter(cfg)
    await a.connect()
    return a


async def test_register_join_ping_and_receive():
    srv = FakeServer()
    port = await srv.start()
    a = await _adapter(port)
    try:
        assert a._connected and a._nick == "KN0O"
        await srv.wait_for(lambda x: x == "JOIN #ech,#ares")
        await asyncio.sleep(0.1)
        assert a._health_detail()["joined"] == ["#ares", "#ech"]

        srv.say("PING :abc123")
        await srv.wait_for(lambda x: x == "PONG :abc123")

        srv.say(":bob!~b@h PRIVMSG #ech :\x02mayday\x02 at the shelter")
        srv.say(":carol!~c@h PRIVMSG KN0O :private note")
        srv.say(":dave!~d@h PRIVMSG #ech :\x01ACTION waves\x01")
        srv.say(":irc.example.net NOTICE * :*** Looking up your hostname")
        srv.say(":SaslServ!SaslServ@services.libera.chat NOTICE KN0O :Last login from: ~k@1.2.3.4")
        srv.say(":NickServ!NickServ@services.libera.chat NOTICE KN0O :You are now identified")
        await srv.writer.drain()
        msgs = [await asyncio.wait_for(a._rx_queue.get(), 2) for _ in range(3)]
        assert (msgs[0].from_id, msgs[0].source_channel, msgs[0].body) == ("bob", "#ech", "mayday at the shelter")
        assert msgs[0].priority == Priority.EMERGENCY and msgs[0].to_id is None
        assert (msgs[1].source_channel, msgs[1].to_id, msgs[1].body) == ("DM", "KN0O", "private note")
        assert msgs[2].body == "* dave waves"
        await asyncio.sleep(0.1)
        assert a._rx_queue.empty()
    finally:
        await a.disconnect()
        await srv.close()


async def test_send_targets():
    srv = FakeServer()
    port = await srv.start()
    a = await _adapter(port)
    try:
        def m(**kw):
            return NormalizedMessage(source_adapter="irc", source_channel="outbound",
                                     from_id="local", body=kw.pop("body", "hi"), **kw)
        assert await a.send(m())
        assert await a.send(m(to_id="#ares"))
        assert await a.send(m(to_id="carol"))
        assert await a.send(m(raw={"channel_name": "ech"}))
        assert not await a.send(m(to_id="bad target"))
        assert not await a.send(m(body="   "))
        await srv.wait_for(lambda x: x == "PRIVMSG #ech :hi" and srv.lines.count(x) == 2)
        assert "PRIVMSG #ares :hi" in srv.lines and "PRIVMSG carol :hi" in srv.lines
        assert a._tx_count == 4
    finally:
        await a.disconnect()
        await srv.close()


async def test_nick_in_use_falls_back():
    srv = FakeServer(taken_nicks={"KN0O"})
    port = await srv.start()
    a = await _adapter(port)
    try:
        assert a._nick == "KN0O_"
    finally:
        await a.disconnect()
        await srv.close()


async def test_sasl_plain():
    srv = FakeServer(sasl=True)
    port = await srv.start()
    a = await _adapter(port, sasl_password="s3cret")
    try:
        token = base64.b64encode(b"\0KN0O\0s3cret").decode()
        assert srv.lines[:1] == ["CAP REQ :sasl"]
        assert f"AUTHENTICATE {token}" in srv.lines and "CAP END" in srv.lines
        assert a._connected
    finally:
        await a.disconnect()
        await srv.close()


async def test_server_close_drops_link():
    srv = FakeServer()
    port = await srv.start()
    a = await _adapter(port)
    try:
        srv.say("ERROR :Closing Link: bye")
        await srv.writer.drain()
        for _ in range(30):
            if not a._connected:
                break
            await asyncio.sleep(0.05)
        assert not a._connected and "Closing Link" in a._last_error
    finally:
        await a.disconnect()
        await srv.close()


# ── Channel browser: LIST / join / leave ──

async def test_list_channels_filters_and_sorts():
    srv = FakeServer()
    port = await srv.start()
    a = await _adapter(port)
    try:
        task = asyncio.create_task(a.list_channels("ham", 5))
        await srv.wait_for(lambda x: x.startswith("LIST"))
        assert "LIST >4,*ham*" in srv.lines
        srv.say(":srv 321 KN0O Channel :Users  Name")
        srv.say(":srv 322 KN0O #hamradio 120 :[+nt] General ham chat")
        srv.say(":srv 322 KN0O #ech 9 :ECH test")          # already ours
        srv.say(":srv 322 KN0O #ham-aprs 40 :APRS talk")
        srv.say(":srv 322 KN0O #tiny-ham 2 :too small")    # server without ELIST ignores >4
        srv.say(":srv 323 KN0O :End of /LIST")
        await srv.writer.drain()
        res = await asyncio.wait_for(task, 3)
        assert [r["channel"] for r in res["channels"]] == ["#hamradio", "#ham-aprs"]
        assert res["channels"][0]["topic"] == "General ham chat"
        assert res["channels"][0]["users"] == 120 and not res["channels"][0]["joined"]
        assert res["error"] is None and not res["timed_out"]
    finally:
        await a.disconnect()
        await srv.close()


async def test_list_channels_server_busy():
    srv = FakeServer()
    port = await srv.start()
    a = await _adapter(port)
    try:
        task = asyncio.create_task(a.list_channels("ham"))
        await srv.wait_for(lambda x: x.startswith("LIST"))
        srv.say(":srv 263 KN0O LIST :Server load is temporarily too heavy. Please wait a while and try again.")
        await srv.writer.drain()
        res = await asyncio.wait_for(task, 3)
        assert res["channels"] == [] and "too heavy" in res["error"]
    finally:
        await a.disconnect()
        await srv.close()


async def test_join_and_part_channel():
    srv = FakeServer()
    srv.banned = {"#closed"}
    port = await srv.start()
    a = await _adapter(port)
    try:
        await srv.wait_for(lambda x: x == "JOIN #ech,#ares")
        res = await a.join_channel("hamradio")
        assert res["channel"] == "#hamradio" and a._channels == ["#ech", "#ares", "#hamradio"]
        assert "#hamradio" in a._health_detail()["joined"]

        with pytest.raises(ValueError, match="banned"):
            await a.join_channel("#closed")
        assert "#closed" not in a._channels
        with pytest.raises(ValueError):
            await a.join_channel("bad name")

        res = await a.part_channel("#ech")               # the default channel
        assert a._channels == ["#ares", "#hamradio"]
        assert res["default_channel"] == "#ares" and a.tx_channel == "#ares"
        await srv.wait_for(lambda x: x.startswith("PART #ech"))
        await a.part_channel("#hamradio")
        with pytest.raises(ValueError, match="only channel"):
            await a.part_channel("#ares")
        with pytest.raises(ValueError):
            await a.part_channel("#nope")
    finally:
        await a.disconnect()
        await srv.close()


async def test_join_part_api_saves_config(tmp_path):
    import yaml
    from unittest.mock import MagicMock
    from httpx import AsyncClient, ASGITransport
    from ech.api.app import create_app

    a = IRCAdapter({"server": "x", "nick": "N", "channels": ["#ech"], "tls": False})
    async def fake_join(ch):
        a._channels.append("#" + ch.lstrip("#"))
        return {"channel": a._channels[-1], "channels": list(a._channels)}
    a.join_channel = fake_join
    router = MagicMock()
    router._adapters = {"irc-libera": a}
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"adapters": [
        {"name": "irc-libera", "type": "irc", "channels": ["#ech"], "default_channel": "#ech"}]}))

    class _Auth:
        async def get_session(self, t): return {"username": "admin", "role": "admin", "must_change_pw": False}
        async def require_session(self, r): return {"username": "admin", "role": "admin"}

    app = create_app(router, MagicMock(), auth=_Auth(), config_path=str(cfg))
    async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 5000)),
                           base_url="http://ech.test") as c:
        c.cookies.set("ech_session", "t")
        r = await c.post("/api/adapters/irc-libera/irc/join", json={"channel": "hamradio"})
        assert r.json()["status"] == "ok", r.text
        assert yaml.safe_load(cfg.read_text())["adapters"][0]["channels"] == ["#ech", "#hamradio"]

        r = await c.post("/api/adapters/irc-libera/irc/part", json={"channel": "#ech"})
        assert r.json()["status"] == "ok", r.text
        saved = yaml.safe_load(cfg.read_text())["adapters"][0]
        assert saved["channels"] == ["#hamradio"] and saved["default_channel"] == "#hamradio"

        r = await c.post("/api/adapters/irc-libera/irc/list", json={"query": "", "min_users": 0})
        assert r.json()["status"] == "error"   # refuses an unfiltered full-network LIST
