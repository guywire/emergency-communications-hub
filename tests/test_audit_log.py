"""
tests/test_audit_log.py
--------------------------
Covers SEC-14/SEC-15: the tamper-evident audit trail for authentication
and admin/service actions. Three layers: the hash-chain storage itself
(database.py), the record() convenience wrapper (audit.py), and the live
HTTP endpoints (auth login/logout/password-change/user-management/
service-restart/self-update all writing real entries).
"""

import pytest


@pytest.mark.asyncio
async def test_add_audit_entry_returns_expected_fields():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    entry = await db.add_audit_entry("alice", "admin", "10.0.0.5", "login", "", True)
    assert entry["username"] == "alice"
    assert entry["action"] == "login"
    assert entry["success"] is True
    assert entry["prev_hash"] == "0" * 64
    assert len(entry["entry_hash"]) == 64
    await db.close()


@pytest.mark.asyncio
async def test_hash_chain_links_consecutive_entries():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    first = await db.add_audit_entry("alice", "admin", "10.0.0.5", "login", "", True)
    second = await db.add_audit_entry("alice", "admin", "10.0.0.5", "logout", "", True)
    assert second["prev_hash"] == first["entry_hash"]
    await db.close()


@pytest.mark.asyncio
async def test_get_audit_log_returns_newest_first():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    await db.add_audit_entry("alice", "admin", "1.1.1.1", "login", "", True)
    await db.add_audit_entry("bob", "operator", "2.2.2.2", "login", "", True)
    entries = await db.get_audit_log()
    assert len(entries) == 2
    assert entries[0]["username"] == "bob"  # most recent first
    await db.close()


@pytest.mark.asyncio
async def test_get_audit_log_filters_by_username_and_action():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    await db.add_audit_entry("alice", "admin", "1.1.1.1", "login", "", True)
    await db.add_audit_entry("alice", "admin", "1.1.1.1", "logout", "", True)
    await db.add_audit_entry("bob", "operator", "2.2.2.2", "login", "", True)

    by_user = await db.get_audit_log(username="alice")
    assert len(by_user) == 2
    assert all(e["username"] == "alice" for e in by_user)

    by_action = await db.get_audit_log(action="login")
    assert len(by_action) == 2
    assert all(e["action"] == "login" for e in by_action)
    await db.close()


@pytest.mark.asyncio
async def test_verify_audit_chain_intact_on_untouched_log():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    for i in range(5):
        await db.add_audit_entry(f"user{i}", "operator", "1.1.1.1", "login", "", True)
    result = await db.verify_audit_chain()
    assert result["intact"] is True
    assert result["checked"] == 5
    assert result["break_at_id"] is None
    await db.close()


@pytest.mark.asyncio
async def test_verify_audit_chain_detects_tampering():
    """The actual point of hash-chaining — directly editing a row's
    content without recomputing hashes must be detectable."""
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    await db.add_audit_entry("alice", "admin", "1.1.1.1", "login", "", True)
    await db.add_audit_entry("bob", "operator", "2.2.2.2", "login", "", True)
    await db.add_audit_entry("carol", "operator", "3.3.3.3", "login", "", True)

    # Tamper with the middle row's action, as if someone edited the DB file
    # directly to cover their tracks, without recomputing the hash chain.
    await db._db.execute("UPDATE audit_log SET action='logout' WHERE username='bob'")
    await db._db.commit()

    result = await db.verify_audit_chain()
    assert result["intact"] is False
    assert result["break_at_id"] is not None
    await db.close()


@pytest.mark.asyncio
async def test_audit_record_wrapper_never_raises_on_db_failure():
    """A broken audit write must never break the action it's auditing."""
    from ech.core.audit import record

    class _BrokenDB:
        async def add_audit_entry(self, **kwargs):
            raise RuntimeError("disk full")

    class _FakeClient:
        host = "9.9.9.9"

    class _FakeRequest:
        client = _FakeClient()

    await record(_BrokenDB(), _FakeRequest(), "alice", "admin", "login")  # must not raise


@pytest.mark.asyncio
async def test_audit_record_wrapper_noop_on_missing_db():
    from ech.core.audit import record

    class _FakeRequest:
        client = None

    await record(None, _FakeRequest(), "alice", "admin", "login")  # must not raise


# ── Live endpoint coverage ──────────────────────────────────────────────────

async def _make_app(tmp_path):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.core.auth import AuthManager
    from ech.api.app import create_app

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    auth = AuthManager(db)
    await auth.init()  # creates default admin/admin
    app = create_app(router, db, auth=auth)
    return app, router, db, auth


@pytest.mark.asyncio
async def test_login_success_and_failure_both_audited(tmp_path):
    from httpx import AsyncClient, ASGITransport
    app, router, db, auth = await _make_app(tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

        r = await client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200

    entries = await db.get_audit_log(action="login")
    assert len(entries) == 2
    successes = {bool(e["success"]) for e in entries}
    assert successes == {True, False}

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_logout_is_audited(tmp_path):
    from httpx import AsyncClient, ASGITransport
    app, router, db, auth = await _make_app(tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        r = await client.post("/api/auth/logout")
        assert r.status_code == 200

    entries = await db.get_audit_log(action="logout")
    assert len(entries) == 1
    assert entries[0]["username"] == "admin"

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_audit_log_endpoint_requires_admin(tmp_path):
    from httpx import AsyncClient, ASGITransport
    app, router, db, auth = await _make_app(tmp_path)
    await auth.create_user("op1", "operatorpass123", role="operator")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "op1", "password": "operatorpass123"})
        r = await client.get("/api/system/audit-log")
        assert r.status_code == 403

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_security_headers_present_on_every_response(tmp_path):
    from httpx import AsyncClient, ASGITransport
    app, router, db, auth = await _make_app(tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/health")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert "frame-ancestors 'self'" in r.headers.get("content-security-policy", "")
        assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    await router.stop()
    await db.close()


@pytest.mark.asyncio
async def test_audit_log_endpoint_returns_entries_for_admin(tmp_path):
    from httpx import AsyncClient, ASGITransport
    app, router, db, auth = await _make_app(tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        # Default admin/admin is created with must_change_pw=True (SEC-01) —
        # every other endpoint 403s until that's cleared. Changing the
        # password also invalidates the just-used session (SEC-06), so
        # log in again with the new password afterward.
        await client.post("/api/auth/change-password",
                           json={"new_password": "a-new-admin-password", "confirm_password": "a-new-admin-password"})
        await client.post("/api/auth/login", json={"username": "admin", "password": "a-new-admin-password"})

        r = await client.get("/api/system/audit-log")
        assert r.status_code == 200
        body = r.json()
        assert any(e["action"] == "login" for e in body["entries"])
        assert any(e["action"] == "password_change" for e in body["entries"])

        v = await client.get("/api/system/audit-log/verify")
        assert v.status_code == 200
        assert v.json()["intact"] is True

    await router.stop()
    await db.close()
