"""
ech/core/water_bodies.py
--------------------------
Named Water Body Service — replaces a single hardcoded buoy with every named
lake, bay, and river EPA has assessed near the hub, each with its own live
conditions where available.

EPA ATTAINS geospatial service (no key needed — public ArcGIS REST):
  https://gispub.epa.gov/arcgis/rest/services/OW/ATTAINS_Assessment/MapServer
  Layer 2 = assessment areas (lakes, bays, estuaries — polygons)
  Layer 1 = assessment lines (rivers, streams — polylines)
  Query with outSR=4326 to get WGS84 lon/lat directly, no reprojection needed.
  Fields used: assessmentunitname, overallstatus, waterbodyreportlink.
  Confirmed live against real Maine data (e.g. "St. George River" — "Fully
  Supporting") — this is a genuine, key-free EPA data source, not a guess.

NOAA NDBC global buoy snapshot (no key needed):
  https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt
  One file, live conditions for every active NDBC buoy worldwide. Used to
  auto-match each discovered water body to its nearest live buoy (if any —
  most lakes/rivers have none, which is expected and reported as such rather
  than silently omitted).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

ATTAINS_BASE = "https://gispub.epa.gov/arcgis/rest/services/OW/ATTAINS_Assessment/MapServer"
NDBC_LATEST_OBS = "https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt"

# NDBC has essentially no freshwater river/stream/lake instrumentation in most
# regions (Maine included) — its buoys are open-ocean/coastal (plus a handful
# of Great Lakes stations). Matching "nearest buoy within N miles" for an
# inland river routinely finds a genuine ocean buoy several towns away, whose
# swell/wave-height readings are meaningless for a calm freshwater river. Skip
# buoy matching entirely for anything freshwater — bays, sounds, coves, and
# harbors are legitimately tidal/coastal and keep it.
#
# ATTAINS has no explicit water-type field, so classification uses two signals:
#  1. Which layer the feature came from — layer 1 ("Assessment Lines") is
#     rivers/streams by definition, always freshwater, no name-guessing needed.
#  2. For layer 2 ("Assessment Areas", which mixes lakes/ponds with legitimately
#     coastal bays/coves), full freshwater words in the name, OR — because Maine
#     abbreviates the water-type suffix as the final word (e.g. "LOVEJOY P" for
#     "Lovejoy Pond", "THREEMILE P" for "Threemile Pond") — common abbreviations
#     when they're the last token.
_FRESHWATER_WORDS = {"river", "stream", "brook", "creek", "pond", "lake", "reservoir"}
_FRESHWATER_LAST_TOKEN_ABBR = {"p", "pd", "l", "res", "str", "strm", "bk", "brk"}


def _is_freshwater(name: str, source_layer: str) -> bool:
    if source_layer == "line":
        return True
    n = name.lower()
    if any(w in n for w in _FRESHWATER_WORDS):
        return True
    tokens = re.findall(r"[a-z]+", n)
    return bool(tokens) and tokens[-1] in _FRESHWATER_LAST_TOKEN_ABBR


STATUS_COLOR = {
    "Fully Supporting": "#00e400",
    "Not Supporting": "#ff0000",
    "Insufficient Information": "#ffff00",
    "Not Assessed": "#888888",
}


def _status_color(status: str) -> str:
    return STATUS_COLOR.get(status, "#888888")


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _num(v):
    return None if v in ("MM", "", None) else float(v)


def _representative_point(parts: list[list[list[float]]]) -> tuple[float, float] | None:
    """Pick a marker location that's guaranteed to actually sit on the
    feature. Averaging every vertex across a winding river or a concave
    coastal shape (bays, islands, multiple disconnected parts) routinely
    lands the "centroid" in empty space between the curves — on land, not on
    the water. Instead: compute the rough centroid, then snap to whichever
    real vertex (across ALL parts, not just the first) is closest to it —
    that vertex is on the feature's boundary by definition."""
    all_pts = [p for part in parts for p in part if len(p) >= 2]
    if not all_pts:
        return None
    clat = sum(p[1] for p in all_pts) / len(all_pts)
    clon = sum(p[0] for p in all_pts) / len(all_pts)
    nearest = min(all_pts, key=lambda p: (p[0] - clon) ** 2 + (p[1] - clat) ** 2)
    return nearest[1], nearest[0]


class WaterBodyService:
    """
    Auto-discovers every EPA-assessed water body (lake/bay/estuary/river)
    within a bounding box of the hub, and matches each to its nearest live
    NDBC buoy (if one exists — most lakes/rivers won't have one, and that's
    reported explicitly rather than silently degraded).
    """

    def __init__(self, config: dict, router=None):
        cfg = config.get("water_body_service", {}) or {}
        self.enabled = bool(cfg.get("enabled", False))

        wx_cfg = config.get("weather_service", {}) or {}
        self._lat: float | None = cfg.get("lat") or wx_cfg.get("nws_lat")
        self._lon: float | None = cfg.get("lon") or wx_cfg.get("nws_lon")

        self._bbox_deg = float(cfg.get("bbox_deg", 0.75))          # ~50mi — water bodies are local
        self._buoy_match_max_mi = float(cfg.get("buoy_match_max_mi", 60))
        self._poll_interval = int(cfg.get("poll_interval_sec", 21600))  # 6h — quality status changes slowly

        self._router = router
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None

        self._water_bodies: list[dict] = []
        self._last_poll: datetime | None = None
        self._poll_count = 0
        self._last_error = ""

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("WaterBodyService: disabled in config")
            return
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="waterbody-poll")
        log.info("WaterBodyService: started, interval=%ds", self._poll_interval)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    async def trigger_poll(self) -> None:
        await self._poll()

    async def _poll_loop(self) -> None:
        await self._poll()
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._poll()
        except asyncio.CancelledError:
            pass

    async def _poll(self) -> None:
        if self._lat is None or self._lon is None:
            self._last_error = "no lat/lon configured (set water_body_service.lat/lon or weather_service.nws_lat/lon)"
            log.warning("WaterBodyService: %s", self._last_error)
            return
        self._poll_count += 1
        self._last_poll = datetime.now(timezone.utc)
        try:
            bodies = await self._fetch_attains_bodies()
            buoys = await self._fetch_ndbc_buoys()
            for b in bodies:
                b["buoy"] = (None if _is_freshwater(b["name"], b["source_layer"])
                             else self._nearest_buoy(b["lat"], b["lon"], buoys))
            self._water_bodies = bodies
            self._last_error = ""
        except httpx.HTTPError as exc:
            self._last_error = f"HTTP error: {exc}"
            log.warning("WaterBodyService: %s", self._last_error)
        except Exception as exc:
            self._last_error = f"poll error: {exc}"
            log.error("WaterBodyService: %s", self._last_error)

    # ── EPA ATTAINS: discover named water bodies ──────────────────────────

    async def _fetch_attains_layer(self, layer: int, geom_field: str, source_layer: str) -> list[dict]:
        b = self._bbox_deg
        bbox = f"{self._lon - b},{self._lat - b},{self._lon + b},{self._lat + b}"
        out = []
        offset = 0
        # ArcGIS silently caps each response at its transfer limit (seen: 100)
        # regardless of resultRecordCount — near a coastline that's often
        # entirely consumed by small coastal segments before inland lakes are
        # ever reached, so lakes just vanish unless every page is fetched.
        for _ in range(20):   # hard stop — never loop forever on a misbehaving service
            resp = await self._client.get(
                f"{ATTAINS_BASE}/{layer}/query",
                params={
                    "geometry": bbox,
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326",
                    "outSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "assessmentunitname,overallstatus,waterbodyreportlink,fishconsumption_use,recreation_use",
                    "f": "json",
                    "resultRecordCount": "1000",
                    "resultOffset": str(offset),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            feats = data.get("features", [])
            for feat in feats:
                attrs = feat.get("attributes", {})
                name = attrs.get("assessmentunitname")
                if not name:
                    continue
                geom = feat.get("geometry", {})
                coords = geom.get(geom_field)
                if not coords:
                    continue
                point = _representative_point(coords)
                if point is None:
                    continue
                clat, clon = point
                out.append({
                    "name": name,
                    "status": attrs.get("overallstatus") or "Not Assessed",
                    "report_link": attrs.get("waterbodyreportlink") or "",
                    "fish_advisory": attrs.get("fishconsumption_use") or "Not Assessed",
                    "recreation_status": attrs.get("recreation_use") or "Not Assessed",
                    "lat": clat, "lon": clon,
                    "color": _status_color(attrs.get("overallstatus") or "Not Assessed"),
                    "source_layer": source_layer,
                })
            if not data.get("exceededTransferLimit") or not feats:
                break
            offset += len(feats)
        return out

    async def _fetch_attains_bodies(self) -> list[dict]:
        areas = await self._fetch_attains_layer(2, "rings", "area")    # lakes/bays/estuaries
        lines = await self._fetch_attains_layer(1, "paths", "line")    # rivers/streams
        # De-dupe by name (a river can span both a line and nearby area records)
        seen: dict[str, dict] = {}
        for b in areas + lines:
            if b["name"] not in seen:
                seen[b["name"]] = b
        return list(seen.values())

    # ── NDBC: nearest live buoy per water body ────────────────────────────

    async def _fetch_ndbc_buoys(self) -> list[dict]:
        resp = await self._client.get(NDBC_LATEST_OBS)
        resp.raise_for_status()
        buoys = []
        for line in resp.text.splitlines():
            if not line or line.startswith("#"):
                continue
            f = line.split()
            if len(f) < 19:
                continue
            try:
                lat, lon = float(f[1]), float(f[2])
            except ValueError:
                continue
            # Header: STN LAT LON YYYY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES PTDY ATMP WTMP DEWP VIS TIDE
            wspd, gst, wvht, pres, ptdy, wtmp = (
                _num(f[9]), _num(f[10]), _num(f[11]), _num(f[15]), _num(f[16]), _num(f[18])
            )
            buoys.append({
                "station": f[0], "lat": lat, "lon": lon,
                "wind_mph": wspd * 2.237 if wspd is not None else None,
                "gust_mph": gst * 2.237 if gst is not None else None,
                "wave_ft": wvht * 3.281 if wvht is not None else None,
                "water_f": wtmp * 9 / 5 + 32 if wtmp is not None else None,
                "pres_hpa": pres,
                "ptdy_hpa": ptdy,
            })
        return buoys

    def _nearest_buoy(self, lat: float, lon: float, buoys: list[dict]) -> dict | None:
        if not buoys:
            return None
        best = min(buoys, key=lambda b: _haversine_mi(lat, lon, b["lat"], b["lon"]))
        dist = _haversine_mi(lat, lon, best["lat"], best["lon"])
        if dist > self._buoy_match_max_mi:
            return None
        out = dict(best)
        out["distance_mi"] = dist
        return out

    # ── Lookup ──────────────────────────────────────────────────────────

    def find(self, query: str) -> dict | None:
        q = query.strip().lower()
        if not q:
            return None
        for b in self._water_bodies:
            if q == b["name"].lower():
                return b
        for b in self._water_bodies:
            if q in b["name"].lower():
                return b
        return None

    def nearest(self, lat: float, lon: float) -> dict | None:
        if not self._water_bodies:
            return None
        return min(self._water_bodies, key=lambda b: _haversine_mi(lat, lon, b["lat"], b["lon"]))

    def list_names(self, limit: int = 8) -> list[str]:
        return [b["name"] for b in self._water_bodies[:limit]]

    # ── Status / API payloads ──────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "poll_count": self._poll_count,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "last_error": self._last_error,
            "water_body_count": len(self._water_bodies),
            "buoy_matched_count": sum(1 for b in self._water_bodies if b.get("buoy")),
        }

    def water_bodies(self) -> list[dict]:
        return self._water_bodies
