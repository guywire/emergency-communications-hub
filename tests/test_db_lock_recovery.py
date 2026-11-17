"""A write that fails on "database is locked" must not wedge the connection.

2026-11-16: an outside script wrote to ech.db while ECH started. One ECH write
hit the lock, its implicit transaction stayed open on a now-stale WAL snapshot,
and every later write failed with "database is locked" until a restart.
"""
import asyncio
import sqlite3

from ech.core.database import Database


async def _log_rows(db):
    async with db._db.execute("SELECT COUNT(*) FROM log_entries") as cur:
        return (await cur.fetchone())[0]


def test_write_recovers_after_an_outside_writer_held_the_lock(tmp_path):
    path = tmp_path / "ech.db"

    async def go():
        db = Database(path)
        await db.connect()
        await db._db.execute("PRAGMA busy_timeout=100")   # keep the test fast
        await db.save_log_entry("INFO", "t", "before")

        outside = sqlite3.connect(path, timeout=0.1, isolation_level=None)
        outside.execute("BEGIN IMMEDIATE")                  # hold the write lock
        await db.save_log_entry("INFO", "t", "during")     # fails (swallowed, not raised)
        # Something else in ECH reads on the same connection while the outside
        # writer still holds the lock: without the fix this pins a snapshot inside
        # the failed write's still-open transaction.
        await _log_rows(db)
        outside.execute("INSERT INTO log_entries(timestamp,level,logger,message) VALUES('x','INFO','o','outside')")
        outside.execute("COMMIT")                           # DB moved on past our snapshot
        outside.close()

        await db.save_log_entry("INFO", "t", "after")       # must succeed again
        n = await _log_rows(db)
        await db.close()
        return n

    # before + outside + after ("during" may be lost; it hit the lock)
    assert asyncio.run(go()) >= 3


def test_failed_log_write_does_not_raise(tmp_path):
    path = tmp_path / "ech.db"

    async def go():
        db = Database(path)
        await db.connect()
        await db._db.execute("PRAGMA busy_timeout=50")
        outside = sqlite3.connect(path, timeout=0.1, isolation_level=None)
        outside.execute("BEGIN IMMEDIATE")
        try:
            await db.save_log_entry("ERROR", "t", "x")      # must not raise into the event loop
        finally:
            outside.execute("ROLLBACK")
            outside.close()
            await db.close()

    asyncio.run(go())
