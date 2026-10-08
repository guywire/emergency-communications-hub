"""
tests/test_retention_archive.py
--------------------------------
Covers O84/O85: per-adapter retention actually purging Meshtastic (not just
APRS/MeshCore), and purged messages being archived to a restorable dated
JSONL file before deletion, with the archive itself purged separately.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ech.core.database import Database
from ech.core.models import NormalizedMessage


@pytest.fixture
async def file_db(tmp_path):
    # In-memory connection (file-based aiosqlite connects hit an unrelated
    # WAL-checkpoint locking quirk on Windows dev boxes — not reproducible on
    # the Linux production target, and orthogonal to what's under test here),
    # but _archive_dir() is purely path-math off self._path, so point it at
    # tmp_path to exercise the real archive-file code path.
    db = Database(":memory:")
    await db.connect()
    db._path = str(tmp_path / "test.db")
    yield db
    await db.close()


def _msg(adapter: str, hours_old: float, body: str = "hello") -> NormalizedMessage:
    return NormalizedMessage(
        source_adapter=adapter,
        source_channel="general",
        from_id="N0CALL",
        body=body,
        timestamp=datetime.now(timezone.utc) - timedelta(hours=hours_old),
    )


@pytest.mark.asyncio
async def test_meshtastic_purges_when_configured(file_db):
    """Meshtastic must honor its own retention key, same as aprs/meshcore."""
    await file_db.save_message(_msg("meshtastic-usb", hours_old=40))
    await file_db.save_message(_msg("meshtastic-usb", hours_old=1))
    await file_db.save_message(_msg("meshcore", hours_old=40))

    deleted = await file_db.purge_old_messages({"meshtastic": 36, "meshcore": 36})

    assert deleted == 2
    cur = await file_db._db.execute("SELECT source_adapter FROM messages")
    remaining = {row[0] for row in await cur.fetchall()}
    assert remaining == {"meshtastic-usb"}  # only the 1h-old one survives


@pytest.mark.asyncio
async def test_purge_archives_before_delete(file_db):
    await file_db.save_message(_msg("meshtastic-usb", hours_old=40, body="old one"))

    deleted = await file_db.purge_old_messages({"meshtastic": 36})
    assert deleted == 1

    dates = await file_db.list_archive_dates()
    assert len(dates) == 1

    restored = await file_db.restore_archived_messages(dates[0])
    assert restored == 1

    cur = await file_db._db.execute("SELECT body FROM messages")
    rows = await cur.fetchall()
    assert rows and rows[0][0] == "old one"


@pytest.mark.asyncio
async def test_purge_without_archive_flag_skips_archive(file_db):
    await file_db.save_message(_msg("aprs", hours_old=40))
    await file_db.purge_old_messages({"aprs": 12}, archive=False)
    assert await file_db.list_archive_dates() == []


@pytest.mark.asyncio
async def test_archive_purge_respects_days(file_db):
    await file_db.save_message(_msg("meshcore", hours_old=100))
    await file_db.purge_old_messages({"meshcore": 36})
    [today_file] = file_db._archive_dir().glob("*.jsonl")

    # Backdate the archive file itself to simulate it being 60 days old.
    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    old_path = today_file.with_name(f"{old_date}.jsonl")
    today_file.rename(old_path)

    # A 90-day retention should leave it alone; a 30-day one should delete it.
    assert await file_db.purge_old_archive(days=90) == 0
    assert await file_db.list_archive_dates() == [old_date]
    assert await file_db.purge_old_archive(days=30) == 1
    assert await file_db.list_archive_dates() == []
