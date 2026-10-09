"""
tests/test_adapter_gui_editor.py
-----------------------------------
Covers the Settings GUI adapter/GPS editor backend: listing attached
serial devices, listing known adapter types (read from main.py's own
dispatch tables, not a duplicated list), and reading/writing the gps:
config block.
"""

import yaml
import pytest


async def _make_app(tmp_path, config_extra=None, monkeypatch=None):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app

    # /api/system/gps-config (like /api/adapter-config) falls back to a
    # relative "config.yaml" when /etc/ech/config.yaml doesn't exist — on a
    # dev machine without that path, it would otherwise pick up THIS
    # repo's real config.yaml sitting in cwd instead of the test's tmp one.
    if monkeypatch:
        monkeypatch.chdir(tmp_path)

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
    return app, router, db, cfg_path


@pytest.mark.asyncio
async def test_adapter_types_matches_main_dispatch_tables(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from ech.main import MOCK_ADAPTER_TYPES, REAL_ADAPTER_TYPES
    app, router, db, _ = await _make_app(tmp_path, monkeypatch=monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/system/adapter-types")
        assert r.status_code == 200
        body = r.json()
        assert set(body["types"]) == set(REAL_ADAPTER_TYPES)
        assert set(body["mock_types"]) == set(MOCK_ADAPTER_TYPES)
        assert "meshcore" in body["types"]
        assert "mock_meshtastic" in body["mock_types"]

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_serial_ports_returns_a_list(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db, _ = await _make_app(tmp_path, monkeypatch=monkeypatch)

    class _FakePortInfo:
        device = "/dev/ttyUSB0"
        description = "USB-Serial Controller"
        hwid = "USB VID:PID=1234:5678"
        manufacturer = "Acme"
        product = "Widget"
        serial_number = "SN123"
        vid = 0x1234
        pid = 0x5678

    import serial.tools.list_ports as list_ports_mod
    monkeypatch.setattr(list_ports_mod, "comports", lambda: [_FakePortInfo()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/system/serial-ports")
        assert r.status_code == 200
        ports = r.json()["ports"]
        assert len(ports) == 1
        assert ports[0]["device"] == "/dev/ttyUSB0"
        assert ports[0]["description"] == "USB-Serial Controller"
        assert ports[0]["vid"] == 0x1234

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_gps_config_defaults_to_disabled(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db, _ = await _make_app(tmp_path, monkeypatch=monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/system/gps-config")
        assert r.status_code == 200
        assert r.json() == {"enabled": False}

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_gps_config_save_and_read_back(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db, cfg_path = await _make_app(tmp_path, monkeypatch=monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/system/gps-config", json={
            "enabled": True, "port": "/dev/ttyUSB0", "baud": 9600,
            "min_satellites": 5, "update_interval": 15, "time_sync": True,
        })
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        r2 = await client.get("/api/system/gps-config")
        body = r2.json()
        assert body["enabled"] is True
        assert body["port"] == "/dev/ttyUSB0"
        assert body["baud"] == 9600
        assert body["min_satellites"] == 5
        assert body["time_sync"] is True

    on_disk = yaml.safe_load(cfg_path.read_text())
    assert on_disk["gps"]["port"] == "/dev/ttyUSB0"

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_gps_config_disable_removes_block(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db, cfg_path = await _make_app(tmp_path, monkeypatch=monkeypatch, config_extra={
        "gps": {"port": "/dev/ttyUSB0", "baud": 9600},
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/system/gps-config")
        assert r.json()["enabled"] is True

        r2 = await client.post("/api/system/gps-config", json={"enabled": False})
        assert r2.json()["status"] == "ok"

        r3 = await client.get("/api/system/gps-config")
        assert r3.json() == {"enabled": False}

    on_disk = yaml.safe_load(cfg_path.read_text())
    assert "gps" not in on_disk

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_gps_config_requires_port_when_enabling(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    app, router, db, _ = await _make_app(tmp_path, monkeypatch=monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/system/gps-config", json={"enabled": True, "port": ""})
        assert r.json()["status"] == "error"

    await router.stop()
    await db.close()
