"""Per-user mutes (adapters and individual channels), saved to the account."""
import pytest


async def _app():
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.core.auth import AuthManager
    from ech.api.app import create_app
    db = Database(":memory:")
    await db.connect()
    auth = AuthManager(db)
    await auth.create_user("op", "pw-123456", "operator")
    await auth.create_user("view", "pw-123456", "viewer")
    return create_app(Router(db, anomaly_engine=AnomalyEngine({})), db, auth=auth), auth


async def _client(app, auth, user):
    from httpx import AsyncClient, ASGITransport
    from ech.core.auth import SESSION_COOKIE
    return AsyncClient(transport=ASGITransport(app=app, client=("192.168.1.50", 5000)), base_url="http://test",
                       cookies={SESSION_COOKIE: await auth.login(user, "pw-123456")})


@pytest.mark.asyncio
async def test_mutes_are_saved_per_user():
    app, auth = await _app()
    async with await _client(app, auth, "op") as op, await _client(app, auth, "view") as view:
        assert (await op.get("/api/user/muted")).json() == {"adapters": [], "channels": [], "saved": False}
        r = await op.put("/api/user/muted", json={"adapters": ["aprs-internet"],
                                                   "channels": ["meshtastic-usb|LongFast", "meshcore-usb|#test"]})
        assert r.status_code == 200
        d = (await op.get("/api/user/muted")).json()
        assert d["saved"] and d["adapters"] == ["aprs-internet"]
        assert d["channels"] == ["meshcore-usb|#test", "meshtastic-usb|LongFast"]
        # another user's mutes are their own, and viewers may set theirs
        assert (await view.get("/api/user/muted")).json()["saved"] is False
        assert (await view.put("/api/user/muted", json={"channels": ["irc-libera|#chat"]})).status_code == 200
        assert (await view.get("/api/user/muted")).json()["channels"] == ["irc-libera|#chat"]
        assert (await op.get("/api/user/muted")).json()["adapters"] == ["aprs-internet"]
        # unmuting everything is a saved, empty set (not "never saved")
        await op.put("/api/user/muted", json={"adapters": [], "channels": []})
        assert (await op.get("/api/user/muted")).json() == {"adapters": [], "channels": [], "saved": True}


@pytest.mark.asyncio
async def test_bad_mutes_are_refused():
    app, auth = await _app()
    async with await _client(app, auth, "op") as op:
        assert (await op.put("/api/user/muted", json={"channels": ["no-separator"]})).status_code == 400
        assert (await op.put("/api/user/muted", json={"adapters": "aprs"})).status_code == 400
        assert (await op.put("/api/user/muted", json={"channels": [f"a|{i}" for i in range(501)]})).status_code == 400
