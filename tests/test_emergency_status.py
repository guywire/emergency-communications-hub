"""
tests/test_emergency_status.py
---------------------------------
Covers O93: operator-entered local emergency status board (hospital beds,
shelters, vehicles, etc.) — the database CRUD layer and the mesh bot
`status` query command. No external API integration exists for this (see
the module docstrings / ECH_REQUIREMENTS_AND_PROGRESS.md O93 for why) —
this is purely operator-entered data.
"""

import pytest

from ech.core.database import Database
from ech.core.mesh_bot import MeshBot


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_add_and_get_emergency_status(db):
    entry_id = await db.add_emergency_status(
        category="hospital_beds", name="Maine Medical Center",
        status="open", details="12 beds available", updated_by="KN0O",
    )
    assert entry_id > 0
    entries = await db.get_emergency_status()
    assert len(entries) == 1
    e = entries[0]
    assert e["name"] == "Maine Medical Center"
    assert e["status"] == "open"
    assert e["updated_by"] == "KN0O"
    assert e["created_at"] == e["updated_at"]  # not yet updated


@pytest.mark.asyncio
async def test_get_emergency_status_filters_by_category(db):
    await db.add_emergency_status(category="hospital_beds", name="A", status="open")
    await db.add_emergency_status(category="warming_shelter", name="B", status="closed")
    hospitals = await db.get_emergency_status(category="hospital_beds")
    assert len(hospitals) == 1
    assert hospitals[0]["name"] == "A"


@pytest.mark.asyncio
async def test_update_emergency_status_bumps_updated_at(db):
    entry_id = await db.add_emergency_status(category="vehicle", name="Engine 3", status="available")
    before = (await db.get_emergency_status())[0]

    ok = await db.update_emergency_status(entry_id, status="unavailable", details="in for repair")
    assert ok is True

    after = (await db.get_emergency_status())[0]
    assert after["status"] == "unavailable"
    assert after["details"] == "in for repair"
    assert after["updated_at"] >= before["updated_at"]
    assert after["created_at"] == before["created_at"]  # unchanged


@pytest.mark.asyncio
async def test_update_emergency_status_rejects_unknown_fields_only(db):
    entry_id = await db.add_emergency_status(category="vehicle", name="Engine 3")
    # Only disallowed/empty fields -> no-op, reports False
    ok = await db.update_emergency_status(entry_id, not_a_real_field="x")
    assert ok is False


@pytest.mark.asyncio
async def test_update_emergency_status_missing_id_returns_false(db):
    ok = await db.update_emergency_status(99999, status="open")
    assert ok is False


@pytest.mark.asyncio
async def test_delete_emergency_status(db):
    entry_id = await db.add_emergency_status(category="vehicle", name="Engine 3")
    assert await db.delete_emergency_status(entry_id) is True
    assert await db.get_emergency_status() == []
    assert await db.delete_emergency_status(entry_id) is False  # already gone


@pytest.mark.asyncio
async def test_emergency_status_with_coordinates(db):
    await db.add_emergency_status(category="warming_shelter", name="High School Gym",
                                  status="open", lat=44.16, lon=-69.12)
    e = (await db.get_emergency_status())[0]
    assert e["lat"] == 44.16
    assert e["lon"] == -69.12


# ── mesh bot 'status' command ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bot_status_no_entries(db):
    bot = MeshBot({"mesh_bot": {"enabled": True}}, db=db)
    reply = await bot._cmd_status("")
    assert "no status board entries" in reply.lower()


@pytest.mark.asyncio
async def test_bot_status_lists_entries(db):
    await db.add_emergency_status(category="hospital_beds", name="Maine Medical", status="open")
    await db.add_emergency_status(category="warming_shelter", name="High School", status="closed")
    bot = MeshBot({"mesh_bot": {"enabled": True}}, db=db)
    reply = await bot._cmd_status("")
    assert "Maine Medical:open" in reply
    assert "High School:closed" in reply


@pytest.mark.asyncio
async def test_bot_status_filters_by_category_arg(db):
    await db.add_emergency_status(category="hospital_beds", name="Maine Medical", status="open")
    await db.add_emergency_status(category="warming_shelter", name="High School", status="closed")
    bot = MeshBot({"mesh_bot": {"enabled": True}}, db=db)
    reply = await bot._cmd_status("hospital")
    assert "Maine Medical" in reply
    assert "High School" not in reply


@pytest.mark.asyncio
async def test_bot_status_unknown_category_arg(db):
    await db.add_emergency_status(category="hospital_beds", name="Maine Medical", status="open")
    bot = MeshBot({"mesh_bot": {"enabled": True}}, db=db)
    reply = await bot._cmd_status("nonexistent_category")
    assert "no entries matching" in reply.lower()


@pytest.mark.asyncio
async def test_bot_status_without_db():
    bot = MeshBot({"mesh_bot": {"enabled": True}})  # no db
    reply = await bot._cmd_status("")
    assert "not available" in reply.lower()
