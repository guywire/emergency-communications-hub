"""O152 (2026-10-16): the operator switches M17 reflector modules from the
compose bar; the link relinks in place and the module is written to
config.yaml (like the Settings server switch, O99), so it is remembered
until changed."""

import asyncio

import pytest
import yaml

from ech.adapters.m17_reflector import M17ReflectorAdapter, encode_callsign


class _Transport:
    """Fake reflector: ACKNs CONN/LSTN for allowed modules, echoes DISC."""

    def __init__(self, adapter, allowed):
        self.a, self.allowed, self.sent = adapter, allowed, []

    def sendto(self, pkt, addr=None):
        self.sent.append(pkt)
        loop = asyncio.get_running_loop()
        if pkt[:4] == b"DISC":
            loop.call_soon(self.a._on_datagram, b"DISC" + pkt[4:10])
        elif pkt[:4] in (b"CONN", b"LSTN") and chr(pkt[10]) in self.allowed:
            loop.call_soon(self.a._on_datagram, b"ACKN")

    def close(self):
        pass


def _adapter():
    a = M17ReflectorAdapter({"name": "m17", "reflector_host": "r.example", "module": "C", "callsign": "KN0O"})
    a._connected = True
    return a


def test_switch_relinks_in_place():
    async def go():
        a = _adapter()
        a._transport = _Transport(a, allowed="ABC")
        assert await a.set_module("a") == "A"
        assert a._module == "A" and a._connected            # the DISC echo didn't drop the link
        assert a._transport.sent[-1] == b"CONN" + encode_callsign("KN0O") + b"A"
        assert a.config["module"] == "A"                    # a rebuild/reconnect keeps it
    asyncio.run(go())


def test_refused_module_falls_back_to_previous(monkeypatch):
    import ech.adapters.m17_reflector as mod
    orig = asyncio.wait_for

    async def fast_wait_for(aw, timeout):
        return await orig(aw, min(timeout, 0.05))

    monkeypatch.setattr(mod.asyncio, "wait_for", fast_wait_for)

    async def go():
        a = _adapter()
        a._transport = _Transport(a, allowed="C")
        with pytest.raises(ValueError, match="still on C"):
            await a.set_module("Z")
        assert a._module == "C" and a.config["module"] == "C"
    asyncio.run(go())


def test_bad_module_letter_rejected():
    a = _adapter()
    a._transport = object()
    with pytest.raises(ValueError):
        asyncio.run(a.set_module("AB"))


def test_api_switch_is_saved_to_config(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from starlette.testclient import TestClient
    from ech.api.app import create_app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(
        {"adapters": [{"type": "m17_reflector", "name": "m17", "module": "C"}]}), encoding="utf-8")

    class _Stub:
        _module = "C"

        async def set_module(self, m):
            self._module = m.upper()
            return self._module

    router = MagicMock()
    router._adapters = {"m17": _Stub()}
    with TestClient(create_app(router, MagicMock())) as c:
        d = c.post("/api/adapters/m17/m17/module", json={"module": "b"}).json()
    assert d == {"status": "ok", "module": "B"}
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["adapters"][0]["module"] == "B"


def test_listen_only_links_with_lstn_and_never_sends():
    """O185: a second, listen-only M17 link watches another reflector's text
    traffic. It joins with LSTN (confirmed in mrefd's protocol.cpp), refuses
    to send (mrefd would drop it anyway), and says so in its health detail so
    the UI leaves it out of the compose toggles."""
    from ech.core.models import NormalizedMessage

    async def go():
        a = M17ReflectorAdapter({"name": "m17-mon", "reflector_host": "r.example", "module": "A",
                                 "callsign": "KN0O", "listen_only": True})
        a._connected = True
        a._transport = _Transport(a, allowed="AB")
        assert a._health_detail()["listen_only"] is True
        msg = NormalizedMessage(source_adapter="ui", source_channel="", from_id="KN0O", body="hi")
        assert await a.send(msg) is False
        assert a._transport.sent == []
        assert await a.set_module("B") == "B"              # relinks with LSTN, not CONN
        assert a._transport.sent[-1] == b"LSTN" + encode_callsign("KN0O") + b"B"
        assert not any(p[:4] in (b"CONN", b"M17P") for p in a._transport.sent)
    asyncio.run(go())
