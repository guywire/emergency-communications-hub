"""Satellite map layer: positions of tracked amateur/weather satellites and
Starlink "trains" (a fresh launch still strung out in a line).

TLEs come from CelesTrak (gp.php groups amateur/stations/weather/starlink),
cached on disk so a restart doesn't re-download. Positions use vectorised SGP4
(``sgp4.api.SatrecArray``) - ~10k Starlinks in tens of ms - and a plain
TEME->ECEF rotation by GMST, which is plenty for plotting on a map.

The "modes" labels in CATALOG are the commonly-reported downlink/beacon modes
for each satellite (CW telemetry beacons, linear-transponder CW/SSB, the
QO-100 narrow-band transponder where FT8 is run). Check AMSAT/the satellite's
own page before relying on a frequency - satellites change status.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
GROUPS = ("amateur", "stations", "weather")
TLE_MAX_AGE_S = 12 * 3600

# name substring (upper case, matched against the TLE name) -> (short label, modes)
CATALOG: list[tuple[str, str, list[str]]] = [
    ("ISS (ZARYA)",   "ISS",        ["APRS 145.825", "SSTV (events)", "FM voice"]),
    ("OSCAR 7",       "AO-7",       ["CW beacon 145.9775/29.502", "SSB/CW linear"]),
    ("JAS-2 (FO-29)", "FO-29",      ["CW telemetry 435.795", "SSB/CW linear"]),
    ("FUNCUBE-1",     "AO-73",      ["CW/BPSK beacon 145.935"]),
    ("RS-44",         "RS-44",      ["CW beacon 435.605", "SSB/CW linear"]),
    ("XW-3",          "CAS-9",      ["CW beacon 145.855?", "SSB/CW linear"]),
    ("JY1SAT",        "JO-97",      ["CW/FSK telemetry 435.100", "SSB/CW linear"]),
    ("LILACSAT-2",    "LilacSat-2", ["CW telemetry 437.200", "FM / digital"]),
    ("SAUDISAT 1C",   "SO-50",      ["FM voice"]),
    ("FOX-1A",        "AO-85",      ["FM voice", "telemetry"]),
    ("FOX-1CLIFF",    "AO-95",      ["FM voice", "telemetry"]),
    ("GREENCUBE",     "IO-117",     ["digipeater 435.310"]),
    ("ASRTU-1",       "AO-123",     ["FM voice"]),
    ("ES'HAIL 2",     "QO-100",     ["FT8 / CW / SSB narrow-band transponder (GEO, 25.5E)"]),
    # NOAA 15/18/19 (APT) were retired in 2025; Meteor-M LRPT is the 137 MHz
    # weather-imagery downlink still flying. GOES-East/West for the GOES receiver.
    ("METEOR-M2 3",   "Meteor-M2 3", ["LRPT 137.9 weather imagery"]),
    ("METEOR-M2 4",   "Meteor-M2 4", ["LRPT 137.9 weather imagery"]),
    ("GOES 19",       "GOES-19 (East)", ["HRIT/EMWIN 1694.1 MHz (GEO)"]),
    ("GOES 18",       "GOES-18 (West)", ["HRIT/EMWIN 1694.1 MHz (GEO)"]),
]

_WGS_A = 6378.137
_WGS_F = 1 / 298.257223563
_WGS_E2 = _WGS_F * (2 - _WGS_F)


def _gmst_rad(jd_ut1: float) -> float:
    d = jd_ut1 - 2451545.0
    return math.radians((280.46061837 + 360.98564736629 * d) % 360.0)


def _ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - _WGS_E2))
    for _ in range(5):
        n = _WGS_A / math.sqrt(1 - _WGS_E2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n if abs(lat) < 1.4 else z / math.sin(lat) - n * (1 - _WGS_E2)
        lat = math.atan2(z, p * (1 - _WGS_E2 * n / (n + alt)))
    return math.degrees(lat), math.degrees(lon), alt


def _hub_ecef(lat: float, lon: float) -> tuple[tuple, tuple]:
    la, lo = math.radians(lat), math.radians(lon)
    n = _WGS_A / math.sqrt(1 - _WGS_E2 * math.sin(la) ** 2)
    pos = (n * math.cos(la) * math.cos(lo), n * math.cos(la) * math.sin(lo), n * (1 - _WGS_E2) * math.sin(la))
    up = (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
    return pos, up


def parse_tle_text(text: str) -> list[tuple[str, str, str]]:
    """3-line TLE text -> [(name, line1, line2)]."""
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    out = []
    i = 0
    while i + 2 < len(lines):
        if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            out.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return out


def catalog_match(name: str) -> tuple[str, list[str]] | None:
    up = name.upper()
    for key, label, modes in CATALOG:
        if key in up:
            return label, modes
    return None


def launch_key(line1: str) -> str:
    """International designator minus the piece letter, e.g. '24069' (year+launch)."""
    return line1[9:14].strip()


class SatelliteService:
    def __init__(self, cache_dir: str | Path | None = None, extra_names: list[str] | None = None):
        self._cache_path = Path(cache_dir) / "tle_cache.json" if cache_dir else None
        self._extra = [n.upper() for n in (extra_names or [])]
        self._tles: dict[str, list] = {}       # group -> [(name,l1,l2)]
        self._fetched: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._result: tuple[float, dict] | None = None
        self._load_cache()

    # ── TLE acquisition ────────────────────────────────────────────────────
    def _load_cache(self) -> None:
        try:
            if self._cache_path and self._cache_path.exists():
                d = json.loads(self._cache_path.read_text(encoding="utf-8"))
                self._tles = {k: [tuple(t) for t in v] for k, v in d.get("tles", {}).items()}
                self._fetched = {k: float(v) for k, v in d.get("fetched", {}).items()}
        except Exception as exc:
            log.warning("Satellites: TLE cache unreadable: %s", exc)

    def _save_cache(self) -> None:
        try:
            if self._cache_path:
                self._cache_path.write_text(json.dumps({"tles": self._tles, "fetched": self._fetched}), encoding="utf-8")
        except Exception as exc:
            log.warning("Satellites: could not write TLE cache: %s", exc)

    async def _ensure(self, groups: tuple[str, ...]) -> None:
        now = time.time()
        stale = [g for g in groups if now - self._fetched.get(g, 0) > TLE_MAX_AGE_S]
        if not stale:
            return
        async with self._lock:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for g in stale:
                    if time.time() - self._fetched.get(g, 0) <= TLE_MAX_AGE_S:
                        continue
                    try:
                        r = await client.get(CELESTRAK.format(group=g))
                        r.raise_for_status()
                        tles = parse_tle_text(r.text)
                        if tles:
                            self._tles[g] = tles
                            self._fetched[g] = time.time()
                    except Exception as exc:
                        log.warning("Satellites: TLE fetch %s failed: %s", g, exc)
                        # keep stale data; avoid hammering on failure
                        self._fetched[g] = time.time() - TLE_MAX_AGE_S + 7200   # CelesTrak 403s repeat downloads inside ~2 h
            self._save_cache()

    # ── computation (sync, run in a thread) ───────────────────────────────
    @staticmethod
    def _positions(tles: list[tuple[str, str, str]], when: datetime):
        from sgp4.api import Satrec, SatrecArray, jday
        import numpy as np
        sats, keep = [], []
        for t in tles:
            try:
                sats.append(Satrec.twoline2rv(t[1], t[2]))
                keep.append(t)
            except Exception:
                continue
        if not sats:
            return [], np.zeros((0, 3))
        jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute,
                      when.second + when.microsecond / 1e6)
        e, r, _v = SatrecArray(sats).sgp4(np.array([jd]), np.array([fr]))
        theta = _gmst_rad(jd + fr)
        c, s = math.cos(theta), math.sin(theta)
        x, y, z = r[:, 0, 0], r[:, 0, 1], r[:, 0, 2]
        ecef = np.stack([x * c + y * s, -x * s + y * c, z], axis=1)
        ok = (e[:, 0] == 0) & np.isfinite(ecef).all(axis=1)
        return [t for t, good in zip(keep, ok) if good], ecef[ok]

    @staticmethod
    def _elevation_deg(ecef, hub) -> "list[float]":
        import numpy as np
        pos, up = _hub_ecef(*hub)
        rng = ecef - np.array(pos)
        dist = np.linalg.norm(rng, axis=1)
        return np.degrees(np.arcsin((rng @ np.array(up)) / np.maximum(dist, 1e-9)))

    def _build(self, hub, include_starlink: bool, now: datetime) -> dict:
        import numpy as np
        tracked = []
        pool: dict[str, tuple] = {}
        for g in GROUPS:
            for t in self._tles.get(g, []):
                pool.setdefault(t[0], t)
        wanted = []
        for name, t in pool.items():
            m = catalog_match(name)
            if m or any(x in name.upper() for x in self._extra):
                wanted.append((t, m))
        kept, ecef = self._positions([w[0] for w in wanted], now)
        meta = {w[0][0]: w[1] for w in wanted}
        elev = self._elevation_deg(ecef, hub) if hub and len(ecef) else [None] * len(kept)
        for t, p, el in zip(kept, ecef, elev):
            lat, lon, alt = _ecef_to_geodetic(float(p[0]), float(p[1]), float(p[2]))
            m = meta.get(t[0])
            tracked.append({
                "name": t[0], "label": m[0] if m else t[0], "modes": m[1] if m else [],
                "lat": round(lat, 3), "lon": round(lon, 3), "alt_km": round(alt, 0),
                "elevation": None if el is None else round(float(el), 1),
                "above_horizon": bool(el is not None and el > 0),
            })
        out = {"satellites": tracked, "starlink": None}
        if include_starlink:
            out["starlink"] = self._starlink(hub, now)
        return out

    def _starlink(self, hub, now: datetime) -> dict:
        import numpy as np
        tles = self._tles.get("starlink", [])
        kept, ecef = self._positions(tles, now)
        info = {"total": len(kept), "trains": [], "above_horizon": 0}
        if not len(kept):
            return info
        elev = self._elevation_deg(ecef, hub) if hub else None
        if elev is not None:
            info["above_horizon"] = int((elev > 10).sum())
        groups: dict[str, list[int]] = {}
        for i, t in enumerate(kept):
            groups.setdefault(launch_key(t[1]), []).append(i)
        for key, idx in groups.items():
            if len(idx) < 5:
                continue
            pts = ecef[idx]
            med = np.median(pts, axis=0)
            near = [i for i, p in zip(idx, pts) if np.linalg.norm(p - med) < 1500.0]
            if len(near) < 5:
                continue
            c = ecef[near].mean(axis=0)
            lat, lon, alt = _ecef_to_geodetic(float(c[0]), float(c[1]), float(c[2]))
            el = float(elev[near].max()) if elev is not None else None
            info["trains"].append({
                "id": key, "count": len(near), "lat": round(lat, 2), "lon": round(lon, 2),
                "alt_km": round(alt, 0), "max_elevation": None if el is None else round(el, 1),
                "visible_now": bool(el is not None and el > 10),
                "rep": kept[near[0]][0],
                "passes": self._passes(kept[near[0]], hub, now) if hub else [],
            })
        info["trains"].sort(key=lambda t: (-int(t["visible_now"]), -t["count"]))
        info["trains"] = info["trains"][:12]
        return info

    @staticmethod
    def _passes(tle, hub, now: datetime, hours: int = 24, min_el: float = 10.0) -> list[dict]:
        from sgp4.api import Satrec, jday
        import numpy as np
        sat = Satrec.twoline2rv(tle[1], tle[2])
        step = 30
        n = hours * 3600 // step
        times = [now + timedelta(seconds=step * i) for i in range(n)]
        jd = np.array([jday(t.year, t.month, t.day, t.hour, t.minute, t.second)[0] for t in times])
        fr = np.array([jday(t.year, t.month, t.day, t.hour, t.minute, t.second)[1] for t in times])
        e, r, _ = sat.sgp4_array(jd, fr)
        pos, up = _hub_ecef(*hub)
        out, cur = [], None
        for i in range(n):
            if e[i] != 0:
                continue
            theta = _gmst_rad(jd[i] + fr[i])
            c, s = math.cos(theta), math.sin(theta)
            x, y, z = r[i]
            p = np.array([x * c + y * s, -x * s + y * c, z])
            rng = p - np.array(pos)
            el = math.degrees(math.asin(float(rng @ np.array(up)) / float(np.linalg.norm(rng))))
            if el >= min_el:
                if cur is None:
                    cur = {"rise": times[i], "max": el, "max_t": times[i]}
                elif el > cur["max"]:
                    cur["max"], cur["max_t"] = el, times[i]
            elif cur is not None:
                out.append({"rise": cur["rise"].isoformat(), "set": times[i].isoformat(),
                            "max_elevation": round(cur["max"], 0), "max_time": cur["max_t"].isoformat()})
                cur = None
        return out[:4]

    # ── public API ────────────────────────────────────────────────────────
    async def snapshot(self, hub: tuple[float, float] | None, include_starlink: bool = False) -> dict:
        if self._result and time.time() - self._result[0] < 8 and \
                bool(self._result[1].get("starlink")) == include_starlink:
            return self._result[1]
        groups = GROUPS + (("starlink",) if include_starlink else ())
        await self._ensure(groups)
        now = datetime.now(timezone.utc)
        data = await asyncio.to_thread(self._build, hub, include_starlink, now)
        data["updated"] = now.isoformat()
        ages = [time.time() - self._fetched[g] for g in groups if g in self._fetched]
        data["tle_age_h"] = round(max(ages) / 3600, 1) if ages else None
        data["hub"] = {"lat": hub[0], "lon": hub[1]} if hub else None
        self._result = (time.time(), data)
        return data

    async def ground_track(self, name: str, minutes: int = 100) -> list[list[float]]:
        await self._ensure(GROUPS)
        t = next((t for g in GROUPS for t in self._tles.get(g, []) if t[0] == name), None)
        if not t:
            return []

        def calc():
            from sgp4.api import Satrec, jday
            sat = Satrec.twoline2rv(t[1], t[2])
            now = datetime.now(timezone.utc)
            pts = []
            for i in range(0, minutes * 60 + 1, 60):
                w = now + timedelta(seconds=i)
                jd, fr = jday(w.year, w.month, w.day, w.hour, w.minute, w.second)
                e, r, _ = sat.sgp4(jd, fr)
                if e:
                    continue
                th = _gmst_rad(jd + fr)
                c, s = math.cos(th), math.sin(th)
                lat, lon, _a = _ecef_to_geodetic(r[0] * c + r[1] * s, -r[0] * s + r[1] * c, r[2])
                pts.append([round(lat, 2), round(lon, 2)])
            return pts
        return await asyncio.to_thread(calc)
