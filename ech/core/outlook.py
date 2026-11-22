"""Local outlook: maple sap runs, ski conditions, regional trains, and a
tongue-in-cheek doom-o-meter, for the mesh bot, the Messages page and the
operator's feed (operator request, 2026-11-22).

Data (all free, no keys):
  * NWS gridpoint forecasts (api.weather.gov/gridpoints/...): hourly temperature,
    humidity, snowfall and rain amounts for the base location and each ski area.
  * Amtrak GTFS (content.amtrak.com/content/gtfs/GTFS.zip): scheduled trains at the
    nearest rail stations, refreshed weekly. Schedules, not live positions.
  * NOAA SWPC planetary K index, JPL close-approach data, USGS significant quakes,
    and the Bulletin of the Atomic Scientists' Doomsday Clock (set by hand, it
    changes once a year) for the doom-o-meter. The meter is for fun.

config.yaml (all optional):
  outlook:
    enabled: true
    ski_areas: [{name: "Camden Snow Bowl", lat: 44.2190, lon: -69.1230}, ...]
    train_stations: ["BRK", "FRE"]     # GTFS stop ids; default: the 2 nearest rail stops
    train_radius_km: 150
    doomsday_clock: {seconds: 85, set: "2026-01-27"}
    garden: {zone: "6a", last_frost: "05-10", first_frost: "10-05"}   # default: looked up / by zone
    hunting: {wmd: 26, expanded_archery: true,           # filter the default Maine seasons to your area
              seasons: [{name, start: "2026-11-02", end: "2026-11-28"}], no_sunday: true,
              hours_before_sunrise_min: 30, hours_after_sunset_min: 30}     # default: Maine deer 2026
    notify: true                       # sap runs, powder days, geomagnetic storms -> the feed
    notify_adapters: []                # also post those to a mesh channel (default: feed only)
    notify_channel: ""                 # e.g. "#weather"
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import httpx

from ech.core import identity

log = logging.getLogger(__name__)

NWS = "https://api.weather.gov"
GTFS_URL = "https://content.amtrak.com/content/gtfs/GTFS.zip"
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
CAD_URL = "https://ssd-api.jpl.nasa.gov/cad.api"
HISTORY_URL = "https://api.open-meteo.com/v1/forecast"   # past_days= gives recent daily history
QUAKE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson"
LD_AU = 0.00257                      # one lunar distance in astronomical units

DEFAULT_SKI_AREAS = [
    {"name": "Camden Snow Bowl", "lat": 44.2190, "lon": -69.1230},
    {"name": "Sugarloaf", "lat": 45.0314, "lon": -70.3131},
    {"name": "Sunday River", "lat": 44.4734, "lon": -70.8564},
    {"name": "Saddleback", "lat": 44.9380, "lon": -70.5050},
]
DOOMSDAY_DEFAULT = {"seconds": 85, "set": "2026-01-27"}   # thebulletin.org, Jan 27 2026

GRID_TTL = 3600
DOOM_TTL = 1800
GTFS_MAX_AGE = 7 * 86400


# ── NWS gridpoint series ───────────────────────────────────────────────────

_DUR_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")


def _span(valid_time: str) -> tuple[datetime, int]:
    """"2026-11-22T01:00:00+00:00/PT5H" -> (start, hours)."""
    start, _, dur = valid_time.partition("/")
    m = _DUR_RE.fullmatch(dur or "PT1H")
    hours = 1
    if m:
        hours = int(m.group(1) or 0) * 24 + int(m.group(2) or 0) + (1 if m.group(3) else 0)
    return datetime.fromisoformat(start), max(1, hours)


def hourly(series: dict | None, spread: bool = False) -> list[tuple[datetime, float]]:
    """Expand an NWS gridpoint series to one value per hour. `spread` divides
    amounts (snow, rain) evenly over their period instead of repeating them."""
    out = []
    for v in (series or {}).get("values", []):
        if v.get("value") is None:
            continue
        start, hours = _span(v["validTime"])
        val = float(v["value"]) / hours if spread else float(v["value"])
        out += [(start + timedelta(hours=h), val) for h in range(hours)]
    return out


def daily(props: dict, tz=None) -> list[dict]:
    """Per local day: high/low (F), snow (in), rain (in), snowmaking hours."""
    tz = tz or datetime.now().astimezone().tzinfo
    days: dict[date, dict] = {}

    def day(dt):
        d = dt.astimezone(tz).date()
        return days.setdefault(d, {"date": d, "temps": [], "snow_mm": 0.0, "qpf_mm": 0.0,
                                   "make_h": 0, "rain_mm": 0.0})
    temps = dict(hourly(props.get("temperature")))
    rh = dict(hourly(props.get("relativeHumidity")))
    for dt, t in temps.items():
        d = day(dt)
        d["temps"].append(t)
        if wet_bulb_c(t, rh.get(dt, 80.0)) <= -2.5:
            d["make_h"] += 1                       # snowmaking weather (wet bulb about 27F or colder)
    for dt, kmh in hourly(props.get("windSpeed")):
        d = day(dt)
        d["wind_max"] = max(d.get("wind_max", 0.0), kmh)
    for dt, mm in hourly(props.get("snowfallAmount"), spread=True):
        day(dt)["snow_mm"] += mm
    for dt, mm in hourly(props.get("quantitativePrecipitation"), spread=True):
        d = day(dt)
        d["qpf_mm"] += mm
        if temps.get(dt, 0.0) > 1.0:
            d["rain_mm"] += mm
    out = []
    for d in sorted(days.values(), key=lambda x: x["date"]):
        if not d["temps"]:
            continue
        out.append({"date": d["date"].isoformat(), "dow": d["date"].strftime("%a"),
                    "hi_f": round(c_to_f(max(d["temps"]))), "lo_f": round(c_to_f(min(d["temps"]))),
                    "snow_in": round(d["snow_mm"] / 25.4, 1), "rain_in": round(d["rain_mm"] / 25.4, 2),
                    "snowmaking_h": d["make_h"], "hours": len(d["temps"]),
                    "wind_max_mph": round(d.get("wind_max", 0.0) * 0.621)})
    return out


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def wet_bulb_c(t: float, rh: float) -> float:
    """Stull (2011) wet-bulb approximation, good to ~1 C for normal conditions."""
    rh = max(5.0, min(99.0, rh))
    return (t * math.atan(0.151977 * math.sqrt(rh + 8.313659)) + math.atan(t + rh)
            - math.atan(rh - 1.676331) + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)


# ── Maple sap ──────────────────────────────────────────────────────────────

def sap_outlook(days: list[dict], today: date | None = None, history: list[dict] | None = None) -> dict:
    """Sap runs on a freeze-thaw cycle: a night below freezing (ideally 20-28F)
    followed by a day above (ideally 40-50F). That only makes a sugaring season
    after the trees have been through a long hard freeze; a November thaw gives
    a thin "fall flow" that isn't worth tapping. So the season needs both the
    late-winter calendar (mid January to April in Maine) and a cold spell in the
    last 45 days (10+ days that never got above freezing, or 150+ freezing
    degree-days). It ends when nights stop freezing and buds break."""
    today = today or date.today()
    out = []
    prev_lo = None
    for d in days:
        lo, hi = d["lo_f"], d["hi_f"]
        night = min(lo, prev_lo) if prev_lo is not None else lo
        if 20 <= night <= 28 and 40 <= hi <= 50:
            rating = "good"
        elif night <= 32 and hi >= 38:
            rating = "fair"
        else:
            rating = "none"
        out.append({"date": d["date"], "dow": d["dow"], "lo_f": lo, "hi_f": hi, "rating": rating})
        prev_lo = lo
    good = [d for d in out if d["rating"] == "good"]
    flowing = [d for d in out if d["rating"] != "none"]
    past = [h for h in (history or []) if h["date"] < today.isoformat()][-45:]
    cold_days = sum(1 for h in past if h["hi_f"] <= 32)
    fdd = round(sum(max(0.0, 32 - (h["hi_f"] + h["lo_f"]) / 2) for h in past))
    primed = (cold_days >= 10 or fdd >= 150) if past else None
    window = date(today.year, 1, 15) <= today <= date(today.year, 4, 30)
    in_season = window and (primed if primed is not None else today.month in (2, 3, 4))
    if in_season:
        season = "tapping season"
    elif window:
        season = f"waiting for a deep cold spell ({cold_days} freezing days so far)"
    elif today.month in (5, 6, 7, 8, 9, 10):
        season = "off-season (trees have budded out)"
    else:
        season = "off-season: trees need a long hard freeze first; taps usually go in late February"
    if flowing:
        best = good[0] if good else flowing[0]
        summary = (f"{len(flowing)} freeze-thaw day(s) in the forecast ({len(good)} good); "
                   f"best next: {best['dow']} {best['lo_f']}F/{best['hi_f']}F")
        if not in_season:
            summary += " (fall/winter flow: thin sap, not worth tapping)" if not window else \
                       " (too early: no deep freeze yet)"
    else:
        summary = "no freeze-thaw days in the forecast: sap won't run"
    advice = ""
    if in_season and len(flowing) >= 3:
        advice = "Buckets out: a run of freeze-thaw days is coming."
    elif in_season and not flowing:
        warm = all(d["lo_f"] > 32 for d in out[:4]) if out else False
        advice = "Nights staying above freezing: the run may be ending." if warm else "Too cold to run yet."
    return {"season": season, "in_season": in_season, "summary": summary, "advice": advice,
            "days": out, "good_days": len(good), "sap_days": len(flowing), "primed": primed,
            "cold_days_45": cold_days if past else None, "freezing_degree_days_45": fdd if past else None}


# ── Skiing ─────────────────────────────────────────────────────────────────

def ski_outlook(name: str, days: list[dict]) -> dict:
    nxt = days[:7]
    snow = round(sum(d["snow_in"] for d in nxt), 1)
    big = max(nxt, key=lambda d: d["snow_in"], default=None)
    make_h = sum(d["snowmaking_h"] for d in nxt)
    rain = [d for d in nxt if d["rain_in"] >= 0.1]
    if big and big["snow_in"] >= 6:
        rating = "powder day"
        why = f"{big['snow_in']:g}\" {big['dow']}"
    elif snow >= 2:
        rating = "fresh snow"
        why = f"{snow:g}\" this week"
    elif rain and (make_h < 12 or sum(d["rain_in"] for d in rain) >= 0.5):
        rating = "thaw / rain"
        wet = max(rain, key=lambda d: d["rain_in"])
        why = f"{wet['rain_in']:g}\" rain {wet['dow']}"
    elif make_h >= 24:
        rating = "snowmaking weather"
        why = f"{make_h} cold hours"
    else:
        rating = "quiet"
        why = "little new snow"
    return {"name": name, "rating": rating, "why": why, "snow_7d_in": snow, "snowmaking_h": make_h,
            "rain_days": [d["dow"] for d in rain],
            "days": [{k: d[k] for k in ("date", "dow", "hi_f", "lo_f", "snow_in", "rain_in")} for d in nxt]}


# ── Trains (GTFS) ──────────────────────────────────────────────────────────

def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def build_timetable(zip_bytes: bytes, lat: float | None, lon: float | None,
                    stations: list[str] | None = None, radius_km: float = 150, nearest: int = 2) -> dict:
    """Reduce a GTFS feed to the rail trips calling at a few stations near the base."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))

    def rows(name):
        return csv.DictReader(io.TextIOWrapper(z.open(name), "utf-8-sig"))
    routes = {r["route_id"]: r for r in rows("routes.txt") if r.get("route_type") == "2"}   # 2 = rail
    trips = {t["trip_id"]: t for t in rows("trips.txt") if t["route_id"] in routes}
    rail_stops = set()
    calls: dict[str, list] = {}
    for st in rows("stop_times.txt"):
        if st["trip_id"] in trips:
            rail_stops.add(st["stop_id"])
            calls.setdefault(st["trip_id"], []).append(st)
    stops = {s["stop_id"]: s for s in rows("stops.txt") if s["stop_id"] in rail_stops}
    if stations:
        pick = [s for s in stations if s in stops]
    elif lat is not None and lon is not None:
        ranked = sorted(stops.values(), key=lambda s: _haversine_km(lat, lon, float(s["stop_lat"]), float(s["stop_lon"])))
        pick = [s["stop_id"] for s in ranked[:nearest]
                if _haversine_km(lat, lon, float(s["stop_lat"]), float(s["stop_lon"])) <= radius_km]
    else:
        pick = []
    cal = {c["service_id"]: c for c in rows("calendar.txt")}
    out_stations = []
    for sid in pick:
        s = stops[sid]
        entries = []
        for tid, sts in calls.items():
            sts.sort(key=lambda x: int(x["stop_sequence"]))
            here = next((x for x in sts if x["stop_id"] == sid), None)
            if not here:
                continue
            t = trips[tid]
            last = sts[-1]
            entries.append({
                "train": t.get("trip_short_name") or tid, "route": routes[t["route_id"]]["route_long_name"],
                "headsign": t.get("trip_headsign") or stops.get(last["stop_id"], {}).get("stop_name", last["stop_id"]),
                "time": (here["departure_time"] or here["arrival_time"]),
                "terminates": here is last, "origin": stops.get(sts[0]["stop_id"], {}).get("stop_name", ""),
                "service": t["service_id"]})
        if entries:
            dist = _haversine_km(lat, lon, float(s["stop_lat"]), float(s["stop_lon"])) if lat is not None else None
            out_stations.append({"id": sid, "name": s["stop_name"], "lat": float(s["stop_lat"]),
                                 "lon": float(s["stop_lon"]), "tz": s.get("stop_timezone") or "America/New_York",
                                 "distance_km": round(dist, 1) if dist is not None else None, "trips": entries})
    used = {e["service"] for st in out_stations for e in st["trips"]}
    return {"built": time.time(), "stations": out_stations,
            "calendar": {k: v for k, v in cal.items() if k in used}}


def _runs_on(cal: dict, day: date) -> bool:
    c = cal or {}
    if not c:
        return True
    d = day.strftime("%Y%m%d")
    if not (c.get("start_date", "0") <= d <= c.get("end_date", "99999999")):
        return False
    return c.get(day.strftime("%A").lower()) == "1"


def next_trains(tt: dict, now: datetime | None = None, limit: int = 6) -> list[dict]:
    """Upcoming calls per station, today then tomorrow."""
    from zoneinfo import ZoneInfo
    out = []
    for st in tt.get("stations", []):
        tz = ZoneInfo(st.get("tz") or "America/New_York")
        n = (now or datetime.now(timezone.utc)).astimezone(tz)
        upcoming = []
        for offset in (0, 1):
            day = n.date() + timedelta(days=offset)
            for e in st["trips"]:
                if not _runs_on(tt.get("calendar", {}).get(e["service"]), day):
                    continue
                h, m, *_ = (int(x) for x in e["time"].split(":"))
                when = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(hours=h, minutes=m)
                if when >= n - timedelta(minutes=2):
                    upcoming.append({**{k: e[k] for k in ("train", "route", "headsign", "terminates", "origin")},
                                     "when": when.isoformat(), "label": _clock(when, n)})
        upcoming.sort(key=lambda x: x["when"])
        out.append({"id": st["id"], "name": st["name"], "distance_km": st.get("distance_km"),
                    "lat": st["lat"], "lon": st["lon"], "next": upcoming[:limit]})
    return out


def _clock(when: datetime, now: datetime) -> str:
    h = when.hour % 12 or 12
    s = f"{h}:{when.minute:02d}{'a' if when.hour < 12 else 'p'}"
    return s if when.date() == now.date() else f"{when.strftime('%a')} {s}"


# ── Doom-o-meter ───────────────────────────────────────────────────────────

DOOM_LEVELS = [
    (15, "Situation normal. Carry on, maybe floss."),
    (35, "Mildly ominous. Keep the radio charged."),
    (60, "Ominous. Fill the water jugs, charge the HT."),
    (85, "Grab the go-bag and check on the neighbors."),
    (101, "Root cellar time. Bring snacks and spare batteries."),
]


def doom_score(kp: float | None, closest_ld: float | None, quake_mag: float | None,
               local_warnings: int, extreme_local: bool, clock_seconds: int | None) -> tuple[int, str]:
    score = 0.0
    if kp is not None:
        score += max(0.0, kp - 3) * 5                         # Kp 9 adds 30
    if closest_ld is not None:
        score += 20 if closest_ld < 1 else 10 if closest_ld < 3 else 3 if closest_ld < 10 else 0
    if quake_mag is not None:
        score += 20 if quake_mag >= 7.5 else 12 if quake_mag >= 7 else 5 if quake_mag >= 6 else 0
    score += 25 if extreme_local else min(20, local_warnings * 8)
    if clock_seconds is not None:
        score += max(0.0, (120 - clock_seconds) / 4)           # 85 s to midnight adds ~9
    score = int(min(100, round(score)))
    label = next(lbl for limit, lbl in DOOM_LEVELS if score < limit)
    return score, label


def parse_kp(rows) -> tuple[float | None, float | None]:
    """SWPC planetary K json: latest Kp and 24 h max. Handles both the list-of-lists
    and the list-of-dicts layouts the product has used."""
    vals = []
    for r in rows or []:
        if isinstance(r, dict):
            v = r.get("Kp", r.get("kp_index"))
        elif isinstance(r, list) and len(r) > 1 and r[0] != "time_tag":
            v = r[1]
        else:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            pass
    if not vals:
        return None, None
    return vals[-1], max(vals[-8:])


def parse_cad(d: dict) -> list[dict]:
    f = d.get("fields") or []
    out = []
    for row in d.get("data") or []:
        r = dict(zip(f, row))
        try:
            dist_ld = float(r["dist"]) / LD_AU
            h = float(r["h"]) if r.get("h") else None
        except (KeyError, ValueError):
            continue
        size = None
        if h is not None:   # diameter from absolute magnitude, albedo 0.14
            size = round(1329 / math.sqrt(0.14) * 10 ** (-h / 5) * 1000)
        out.append({"name": r.get("des"), "date": r.get("cd"), "dist_ld": round(dist_ld, 2), "size_m": size})
    return sorted(out, key=lambda x: x["dist_ld"])


# ── Garden: hardiness zone and tomatoes ────────────────────────────────────

ZONE_URL = "https://phzmapi.org/{zip}.json"          # USDA 2023 Plant Hardiness Zone Map by ZIP
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
# Typical average last spring / first fall frost by USDA zone (month, day). Rough:
# local frost dates vary a lot (coast vs. inland), so config garden.last_frost wins.
FROST_BY_ZONE = {
    "3a": ((6, 1), (9, 8)), "3b": ((5, 25), (9, 15)), "4a": ((5, 20), (9, 20)), "4b": ((5, 15), (9, 25)),
    "5a": ((5, 12), (10, 1)), "5b": ((5, 5), (10, 5)), "6a": ((5, 10), (10, 5)), "6b": ((4, 25), (10, 15)),
    "7a": ((4, 15), (10, 25)), "7b": ((4, 5), (11, 1)), "8a": ((3, 28), (11, 10)), "8b": ((3, 18), (11, 20)),
    "9a": ((3, 5), (11, 30)), "9b": ((2, 20), (12, 10)), "10a": ((1, 31), (12, 20)), "10b": ((1, 15), (12, 31)),
}


def _md(s: str | None) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})\s*", str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def tomato_plan(zone: str | None, today: date | None = None, last_frost: str | None = None,
                first_frost: str | None = None, days: list[dict] | None = None) -> dict:
    """Start seeds indoors 6-8 weeks before the last frost; set plants out 1-2 weeks
    after it, once nights stay at 50F or warmer (cold nights stall tomatoes)."""
    today = today or date.today()
    z = (zone or "").lower()
    lf, ff = FROST_BY_ZONE.get(z, ((5, 15), (10, 1)))
    lf = _md(last_frost) or lf
    ff = _md(first_frost) or ff
    year = today.year
    frost = date(year, *lf)
    if today > date(year, *ff):                      # this season is over: plan next spring
        frost = date(year + 1, *lf)
    fall = date(frost.year, *ff)
    seed_from, seed_to = frost - timedelta(weeks=8), frost - timedelta(weeks=6)
    plant_from, plant_to = frost + timedelta(weeks=1), frost + timedelta(weeks=2)
    md = lambda d: f"{d:%b} {d.day}"
    if today < seed_from:
        stage, advice = "wait", f"Start seeds indoors {md(seed_from)}-{md(seed_to)}."
    elif today <= seed_to:
        stage, advice = "start seeds", "Start tomato seeds indoors now (6-8 weeks before last frost)."
    elif today < plant_from - timedelta(days=7):
        stage, advice = "grow on", f"Seedlings growing: harden them off outside a week before {md(plant_from)}."
    elif today <= fall:
        stage, advice = "plant out", "Planting window."
    else:
        stage, advice = "season over", ""
    warm_nights = None
    if stage == "plant out" and days:
        lows = [d["lo_f"] for d in days[:7]]
        warm_nights = all(lo >= 50 for lo in lows[:5]) if len(lows) >= 5 else None
        cold = min(lows) if lows else None
        if today < plant_from - timedelta(days=7):
            advice = "Too early: harden seedlings off first."
        elif warm_nights:
            advice = "Nights are staying at 50F+ this week: plant tomatoes out."
        elif cold is not None and cold <= 32:
            advice = f"Frost risk this week (low {cold}F): keep tomatoes inside or covered."
        else:
            advice = f"Nights still dip to {cold}F: wait for 50F+ nights, or plant under cover."
        if today > fall - timedelta(weeks=3):
            advice = "Late season: ripen what's on the vine; first frost is near."
    return {"zone": zone, "last_frost": frost.isoformat(), "first_frost": fall.isoformat(),
            "seed_start": [seed_from.isoformat(), seed_to.isoformat()],
            "plant_out": [plant_from.isoformat(), plant_to.isoformat()],
            "stage": stage, "advice": advice, "warm_nights": warm_nights,
            "frost_source": "config" if _md(last_frost) else "typical for the zone"}


# ── Hunting ────────────────────────────────────────────────────────────────

# Maine 2026 deer seasons (MDIFW season dates & bag limits). Sunday hunting is
# closed statewide. Check the lawbook for WMD-specific rules and permits.
MAINE_DEER_2026 = [
    {"name": "Expanded archery (designated areas)", "start": "2026-09-12", "end": "2026-12-12",
     "expanded_archery": True},
    {"name": "Regular archery", "start": "2026-10-03", "end": "2026-10-30"},
    {"name": "Youth deer day", "start": "2026-10-23", "end": "2026-10-24"},
    {"name": "Resident-only day", "start": "2026-10-31", "end": "2026-10-31"},
    {"name": "Firearms", "start": "2026-11-02", "end": "2026-11-28"},
    {"name": "Muzzleloader (statewide)", "start": "2026-11-30", "end": "2026-12-05"},
    {"name": "Muzzleloader extended (WMDs 12-18, 20-29)", "start": "2026-12-07", "end": "2026-12-12",
     "wmds": list(range(12, 19)) + list(range(20, 30))},
]
SYNODIC = 29.530588853
# MDIFW's own published layers (ArcGIS Online): WMD polygons and Expanded Archery Areas.
MDIFW_GIS = "https://services1.arcgis.com/RbMX0mRVOFNTdLzd/ArcGIS/rest/services"


async def maine_hunt_area(client, lat: float, lon: float) -> dict:
    """{wmd, expanded_archery, nearby_archery_area} for a point, from MDIFW's GIS."""
    pt = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
          "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false", "f": "json"}
    r = await client.get(f"{MDIFW_GIS}/WMD/FeatureServer/0/query", params={**pt, "outFields": "IDENTIFIER"})
    r.raise_for_status()
    feats = r.json().get("features") or []
    wmd = feats[0]["attributes"].get("IDENTIFIER") if feats else None
    r = await client.get(f"{MDIFW_GIS}/MaineDIFW_ExpandedArcheryAreas/FeatureServer/0/query",
                         params={**pt, "outFields": "NAME"})
    r.raise_for_status()
    inside = [f["attributes"].get("NAME") for f in r.json().get("features") or []]
    near = []
    if not inside:
        r = await client.get(f"{MDIFW_GIS}/MaineDIFW_ExpandedArcheryAreas/FeatureServer/0/query",
                             params={**pt, "outFields": "NAME", "distance": "10000", "units": "esriSRUnit_Meter"})
        if r.status_code == 200:
            near = [f["attributes"].get("NAME") for f in r.json().get("features") or []]
    title = lambda s: (s or "").title()
    return {"wmd": wmd, "expanded_archery": bool(inside), "archery_area": title(inside[0]) if inside else None,
            "nearby_archery_area": title(near[0]) if near else None, "source": "MDIFW GIS"}


def moon_age(when: datetime | None = None) -> float:
    when = when or datetime.now(timezone.utc)
    jd = when.timestamp() / 86400 + 2440587.5
    return (jd - 2451550.1) % SYNODIC


def moon_phase_name(age: float) -> str:
    names = ["new moon", "waxing crescent", "first quarter", "waxing gibbous",
             "full moon", "waning gibbous", "last quarter", "waning crescent"]
    return names[int((age / SYNODIC) * 8 + 0.5) % 8]


def sun_times(lat: float, lon: float, day: date, tz=None) -> tuple[datetime | None, datetime | None]:
    """Sunrise and sunset (NOAA solar position algorithm, ~1 min accuracy)."""
    tz = tz or datetime.now().astimezone().tzinfo
    n = day.timetuple().tm_yday
    out = []
    for rising in (True, False):
        lng_hour = lon / 15
        t0 = n + ((6 if rising else 18) - lng_hour) / 24
        m = 0.9856 * t0 - 3.289
        lam = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(lam)))) % 360
        ra = (ra + (math.floor(lam / 90) * 90 - math.floor(ra / 90) * 90)) / 15
        sin_dec = 0.39782 * math.sin(math.radians(lam))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(math.radians(90.833)) - sin_dec * math.sin(math.radians(lat))) / (cos_dec * math.cos(math.radians(lat)))
        if not -1 <= cos_h <= 1:
            out.append(None)
            continue
        h = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
        local_t = h / 15 + ra - 0.06571 * t0 - 6.622
        ut = (local_t - lng_hour) % 24
        out.append((datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=ut)).astimezone(tz))
    return out[0], out[1]


def deer_day(d: dict, prev: dict | None, age: float, day: date) -> dict:
    """Deer movement heuristic: cold snaps, light wind, dry weather, the rut
    (peak in Maine roughly Nov 5-25) and, a little, the moon. Not a guarantee."""
    score, why = 0.5, []
    if prev and prev["hi_f"] - d["hi_f"] >= 8:
        score += 0.2; why.append(f"cold snap ({prev['hi_f'] - d['hi_f']}F colder)")
    elif prev and d["hi_f"] - prev["hi_f"] >= 10:
        score -= 0.1; why.append("warm spell")
    w = d.get("wind_max_mph")
    if w is not None:
        if w >= 20:
            score -= 0.2; why.append(f"windy ({w} mph)")
        elif w <= 10:
            score += 0.05; why.append("light wind")
    if d.get("rain_in", 0) >= 0.5:
        score -= 0.2; why.append("heavy rain")
    elif d.get("snow_in", 0) >= 0.5:
        score += 0.1; why.append("tracking snow")
    if date(day.year, 11, 5) <= day <= date(day.year, 11, 25):
        score += 0.15; why.append("rut")
    dist = min(age, abs(age - SYNODIC / 2), SYNODIC - age)
    if dist <= 2:
        score += 0.05; why.append(moon_phase_name(age))
    rating = "excellent" if score >= 0.8 else "good" if score >= 0.6 else "fair" if score >= 0.4 else "poor"
    return {"date": d["date"], "dow": d["dow"], "rating": rating, "why": ", ".join(why), "hi_f": d["hi_f"],
            "lo_f": d["lo_f"], "wind_max_mph": w}


def hunt_outlook(days: list[dict], lat: float, lon: float, seasons: list[dict], no_sunday: bool = True,
                 before_min: int = 30, after_min: int = 30, today: date | None = None) -> dict:
    today = today or date.today()
    iso = today.isoformat()
    open_now = [s for s in seasons if s["start"] <= iso <= s["end"]]
    upcoming = sorted((s for s in seasons if s["start"] > iso), key=lambda s: s["start"])
    sunday = no_sunday and today.weekday() == 6
    rise, sset = sun_times(lat, lon, today)
    hours = None
    if rise and sset:
        a, b = rise - timedelta(minutes=before_min), sset + timedelta(minutes=after_min)
        hours = [a.isoformat(), b.isoformat()]
    out_days = []
    prev = None
    for d in days[:7]:
        dd = date.fromisoformat(d["date"])
        x = deer_day(d, prev, moon_age(datetime(dd.year, dd.month, dd.day, 12, tzinfo=timezone.utc)), dd)
        x["closed"] = (no_sunday and dd.weekday() == 6) or not any(s["start"] <= d["date"] <= s["end"] for s in seasons)
        out_days.append(x)
        prev = d
    return {"open": [s["name"] for s in open_now], "sunday_closed": sunday,
            "next_season": upcoming[0] if upcoming else None, "legal_hours": hours,
            "days": out_days, "note": "deer movement estimate; check the MDIFW lawbook for your WMD"}


# ── Service ────────────────────────────────────────────────────────────────

class OutlookService:
    def __init__(self, config: dict, state=None, router=None, wx_service=None):
        self.configure(config)
        self._state = state
        self._router = router
        self._wx = wx_service
        db_path = (config.get("database") or {}).get("path", "ech.db")
        self._data_dir = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else None
        self._grid_cache: dict[tuple, tuple[float, list]] = {}
        self._points: dict[tuple, str] = {}
        self._doom: tuple[float, dict] | None = None
        self._tt: dict | None = None
        self._tt_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._notified: dict[str, float] = {}
        self.last_error = ""

    def configure(self, config: dict) -> None:
        cfg = config.get("outlook") or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.ski_areas = cfg.get("ski_areas") or DEFAULT_SKI_AREAS
        self.train_stations = [str(s).upper() for s in (cfg.get("train_stations") or [])]
        self.train_radius = float(cfg.get("train_radius_km", 150))
        self.clock = {**DOOMSDAY_DEFAULT, **(cfg.get("doomsday_clock") or {})}
        self.notify = bool(cfg.get("notify", True))
        self.notify_adapters = list(cfg.get("notify_adapters") or [])
        self.notify_channel = str(cfg.get("notify_channel") or "")
        self.garden = cfg.get("garden") or {}
        self.hunting = cfg.get("hunting") or {}

    def coords(self) -> tuple[float | None, float | None]:
        s = self._state
        lat, lon = getattr(s, "_base_lat", None), getattr(s, "_base_lon", None)
        return lat, lon

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers={"User-Agent": identity.user_agent()}, timeout=20.0,
                                 follow_redirects=True)

    # data
    async def grid_days(self, lat: float, lon: float) -> list[dict]:
        key = (round(lat, 3), round(lon, 3))
        hit = self._grid_cache.get(key)
        if hit and time.time() - hit[0] < GRID_TTL:
            return hit[1]
        try:
            async with self._client() as c:
                url = self._points.get(key)
                if not url:
                    r = await c.get(f"{NWS}/points/{lat:.4f},{lon:.4f}")
                    r.raise_for_status()
                    url = self._points[key] = r.json()["properties"]["forecastGridData"]
                r = await c.get(url)
                r.raise_for_status()
                days = daily(r.json()["properties"])
        except Exception:
            if hit:
                return hit[1]
            raise
        today = datetime.now().date().isoformat()
        days = [d for d in days if d["date"] >= today and d["hours"] >= 6]
        self._grid_cache[key] = (time.time(), days)
        return days

    async def history(self, lat: float, lon: float) -> list[dict]:
        """Daily highs/lows for the last 45 days (Open-Meteo, free, no key), cached 6 h."""
        key = (round(lat, 2), round(lon, 2))
        hit = getattr(self, "_hist", {}).get(key)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        async with self._client() as c:
            r = await c.get(HISTORY_URL, params={
                "latitude": f"{lat:.3f}", "longitude": f"{lon:.3f}", "past_days": "45", "forecast_days": "1",
                "daily": "temperature_2m_max,temperature_2m_min", "temperature_unit": "fahrenheit",
                "timezone": "auto"})
            r.raise_for_status()
            d = r.json().get("daily") or {}
        rows = [{"date": dt, "hi_f": hi, "lo_f": lo}
                for dt, hi, lo in zip(d.get("time", []), d.get("temperature_2m_max", []), d.get("temperature_2m_min", []))
                if hi is not None and lo is not None]
        self._hist = {**getattr(self, "_hist", {}), key: (time.time(), rows)}
        return rows

    async def sap(self) -> dict:
        lat, lon = self.coords()
        if lat is None:
            return {"error": "base location not set"}
        try:
            hist = await self.history(lat, lon)
        except Exception as exc:
            log.debug("Outlook: temperature history failed: %s", exc)
            hist = None
        return sap_outlook(await self.grid_days(lat, lon), history=hist)

    async def ski(self) -> list[dict]:
        async def one(a):
            try:
                return ski_outlook(a["name"], await self.grid_days(float(a["lat"]), float(a["lon"])))
            except Exception as exc:
                return {"name": a["name"], "rating": "unavailable", "why": type(exc).__name__, "days": []}
        return list(await asyncio.gather(*(one(a) for a in self.ski_areas)))

    async def _timetable(self) -> dict:
        async with self._tt_lock:
            path = os.path.join(self._data_dir, "train_timetable.json") if self._data_dir else None
            if self._tt is None and path and os.path.exists(path):
                try:
                    self._tt = json.load(open(path, encoding="utf-8"))
                except (OSError, ValueError):
                    self._tt = None
            if self._tt and time.time() - self._tt.get("built", 0) < GTFS_MAX_AGE:
                return self._tt
            lat, lon = self.coords()
            try:
                async with self._client() as c:
                    r = await c.get(GTFS_URL, timeout=90.0)
                    r.raise_for_status()
                tt = await asyncio.to_thread(build_timetable, r.content, lat, lon,
                                             self.train_stations or None, self.train_radius)
            except Exception as exc:
                log.warning("Outlook: GTFS refresh failed: %s", exc)
                if self._tt:
                    return self._tt
                raise
            self._tt = tt
            if path:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(tt, f)
                except OSError as exc:
                    log.debug("Outlook: timetable save failed: %s", exc)
            log.info("Outlook: train timetable built for %s", [s["id"] for s in tt["stations"]])
            return tt

    async def trains(self) -> list[dict]:
        return next_trains(await self._timetable())

    async def doom(self) -> dict:
        if self._doom and time.time() - self._doom[0] < DOOM_TTL:
            return self._doom[1]
        kp = kp_max = closest = quake = None
        quakes: list = []
        rocks: list = []
        async with self._client() as c:
            async def get(url, **params):
                try:
                    r = await c.get(url, params=params or None, timeout=15.0)
                    r.raise_for_status()
                    return r.json()
                except Exception as exc:
                    log.debug("Outlook doom fetch %s: %s", url, exc)
                    return None
            k, cad, eq = await asyncio.gather(
                get(KP_URL), get(CAD_URL, **{"dist-max": "0.05", "date-min": "now", "date-max": "+30"}),
                get(QUAKE_URL))
        if k is not None:
            kp, kp_max = parse_kp(k)
        if cad:
            rocks = parse_cad(cad)
            closest = rocks[0]["dist_ld"] if rocks else None
        if eq:
            for f in eq.get("features", []):
                p = f.get("properties") or {}
                if p.get("mag") is not None:
                    quakes.append({"mag": p["mag"], "place": p.get("place", ""), "time": p.get("time")})
            quakes.sort(key=lambda q: -q["mag"])
            quake = quakes[0]["mag"] if quakes else None
        alerts = list(getattr(self._wx, "_active_alerts", []) or [])
        warnings = [a for a in alerts if str(a.get("event", "")).endswith("Warning")]
        extreme = any(a.get("severity") == "Extreme" for a in alerts)
        secs = self.clock.get("seconds")
        score, label = doom_score(kp_max, closest, quake, len(warnings), extreme,
                                  int(secs) if secs is not None else None)
        aurora = kp_max is not None and kp_max >= 6
        out = {"score": score, "label": label,
               "kp": kp, "kp_24h_max": kp_max, "aurora_possible": aurora,
               "asteroids_30d": len(rocks), "closest_asteroid": rocks[0] if rocks else None,
               "quakes_week": len(quakes), "biggest_quake": quakes[0] if quakes else None,
               "local_warnings": [a.get("event") for a in warnings],
               "doomsday_clock": self.clock,
               "zombies": "0.0% (no change)", "alien_contact": "awaiting QSL card",
               "note": "for fun: real data, silly math"}
        self._doom = (time.time(), out)
        return out

    async def zone(self) -> dict:
        """USDA hardiness zone for the base location: config garden.zone, else
        base location -> ZIP (OpenStreetMap) -> phzmapi.org, cached on disk."""
        if self.garden.get("zone"):
            return {"zone": str(self.garden["zone"]), "source": "config"}
        lat, lon = self.coords()
        if lat is None:
            return {"zone": None, "error": "base location not set"}
        key = f"{lat:.2f},{lon:.2f}"
        path = os.path.join(self._data_dir, "garden_zone.json") if self._data_dir else None
        cache = getattr(self, "_zone_cache", None)
        if cache is None and path and os.path.exists(path):
            try:
                cache = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                cache = None
        cache = cache or {}
        self._zone_cache = cache
        if key in cache:
            return cache[key]
        async with self._client() as c:
            r = await c.get(NOMINATIM_REVERSE, params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}",
                                                       "format": "json", "zoom": "16"})
            r.raise_for_status()
            zip5 = ((r.json().get("address") or {}).get("postcode") or "")[:5]
            if not re.fullmatch(r"\d{5}", zip5):
                return {"zone": None, "error": "no US ZIP for the base location"}
            r = await c.get(ZONE_URL.format(zip=zip5))
            r.raise_for_status()
            d = r.json()
        out = {"zone": d.get("zone"), "temp_range_f": d.get("temperature_range"), "zip": zip5,
               "source": "USDA 2023 map (phzmapi.org)"}
        cache[key] = out
        if path:
            try:
                json.dump(cache, open(path, "w", encoding="utf-8"))
            except OSError:
                pass
        return out

    async def garden_outlook(self) -> dict:
        z = await self.zone()
        days = None
        lat, lon = self.coords()
        if lat is not None:
            try:
                days = await self.grid_days(lat, lon)
            except Exception:
                days = None
        plan = tomato_plan(z.get("zone"), last_frost=self.garden.get("last_frost"),
                           first_frost=self.garden.get("first_frost"), days=days)
        return {**z, "tomato": plan}

    async def hunt(self) -> dict:
        lat, lon = self.coords()
        if lat is None:
            return {"error": "base location not set"}
        h = self.hunting
        seasons = h.get("seasons") or MAINE_DEER_2026
        area = await self.hunt_area(lat, lon) if not h.get("seasons") else {}
        wmd = h.get("wmd") if h.get("wmd") not in (None, "") else area.get("wmd")
        if wmd not in (None, ""):          # only the seasons that apply to this WMD
            seasons = [s for s in seasons if not s.get("wmds") or int(wmd) in s["wmds"]]
        ea = h.get("expanded_archery")
        if ea is None:
            ea = area.get("expanded_archery")
        if ea is False:
            seasons = [s for s in seasons if not s.get("expanded_archery")]
        out = hunt_outlook(await self.grid_days(lat, lon), lat, lon, seasons,
                            bool(h.get("no_sunday", True)), int(h.get("hours_before_sunrise_min", 30)),
                            int(h.get("hours_after_sunset_min", 30)))
        out["wmd"] = wmd
        out["archery_area"] = area.get("archery_area")
        if ea is False and area.get("nearby_archery_area"):
            out["nearby_archery_area"] = area["nearby_archery_area"]
        return out

    async def hunt_area(self, lat: float, lon: float) -> dict:
        """WMD and expanded-archery status for the base, looked up once and cached on disk."""
        key = f"{lat:.3f},{lon:.3f}"
        path = os.path.join(self._data_dir, "hunt_area.json") if self._data_dir else None
        cache = getattr(self, "_area_cache", None)
        if cache is None:
            cache = {}
            if path and os.path.exists(path):
                try:
                    cache = json.load(open(path, encoding="utf-8"))
                except (OSError, ValueError):
                    cache = {}
            self._area_cache = cache
        if key in cache:
            return cache[key]
        try:
            async with self._client() as c:
                area = await maine_hunt_area(c, lat, lon)
        except Exception as exc:
            log.debug("Outlook: WMD lookup failed: %s", exc)
            return {}
        cache[key] = area
        if path:
            try:
                json.dump(cache, open(path, "w", encoding="utf-8"))
            except OSError:
                pass
        return area

    async def bot_hunt(self, max_len: int = 160) -> str:
        h = await self.hunt()
        if h.get("error"):
            return f"hunt: {h['error']}"
        clk = lambda s: (lambda x: f"{x.hour % 12 or 12}:{x.minute:02d}{'a' if x.hour < 12 else 'p'}")(datetime.fromisoformat(s))
        if h["sunday_closed"]:
            head = "Hunt: Sunday, closed"
        elif h["open"]:
            head = f"Hunt: {h['open'][-1]} open"
        else:
            ns = h.get("next_season")
            head = "Hunt: no deer season open" + (f"; next {ns['name']} {ns['start'][5:]}" if ns else "")
        hrs = f", legal {clk(h['legal_hours'][0])}-{clk(h['legal_hours'][1])}" if h.get("legal_hours") and h["open"] and not h["sunday_closed"] else ""
        days = " ".join(f"{d['dow'][:2]}:{'closed' if d['closed'] else d['rating']}" for d in h["days"][:5])
        return f"{head}{hrs}. Deer movement {days}"[:max_len]

    async def bot_garden(self, max_len: int = 160) -> str:
        g = await self.garden_outlook()
        t = g["tomato"]
        d = lambda s: (lambda x: f"{x:%b} {x.day}")(date.fromisoformat(s))
        text = (f"Zone {g.get('zone') or '?'}. Tomatoes: seeds indoors {d(t['seed_start'][0])}-{d(t['seed_start'][1])}, "
                f"plant out {d(t['plant_out'][0])}-{d(t['plant_out'][1])} (last frost ~{d(t['last_frost'])})." + ("" if t["stage"] == "wait" else " " + t["advice"]))
        return text[:max_len]

    async def summary(self) -> dict:
        async def safe(coro):
            try:
                return await coro
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"[:160]}
        sap, ski, trains, doom, garden, hunt = await asyncio.gather(
            safe(self.sap()), safe(self.ski()), safe(self.trains()), safe(self.doom()), safe(self.garden_outlook()),
            safe(self.hunt()))
        return {"sap": sap, "ski": ski, "trains": trains, "doom": doom, "garden": garden, "hunt": hunt,
                "generated": datetime.now(timezone.utc).isoformat()}

    # bot replies (one mesh message each)
    async def bot_sap(self, max_len: int = 160) -> str:
        s = await self.sap()
        if s.get("error"):
            return f"sap: {s['error']}"
        days = " ".join(f"{d['dow'][:2]}{'*' if d['rating'] == 'good' else '+' if d['rating'] == 'fair' else '-'}"
                        for d in s["days"][:7])
        head = "Sap" if s["in_season"] else "Sap (no season yet)" if s.get("primed") is False else "Sap (off-season)"
        if s["sap_days"]:
            best = next((d for d in s["days"] if d["rating"] == "good"), None) or \
                next(d for d in s["days"] if d["rating"] != "none")
            body = f"{s['sap_days']} run days, {s['good_days']} good; best {best['dow']} {best['lo_f']}/{best['hi_f']}F"
        else:
            body = "no freeze-thaw days ahead"
        text = f"{head}: {body}. {days} (*good +ok)"
        if s["advice"] and len(text) + len(s["advice"]) < max_len:
            text += " " + s["advice"]
        return text[:max_len]

    async def bot_ski(self, max_len: int = 160) -> str:
        areas = await self.ski()
        full = "Ski: " + "; ".join(f"{_ski_short(a['name'])} {a['rating']} ({a['why']})" for a in areas)
        if len(full) <= max_len:
            return full
        return ("Ski: " + "; ".join(f"{_ski_short(a['name'])} {_RATING_SHORT.get(a['rating'], a['rating'])}"
                                    for a in areas))[:max_len]

    async def bot_trains(self, max_len: int = 160) -> str:
        sts = await self.trains()
        if not sts:
            return "trains: no rail stations near the base location"
        st = sts[0]
        nxt = [f"{t['label']} #{t['train']} {'arr' if t['terminates'] else 'to ' + _short(t['headsign'])}"
               for t in st["next"][:4]]
        return (f"{st['name'].replace(' Amtrak Station', '')}: " + ("; ".join(nxt) or "no trains soon")
                + " (schedule)")[:max_len]

    async def bot_doom(self, max_len: int = 160) -> str:
        d = await self.doom()
        bits = [f"Doom-o-meter {d['score']}/100: {d['label']}"]
        if d.get("kp_24h_max") is not None:
            bits.append(f"Kp {d['kp_24h_max']:g}" + (" (aurora!)" if d["aurora_possible"] else ""))
        if d.get("closest_asteroid"):
            bits.append(f"rock {d['closest_asteroid']['dist_ld']:g}LD")
        if d.get("biggest_quake"):
            bits.append(f"M{d['biggest_quake']['mag']:g} quake")
        bits.append(f"clock {d['doomsday_clock'].get('seconds')}s")
        bits.append("zombies 0%")
        return " | ".join(bits)[:max_len]

    # notifications
    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="outlook")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(90)
        while True:
            try:
                await self.check_notifications()
            except Exception as exc:
                log.debug("Outlook: notify check failed: %s", exc)
            await asyncio.sleep(3 * 3600)

    def _due(self, key: str, cooldown: float) -> bool:
        if time.time() - self._notified.get(key, 0) < cooldown:
            return False
        self._notified[key] = time.time()
        return True

    async def check_notifications(self) -> list[str]:
        if not self.notify:
            return []
        notes = []
        try:
            s = await self.sap()
            if s.get("in_season") and s.get("sap_days", 0) >= 3 and self._due("sap", 5 * 86400):
                notes.append(f"Sap run coming: {s['summary']}.")
        except Exception:
            pass
        try:
            for a in await self.ski():
                if a.get("rating") == "powder day" and self._due("ski:" + a["name"], 2 * 86400):
                    notes.append(f"Powder day at {a['name']}: {a['why']}.")
        except Exception:
            pass
        try:
            d = await self.doom()
            if d.get("aurora_possible") and self._due("aurora", 12 * 3600):
                notes.append(f"Geomagnetic storm, Kp {d['kp_24h_max']:g}: aurora possible tonight. Look north.")
            ca = d.get("closest_asteroid")
            if ca and ca["dist_ld"] < 1 and self._due("rock:" + str(ca["name"]), 7 * 86400):
                notes.append(f"Asteroid {ca['name']} passes {ca['dist_ld']:g} lunar distances away {ca['date']} UTC.")
        except Exception:
            pass
        for n in notes:
            await self._post(n)
        return notes

    async def _post(self, text: str) -> None:
        from ech.core.models import NormalizedMessage, Priority
        log.info("Outlook: %s", text)
        if not self._router:
            return
        msg = NormalizedMessage(source_adapter="outlook", source_channel="Outlook", from_id="outlook",
                                from_display="Outlook", body=text, priority=Priority.NORMAL)
        await self._router._handle_inbound(msg)
        if self.notify_adapters:
            await self._router.send(body=text[:150], adapter_names=self.notify_adapters,
                                    raw={"channel_name": self.notify_channel} if self.notify_channel else None)


_RATING_SHORT = {"powder day": "POWDER", "fresh snow": "fresh", "thaw / rain": "rain",
                 "snowmaking weather": "making snow", "quiet": "quiet", "unavailable": "n/a"}


def _ski_short(name: str) -> str:
    return {"Camden Snow Bowl": "Snow Bowl", "Sunday River": "SunRiver"}.get(name, name)


def _short(s: str) -> str:
    return re.sub(r"\s*(Amtrak Station|Station|, ?[A-Z]{2})$", "", s or "").strip()


_service: OutlookService | None = None


def get_service(config: dict | None = None, state=None, router=None, wx_service=None) -> OutlookService:
    global _service
    if _service is None:
        _service = OutlookService(config or {}, state=state, router=router, wx_service=wx_service)
    else:
        if state is not None and _service._state is None:
            _service._state = state
        if router is not None and _service._router is None:
            _service._router = router
        if wx_service is not None and _service._wx is None:
            _service._wx = wx_service
    return _service
