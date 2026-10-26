"""
tests/test_admin_networks.py
----------------------------
O182: server.admin_networks limits which source networks can reach admin
pages/APIs at all — e.g. the home LAN can, the wider AREDN mesh (reaching the
web UI through enp4s0.11) can't, even with a valid admin login.
"""

import pytest

LAN = ("192.168.6.50", 50000)
MESH = ("10.44.1.2", 50000)


async def _make_app(tmp_path, admin_networks=None):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.core.auth import AuthManager
    from ech.api.app import create_app

    cfg = tmp_path / "config.yaml"
    server = {"host": "0.0.0.0", "port": 8765}
    if admin_networks is not None:
        server["admin_networks"] = admin_networks
    import yaml
    cfg.write_text(yaml.safe_dump({"server": server}), encoding="utf-8")

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    auth = AuthManager(db)
    await auth.init()  # creates default admin/admin
    app = create_app(router, db, auth=auth, config_path=str(cfg))
    return app, router, db, auth


def _client(app, client_addr):
    from httpx import AsyncClient, ASGITransport
    return AsyncClient(transport=ASGITransport(app=app, client=client_addr), base_url="http://test")


async def _login_admin(client):
    await client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    await client.post("/api/auth/change-password",
                      json={"new_password": "a-new-admin-password", "confirm_password": "a-new-admin-password",
                            "accept_legal": True})
    await client.post("/api/auth/login", json={"username": "admin", "password": "a-new-admin-password"})


@pytest.mark.asyncio
async def test_admin_api_refused_from_mesh_even_for_admin(tmp_path):
    app, router, db, auth = await _make_app(tmp_path, ["192.168.6.0/24", "127.0.0.0/8"])
    async with _client(app, MESH) as client:
        await _login_admin(client)
        r = await client.get("/api/system/audit-log")
        assert r.status_code == 403
        assert "network" in r.json()["detail"]
        r = await client.get("/settings", follow_redirects=False)
        assert r.status_code == 302
        assert "access=denied" in r.headers["location"]
    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_admin_api_allowed_from_lan(tmp_path):
    app, router, db, auth = await _make_app(tmp_path, ["192.168.6.0/24", "127.0.0.0/8"])
    async with _client(app, LAN) as client:
        await _login_admin(client)
        r = await client.get("/api/system/audit-log")
        assert r.status_code == 200
    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_mesh_keeps_non_admin_access(tmp_path):
    app, router, db, auth = await _make_app(tmp_path, ["192.168.6.0/24"])
    async with _client(app, MESH) as client:
        await _login_admin(client)
        r = await client.get("/api/health")
        assert r.status_code == 200
        # read-only adapter GETs stay available, same as for operators
        r = await client.get("/api/adapters/none/health")
        assert r.status_code != 403
    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_unset_admin_networks_means_no_restriction(tmp_path):
    app, router, db, auth = await _make_app(tmp_path)
    async with _client(app, MESH) as client:
        await _login_admin(client)
        r = await client.get("/api/system/audit-log")
        assert r.status_code == 200
    await router.stop()
    await db.close()
