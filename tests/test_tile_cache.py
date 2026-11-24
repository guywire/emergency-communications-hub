"""Map tiles: offline file, cache, internet, then the mesh; peers get file/cache only."""
import asyncio
import os
import sqlite3
import time

import httpx

from ech.core.tile_cache import TileCache, image_type

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 50
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 50


class _R:
    def __init__(self, code, content):
        self.status_code, self.content = code, content


class FakeClient:
    """internet: what the tile server answers (None = no internet); peers: url prefix -> content."""
    def __init__(self, internet=None, peers=None):
        self.internet, self.peers, self.calls = internet, peers or {}, []

    async def get(self, url, **kw):
        self.calls.append(url)
        for prefix, content in self.peers.items():
            if url.startswith(prefix):
                return _R(200, content) if content else _R(404, b"")
        if self.internet is None:
            raise httpx.ConnectError("no route to host")
        return _R(200, self.internet)


def _cache(tmp_path, peers=(), **cfg):
    t = TileCache()
    t.configure(cfg, str(tmp_path), peers_fn=lambda: list(peers))
    return t


def test_image_types():
    assert image_type(PNG) == "image/png" and image_type(JPG) == "image/jpeg"
    assert image_type(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert image_type(b"<html>") is None and image_type(b"") is None
    assert image_type(b"\x89PNG\r\n\x1a\n" + b"\0" * 700_000) is None          # too big for a tile


def test_internet_then_cache(tmp_path):
    t, c = _cache(tmp_path), FakeClient(internet=PNG)
    assert asyncio.run(t.get(5, 3, 4, client=c)) == (PNG, "image/png")
    assert asyncio.run(t.get(5, 3, 4, client=FakeClient())) == (PNG, "image/png")   # second time: from disk
    assert t.stats["internet"] == 1 and t.stats["cache"] == 1
    assert c.calls == ["https://tile.openstreetmap.org/5/3/4.png"]


def test_offline_file_first(tmp_path):
    db = tmp_path / "area.mbtiles"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    con.execute("INSERT INTO tiles VALUES (2, 1, 0, ?)", (JPG,))       # row 0 from the south = y 3 at z2
    con.commit()
    con.close()
    t = _cache(tmp_path, mbtiles=str(db))
    c = FakeClient(internet=PNG)
    assert asyncio.run(t.get(2, 1, 3, client=c)) == (JPG, "image/jpeg") and not c.calls
    assert t.layer_info()["offline_file"] is True


def test_no_internet_asks_the_mesh_and_backs_off(tmp_path):
    t = _cache(tmp_path, peers=[("http://10.9.0.1:8088", "a:8088"), ("http://10.9.0.2:8088", "b:8088")])
    c = FakeClient(internet=None, peers={"http://10.9.0.1": None, "http://10.9.0.2": PNG})
    assert asyncio.run(t.get(6, 10, 20, client=c)) == (PNG, "image/png")
    assert c.calls[1:] == ["http://10.9.0.1:8088/peer/v1/tiles/6/10/20.png",
                           "http://10.9.0.2:8088/peer/v1/tiles/6/10/20.png"]
    assert t.stats["peer"] == 1
    c.calls.clear()
    asyncio.run(t.get(6, 11, 20, client=c))
    assert not any("openstreetmap" in u for u in c.calls)      # offline: the internet isn't tried again for 5 min


def test_peer_junk_is_not_cached(tmp_path):
    t = _cache(tmp_path, peers=[("http://10.9.0.1:8088", "a:8088")])
    c = FakeClient(internet=None, peers={"http://10.9.0.1": b"<html>not a tile</html>"})
    assert asyncio.run(t.get(6, 10, 20, client=c)) is None
    assert t.stats["missing"] == 1


def test_peers_get_only_file_and_cache(tmp_path):
    t = _cache(tmp_path)
    c = FakeClient(internet=PNG)
    assert asyncio.run(t.get(4, 1, 1, for_peer=True, client=c)) is None and not c.calls
    asyncio.run(t.get(4, 1, 1, client=c))                                    # someone here looked at it
    assert asyncio.run(t.get(4, 1, 1, for_peer=True, client=c)) == (PNG, "image/png")
    assert _cache(tmp_path, share=False).layer_info() and asyncio.run(
        _cache(tmp_path, share=False).get(4, 1, 1, for_peer=True, client=c)) is None


def test_stale_tiles_still_serve_when_offline(tmp_path):
    t = _cache(tmp_path, cache_days=1)
    asyncio.run(t.get(3, 1, 1, client=FakeClient(internet=PNG)))
    old = time.time() - 3 * 86400
    os.utime(t._path(3, 1, 1), (old, old))
    assert asyncio.run(t.get(3, 1, 1, client=FakeClient(internet=None))) == (PNG, "image/png")
    fresh = FakeClient(internet=JPG)
    t._offline_until = 0
    assert asyncio.run(t.get(3, 1, 1, client=fresh)) == (JPG, "image/jpeg")   # online again: refreshed


def test_bad_coordinates_and_pruning(tmp_path):
    t = _cache(tmp_path, cache_mb=10)
    assert asyncio.run(t.get(25, 0, 0, client=FakeClient(internet=PNG))) is None
    assert asyncio.run(t.get(2, 4, 0, client=FakeClient(internet=PNG))) is None
    t.max_bytes = 300
    for i in range(8):
        t._store(8, i, 0, PNG)
        os.utime(t._path(8, i, 0), (i, i))
    t.prune()
    left = sorted(int(os.path.basename(os.path.dirname(t._path(8, i, 0)))) for i in range(8)
                  if os.path.exists(t._path(8, i, 0)))
    assert left and left == list(range(8 - len(left), 8))                 # the oldest went first
