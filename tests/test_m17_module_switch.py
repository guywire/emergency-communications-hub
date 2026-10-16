"""O152 (2026-10-16): the operator switches M17 reflector modules from the
compose bar; the link relinks in place and the module is written to
config.yaml (like the Settings server switch, O99), so it is remembered
until changed."""

import asyncio

import pytest
import yaml

from ech.adapters.m17_reflector import M17ReflectorAdapter, encode_callsign


class _Transport:
    """Fake reflector: ACKNs CONN for allowed modules, echoes DISC."""

    def __init__(self, adapter, allowed):
        self.a, self.allowed, self.sent = adapter, allowed, []

    def sendto(self, pkt, addr=None):
        self.sent.append(pkt)
        loop = asyncio.get_running_loop()
        if pkt[:4] == b"DISC":
            loop.call_soon(self.a._on_datagram, b"DISC" + pkt[4:10])
        elif pkt[:4] == b"CONN" and chr(pkt[10]) in self.allowed:
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
