"""
ech/core/air_quality.py
------------------------
Air Quality & Wildfire Smoke Service.

Polls AirNow (EPA) for every monitor station in a bounding box around the hub
(one bbox call per poll — not a probed grid, so coverage matches whatever
stations actually exist, no artificial holes). Polls NASA FIRMS for active
fire hotspots and NOAA HMS for actual smoke-plume polygons, so the mesh bot
can answer "why is it smoky" and the map can show real smoke coverage even
when the source fire is hundreds of miles away (e.g. Canadian wildfire smoke
drifting south) — FIRMS only reports point-in-time fire detections, it can't
tell you where the smoke has drifted, which is what HMS is for.

AirNow API (free key — https://docs.airnowapi.org/):
  GET https://www.airnowapi.org/aq/observation/latLong/current/
    ?format=application/json&latitude=..&longitude=..&distance=..&API_KEY=..
  GET https://www.airnowapi.org/aq/data/
    ?startDate=YYYY-MM-DDTHH&endDate=YYYY-MM-DDTHH&parameters=PM25,PM10,OZONE
    &BBOX=minLon,minLat,maxLon,maxLat&dataType=A&format=application/json
    &verbose=0&monitorType=2&includerawconcentrations=0&API_KEY=..
  Returns every monitor reading in the box for that hour (dataType=A = AQI).

NASA FIRMS API (free key — https://firms.modaps.eosdis.nasa.gov/api/area/):
  GET https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{source}/{area}/{dayrange}
  area = "minLon,minLat,maxLon,maxLat"; source e.g. VIIRS_SNPP_NRT. Returns CSV
  of satellite-detected active-fire hotspots (points, not smoke).

NOAA HMS smoke polygons (no key — public domain):
  GET https://satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Smoke_Polygons/KML/{YYYY}/{MM}/hms_smoke{YYYYMMDD}.kml
  KML with three Folders (Smoke Light/Medium/Heavy), each Placemark a Polygon
  with density also stated in the description text ("Density: Heavy"). URL
  and structure confirmed against the live NOAA archive.

PurpleAir API (free key — https://develop.purpleair.com/):
  GET https://api.purpleair.com/v1/sensors
    ?fields=name,latitude,longitude,pm2.5,confidence&location_type=0
    &nwlng=..&nwlat=..&selng=..&selat=..
  Header: X-API-Key: <key>
  Response is column-oriented: {"fields": [...], "data": [[v1,v2,...], ...]}.
  Denser crowd-sourced network than AirNow, but sensors report raw/uncorrected
  PM2.5 (no official AQI field) — converted here via the standard EPA PM2.5
  breakpoint table, same as AirNow uses, so both render on the same color
  scale. Uncorrected PurpleAir PM2.5 tends to read high in smoke — treat as
  a denser-but-rougher supplement to AirNow, not a replacement.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

AIRNOW_BASE = "https://www.airnowapi.org"
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
HMS_BASE = "https://satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Smoke_Polygons/KML"
PURPLEAIR_BASE = "https://api.purpleair.com/v1/sensors"

# EPA PM2.5 -> AQI breakpoints: (conc_lo, conc_hi, aqi_lo, aqi_hi)
PM25_AQI_BREAKPOINTS = [
    (0.0,   12.0,   0,   50),
    (12.1,  35.4,   51,  100),
    (35.5,  55.4,   101, 150),
    (55.5,  150.4,  151, 200),
    (150.5, 250.4,  201, 300),
    (250.5, 350.4,  301, 400),
    (350.5, 500.4,  401, 500),
]


def _pm25_to_aqi(pm25: float) -> int:
    pm25 = max(0.0, pm25)
    for lo, hi, aqi_lo, aqi_hi in PM25_AQI_BREAKPOINTS:
        if pm25 <= hi:
            return round((aqi_hi - aqi_lo) / (hi - lo) * (pm25 - lo) + aqi_lo)
    return 500

# EPA AQI breakpoints (ceiling, label, color) — standard AirNow palette.
AQI_CATEGORIES = [
    (50,     "Good",                           "#00e400"),
    (100,    "Moderate",                       "#ffff00"),
    (150,    "Unhealthy for Sensitive Groups",  "#ff7e00"),
    (200,    "Unhealthy",                       "#ff0000"),
    (300,    "Very Unhealthy",                  "#8f3f97"),
    (10**6,  "Hazardous",                       "#7e0023"),
]

# Smoke density → fill color/opacity (dark = heavier smoke)
SMOKE_DENSITY_STYLE = {
    "Light":   {"color": "#999999", "opacity": 0.15},
    "Medium":  {"color": "#666666", "opacity": 0.28},
    "Heavy":   {"color": "#333333", "opacity": 0.42},
    "Unknown": {"color": "#888888", "opacity": 0.20},
}


def _aqi_category(aqi) -> tuple[str, str]:
    if aqi is None:
        return "Unknown", "#888888"
    for ceiling, label, color in AQI_CATEGORIES:
        if aqi <= ceiling:
            return label, color
    return "Hazardous", "#7e0023"


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _bearing(lat1, lon1, lat2, lon2) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _COMPASS[int((deg + 11.25) / 22.5) % 16]


def _parse_hms_smoke_kml(text: str) -> list[dict]:
    """Parse an HMS smoke KML into [{coordinates: [[lon,lat],...], density: str}, ...]."""
    root = ET.fromstring(text)
    ns_uri = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else ""
    def tag(name: str) -> str:
        return f"{{{ns_uri}}}{name}" if ns_uri else name

    polygons = []
    for pm in root.iter(tag("Placemark")):
        density = "Unknown"
        desc_el = pm.find(tag("description"))
        if desc_el is not None and desc_el.text:
            m = re.search(r"Density:\s*(\w+)", desc_el.text)
            if m:
                density = m.group(1)
        coords_el = pm.find(f".//{tag('coordinates')}")
        if coords_el is None or not coords_el.text:
            continue
        pts = []
        for triple in coords_el.text.split():
            parts = triple.split(",")
            if len(parts) >= 2:
                try:
                    pts.append([float(parts[0]), float(parts[1])])
                except ValueError:
                    continue
        if len(pts) >= 3:
            polygons.append({"coordinates": pts, "density": density})
    return polygons


def _polygon_bbox_overlaps(pts: list[list[float]], lat: float, lon: float, deg: float) -> bool:
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (min(lons) <= lon + deg and max(lons) >= lon - deg
            and min(lats) <= lat + deg and max(lats) >= lat - deg)


class AirQualityService:
    """
    Polls AirNow current-conditions + every monitor station in a bounding box
    around the hub, NASA FIRMS active-fire hotspots, and NOAA HMS smoke-plume
    polygons — so mesh_bot can explain *why* the air is smoky and the map can
    show real coverage instead of an artificial grid with holes.
    """

    def __init__(self, config: dict, router=None):
        cfg = config.get("air_quality_service", {}) or {}
        self.enabled         = bool(cfg.get("enabled", False))
        self._airnow_key     = str(cfg.get("airnow_api_key", "") or "").strip()
        self._firms_key      = str(cfg.get("firms_api_key", "") or "").strip()
        self._purpleair_key  = str(cfg.get("purpleair_api_key", "") or "").strip()

        # Falls back to weather_service coords, same convention as mesh_bot.
        wx_cfg = config.get("weather_service", {}) or {}
        self._lat: float | None = cfg.get("lat") or wx_cfg.get("nws_lat")
        self._lon: float | None = cfg.get("lon") or wx_cfg.get("nws_lon")

        self._poll_interval    = int(cfg.get("poll_interval_sec", 1800))
        self._station_bbox_deg = float(cfg.get("station_bbox_deg", 4.0))     # ~275mi radius — 1 API call
        self._smoke_bbox_deg   = float(cfg.get("smoke_bbox_deg", 15.0))      # smoke plumes travel far
        # Fire hotspot search defaults to match the smoke box — otherwise a
        # fire producing smoke you can see on the map falls outside the (smaller)
        # fire search box and never shows up as a hotspot marker.
        self._fire_bbox_deg    = float(cfg.get("fire_bbox_deg", self._smoke_bbox_deg))
        self._fire_source      = str(cfg.get("firms_source", "VIIRS_SNPP_NRT"))
        self._fire_dayrange    = int(cfg.get("firms_dayrange", 2))

        self._router = router
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None

        self._current: dict | None = None      # nearest-station AQI reading at the hub
        self._stations: list[dict] = []         # every real AirNow monitor in the bbox
        self._purpleair_sensors: list[dict] = []  # PurpleAir crowd-sourced sensors in the bbox
        self._hotspots: list[dict] = []          # FIRMS fire detections
        self._smoke_polygons: list[dict] = []    # HMS smoke plume polygons
        self._last_poll: datetime | None = None
        self._poll_count = 0
        self._last_error = ""

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("AirQualityService: disabled in config")
            return
        if not self._airnow_key:
            log.warning("AirQualityService: enabled but air_quality_service.airnow_api_key not set — "
                        "AQI data unavailable (free key: docs.airnowapi.org)")
        if not self._firms_key:
            log.info("AirQualityService: air_quality_service.firms_api_key not set — "
                      "fire-hotspot lookup disabled (free key: firms.modaps.eosdis.nasa.gov/api/area)")
        if not self._purpleair_key:
            log.info("AirQualityService: air_quality_service.purpleair_api_key not set — "
                      "denser PurpleAir sensor layer disabled (free key: develop.purpleair.com)")
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="aq-poll")
        log.info("AirQualityService: started, interval=%ds", self._poll_interval)

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
            self._last_error = "no lat/lon configured (set air_quality_service.lat/lon or weather_service.nws_lat/lon)"
            log.warning("AirQualityService: %s", self._last_error)
            return
        self._poll_count += 1
        self._last_poll = datetime.now(timezone.utc)
        if self._airnow_key:
            try:
                await self._poll_current()
                await self._poll_stations()
                self._last_error = ""
            except httpx.HTTPError as exc:
                self._last_error = f"AirNow poll HTTP error: {exc}"
                log.warning("AirQualityService: %s", self._last_error)
            except Exception as exc:
                self._last_error = f"AirNow poll error: {exc}"
                log.error("AirQualityService: %s", self._last_error)
        if self._firms_key:
            try:
                await self._poll_fires()
            except httpx.HTTPError as exc:
                self._last_error = f"FIRMS poll HTTP error: {exc}"
                log.warning("AirQualityService: %s", self._last_error)
            except Exception as exc:
                self._last_error = f"FIRMS poll error: {exc}"
                log.error("AirQualityService: %s", self._last_error)
        if self._purpleair_key:
            try:
                await self._poll_purpleair()
            except httpx.HTTPError as exc:
                log.warning("AirQualityService: PurpleAir poll HTTP error: %s", exc)
            except Exception as exc:
                log.error("AirQualityService: PurpleAir poll error: %s", exc)
        # HMS smoke needs no API key — always attempted.
        try:
            await self._poll_smoke()
        except httpx.HTTPError as exc:
            log.warning("AirQualityService: HMS smoke poll HTTP error: %s", exc)
        except Exception as exc:
            log.error("AirQualityService: HMS smoke poll error: %s", exc)

    # ── AirNow: current conditions at a point ────────────────────────────

    async def _fetch_current_at(self, lat: float, lon: float, distance_mi: float = 50) -> dict | None:
        resp = await self._client.get(
            f"{AIRNOW_BASE}/aq/observation/latLong/current/",
            params={
                "format": "application/json",
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "distance": str(max(5, int(distance_mi))),
                "API_KEY": self._airnow_key,
            },
        )
        resp.raise_for_status()
        obs = resp.json()
        if not obs:
            return None
        # A station reports one entry per pollutant (PM2.5, Ozone, ...) — the
        # AQI is the worst of those, per EPA convention (not an average).
        worst = max(obs, key=lambda o: o.get("AQI", -1))
        label, color = _aqi_category(worst.get("AQI"))
        return {
            "aqi": worst.get("AQI"),
            "category": (worst.get("Category") or {}).get("Name") or label,
            "parameter": worst.get("ParameterName"),
            "reporting_area": worst.get("ReportingArea"),
            "state": worst.get("StateCode"),
            "color": color,
            "lat": lat, "lon": lon,
        }

    async def _poll_current(self) -> None:
        self._current = await self._fetch_current_at(self._lat, self._lon, distance_mi=50)

    # ── AirNow: every monitor station in a bounding box (real coverage) ──

    async def _fetch_bbox_hour(self, bbox: str, hour: str) -> list[dict]:
        resp = await self._client.get(
            f"{AIRNOW_BASE}/aq/data/",
            params={
                "startDate": hour,
                "endDate": hour,
                "parameters": "PM25,PM10,OZONE",
                "BBOX": bbox,
                "dataType": "A",
                "format": "application/json",
                "verbose": "0",
                "monitorType": "2",
                "includerawconcentrations": "0",
                "API_KEY": self._airnow_key,
            },
        )
        resp.raise_for_status()
        return resp.json() or []

    async def _poll_stations(self) -> None:
        """Every real AirNow monitor station within station_bbox_deg — one API
        call, so coverage reflects the actual (sparse) monitor network instead
        of a probed grid with holes wherever a probe missed a nearby station."""
        b = self._station_bbox_deg
        bbox = f"{self._lon - b:.2f},{self._lat - b:.2f},{self._lon + b:.2f},{self._lat + b:.2f}"
        now = datetime.now(timezone.utc)
        hour = now.strftime("%Y-%m-%dT%H")
        records = await self._fetch_bbox_hour(bbox, hour)
        if not records:
            # AirNow typically posts a given hour ~30-60min late — retry the previous hour.
            prev_hour = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
            records = await self._fetch_bbox_hour(bbox, prev_hour)

        sites: dict[tuple, dict] = {}
        for r in records:
            aqi = r.get("AQI")
            lat, lon = r.get("Latitude"), r.get("Longitude")
            if aqi is None or aqi < 0 or lat is None or lon is None:
                continue
            key = (round(lat, 3), round(lon, 3))
            cur = sites.get(key)
            if cur is not None and cur["aqi"] >= aqi:
                continue
            # dataType=A returns Category as a plain int (1-6), not the nested
            # {Number,Name} object the latLong/current point-lookup uses —
            # derive the label from the AQI value itself instead.
            label, color = _aqi_category(aqi)
            sites[key] = {
                "lat": lat, "lon": lon,
                "aqi": aqi,
                "category": label,
                "parameter": r.get("Parameter") or r.get("ParameterName"),
                "site_name": r.get("SiteName", ""),
                "color": color,
            }
        self._stations = list(sites.values())

    # ── PurpleAir: denser crowd-sourced sensor network ────────────────────

    async def _poll_purpleair(self) -> None:
        b = self._station_bbox_deg
        resp = await self._client.get(
            PURPLEAIR_BASE,
            headers={"X-API-Key": self._purpleair_key},
            params={
                "fields": "name,latitude,longitude,pm2.5,confidence",
                "location_type": "0",   # outdoor sensors only
                "nwlng": f"{self._lon - b:.4f}", "nwlat": f"{self._lat + b:.4f}",
                "selng": f"{self._lon + b:.4f}", "selat": f"{self._lat - b:.4f}",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        fields = payload.get("fields", [])
        idx = {name: i for i, name in enumerate(fields)}
        sensors = []
        for row in payload.get("data", []):
            try:
                lat = row[idx["latitude"]]
                lon = row[idx["longitude"]]
                pm25 = row[idx["pm2.5"]]
                if lat is None or lon is None or pm25 is None:
                    continue
                aqi = _pm25_to_aqi(float(pm25))
                label, color = _aqi_category(aqi)
                sensors.append({
                    "lat": lat, "lon": lon,
                    "aqi": aqi, "pm25": round(float(pm25), 1),
                    "category": label, "color": color,
                    "name": row[idx["name"]] if "name" in idx else "",
                })
            except (KeyError, IndexError, ValueError, TypeError):
                continue
        self._purpleair_sensors = sensors

    # ── NASA FIRMS: active fire hotspots (points, not smoke) ─────────────

    async def _poll_fires(self) -> None:
        b = self._fire_bbox_deg
        area = f"{self._lon - b:.2f},{self._lat - b:.2f},{self._lon + b:.2f},{self._lat + b:.2f}"
        url = f"{FIRMS_BASE}/{self._firms_key}/{self._fire_source}/{area}/{self._fire_dayrange}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        hotspots = []
        for row in reader:
            try:
                hotspots.append({
                    "lat": float(row["latitude"]),
                    "lon": float(row["longitude"]),
                    "brightness": float(row.get("bright_ti4") or row.get("brightness") or 0),
                    "confidence": row.get("confidence", ""),
                    "acq_date": row.get("acq_date", ""),
                    "acq_time": row.get("acq_time", ""),
                })
            except (KeyError, ValueError):
                continue
        self._hotspots = hotspots

    # ── NOAA HMS: actual smoke-plume polygons ─────────────────────────────

    async def _fetch_hms_kml(self, day: datetime) -> str | None:
        url = f"{HMS_BASE}/{day:%Y}/{day:%m}/hms_smoke{day:%Y%m%d}.kml"
        resp = await self._client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text

    async def _poll_smoke(self) -> None:
        today = datetime.now(timezone.utc)
        text = await self._fetch_hms_kml(today)
        if text is None:
            # Today's file sometimes isn't posted yet — fall back to yesterday's.
            text = await self._fetch_hms_kml(today - timedelta(days=1))
        if text is None:
            self._smoke_polygons = []
            return
        polygons = _parse_hms_smoke_kml(text)
        b = self._smoke_bbox_deg
        self._smoke_polygons = [
            p for p in polygons
            if _polygon_bbox_overlaps(p["coordinates"], self._lat, self._lon, b)
        ]

    # ── Mesh bot summary ─────────────────────────────────────────────────

    def nearest_hotspot(self, lat: float, lon: float) -> dict | None:
        if not self._hotspots:
            return None
        best = min(self._hotspots, key=lambda h: _haversine_mi(lat, lon, h["lat"], h["lon"]))
        best = dict(best)
        best["distance_mi"] = _haversine_mi(lat, lon, best["lat"], best["lon"])
        best["bearing"] = _bearing(lat, lon, best["lat"], best["lon"])
        return best

    def summary_text(self, lat: float | None = None, lon: float | None = None) -> str:
        """Mesh-safe one-line 'why is it smoky' summary for the mesh bot."""
        if not self.enabled:
            return "smoke/aqi: not enabled — set air_quality_service.enabled in config"
        if not self._airnow_key:
            return "smoke/aqi: no AirNow API key configured (air_quality_service.airnow_api_key)"
        lat = lat if lat is not None else self._lat
        lon = lon if lon is not None else self._lon
        if lat is None or lon is None:
            return "smoke/aqi: no location — set air_quality_service.lat/lon or base location in Settings"
        if not self._current:
            return "smoke/aqi: no data yet (waiting on first poll)"

        c = self._current
        parts = [f"AQI {c['aqi']} ({c['category']})"]
        if c.get("parameter"):
            parts.append(str(c["parameter"]))
        if c.get("reporting_area"):
            parts.append(f"near {c['reporting_area']}")
        body = " ".join(parts)

        if self._smoke_polygons:
            worst = max(
                (p["density"] for p in self._smoke_polygons),
                key=lambda d: {"Heavy": 3, "Medium": 2, "Light": 1}.get(d, 0),
                default=None,
            )
            if worst:
                body += f" | HMS smoke plume overhead: {worst}"

        if self._firms_key:
            fire = self.nearest_hotspot(lat, lon)
            if fire:
                age = f"{fire['acq_date']} {fire['acq_time']}Z" if fire["acq_date"] else ""
                count_note = f" ({len(self._hotspots)} total in range)" if len(self._hotspots) > 1 else ""
                body += (f" | Nearest fire: {fire['distance_mi']:.0f}mi {fire['bearing']}"
                         f" ({fire['confidence']} conf{', ' + age if age else ''}){count_note}")
            elif not self._smoke_polygons:
                body += " | No active fires detected within range"

        return body[:200]

    def hotspots_with_distance(self, lat: float, lon: float) -> list[dict]:
        """All FIRMS hotspots with distance_mi/bearing from the given point,
        nearest first — used by the map so each fire marker can say how far away it is."""
        out = []
        for h in self._hotspots:
            h2 = dict(h)
            h2["distance_mi"] = _haversine_mi(lat, lon, h["lat"], h["lon"])
            h2["bearing"] = _bearing(lat, lon, h["lat"], h["lon"])
            out.append(h2)
        out.sort(key=lambda x: x["distance_mi"])
        return out

    # ── Status / API payloads ────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": bool(self._airnow_key),
            "fire_lookup_configured": bool(self._firms_key),
            "purpleair_configured": bool(self._purpleair_key),
            "poll_count": self._poll_count,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "last_error": self._last_error,
            "current": self._current,
            "station_count": len(self._stations),
            "purpleair_count": len(self._purpleair_sensors),
            "hotspot_count": len(self._hotspots),
            "smoke_polygon_count": len(self._smoke_polygons),
        }

    def stations(self) -> list[dict]:
        return self._stations

    def purpleair_sensors(self) -> list[dict]:
        return self._purpleair_sensors

    def hotspots(self) -> list[dict]:
        return self._hotspots

    def smoke_polygons(self) -> list[dict]:
        return self._smoke_polygons
