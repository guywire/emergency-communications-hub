"""
tests/test_aredn_discovery_endpoint.py
-----------------------------------------
Covers GET /api/pbx/aredn-discovery — the live-query endpoint wrapping
ech/core/aredn_service_discovery.py. Kept separate from
GET /api/pbx/directory's own tests since this one does real network I/O
and has its own not-configured/error/ok states.
"""

import yaml
import httpx
import pytest


def _mock_client_factory(handler):
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _MockAsyncClient


async def _make_app(tmp_path, monkeypatch, config_extra=None):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app

    cfg = {"adapters": []}
    if config_extra:
        cfg.update(config_extra)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    app = create_app(router, db, config_path=str(cfg_path))
    return app, router, db


@pytest.mark.asyncio
async def test_aredn_discovery_not_configured(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db = await _make_app(tmp_path, monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/pbx/aredn-discovery")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "not_configured"

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_aredn_discovery_ok(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db = await _make_app(tmp_path, monkeypatch, config_extra={
        "radio_directory": {"aredn_discovery": {"sysinfo_url": "http://10.0.0.1/cgi-bin/sysinfo.json"}}
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "node": "TEST-SUPERNODE",
            "services": [
                {"name": "Test PBX [phone]", "ip": "10.1.1.1", "link": "sip://testnode:5060/", "protocol": "tcp"},
                {"name": "Some Camera [camera]", "ip": "10.1.1.2", "link": "http://cam:80/", "protocol": "tcp"},
            ],
            "services_local": [],
        })

    import ech.core.aredn_service_discovery as discovery_mod
    monkeypatch.setattr(discovery_mod.httpx, "AsyncClient", _mock_client_factory(handler))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/pbx/aredn-discovery")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["node"] == "TEST-SUPERNODE"
        assert len(body["entries"]) == 1
        assert body["entries"][0]["destination"] == "SIP/testnode:5060"

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_aredn_discovery_network_error(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db = await _make_app(tmp_path, monkeypatch, config_extra={
        "radio_directory": {"aredn_discovery": {"sysinfo_url": "http://10.0.0.1/cgi-bin/sysinfo.json"}}
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    import ech.core.aredn_service_discovery as discovery_mod
    monkeypatch.setattr(discovery_mod.httpx, "AsyncClient", _mock_client_factory(handler))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/pbx/aredn-discovery")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "error"

    await router.stop()
    await db.close()
