"""
tests/test_m17_server_switch.py
--------------------------------
Covers O99: letting an operator switch an M17 reflector adapter to a
different server/module from the UI (POST /api/adapters/{name}/m17/server)
instead of requiring a config.yaml edit + manual restart. Mirrors the
generic /api/adapters/{name}/reconnect pattern already used elsewhere.
"""

import yaml
import pytest

from ech.adapters.m17_reflector import M17ReflectorAdapter


class _NoNetworkM17Adapter(M17ReflectorAdapter):
    """Same class (so isinstance() checks pass) but connect()/disconnect()
    never touch a real socket, so tests don't depend on network/timing."""

    async def connect(self) -> None:
        self._connected = False  # never actually "connects" — receive() exits immediately

    async def disconnect(self) -> None:
        self._connected = False


def _m17_cfg(**overrides):
    cfg = {
        "type": "m17_reflector", "name": "m17-test",
        "reflector_host": "old.reflector.example", "reflector_port": 17000,
        "module": "A", "callsign": "N0CALL", "listen_only": True,
    }
    cfg.update(overrides)
    return cfg


def _make_config(tmp_path, adapters):
    cfg = {"adapters": adapters}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg))
    return path


@pytest.mark.asyncio
async def test_set_m17_server_updates_config_and_reconnects(tmp_path, monkeypatch):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    adapters_cfg = [{
        "type": "m17_reflector",
        "name": "m17-test",
        "reflector_host": "old.reflector.example",
        "reflector_port": 17000,
        "module": "A",
        "callsign": "N0CALL",
        "listen_only": True,
    }]
    cfg_path = _make_config(tmp_path, adapters_cfg)
    monkeypatch.chdir(tmp_path)

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()

    existing = _NoNetworkM17Adapter(_m17_cfg())
    await router.start_adapter(existing)

    def fake_build_adapter(adapter_cfg):
        return _NoNetworkM17Adapter(adapter_cfg)
    import ech.main
    monkeypatch.setattr(ech.main, "build_adapter", fake_build_adapter)

    app = create_app(router, db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/adapters/m17-test/m17/server", json={
            "reflector_host": "new.reflector.example",
            "reflector_port": 17171,
            "module": "C",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["reflector_host"] == "new.reflector.example"
        assert body["reflector_port"] == 17171
        assert body["module"] == "C"

    on_disk = yaml.safe_load(cfg_path.read_text())
    updated = next(a for a in on_disk["adapters"] if a["name"] == "m17-test")
    assert updated["reflector_host"] == "new.reflector.example"
    assert updated["reflector_port"] == 17171
    assert updated["module"] == "C"

    new_adapter = router._adapters["m17-test"]
    assert new_adapter._host == "new.reflector.example"
    assert new_adapter._port == 17171
    assert new_adapter._module == "C"

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_set_m17_server_rejects_bad_module(tmp_path, monkeypatch):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    adapters_cfg = [{
        "type": "m17_reflector", "name": "m17-test",
        "reflector_host": "old.reflector.example", "reflector_port": 17000,
        "module": "A", "callsign": "N0CALL", "listen_only": True,
    }]
    _make_config(tmp_path, adapters_cfg)
    monkeypatch.chdir(tmp_path)

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()

    existing = _NoNetworkM17Adapter(_m17_cfg())
    await router.start_adapter(existing)

    app = create_app(router, db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/adapters/m17-test/m17/server", json={
            "reflector_host": "new.reflector.example",
            "module": "AB",  # invalid: must be a single letter
        })
        assert r.status_code == 200
        assert r.json()["status"] == "error"

        r2 = await client.post("/api/adapters/m17-test/m17/server", json={
            "module": "C",  # missing reflector_host
        })
        assert r2.json()["status"] == "error"

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_set_m17_server_rejects_non_m17_adapter(tmp_path, monkeypatch):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.adapters.base import Adapter
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    class _FakeOtherAdapter(Adapter):
        async def connect(self): self._connected = False
        async def disconnect(self): self._connected = False
        async def send(self, message): pass
        async def _run(self): pass

    monkeypatch.chdir(tmp_path)
    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()

    other = _FakeOtherAdapter({"name": "not-m17"})
    await router.start_adapter(other)

    app = create_app(router, db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/adapters/not-m17/m17/server", json={
            "reflector_host": "x", "module": "A",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "error"
        assert "not an M17" in body["detail"]

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_set_m17_server_404_on_unknown_adapter(tmp_path, monkeypatch):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    monkeypatch.chdir(tmp_path)
    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    app = create_app(router, db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/adapters/nope/m17/server", json={
            "reflector_host": "x", "module": "A",
        })
        assert r.status_code == 404

    await router.stop()
    await db.close()
