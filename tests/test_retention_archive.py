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
async def test_restored_day_survives_next_purge(file_db):
    """A restored day older than the retention window must not be purged
    (and re-archived) again by the very next hourly sweep."""
    await file_db.save_message(_msg("meshcore-usb", hours_old=300, body="old mesh"))
    assert await file_db.purge_old_messages({"meshcore": 240}) == 1
    [date] = await file_db.list_archive_dates()
    archive = file_db._archive_dir() / f"{date}.jsonl"
    lines_before = archive.read_text().count("\n")

    assert await file_db.restore_archived_messages(date) == 1
    assert await file_db.purge_old_messages({"meshcore": 240}) == 0

    cur = await file_db._db.execute("SELECT body FROM messages")
    assert [r[0] for r in await cur.fetchall()] == ["old mesh"]
    assert archive.read_text().count("\n") == lines_before


@pytest.mark.asyncio
async def test_restored_rows_expire_after_window_from_restore(file_db):
    """Once the retention window has passed since the restore, the row goes —
    without another copy being appended to the archive file."""
    await file_db.save_message(_msg("meshcore-usb", hours_old=300))
    await file_db.purge_old_messages({"meshcore": 240})
    [date] = await file_db.list_archive_dates()
    archive = file_db._archive_dir() / f"{date}.jsonl"
    lines_before = archive.read_text().count("\n")
    await file_db.restore_archived_messages(date)

    long_ago = (datetime.now(timezone.utc) - timedelta(hours=241)).isoformat()
    await file_db._db.execute("UPDATE messages SET restored_at = ?", (long_ago,))
    assert await file_db.purge_old_messages({"meshcore": 240}) == 1
    assert archive.read_text().count("\n") == lines_before


@pytest.mark.asyncio
async def test_get_messages_before_pages_back_to_restored_day(file_db):
    """The feed's "Load older" pages with before=<oldest shown>; a restored
    day well past the feed's 24h window must come back that way."""
    await file_db.save_message(_msg("meshcore-usb", hours_old=1, body="recent"))
    await file_db.save_message(_msg("meshcore-usb", hours_old=400, body="restored day"))
    await file_db.save_message(_msg("aprs-internet", hours_old=300, body="aprs"))

    recent = await file_db.get_messages(limit=500, adapter="meshcore-usb")
    oldest_shown = recent[0]["timestamp"]
    older = await file_db.get_messages(limit=500, adapter="meshcore-usb", before=oldest_shown)
    assert [m["body"] for m in older] == ["restored day"]


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
