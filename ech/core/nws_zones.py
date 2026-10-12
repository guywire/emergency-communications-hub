"""
ech/core/nws_zones.py
---------------------
NWS zone boundary shapes for drawing zone-based alerts on the map.

Most Special Weather Statements, and most winter/wind/flood watches and
advisories, carry `geometry: null` in the NWS alerts feed. They are issued
for UGC forecast zones or counties, not for a drawn polygon. To draw them,
fetch each zone's boundary once from api.weather.gov/zones/..., simplify it
(raw coastal zones run to ~1 MB of coordinates, e.g. MEZ027), and keep it in
a small JSON cache on disk next to the database. Zone boundaries change only
a few times a year, so the cache also lets alerts that arrive with no
internet (GOES EMWIN text from a local receiver, see goes_receiver.py) be
drawn from shapes fetched earlier.

UGC codes: SSCnnn = county, SSZnnn = forecast/marine zone (e.g. MEZ027,
MEC005, ANZ150). api.weather.gov serves marine zones under /zones/forecast/
too (confirmed: /zones/forecast/ANZ150 → 200).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterable

log = logging.getLogger(__name__)

NWS_API_BASE = "https://api.weather.gov"

# Only api.weather.gov zone URLs are ever fetched. affectedZones URLs come
# from the NWS feed, but they are still checked against this pattern.
ZONE_URL_RE = re.compile(
    r"^https://api\.weather\.gov/zones/(forecast|county|fire|marine|offshore|coastal|public)/([A-Z]{2}[CZ]\d{3})$")
UGC_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")

# Douglas-Peucker tolerance in degrees (~0.005° ≈ 500 m). That is well under a
# map pixel at the zoom levels where a whole zone is on screen.
SIMPLIFY_TOL_DEG = 0.005
# Islands/slivers smaller than this (deg², ~1 km²) are dropped.
MIN_RING_AREA_DEG2 = 1e-4
# Failed fetches are not retried for this long.
NEGATIVE_TTL_SEC = 3600


def ugc_url(ugc: str) -> str:
    kind = "county" if ugc[2] == "C" else "forecast"
    return f"{NWS_API_BASE}/zones/{kind}/{ugc}"


def ugc_from_url(url: str) -> str | None:
    m = ZONE_URL_RE.match(url or "")
    return m.group(2) if m else None


def expand_ugc(text: str) -> tuple[list[str], str | None]:
    """Expand a UGC line, e.g. 'MEZ001>003-005-NHZ001-121630-', into
    (['MEZ001','MEZ002','MEZ003','MEZ005','NHZ001'], '121630').
    The final 6-digit group is the product expiry (DDHHMM, UTC)."""
    codes: list[str] = []
    expiry = None
    prefix = ""
    for tok in re.sub(r"\s+", "", text).split("-"):
        if not tok:
            continue
        if re.fullmatch(r"\d{6}", tok):
            expiry = tok
            continue
        m = re.fullmatch(r"([A-Z]{2}[CZ])(\d{3})(?:>(\d{3}))?", tok)
        if m:
            prefix = m.group(1)
            start, end = int(m.group(2)), int(m.group(3) or m.group(2))
        else:
            m = re.fullmatch(r"(\d{3})(?:>(\d{3}))?", tok)
            if not m or not prefix:
                continue
            start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        if end < start or end - start > 999:
            continue
        codes.extend(f"{prefix}{n:03d}" for n in range(start, end + 1))
    return codes, expiry


# ── Geometry simplification ──────────────────────────────────────────────

def _dp(points: list, tol: float) -> list:
    """Iterative Douglas-Peucker on a list of [lon, lat] points."""
    n = len(points)
    if n < 3:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = points[a][0], points[a][1]
        bx, by = points[b][0], points[b][1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best_i, best_d = -1, tol2
        for i in range(a + 1, b):
            px, py = points[i][0], points[i][1]
            if seg2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best_d:
                best_i, best_d = i, d
        if best_i >= 0:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(points, keep) if k]


def _ring_area(ring: list) -> float:
    s = 0.0
    for i in range(len(ring) - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(s) / 2.0


def _simplify_ring(ring: list, tol: float) -> list | None:
    if len(ring) < 4 or _ring_area(ring) < MIN_RING_AREA_DEG2:
        return None
    # A closed ring has first == last, so Douglas-Peucker would collapse it to
    # that one point. Split at the farthest vertex and simplify each half.
    far = max(range(len(ring)), key=lambda i: (ring[i][0] - ring[0][0]) ** 2 + (ring[i][1] - ring[0][1]) ** 2)
    out = _dp(ring[:far + 1], tol)[:-1] + _dp(ring[far:], tol)
    out = [[round(p[0], 4), round(p[1], 4)] for p in out]
    if len(out) < 4:
        return None
    return out


def simplify_geometry(geom: dict | None, tol: float = SIMPLIFY_TOL_DEG) -> dict | None:
    """Simplify a GeoJSON Polygon/MultiPolygon. Returns a MultiPolygon, or
    None if nothing drawable is left."""
    if not geom:
        return None
    if geom.get("type") == "Polygon":
        polys = [geom.get("coordinates") or []]
    elif geom.get("type") == "MultiPolygon":
        polys = geom.get("coordinates") or []
    else:
        return None
    out = []
    for poly in polys:
        if not poly:
            continue
        outer = _simplify_ring(poly[0], tol)
        if not outer:
            continue
        holes = [h for h in (_simplify_ring(r, tol) for r in poly[1:]) if h]
        out.append([outer] + holes)
    return {"type": "MultiPolygon", "coordinates": out} if out else None


def geometry_bbox(geom: dict | None) -> tuple[float, float, float, float] | None:
    """(min_lon, min_lat, max_lon, max_lat) of a Polygon/MultiPolygon."""
    if not geom:
        return None
    polys = [geom["coordinates"]] if geom.get("type") == "Polygon" else geom.get("coordinates") or []
    xs, ys = [], []
    for poly in polys:
        for ring in poly[:1]:
            for p in ring:
                xs.append(p[0])
                ys.append(p[1])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ── Cache ────────────────────────────────────────────────────────────────

class ZoneShapeCache:
    """UGC code → simplified MultiPolygon, kept in memory and in a JSON file."""

    def __init__(self, path: str | None):
        self._path = path
        self._shapes: dict[str, dict] = {}
        self._failed: dict[str, float] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._shapes = {k: v for k, v in (data.get("zones") or {}).items() if UGC_RE.match(k)}
            log.info("NWS zones: loaded %d cached zone shape(s) from %s", len(self._shapes), self._path)
        except Exception as exc:
            log.warning("NWS zones: could not read cache %s: %s", self._path, exc)

    def save(self) -> None:
        if not self._dirty or not self._path:
            return
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"zones": self._shapes}, f, separators=(",", ":"))
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception as exc:
            log.warning("NWS zones: could not write cache %s: %s", self._path, exc)

    def get(self, ugc: str) -> dict | None:
        return self._shapes.get(ugc)

    def put(self, ugc: str, geom: dict | None) -> None:
        if geom:
            self._shapes[ugc] = geom
            self._dirty = True

    def __len__(self) -> int:
        return len(self._shapes)

    async def ensure(self, ugcs: Iterable[str], client: Any, max_fetch: int = 25) -> int:
        """Fetch shapes not yet cached, at most max_fetch per call so one big
        winter storm (100+ zones) fills in over a few polls instead of
        bursting api.weather.gov. Returns how many were fetched."""
        now = time.time()
        fetched = 0
        for ugc in dict.fromkeys(ugcs):
            if fetched >= max_fetch:
                break
            if not UGC_RE.match(ugc) or ugc in self._shapes:
                continue
            if now - self._failed.get(ugc, 0) < NEGATIVE_TTL_SEC:
                continue
            fetched += 1
            try:
                resp = await client.get(ugc_url(ugc))
                resp.raise_for_status()
                geom = simplify_geometry(resp.json().get("geometry"))
                if geom:
                    self.put(ugc, geom)
                else:
                    self._failed[ugc] = now
            except Exception as exc:
                log.debug("NWS zones: fetch %s failed: %s", ugc, exc)
                self._failed[ugc] = now
        if fetched:
            self.save()
        return fetched


# ── State neighbours (to poll a region, not just the hub's own point) ────

STATE_NEIGHBORS: dict[str, list[str]] = {
    "AL": ["FL", "GA", "MS", "TN"], "AK": [], "AZ": ["CA", "CO", "NM", "NV", "UT"],
    "AR": ["LA", "MO", "MS", "OK", "TN", "TX"], "CA": ["AZ", "NV", "OR"],
    "CO": ["AZ", "KS", "NE", "NM", "OK", "UT", "WY"], "CT": ["MA", "NY", "RI"],
    "DE": ["MD", "NJ", "PA"], "DC": ["MD", "VA"], "FL": ["AL", "GA"],
    "GA": ["AL", "FL", "NC", "SC", "TN"], "HI": [], "ID": ["MT", "NV", "OR", "UT", "WA", "WY"],
    "IL": ["IA", "IN", "KY", "MO", "WI"], "IN": ["IL", "KY", "MI", "OH"],
    "IA": ["IL", "MN", "MO", "NE", "SD", "WI"], "KS": ["CO", "MO", "NE", "OK"],
    "KY": ["IL", "IN", "MO", "OH", "TN", "VA", "WV"], "LA": ["AR", "MS", "TX"],
    "ME": ["NH"], "MD": ["DC", "DE", "PA", "VA", "WV"], "MA": ["CT", "NH", "NY", "RI", "VT"],
    "MI": ["IN", "OH", "WI"], "MN": ["IA", "ND", "SD", "WI"],
    "MS": ["AL", "AR", "LA", "TN"], "MO": ["AR", "IA", "IL", "KS", "KY", "NE", "OK", "TN"],
    "MT": ["ID", "ND", "SD", "WY"], "NE": ["CO", "IA", "KS", "MO", "SD", "WY"],
    "NV": ["AZ", "CA", "ID", "OR", "UT"], "NH": ["MA", "ME", "VT"],
    "NJ": ["DE", "NY", "PA"], "NM": ["AZ", "CO", "OK", "TX", "UT"],
    "NY": ["CT", "MA", "NJ", "PA", "VT"], "NC": ["GA", "SC", "TN", "VA"],
    "ND": ["MN", "MT", "SD"], "OH": ["IN", "KY", "MI", "PA", "WV"],
    "OK": ["AR", "CO", "KS", "MO", "NM", "TX"], "OR": ["CA", "ID", "NV", "WA"],
    "PA": ["DE", "MD", "NJ", "NY", "OH", "WV"], "RI": ["CT", "MA"],
    "SC": ["GA", "NC"], "SD": ["IA", "MN", "MT", "ND", "NE", "WY"],
    "TN": ["AL", "AR", "GA", "KY", "MO", "MS", "NC", "VA"],
    "TX": ["AR", "LA", "NM", "OK"], "UT": ["AZ", "CO", "ID", "NM", "NV", "WY"],
    "VT": ["MA", "NH", "NY"], "VA": ["DC", "KY", "MD", "NC", "TN", "WV"],
    "WA": ["ID", "OR"], "WV": ["KY", "MD", "OH", "PA", "VA"],
    "WI": ["IA", "IL", "MI", "MN"], "WY": ["CO", "ID", "MT", "NE", "SD", "UT"],
    "PR": ["VI"], "VI": ["PR"], "GU": [], "AS": [],
}

# Coastal states → the marine area codes NWS uses for their waters
# (api.weather.gov `area=` accepts these, e.g. GM = Gulf of Mexico).
STATE_MARINE: dict[str, list[str]] = {
    "ME": ["AN"], "NH": ["AN"], "MA": ["AN"], "RI": ["AN"], "CT": ["AN"], "NY": ["AN"],
    "NJ": ["AN"], "DE": ["AN"], "MD": ["AN"], "VA": ["AN"], "NC": ["AM"], "SC": ["AM"],
    "GA": ["AM"], "FL": ["AM", "GM"], "AL": ["GM"], "MS": ["GM"], "LA": ["GM"], "TX": ["GM"],
    "CA": ["PZ"], "OR": ["PZ"], "WA": ["PZ"], "HI": ["PH"], "AK": ["PK"],
    "MI": ["LM", "LH", "LS", "LE"], "WI": ["LM", "LS"], "MN": ["LS"], "IL": ["LM"],
    "IN": ["LM"], "OH": ["LE"], "PA": ["LE"],
}


def region_areas(state: str) -> list[str]:
    state = (state or "").upper()
    return [state] + STATE_NEIGHBORS.get(state, []) + STATE_MARINE.get(state, [])
