"""
ech/core/goes_receiver.py
-------------------------
GOES data from a local satellite receiver (RTL-SDR + 1.7 GHz dish/LNA running
goestools or SatDump), as an alternative to the internet sources.

Two products are used, both read from directories the receiver writes to:

1. Full-disk imagery (e.g. GOES-East band 13 clean IR from HRIT). The newest
   image is reprojected from the satellite's geostationary view onto a Web
   Mercator box around the hub, so the map can lay it over the base map the
   same way it lays the internet (IEM) GOES tiles.

   Navigation assumes the image is the full ABI fixed grid: a square image
   spanning ±0.151872 rad scan angle in x and y (5424 px at 2 km, 2712 px at
   4 km, etc.; this is what goesproc and SatDump write for full-disk
   products). Mesoscale/CONUS crops are not navigable without their own
   metadata and are reported as unsupported rather than drawn in the wrong
   place.

2. EMWIN text products (goesproc's "emwin" handler output). NWS warning
   products are parsed for VTEC, UGC zones and LAT...LON polygons, so
   warnings and Special Weather Statements reach the map with no internet.
   Zone-only products are drawn from the shared NWS zone-shape cache
   (nws_zones.py), which fills in while the internet is up.

Geostationary projection maths: GOES-R Product User's Guide (PUG) vol. 3,
section 5.1.2.8 (lat/lon → fixed grid scan angles).
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import math
import os
import re
import time
import zipfile
from datetime import datetime, timezone

from ech.core import nws_zones

log = logging.getLogger(__name__)

# GRS80 ellipsoid + GOES-R perspective point, from the PUG
_R_EQ = 6378137.0
_R_POL = 6356752.31414
_H = 42164160.0          # perspective_point_height + r_eq
_E2 = 0.0066943800229    # first eccentricity squared
_FD_HALF_SPAN = 0.151872  # rad: half of 5424 px × 56 µrad

_IMAGE_EXTS = (".png", ".jpg", ".jpeg")
_TEXT_EXTS = (".txt", ".zis", ".zip")


# ── Geostationary navigation ─────────────────────────────────────────────

def latlon_to_scan(lat_deg, lon_deg, sat_lon_deg):
    """Lat/lon (numpy arrays, degrees) → (x, y, visible) fixed-grid scan
    angles in radians, per the GOES-R PUG."""
    import numpy as np
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    lam0 = math.radians(sat_lon_deg)
    phi_c = np.arctan((_R_POL ** 2 / _R_EQ ** 2) * np.tan(lat))
    rc = _R_POL / np.sqrt(1.0 - _E2 * np.cos(phi_c) ** 2)
    sx = _H - rc * np.cos(phi_c) * np.cos(lon - lam0)
    sy = -rc * np.cos(phi_c) * np.sin(lon - lam0)
    sz = rc * np.sin(phi_c)
    visible = _H * (_H - sx) >= sy ** 2 + (_R_EQ ** 2 / _R_POL ** 2) * sz ** 2
    x = np.arcsin(-sy / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2))
    y = np.arctan(sz / sx)
    return x, y, visible


def _merc_y(lat_deg: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2))


def region_bounds(lat: float, lon: float, half_lon: float, half_lat: float):
    s = max(-80.0, lat - half_lat)
    n = min(80.0, lat + half_lat)
    return s, lon - half_lon, n, lon + half_lon


def reproject_full_disk(img, sat_lon: float, bounds, out_w: int = 1024):
    """Reproject a full-disk PIL image onto a Web Mercator grid covering
    bounds = (south, west, north, east). Returns an RGBA PIL image, or None
    if the box is entirely off the disk."""
    import numpy as np
    from PIL import Image

    s, w, n, e = bounds
    ys_merc = _merc_y(n), _merc_y(s)
    span_x = math.radians(e - w)
    out_h = max(16, int(round(out_w * (ys_merc[0] - ys_merc[1]) / span_x)))
    lons = np.linspace(w, e, out_w)
    yv = np.linspace(ys_merc[0], ys_merc[1], out_h)
    lats = np.degrees(2 * np.arctan(np.exp(yv)) - math.pi / 2)
    LON, LAT = np.meshgrid(lons, lats)
    x, y, vis = latlon_to_scan(LAT, LON, sat_lon)

    nx, ny = img.size
    col = np.round((x + _FD_HALF_SPAN) / (2 * _FD_HALF_SPAN / nx) - 0.5).astype(np.int64)
    row = np.round((_FD_HALF_SPAN - y) / (2 * _FD_HALF_SPAN / ny) - 0.5).astype(np.int64)
    ok = vis & (col >= 0) & (col < nx) & (row >= 0) & (row < ny)
    if not ok.any():
        return None
    # Only decode the part of the disk the box needs
    c0, c1 = int(col[ok].min()), int(col[ok].max()) + 1
    r0, r1 = int(row[ok].min()), int(row[ok].max()) + 1
    src = img.crop((c0, r0, c1, r1))
    if src.mode not in ("L", "RGB"):
        src = src.convert("RGB")
    arr = np.asarray(src)
    out = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    rr, cc = row[ok] - r0, col[ok] - c0
    if arr.ndim == 2:
        v = arr[rr, cc]
        out[ok, 0] = v
        out[ok, 1] = v
        out[ok, 2] = v
    else:
        out[ok, :3] = arr[rr, cc, :3]
    out[ok, 3] = 255
    return Image.fromarray(out, "RGBA")


def newest_file(root: str, pattern: str, exts: tuple, max_depth: int = 6) -> str | None:
    """Newest matching file under root without walking every file the
    receiver ever wrote: at each level, compare the files there and descend
    only into the most recently modified subdirectory (goestools/SatDump
    write into per-date folders)."""
    best, best_m = None, -1.0
    path = root
    for _ in range(max_depth + 1):
        try:
            entries = list(os.scandir(path))
        except OSError:
            break
        newest_dir, newest_dir_m = None, -1.0
        for ent in entries:
            try:
                if ent.is_dir(follow_symlinks=False):
                    m = ent.stat().st_mtime
                    if m > newest_dir_m:
                        newest_dir, newest_dir_m = ent.path, m
                elif ent.is_file() and ent.name.lower().endswith(exts) and fnmatch.fnmatch(ent.name, pattern):
                    m = ent.stat().st_mtime
                    if m > best_m:
                        best, best_m = ent.path, m
            except OSError:
                continue
        if not newest_dir:
            break
        path = newest_dir
    return best


def image_time(path: str) -> datetime:
    """Scan time from a goestools/SatDump filename (…20261012T150020Z…), else mtime."""
    m = re.search(r"(20\d{6})[T_]?(\d{6})Z?", os.path.basename(path))
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)


# ── EMWIN text products ──────────────────────────────────────────────────

_WMO_RE = re.compile(r"^([A-Z]{4}\d{2}) ([A-Z]{4}) (\d{6})", re.M)
_AWIPS_RE = re.compile(r"^([A-Z0-9]{4,6})\s*$", re.M)
_VTEC_RE = re.compile(
    r"/([OTEX])\.(NEW|CON|EXT|EXA|EXB|UPG|CAN|EXP|COR|ROU)\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\."
    r"(\d{6}T\d{4}Z)-(\d{6}T\d{4}Z)/")
_UGC_BLOCK_RE = re.compile(r"^([A-Z]{2}[CZ]\d{3}[A-Z0-9>\-\s]*?\d{6}-)", re.M | re.S)
_LATLON_RE = re.compile(r"LAT\.\.\.LON((?:\s+\d{4,5})+)")

_SIG = {"W": "Warning", "A": "Watch", "Y": "Advisory", "S": "Statement"}
_PHEN = {
    "TO": "Tornado", "SV": "Severe Thunderstorm", "FF": "Flash Flood", "FL": "Flood",
    "FA": "Areal Flood", "MA": "Special Marine", "SQ": "Snow Squall", "EW": "Extreme Wind",
    "DS": "Dust Storm", "WS": "Winter Storm", "WW": "Winter Weather", "BZ": "Blizzard",
    "IS": "Ice Storm", "LE": "Lake Effect Snow", "HW": "High Wind", "WI": "Wind",
    "EH": "Excessive Heat", "HT": "Heat", "XH": "Extreme Heat", "FG": "Dense Fog",
    "SC": "Small Craft", "GL": "Gale", "SR": "Storm", "HF": "Hurricane Force Wind",
    "FR": "Frost", "FZ": "Freeze", "HZ": "Hard Freeze", "WC": "Wind Chill",
    "EC": "Extreme Cold", "CW": "Cold Weather", "HU": "Hurricane", "TR": "Tropical Storm",
    "SS": "Storm Surge", "CF": "Coastal Flood", "LS": "Lakeshore Flood", "SU": "High Surf",
    "RP": "Rip Current", "FW": "Red Flag", "AQ": "Air Quality", "AS": "Air Stagnation",
    "SM": "Dense Smoke", "ZF": "Freezing Fog", "UP": "Heavy Freezing Spray", "BW": "Brisk Wind",
}


def _vtec_event(phen: str, sig: str) -> str:
    if phen == "FW" and sig == "W":
        return "Red Flag Warning"
    if phen == "MA" and sig == "W":
        return "Special Marine Warning"
    return f"{_PHEN.get(phen, phen)} {_SIG.get(sig, sig)}"


def _vtec_severity(phen: str, sig: str) -> str:
    if sig == "W":
        return "Extreme" if phen in ("TO", "EW") else "Severe"
    if sig == "A":
        return "Moderate"
    return "Minor"


def _vtec_time(s: str) -> datetime | None:
    if s.startswith("000000"):
        return None
    try:
        return datetime.strptime(s, "%y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _latlon_polygon(text: str) -> dict | None:
    nums = [int(n) for n in text.split()]
    if len(nums) < 6 or len(nums) % 2:
        return None
    ring = [[-(lon / 100.0), lat / 100.0] for lat, lon in zip(nums[0::2], nums[1::2])]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _ddhhmm_time(ddhhmm: str, ref: datetime) -> datetime | None:
    from ech.core.aprs_wx import parse_expiry
    return parse_expiry(ddhhmm, ref)


def parse_emwin_product(text: str, ref: datetime | None = None) -> list[dict]:
    """Parse one NWS text product. Returns one entry per segment and VTEC
    event: {key, action, event, severity, ugcs, geometry, expires, office,
    product, headline}. SPS without VTEC is keyed by office + issuance."""
    ref = ref or datetime.now(timezone.utc)
    head = text[:400]
    wmo = _WMO_RE.search(head)
    if not wmo:
        return []
    awips = _AWIPS_RE.search(head, wmo.end())
    pil = awips.group(1) if awips else ""
    product = pil[:3]
    office = wmo.group(2)
    out: list[dict] = []
    for seg in text[wmo.end():].split("$$"):
        ugc_m = _UGC_BLOCK_RE.search(seg)
        if not ugc_m:
            continue
        ugcs, ugc_exp = nws_zones.expand_ugc(ugc_m.group(1))
        if not ugcs:
            continue
        exp_ugc = _ddhhmm_time(ugc_exp, ref) if ugc_exp else None
        ll = _LATLON_RE.search(seg)
        geom = _latlon_polygon(ll.group(1)) if ll else None
        headline = ""
        hm = re.search(r"^\.\.\.(.+?)\.\.\.\s*$", seg, re.M)
        if hm:
            headline = hm.group(1).strip()
        vtecs = [v for v in _VTEC_RE.findall(seg) if v[0] == "O"]
        if vtecs:
            for _cls, action, v_office, phen, sig, etn, _beg, end in vtecs:
                exp = _vtec_time(end) or exp_ugc
                out.append({
                    "key": f"{v_office}.{phen}.{sig}.{etn}",
                    "action": action,
                    "event": _vtec_event(phen, sig),
                    "severity": _vtec_severity(phen, sig),
                    "ugcs": ugcs, "geometry": geom,
                    "expires": exp.isoformat() if exp else None,
                    "office": v_office, "product": product, "headline": headline,
                })
        elif product == "SPS":
            out.append({
                "key": f"{office}.SPS.{wmo.group(3)}.{ugcs[0]}",
                "action": "NEW",
                "event": "Special Weather Statement",
                "severity": "Moderate",
                "ugcs": ugcs, "geometry": geom,
                "expires": exp_ugc.isoformat() if exp_ugc else None,
                "office": office, "product": product, "headline": headline,
            })
    return out


def _read_text_file(path: str) -> str | None:
    try:
        if os.path.getsize(path) > 512 * 1024:
            return None
        if path.lower().endswith((".zis", ".zip")):
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.lower().endswith(".txt"):
                        return z.read(name)[:512 * 1024].decode("latin-1", "replace")
            return None
        with open(path, "rb") as f:
            return f.read().decode("latin-1", "replace")
    except Exception as exc:
        log.debug("GOES EMWIN: cannot read %s: %s", path, exc)
        return None


# ── Service ──────────────────────────────────────────────────────────────

class GoesReceiverService:
    """Watches the receiver's output directories. Off unless configured."""

    def __init__(self, config: dict, wx_service=None):
        cfg = config.get("goes_receiver") or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.imagery_dir = cfg.get("imagery_dir") or ""
        self.imagery_glob = cfg.get("imagery_glob") or "*"
        self.sat_lon = float(cfg.get("satellite_lon", -75.2))   # GOES-19 (GOES-East)
        self.imagery_source = str(cfg.get("imagery_source", "auto")).lower()
        self.alerts_source = str(cfg.get("alerts_source", "auto")).lower()
        self.max_image_age_min = int(cfg.get("max_image_age_min", 90))
        self.region_half_lon = float(cfg.get("region_half_width_deg", 10))
        self.region_half_lat = float(cfg.get("region_half_height_deg", 7))
        self.output_px = max(256, min(2048, int(cfg.get("output_px", 1024))))
        self.emwin_dir = cfg.get("emwin_dir") or ""
        self.emwin_max_age_h = float(cfg.get("emwin_max_age_hours", 12))
        self.scan_interval = max(30, int(cfg.get("scan_interval_sec", 60)))

        self._wx = wx_service
        _db_path = (config.get("database") or {}).get("path", "ech.db")
        base = os.path.dirname(os.path.abspath(_db_path)) if _db_path != ":memory:" else os.getcwd()
        self.cache_dir = cfg.get("cache_dir") or os.path.join(base, "goes_cache")
        self.image_path = os.path.join(self.cache_dir, "goes_local.png")

        self.image_info: dict | None = None     # {time, bounds, file, built_at}
        self.image_error = ""
        self._image_key: tuple | None = None
        self._emwin_seen: dict[str, float] = {}
        self._emwin_alerts: dict[str, dict] = {}
        self.emwin_last_file: float = 0.0
        self.emwin_products = 0
        self._task: asyncio.Task | None = None

    # ── lifecycle

    async def start(self) -> None:
        if not self.enabled:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        if self._wx is not None:
            self._wx.zone_providers.append(
                lambda: [u for a in self._emwin_alerts.values() if not a.get("geometry") for u in a["ugcs"]])
        self._task = asyncio.create_task(self._loop(), name="goes-receiver")
        log.info("GOES receiver: watching imagery=%r emwin=%r (imagery_source=%s, alerts_source=%s)",
                 self.imagery_dir, self.emwin_dir, self.imagery_source, self.alerts_source)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                if self.imagery_dir:
                    await asyncio.to_thread(self.scan_imagery)
                if self.emwin_dir:
                    await asyncio.to_thread(self.scan_emwin)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("GOES receiver: scan error: %s", exc)
            await asyncio.sleep(self.scan_interval)

    # ── imagery

    def _hub(self) -> tuple[float, float] | None:
        if self._wx is not None and self._wx._lat is not None and self._wx._lon is not None:
            return float(self._wx._lat), float(self._wx._lon)
        return None

    def scan_imagery(self) -> None:
        path = newest_file(self.imagery_dir, self.imagery_glob, _IMAGE_EXTS)
        if not path:
            self.image_error = f"no images found in {self.imagery_dir}"
            return
        hub = self._hub()
        if not hub:
            self.image_error = "hub location not set (Settings → base location)"
            return
        key = (path, os.path.getmtime(path), round(hub[0], 2), round(hub[1], 2))
        if key == self._image_key:
            return
        try:
            from PIL import Image
        except ImportError:
            self.image_error = "Pillow not installed (pip install Pillow)"
            return
        bounds = region_bounds(hub[0], hub[1], self.region_half_lon, self.region_half_lat)
        with Image.open(path) as img:
            w, h = img.size
            if abs(w - h) > max(2, w // 100):
                self.image_error = (f"{os.path.basename(path)} is {w}x{h}, not a square full-disk image; "
                                    "point imagery_dir at a full-disk (FD) channel folder")
                self._image_key = key
                return
            out = reproject_full_disk(img, self.sat_lon, bounds, self.output_px)
        if out is None:
            self.image_error = "hub region is not visible from satellite_lon — check the setting"
            self._image_key = key
            return
        tmp = self.image_path + ".tmp.png"
        out.save(tmp, "PNG", optimize=False)
        os.replace(tmp, self.image_path)
        self._image_key = key
        self.image_error = ""
        self.image_info = {
            "time": image_time(path).isoformat(),
            "bounds": [[bounds[0], bounds[1]], [bounds[2], bounds[3]]],
            "file": os.path.basename(path),
            "built_at": time.time(),
        }
        log.info("GOES receiver: reprojected %s for the map", os.path.basename(path))

    def image_age_min(self) -> float | None:
        if not self.image_info:
            return None
        t = datetime.fromisoformat(self.image_info["time"])
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0

    def use_local_imagery(self) -> bool:
        if not self.enabled or self.imagery_source == "internet" or not self.image_info:
            return False
        if self.imagery_source == "local":
            return True
        age = self.image_age_min()
        return age is not None and age <= self.max_image_age_min

    # ── EMWIN

    def scan_emwin(self) -> None:
        cutoff = time.time() - self.emwin_max_age_h * 3600
        files: list[tuple[float, str]] = []
        for dirpath, _dirs, names in os.walk(self.emwin_dir):
            for name in names:
                if not name.lower().endswith(_TEXT_EXTS):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m >= cutoff and self._emwin_seen.get(p) != m:
                    files.append((m, p))
        for m, p in sorted(files):
            self._emwin_seen[p] = m
            text = _read_text_file(p)
            if not text:
                continue
            ref = datetime.fromtimestamp(m, tz=timezone.utc)
            entries = parse_emwin_product(text, ref)
            if entries:
                self.emwin_products += 1
                self.emwin_last_file = max(self.emwin_last_file, m)
            # One event (VTEC ETN) can span several segments, each with its
            # own zones, and a segment can cancel some zones while others
            # continue. Within one product: union the zones still in effect.
            by_key: dict[str, list[dict]] = {}
            for entry in entries:
                by_key.setdefault(entry["key"], []).append(entry)
            for key, group in by_key.items():
                live = [e for e in group if e["action"] not in ("CAN", "EXP", "UPG")]
                ended = {u for e in group if e["action"] in ("CAN", "EXP", "UPG") for u in e["ugcs"]}
                prev = self._emwin_alerts.get(key)
                if not live:
                    if prev:
                        rest = [u for u in prev["ugcs"] if u not in ended]
                        if rest and not prev.get("geometry"):
                            prev["ugcs"] = rest
                        else:
                            del self._emwin_alerts[key]
                    continue
                entry = dict(live[0])
                entry["ugcs"] = list(dict.fromkeys(u for e in live for u in e["ugcs"]))
                entry["geometry"] = next((e["geometry"] for e in live if e["geometry"]), None)
                if prev and not entry["geometry"] and prev.get("geometry") and entry["action"] in ("CON", "EXT"):
                    entry["geometry"] = prev["geometry"]
                entry["received"] = ref.isoformat()
                self._emwin_alerts[key] = entry
        self._emwin_seen = {p: m for p, m in self._emwin_seen.items() if m >= cutoff}
        self._prune()

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        for k, a in list(self._emwin_alerts.items()):
            exp = a.get("expires")
            try:
                if exp and datetime.fromisoformat(exp) < now:
                    del self._emwin_alerts[k]
            except ValueError:
                pass

    def emwin_alerts(self) -> list[dict]:
        self._prune()
        return list(self._emwin_alerts.values())

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "imagery_source": self.imagery_source,
            "alerts_source": self.alerts_source,
            "imagery_dir": self.imagery_dir,
            "emwin_dir": self.emwin_dir,
            "satellite_lon": self.sat_lon,
            "use_local_imagery": self.use_local_imagery(),
            "image": ({**self.image_info, "age_min": round(self.image_age_min() or 0, 1)}
                      if self.image_info else None),
            "image_error": self.image_error,
            "emwin_active": len(self._emwin_alerts),
            "emwin_products_parsed": self.emwin_products,
            "emwin_last_file": (datetime.fromtimestamp(self.emwin_last_file, tz=timezone.utc).isoformat()
                                if self.emwin_last_file else None),
        }
