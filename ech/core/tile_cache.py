"""
Map tiles for people on the mesh without internet.

The page asks this box for its map tiles (tiles/Z/X/Y.png), never the
internet directly. Each tile comes from the first of these that has it:

  1. an offline tile file (tiles.mbtiles): an MBTiles file of your area,
     from your tile provider or a tool whose source permits it;
  2. the tile cache on disk: tiles this box has served before (kept
     cache_days, 30 by default; older ones still served when offline);
  3. the internet tile server (tiles.url, OpenStreetMap by default), when this
     box has internet; the tile goes into the cache;
  4. other copies of this map on the mesh (the "tiles" layer of the exchange,
     aredn_sharing.py), which answer only from their own offline file and cache:
     a copy never fetches from the internet on another's behalf.

So one box with internet, or with an offline file, gives the whole mesh a map,
and every box that served a tile can pass it on.

OpenStreetMap's tile servers are run by volunteers. Their usage policy asks for
an honest User-Agent, caching, and no bulk downloading: this cache fetches only
tiles somebody looked at and never pre-fetches. For heavier use point tiles.url
at your own or a commercial tile server, or provide an MBTiles file.

A tile from a peer is checked to be a PNG, JPEG or WebP image of sane size
before it is cached or shown; it is only a picture, never run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_PATH = "/peer/v1/tiles/{z}/{x}/{y}.png"
MAX_TILE = 600 * 1024
OFFLINE_BACKOFF_S = 300
PEER_TIMEOUT = 4.0
MAX_PEERS_PER_TILE = 4
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


def image_type(data: bytes) -> str | None:
    if not data or len(data) > MAX_TILE:
        return None
    for magic, ctype in _MAGIC:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class TileCache:
    def __init__(self) -> None:
        self.url = OSM_URL
        self.user_agent = "SignalMatrix (+https://github.com/guywire/emergency-communications-hub)"
        self.internet = True
        self.share = True
        self.dir: str | None = None
        self.mbtiles = ""
        self.max_zoom = 19
        self.max_age_s = 30 * 86400
        self.max_bytes = 1000 * 10 ** 6
        self.peers_fn = lambda: []
        self._offline_until = 0.0
        self._writes = 0
        self._db = None
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._client = None
        self.stats = {"offline_file": 0, "cache": 0, "internet": 0, "peer": 0, "missing": 0}

    def configure(self, cfg: dict, data_dir: str, peers_fn=None, user_agent: str | None = None) -> None:
        cfg = cfg or {}
        self.url = str(cfg.get("url") or OSM_URL)
        self.internet = bool(cfg.get("internet", True))
        self.share = bool(cfg.get("share", True))
        self.mbtiles = str(cfg.get("mbtiles") or "")
        try:
            self.max_zoom = min(22, max(1, int(cfg.get("max_zoom", 19))))
            self.max_age_s = max(1.0, float(cfg.get("cache_days", 30))) * 86400
            self.max_bytes = int(max(10.0, float(cfg.get("cache_mb", 1000))) * 10 ** 6)
        except (TypeError, ValueError):
            pass
        self.dir = os.path.join(data_dir, "tiles")
        os.makedirs(self.dir, exist_ok=True)
        if peers_fn:
            self.peers_fn = peers_fn
        if user_agent:
            self.user_agent = user_agent
        self._db = None
        if self.mbtiles:
            try:
                self._db = sqlite3.connect(f"file:{self.mbtiles}?mode=ro", uri=True, check_same_thread=False)
            except sqlite3.Error as exc:
                log.warning("tiles: can't open the offline tile file %s: %s", self.mbtiles, exc)

    def layer_info(self) -> dict:
        """The tiles layer's entry in /peer/v1/info."""
        return {"max_zoom": self.max_zoom, "offline_file": bool(self._db)}

    def valid(self, z: int, x: int, y: int) -> bool:
        return 0 <= z <= self.max_zoom and 0 <= x < 2 ** z and 0 <= y < 2 ** z

    # ── the sources ─────────────────────────────────────────────────────────
    def _from_file(self, z, x, y) -> bytes | None:
        if not self._db:
            return None
        try:
            row = self._db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                   (z, x, 2 ** z - 1 - y)).fetchone()          # MBTiles rows count from the south
        except sqlite3.Error:
            return None
        return bytes(row[0]) if row and row[0] else None

    def _path(self, z, x, y) -> str:
        return os.path.join(self.dir, str(z), str(x), f"{y}.tile")

    def _from_cache(self, z, x, y) -> tuple[bytes, bool] | None:
        """(data, fresh) from the disk cache."""
        p = self._path(z, x, y)
        try:
            with open(p, "rb") as f:
                data = f.read()
            return data, time.time() - os.path.getmtime(p) < self.max_age_s
        except OSError:
            return None

    def _store(self, z, x, y, data: bytes) -> None:
        p = self._path(z, x, y)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p + ".tmp", "wb") as f:
                f.write(data)
            os.replace(p + ".tmp", p)
        except OSError as exc:
            log.debug("tiles: cache write failed: %s", exc)
            return
        self._writes += 1
        if self._writes % 200 == 0:
            self.prune()

    def prune(self) -> None:
        """Keep the cache under cache_mb: drop the least recently written tiles."""
        files = []
        for root, _, names in os.walk(self.dir):
            for n in names:
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                    files.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass
        total = sum(f[1] for f in files)
        if total <= self.max_bytes:
            return
        files.sort()
        for _, size, p in files:
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
            if total <= self.max_bytes * 0.9:
                break

    async def _from_internet(self, client, z, x, y) -> bytes | None:
        if not self.internet or time.time() < self._offline_until:
            return None
        url = self.url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        try:
            r = await client.get(url, timeout=10, headers={"User-Agent": self.user_agent})
        except Exception as exc:
            import httpx
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                self._offline_until = time.time() + OFFLINE_BACKOFF_S        # no internet: stop waiting on it
            return None
        return r.content if r.status_code == 200 and image_type(r.content) else None

    async def _from_peers(self, client, z, x, y) -> bytes | None:
        for base, host in list(self.peers_fn() or [])[:MAX_PEERS_PER_TILE]:
            try:
                r = await client.get(base + TILE_PATH.format(z=z, x=x, y=y), headers={"Host": host},
                                     timeout=PEER_TIMEOUT, follow_redirects=False)
            except Exception:
                continue
            if r.status_code == 200 and image_type(r.content):
                return r.content
        return None

    # ── what the routes call ────────────────────────────────────────────────
    async def get(self, z: int, x: int, y: int, for_peer: bool = False, client=None) -> tuple[bytes, str] | None:
        """(image, content type), or None. for_peer: answer another copy, from the
        offline file and the cache only."""
        if not self.valid(z, x, y) or (for_peer and not self.share):
            return None
        if client is None:
            import httpx
            if self._client is None:
                self._client = httpx.AsyncClient(follow_redirects=True)
            client = self._client
        data = self._from_file(z, x, y)
        if data and image_type(data):
            self.stats["offline_file"] += 1
            return data, image_type(data)
        cached = self._from_cache(z, x, y)
        if cached and (cached[1] or for_peer):
            self.stats["cache"] += 1
            return cached[0], image_type(cached[0]) or "image/png"
        if for_peer:
            return None
        key = (z, x, y)
        if key in self._inflight:                         # the same tile asked for twice at once
            return await asyncio.shield(self._inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            res = None
            data = await self._from_internet(client, z, x, y)
            src = "internet"
            if not data and cached:
                res = (cached[0], image_type(cached[0]) or "image/png")      # stale, but better than nothing
                self.stats["cache"] += 1
            elif not data:
                data, src = await self._from_peers(client, z, x, y), "peer"
            if data:
                self._store(z, x, y, data)
                self.stats[src] += 1
                res = (data, image_type(data))
            if res is None:
                self.stats["missing"] += 1
            fut.set_result(res)
            return res
        except Exception as exc:
            fut.set_result(None)
            log.debug("tiles: %s/%s/%s failed: %s", z, x, y, exc)
            return None
        finally:
            self._inflight.pop(key, None)


tiles = TileCache()
