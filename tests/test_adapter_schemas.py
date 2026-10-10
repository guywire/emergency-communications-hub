"""
tests/test_adapter_schemas.py
---------------------------------
Covers the O109 follow-up (operator feedback, 2026-10-09): the adapter
editor let you add an adapter and a field but gave zero guidance on what
keys a type takes or what a sane default looks like. ech/core/
adapter_schemas.py is the fix — a curated per-type field schema, sourced
from each adapter's own "Config keys:" docstring, used to pre-fill real
defaults and power a real field picker instead of a blind prompt.
"""

import pytest

from ech.core.adapter_schemas import ADAPTER_SCHEMAS, get_schema
from ech.main import MOCK_ADAPTER_TYPES, REAL_ADAPTER_TYPES


def test_every_schema_key_is_a_real_adapter_type():
    """Guards against the schema module drifting from main.py's own
    dispatch tables — e.g. a typo'd type name that would silently never
    match anything."""
    known = set(REAL_ADAPTER_TYPES) | set(MOCK_ADAPTER_TYPES)
    for type_name in ADAPTER_SCHEMAS:
        assert type_name in known, f"{type_name!r} is not a real adapter type"


def test_every_field_has_required_shape():
    for type_name, schema in ADAPTER_SCHEMAS.items():
        for f in schema["fields"]:
            assert "key" in f and f["key"]
            assert f["kind"] in ("string", "number", "boolean", "select", "list")
            assert "required" in f
            assert "default" in f
            assert "help" in f and f["help"], f"{type_name}.{f['key']} has no help text"
            if f["kind"] == "select":
                assert f.get("options"), f"{type_name}.{f['key']} is kind=select with no options"
                assert f["default"] in f["options"]


def test_meshcore_schema_excludes_unimplemented_ble_transport():
    """meshcore.py's module docstring documents 'ble' as a transport
    option, but _make_transport() doesn't actually implement it (confirmed
    by reading the code) — the schema must not offer a choice that fails."""
    schema = get_schema("meshcore")
    transport_field = next(f for f in schema["fields"] if f["key"] == "transport")
    assert "ble" not in transport_field["options"]
    assert set(transport_field["options"]) == {"serial", "tcp", "browser"}


def test_meshtastic_schema_includes_ble_and_browser():
    """Unlike meshcore, meshtastic_adapter.py's transport dispatch really
    does support both — confirmed from its own module docstring."""
    schema = get_schema("meshtastic")
    transport_field = next(f for f in schema["fields"] if f["key"] == "transport")
    assert set(transport_field["options"]) == {"serial", "tcp", "ble", "browser"}


def test_get_schema_returns_none_for_unknown_type():
    assert get_schema("not_a_real_type") is None


def test_m17_reflector_schema_matches_confirmed_required_fields():
    schema = get_schema("m17_reflector")
    required = {f["key"] for f in schema["fields"] if f["required"]}
    assert required == {"reflector_host", "module", "callsign"}


@pytest.mark.asyncio
async def test_adapter_schema_endpoint_returns_curated_schema(tmp_path, monkeypatch):
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    app = create_app(router, db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/system/adapter-schema/meshcore")
        assert r.status_code == 200
        body = r.json()
        keys = {f["key"] for f in body["fields"]}
        assert "transport" in keys
        assert "channel_idx" in keys

        r2 = await client.get("/api/system/adapter-schema/not_a_real_type")
        assert r2.status_code == 200
        assert r2.json() == {"fields": []}

    await router.stop()
    await db.close()
