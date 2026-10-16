"""Server-side node position history behind the map's 7-day tracks."""
import asyncio
import time

import aiosqlite

from ech.core import database as _dbmod
from ech.core.database import Database
from ech.core.track_history import TrackHistory, downsample, point_info


class _Router:
    def __init__(self):
        self._adapters = {"aprs": object()}
        self.nodes = []

    async def nodes_for(self, name):
        return list(self.nodes)


async def _open(path):
    """Database.connect() trips a py3.12 'table is locked' on its WAL PRAGMA in
    this harness; the position table doesn't need the full schema."""
    sql = _dbmod.SCHEMA
    a = sql.index("CREATE TABLE IF NOT EXISTS node_positions")
    b = sql.index("CREATE TABLE IF NOT EXISTS anomaly_findings")
    db = Database(path)
    db._db = await aiosqlite.connect(str(path))
    await db._db.executescript(sql[a:b])
    return db


def _run(coro):
    return asyncio.run(coro)


def test_records_only_moved_nodes_and_serves_tracks(tmp_path):
    async def go():
        db = await _open(tmp_path / "t.db")
        r = _Router()
        th = TrackHistory(r, db, {})
        t0 = time.time()
        r.nodes = [{"node_id": "A", "lat": 44.0, "lon": -69.0}, {"node_id": "B", "lat": 43.0, "lon": -70.0}]
        assert await th.sample_once(t0) == 2
        # too soon -> nothing; later but A jitters 5 m and B moves 1 km -> only B
        assert await th.sample_once(t0 + 10) == 0
        r.nodes = [{"node_id": "A", "lat": 44.00004, "lon": -69.0}, {"node_id": "B", "lat": 43.01, "lon": -70.0}]
        assert await th.sample_once(t0 + 120) == 1
        rows = await db.get_tracks(t0 - 1)
        assert sorted({x[0] for x in rows}) == ["A", "B"]
        assert [x[0] for x in rows].count("B") == 2
        assert {x[5] for x in rows} == {"aprs"}   # adapter comes back for the map
        # purge removes everything older than keep_days
        await db._db.execute("UPDATE node_positions SET ts = ts - ?", (8 * 86400,))
        await db._db.commit()
        assert await db.purge_positions(7) == 3
        await db._db.close()
    _run(go())


def test_zero_zero_and_missing_positions_ignored(tmp_path):
    async def go():
        db = await _open(tmp_path / "t.db")
        r = _Router()
        r.nodes = [{"node_id": "Z", "lat": 0, "lon": 0}, {"node_id": "N", "lat": None, "lon": None}]
        assert await TrackHistory(r, db, {}).sample_once() == 0
        await db._db.close()
    _run(go())


def test_downsample_keeps_ends():
    pts = [[i, 0.0, 0.0] for i in range(10000)]
    out = downsample(pts, 100)
    assert len(out) == 100 and out[0] == pts[0] and out[-1] == pts[-1]
    assert downsample(pts[:50], 100) == pts[:50]


def test_point_info_snapshot(tmp_path):
    async def go():
        db = await _open(tmp_path / "t.db")
        r = _Router()
        r.nodes = [{"node_id": "icao:abc", "lat": 44.0, "lon": -69.0, "snr": 7.5,
                    "meta": {"altitude_ft": 12000, "speed_kts": 310, "junk": [1, 2]}}]
        assert await TrackHistory(r, db, {}).sample_once() == 1
        rows = await db.get_tracks(0)
        import json
        assert json.loads(rows[0][4]) == {"snr": 7.5, "altitude_ft": 12000, "speed_kts": 310}
        await db._db.close()
    _run(go())
    assert point_info({"lat": 1, "lon": 2}) is None
