"""
tests/test_security_hardening.py
--------------------------------
Cross-site request refusal, /metrics behind login (except from this machine),
random first-admin password, and owner-only config.yaml writes.
"""

import os
import sys

import pytest


async def _make_app(tmp_path):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.core.auth import AuthManager
    from ech.api.app import create_app
    import yaml

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"server": {"host": "0.0.0.0", "port": 8765}}), encoding="utf-8")
    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    auth = AuthManager(db)
    await auth.init()
    return create_app(router, db, auth=auth, config_path=str(cfg)), router, db


def _client(app, host="192.168.6.50"):
    from httpx import AsyncClient, ASGITransport
    return AsyncClient(transport=ASGITransport(app=app, client=(host, 50000)),
                       base_url="http://ech.test:8766")


LOGIN = {"username": "admin", "password": "admin"}


@pytest.mark.asyncio
async def test_cross_site_post_refused(tmp_path):
    app, router, db = await _make_app(tmp_path)
    async with _client(app) as client:
        r = await client.post("/api/auth/login", json=LOGIN, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        r = await client.post("/api/auth/login", json=LOGIN, headers={"Origin": "null"})
        assert r.status_code == 403
        # Referer is the fallback when a browser leaves Origin off
        r = await client.post("/api/auth/login", json=LOGIN,
                              headers={"Referer": "http://evil.example/page"})
        assert r.status_code == 403
        # Same port, different host is still a different site
        r = await client.post("/api/auth/login", json=LOGIN, headers={"Origin": "http://ech.test:8765"})
        assert r.status_code == 403
    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_same_site_and_headerless_posts_allowed(tmp_path):
    app, router, db = await _make_app(tmp_path)
    async with _client(app) as client:
        r = await client.post("/api/auth/login", json=LOGIN, headers={"Origin": "http://ech.test:8766"})
        assert r.status_code == 200
        r = await client.post("/api/auth/login", json=LOGIN,
                              headers={"Referer": "http://ech.test:8766/login"})
        assert r.status_code == 200
        r = await client.post("/api/auth/login", json=LOGIN)   # curl / scripts
        assert r.status_code == 200
        # GETs are never refused for Origin
        r = await client.get("/api/health", headers={"Origin": "http://evil.example"})
        assert r.status_code == 200
    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_metrics_needs_login_except_loopback(tmp_path):
    app, router, db = await _make_app(tmp_path)
    async with _client(app, host="192.168.6.50") as client:
        r = await client.get("/metrics", follow_redirects=False)
        assert r.status_code in (302, 401)
    async with _client(app, host="127.0.0.1") as client:
        r = await client.get("/metrics")
        assert r.status_code == 200
    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_first_admin_password_is_random_and_saved(tmp_path, monkeypatch):
    from ech.core.database import Database
    from ech.core.auth import AuthManager
    monkeypatch.delenv("ECH_INITIAL_ADMIN_PASSWORD", raising=False)
    db = Database(":memory:")
    await db.connect()
    auth = AuthManager(db)
    pw_file = tmp_path / "initial-admin-password.txt"
    await auth.init(password_file=str(pw_file))

    assert await auth.login("admin", "admin") is None
    lines = dict(l.split(": ", 1) for l in pw_file.read_text().splitlines())
    assert lines["username"] == "admin"
    assert len(lines["password"]) >= 12
    assert await auth.login("admin", lines["password"])
    if sys.platform != "win32":
        assert (os.stat(pw_file).st_mode & 0o777) == 0o600
    await db.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_config_write_is_owner_only(tmp_path):
    from ech.api.app import atomic_write_yaml
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    os.chmod(p, 0o644)
    atomic_write_yaml(p, {"a": 2, "secret": "x"})
    assert (os.stat(p).st_mode & 0o777) == 0o600


# ── WebSockets ────────────────────────────────────────────────────────────
# (starlette's TestClient always sends Host: testserver on WebSocket requests)

class _FakeAuth:
    """Sessions by token; tests delete entries to simulate logout/expiry."""
    def __init__(self, sessions):
        self.sessions = sessions

    async def get_session(self, token):
        return self.sessions.get(token)

    async def require_session(self, request):
        return None


def _ws_app(tmp_path, sessions):
    from unittest.mock import MagicMock
    import yaml
    from ech.api import app as app_mod
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({}))
    auth = _FakeAuth(sessions)
    return app_mod.create_app(MagicMock(), MagicMock(), auth=auth, config_path=str(cfg)), auth


def _ws_client(app, token):
    from starlette.testclient import TestClient
    c = TestClient(app, base_url="http://ech.test")
    if token:
        c.cookies.set("ech_session", token)
    return c


def _closed_with(c, path, headers=None):
    from starlette.websockets import WebSocketDisconnect
    try:
        with c.websocket_connect(path, headers=headers or {}) as ws:
            ws.receive_text()
    except WebSocketDisconnect as exc:
        return exc.code
    return None


def test_ws_refuses_cross_site_and_no_session(tmp_path):
    app, _ = _ws_app(tmp_path, {"t1": {"username": "a", "role": "operator", "must_change_pw": False}})
    with _ws_client(app, "t1") as c:
        assert _closed_with(c, "/ws", {"Origin": "http://evil.example"}) == 4403
    with _ws_client(app, None) as c:
        assert _closed_with(c, "/ws", {"Origin": "http://testserver"}) == 4401


def test_ws_refuses_pending_password_change(tmp_path):
    app, _ = _ws_app(tmp_path, {"t1": {"username": "admin", "role": "admin", "must_change_pw": True}})
    with _ws_client(app, "t1") as c:
        assert _closed_with(c, "/ws", {"Origin": "http://testserver"}) == 4401


def test_ws_closes_when_session_ends(tmp_path, monkeypatch):
    import time
    from starlette.websockets import WebSocketDisconnect
    from ech.api import app as app_mod
    monkeypatch.setattr(app_mod, "WS_SESSION_RECHECK_SEC", 0.05)
    app, auth = _ws_app(tmp_path, {"t1": {"username": "a", "role": "operator", "must_change_pw": False}})
    with _ws_client(app, "t1") as c:
        with c.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
            auth.sessions.clear()          # logout / expiry / password change
            time.sleep(0.3)
            try:
                ws.receive_text()
                code = None
            except WebSocketDisconnect as exc:
                code = exc.code
    assert code == 4401


def test_remote_hw_refuses_viewer(tmp_path):
    app, _ = _ws_app(tmp_path, {
        "v": {"username": "v", "role": "viewer", "must_change_pw": False},
    })
    with _ws_client(app, "v") as c:
        assert _closed_with(c, "/ws/remote-hw", {"Origin": "http://testserver"}) == 4403
