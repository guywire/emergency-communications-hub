"""
tests/test_http_port_login.py
-----------------------------
With TLS on, the same app serves HTTPS (8765) and plain HTTP (8766, the port
the AREDN mesh can use — no encryption over amateur RF). The session cookie
was always marked Secure, so browsers dropped it on the HTTP port and every
login there bounced back to /login. Secure + HSTS now follow the request's
scheme.
"""

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
    await auth.init()  # default admin/admin
    return create_app(router, db, auth=auth, config_path=str(cfg), secure_cookies=True), router


def _client(app, base_url):
    from httpx import AsyncClient, ASGITransport
    return AsyncClient(transport=ASGITransport(app=app, client=("192.168.6.50", 50000)), base_url=base_url)


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,secure", [("http://test:8766", False), ("https://test:8765", True)])
async def test_cookie_secure_and_hsts_follow_scheme(tmp_path, base_url, secure):
    app, _router = await _make_app(tmp_path)
    async with _client(app, base_url) as client:
        r = await client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200
        set_cookie = r.headers["set-cookie"].lower()
        assert ("; secure" in set_cookie) is secure
        assert ("strict-transport-security" in r.headers) is secure
