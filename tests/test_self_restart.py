"""
tests/test_self_restart.py
-----------------------------
Real incident, 2026-10-09: clicking "Restart ECH" (POST /api/system/
services/ech/restart) used to await `sudo systemctl restart ech`
completing — but that command SIGTERMs the very process handling this
request. The handler died before it could ever send a response, so the
browser's fetch() saw the connection just drop mid-flight (surfaced as a
confusing "unexpected end of file"/JSON-parse-style error), even though
the restart itself worked fine. Not a real config.yaml corruption — a
self-referential restart hazard, the same class self_update.py's own
module docstring already documents for its own restart call.

Fix: special-case service == "ech" to fire-and-forget a detached
subprocess (exactly like self_update.py's _run_update()) and respond
immediately, instead of awaiting its own death.
"""

import pytest


async def _make_app(tmp_path, monkeypatch):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("adapters: []\n")

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    app = create_app(router, db)
    return app, router, db


@pytest.mark.asyncio
async def test_restarting_ech_itself_does_not_await_the_subprocess(tmp_path, monkeypatch):
    """The actual regression test: restarting 'ech' must return promptly
    and must NOT call communicate()/wait() on the spawned process — doing
    so is exactly what used to block this handler until the process
    restarting itself got killed."""
    import asyncio
    from httpx import AsyncClient, ASGITransport

    app, router, db = await _make_app(tmp_path, monkeypatch)

    create_calls = []
    communicate_called = {"value": False}

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            communicate_called["value"] = True
            return b"", b""
        async def wait(self):
            communicate_called["value"] = True
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        create_calls.append((args, kwargs))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/system/services/ech/restart")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"

    assert not communicate_called["value"], (
        "restart_service() awaited the self-restart subprocess's exit — "
        "this is exactly the bug that caused the live incident"
    )
    assert len(create_calls) == 1
    args, kwargs = create_calls[0]
    assert args == ("sudo", "-n", "systemctl", "restart", "ech")
    assert kwargs.get("start_new_session") is True

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_restarting_other_services_still_awaits_and_reports_result(tmp_path, monkeypatch):
    """Non-self-referential services (pat, asterisk, etc.) are safe to
    await — restarting them can't kill the process handling the request —
    and this path should keep reporting a real success/failure result."""
    import asyncio
    from httpx import AsyncClient, ASGITransport

    app, router, db = await _make_app(tmp_path, monkeypatch)

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return b"restarted pat\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/system/services/pat/restart")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["stdout"] == "restarted pat\n"

    await router.stop()
    await db.close()
