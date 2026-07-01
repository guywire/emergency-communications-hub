"""
ech/core/mesh_bot.py
---------------------
General-purpose mesh channel bot.  Replaces weather_bot.py.

Commands (case-insensitive, detected anywhere in the message body):
  ping              — round-trip echo with SNR / hop metadata
  weather [place]   — NWS conditions + forecast; place = zip, city name, or blank for base location
  wx [place]        — alias for weather
  overhead          — aircraft within radius via local dump1090 JSON
  satpass [name]    — next satellite pass (ISS, NOAA 19, …)
  solar / space     — NOAA space weather: SFI, SSN, K-index
  ships             — nearest AIS vessels from connected AIS-catcher adapter
  fcc <callsign>    — FCC ULS amateur license lookup
  trivia            — random general-knowledge question (Open Trivia DB, no key)
  dad               — dad joke (icanhazdadjoke.com)
  help              — list available commands

Config block (config.yaml):
  mesh_bot:
    enabled: true
    channels: ["#weather", "#cmd"]   # ["*"] = every channel
    adapters: []                      # [] = every adapter
    reply_dm: true                    # true = DM sender; false = channel broadcast
    per_user_cooldown_sec: 30         # per-sender cooldown (all commands share it)
    global_cooldown_sec: 5            # minimum gap between ANY two bot replies
    max_reply_len: 200                # hard cap (one LoRa payload)
    lat: null                         # observer lat; falls back to weather_service.nws_lat / state base
    lon: null                         # observer lon; falls back to weather_service.nws_lon / state base
    dump1090_path: "/run/dump1090-fa/aircraft.json"
    overhead_radius_nm: 20            # nautical miles
    ships_radius_nm: 50               # nautical miles for nearby ship list
    tle_targets: ["ISS (ZARYA)", "NOAA 19", "NOAA 18"]
    solar_cache_sec: 900              # re-fetch solar data after this many seconds
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

NWS_BASE       = "https://api.weather.gov"
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
HAMQSL_URL     = "https://www.hamqsl.com/solarxml.php"
METAR_URL      = "https://aviationweather.gov/api/data/metar"
FCC_URL        = "https://data.fcc.gov/api/license-view/basicSearch/getLicenses"
TRIVIA_URL     = "https://opentdb.com/api.php"
DADJOKE_URL    = "https://icanhazdadjoke.com/"
CELESTRAK_URLS = {
    "stations": "https://celestrak.org/SOCRATES/GP.php?GROUP=stations&FORMAT=TLE",
    "noaa":     "https://celestrak.org/SOCRATES/GP.php?GROUP=noaa&FORMAT=TLE",
    "weather":  "https://celestrak.org/SOCRATES/GP.php?GROUP=weather&FORMAT=TLE",
}
USER_AGENT = "(ECH Emergency Communications Hub, ech@emergency.local)"

_WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
              "S","SSW","SW","WSW","W","WNW","NW","NNW"]
_CARD8 = ["N","NE","E","SE","S","SW","W","NW"]

_CMD_RE = re.compile(
    r'(?<![a-z0-9])'
    r'(ping|weather\??|wx|overhead|satpass|sat|solar|space|ships|fcc|trivia|dad|alerts|metar|sun|nodes|aprs|anomalies|help)'
    r'(?:\s+([^\s].{0,40}))?'
    r'(?=\s|$|[^a-z0-9])',
    re.IGNORECASE,
)
_ICAO_RE = re.compile(r'\b([A-Z]{4})\b', re.IGNORECASE)
APRS_FI_URL = "https://api.aprs.fi/api/get"
_ZIP_RE      = re.compile(r'\b(\d{5})\b')
_CALL_RE     = re.compile(r'\b([AKNW][A-Z0-9]{1,2}\d[A-Z]{1,3})\b', re.IGNORECASE)

_HEX_NODE_RE = re.compile(r'^[0-9a-fA-F]{8,}')


def _parse_channel_idx(source_channel: str | None) -> int | None:
    """Return channel index from 'ch2:weather' or 'ch2', else None."""
    if not source_channel:
        return None
    m = re.match(r'^ch(\d+)', source_channel.lower())
    return int(m.group(1)) if m else None


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    r = 3440.065  # Earth radius in nautical miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _card8(bearing: float) -> str:
    return _CARD8[int((bearing + 22.5) / 45) % 8]


# ── TLE cache ─────────────────────────────────────────────────────────────────

class _TleCache:
    def __init__(self, ttl_sec: int = 86400):
        self._ttl = ttl_sec
        self._data: dict[str, tuple[str, str]] = {}   # name → (line1, line2)
        self._fetched_at: float = 0.0

    def get(self, name: str) -> tuple[str, str] | None:
        return self._data.get(name.upper())

    def expired(self) -> bool:
        return time.monotonic() - self._fetched_at > self._ttl

    def load_text(self, text: str) -> int:
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        loaded = 0
        i = 0
        while i + 2 < len(lines):
            name_ln = lines[i]
            l1 = lines[i + 1]
            l2 = lines[i + 2]
            if l1.startswith("1 ") and l2.startswith("2 "):
                self._data[name_ln.strip().upper()] = (l1, l2)
                loaded += 1
                i += 3
            else:
                i += 1
        self._fetched_at = time.monotonic()
        return loaded


# ── Main bot class ────────────────────────────────────────────────────────────

class MeshBot:
    def __init__(self, config: dict, router=None, state=None):
        self._config = config
        cfg = config.get("mesh_bot", config.get("weather_bot", {}))
        self.enabled               = bool(cfg.get("enabled", False))
        self._channels             = [c.lower() for c in cfg.get("channels", ["#weather", "#cmd"])]
        self._adapter_filter: list = cfg.get("adapters", [])
        self._reply_dm             = bool(cfg.get("reply_dm", True))
        self._user_cooldown        = int(cfg.get("per_user_cooldown_sec", 30))
        self._global_cooldown      = int(cfg.get("global_cooldown_sec", 5))
        self._max_len              = int(cfg.get("max_reply_len", 200))
        self._dump1090             = cfg.get("dump1090_path", "/run/dump1090-fa/aircraft.json")
        self._radius_nm            = float(cfg.get("overhead_radius_nm", 20))
        self._tle_targets          = [t.upper() for t in cfg.get("tle_targets", ["ISS (ZARYA)", "NOAA 19", "NOAA 18"])]
        self._solar_cache_sec      = int(cfg.get("solar_cache_sec", 900))

        # Observer coordinates — priority: mesh_bot.lat/lon → weather_service.nws_lat/lon → state.base_lat/lon
        wx_cfg = config.get("weather_service", {})
        self._lat: float | None = cfg.get("lat") or wx_cfg.get("nws_lat")
        self._lon: float | None = cfg.get("lon") or wx_cfg.get("nws_lon")
        self._state = state   # held so _resolve_coords() can pull live base position

        self._router = router
        self._user_ts:  dict[str, float] = {}   # from_id → last reply time
        self._global_ts: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._tle_cache = _TleCache()
        self._solar_cache: str = ""
        self._solar_ts: float = 0.0

    def _resolve_coords(self) -> tuple[float | None, float | None]:
        """Return best-available (lat, lon): config override → weather service → station base."""
        if self._lat is not None and self._lon is not None:
            return self._lat, self._lon
        if self._state is not None:
            slat = getattr(self._state, "_base_lat", None)
            slon = getattr(self._state, "_base_lon", None)
            if slat is not None and slon is not None:
                return slat, slon
        return None, None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("MeshBot: disabled in config")
            return
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=20.0,
            follow_redirects=True,
        )
        asyncio.ensure_future(self._prefetch_tles())
        log.info("MeshBot: started, channels=%s, reply_dm=%s", self._channels, self._reply_dm)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Inbound pipeline hook ─────────────────────────────────────────────────

    async def handle(self, msg: NormalizedMessage) -> None:
        if not self.enabled or not self._client:
            return
        if msg.from_id in ("local", "NWS", "wx-service", "weather-bot", "mesh-bot"):
            return
        if msg.source_adapter in ("wx-service",):
            return
        if self._adapter_filter and msg.source_adapter not in self._adapter_filter:
            return
        # DM-only: ignore channel broadcasts entirely
        if (msg.source_channel or "").upper() != "DM":
            return

        m = _CMD_RE.search(msg.body)
        if not m:
            return

        cmd  = m.group(1).lower().rstrip("?")   # normalise "weather?" → "weather"
        args = (m.group(2) or "").strip()

        # Global cooldown — prevent burst flooding
        now = time.monotonic()
        if now - self._global_ts < self._global_cooldown:
            return
        # Per-user cooldown
        if now - self._user_ts.get(msg.from_id, 0) < self._user_cooldown:
            log.debug("MeshBot: rate-limiting %s (user cooldown)", msg.from_id)
            return

        self._global_ts = now
        self._user_ts[msg.from_id] = now

        asyncio.ensure_future(self._dispatch(msg, cmd, args))

    async def _dispatch(self, msg: NormalizedMessage, cmd: str, args: str) -> None:
        try:
            if cmd == "ping":
                reply = self._cmd_ping(msg)
            elif cmd in ("weather", "wx"):
                reply = await self._cmd_weather(args)
            elif cmd == "overhead":
                reply = await self._cmd_overhead()
            elif cmd in ("satpass", "sat"):
                reply = await self._cmd_satpass(args)
            elif cmd in ("solar", "space"):
                reply = await self._cmd_solar()
            elif cmd == "ships":
                reply = await self._cmd_ships()
            elif cmd == "fcc":
                reply = await self._cmd_fcc(args)
            elif cmd == "trivia":
                reply = await self._cmd_trivia()
            elif cmd == "dad":
                reply = await self._cmd_dad()
            elif cmd == "alerts":
                reply = await self._cmd_alerts()
            elif cmd == "metar":
                reply = await self._cmd_metar(args)
            elif cmd == "sun":
                reply = await self._cmd_sun()
            elif cmd == "nodes":
                reply = await self._cmd_nodes()
            elif cmd == "aprs":
                reply = await self._cmd_aprs(args)
            elif cmd == "anomalies":
                reply = await self._cmd_anomalies()
            elif cmd == "help":
                reply = self._cmd_help()
            else:
                return
        except Exception as exc:
            log.warning("MeshBot: %s handler error: %s", cmd, exc)
            reply = f"{cmd}: error ({type(exc).__name__})"

        await self._send(msg, reply)

    async def _send(self, msg: NormalizedMessage, text: str) -> None:
        if not self._router:
            return
        body = text[:self._max_len]
        from_id = msg.from_id or ""
        # Reply as DM back to the sender.  from_id for a real DM is always
        # the sender's pubkey hex (set by _handle_contact_msg).
        to_id: str | None = from_id if _HEX_NODE_RE.match(from_id) else None
        await self._router.send(
            body=body,
            adapter_names=[msg.source_adapter],
            to_id=to_id,
            priority=Priority.NORMAL,
        )
        log.info("MeshBot: DM reply to %s/%s: %s", msg.source_adapter, from_id[:12], body[:60])

    # ── Command handlers ──────────────────────────────────────────────────────

    def _cmd_ping(self, msg: NormalizedMessage) -> str:
        parts = ["pong"]
        snr = msg.raw.get("snr") if msg.raw else None
        if snr is not None:
            parts.append(f"SNR:{snr:+.1f}dB")
        hops = msg.hop_count
        if hops is not None:
            parts.append(f"Hops:{hops}")
        return " | ".join(parts)

    def _cmd_help(self) -> str:
        return "Cmds: ping|wx [place]|overhead|satpass|solar|alerts|metar [ICAO/place]|sun|nodes|aprs <call>|ships|fcc <call>|anomalies|trivia|dad"

    # ── Weather ───────────────────────────────────────────────────────────────

    async def _cmd_weather(self, args: str) -> str:
        args = args.strip()
        client = self._client
        assert client is not None

        if not args:
            lat, lon = self._resolve_coords()
            if lat is None:
                return "weather: provide a zip/city or set base location in Settings"
            return await self._fetch_weather_coords(client, lat, lon, "base")

        m = _ZIP_RE.search(args)
        if m:
            return await self._fetch_weather_zip(client, m.group(1))

        # Free-text place name — Nominatim geocode then NWS
        return await self._fetch_weather_place(client, args)

    async def _fetch_weather_zip(self, client: Any, zip5: str) -> str:
        geo = await client.get(
            NOMINATIM_URL,
            params={"postalcode": zip5, "countrycodes": "us", "format": "json", "limit": "1"},
        )
        geo.raise_for_status()
        hits = geo.json()
        if not hits:
            return f"zip {zip5} not found"
        lat   = float(hits[0]["lat"])
        lon   = float(hits[0]["lon"])
        place = hits[0].get("display_name", zip5).split(",")[0].strip()
        return await self._fetch_weather_coords(client, lat, lon, place or zip5)

    async def _fetch_weather_place(self, client: Any, query: str) -> str:
        geo = await client.get(
            NOMINATIM_URL,
            params={"q": query, "countrycodes": "us", "format": "json", "limit": "1"},
        )
        geo.raise_for_status()
        hits = geo.json()
        if not hits:
            # retry without US restriction — might be a Canadian city, etc.
            geo2 = await client.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": "1"},
            )
            geo2.raise_for_status()
            hits = geo2.json()
        if not hits:
            return f"place '{query}' not found"
        lat   = float(hits[0]["lat"])
        lon   = float(hits[0]["lon"])
        place = hits[0].get("display_name", query).split(",")[0].strip()
        return await self._fetch_weather_coords(client, lat, lon, place or query)

    async def _fetch_weather_coords(self, client: Any, lat: float, lon: float, label: str) -> str:
        try:
            pts = await client.get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
            pts.raise_for_status()
        except Exception:
            return f"WX: NWS doesn't cover {label} (non-US or offshore?)"
        nws          = pts.json()["properties"]
        forecast_url = nws["forecast"]
        stations_url = nws["observationStations"]

        current = "obs unavail"
        try:
            st = await client.get(stations_url)
            st.raise_for_status()
            sid   = st.json()["features"][0]["properties"]["stationIdentifier"]
            obs_r = await client.get(f"{NWS_BASE}/stations/{sid}/observations/latest")
            obs_r.raise_for_status()
            obs    = obs_r.json()["properties"]
            temp_c = (obs.get("temperature") or {}).get("value")
            temp_f = round(temp_c * 9 / 5 + 32) if temp_c is not None else None
            desc   = obs.get("textDescription", "")
            w_mps  = (obs.get("windSpeed") or {}).get("value")
            w_mph  = round(w_mps * 2.237) if w_mps is not None else None
            w_deg  = (obs.get("windDirection") or {}).get("value")
            w_dir  = _WIND_DIRS[int((w_deg + 11.25) / 22.5) % 16] if w_deg is not None else ""
            parts: list[str] = []
            if temp_f is not None: parts.append(f"{temp_f}F")
            if desc: parts.append(desc)
            if w_mph is not None: parts.append(f"Wind {w_mph}mph {w_dir}".strip())
            if parts: current = ", ".join(parts)
        except Exception as exc:
            log.debug("MeshBot/weather obs error: %s", exc)

        forecast = "fcst unavail"
        try:
            fc = await client.get(forecast_url)
            fc.raise_for_status()
            p        = fc.json()["properties"]["periods"][0]
            forecast = f"{p['name']}: {p['temperature']}F {p['shortForecast']} wind {p['windSpeed']}"
        except Exception as exc:
            log.debug("MeshBot/weather fcst error: %s", exc)

        return f"WX {label}|Now:{current}|{forecast}"

    # ── Aircraft overhead ─────────────────────────────────────────────────────

    async def _cmd_overhead(self) -> str:
        lat, lon = self._resolve_coords()
        if lat is None or lon is None:
            return "overhead: observer position not set (set base location in Settings or mesh_bot.lat/lon in config)"
        try:
            if self._dump1090.startswith("http://") or self._dump1090.startswith("https://"):
                client = self._client
                assert client is not None
                r = await client.get(self._dump1090, timeout=8.0)
                r.raise_for_status()
                text = r.text
            else:
                text = await asyncio.to_thread(self._read_dump1090)
        except FileNotFoundError:
            return f"overhead: {self._dump1090} not found (is dump1090 running?)"
        except Exception as exc:
            return f"overhead: read error ({exc})"
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return "overhead: invalid aircraft.json"

        now_ts = data.get("now", time.time())
        aircraft = data.get("aircraft", [])
        nearby: list[tuple[float, dict]] = []

        for ac in aircraft:
            ac_lat = ac.get("lat")
            ac_lon = ac.get("lon")
            if ac_lat is None or ac_lon is None:
                continue
            # Skip stale entries (not updated in last 60 s relative to file timestamp)
            seen_ago = ac.get("seen", 999)
            if seen_ago > 60:
                continue
            dist = _haversine_nm(lat, lon, ac_lat, ac_lon)
            if dist <= self._radius_nm:
                nearby.append((dist, ac))

        if not nearby:
            return f"No aircraft within {self._radius_nm:.0f}nm"

        nearby.sort(key=lambda x: x[0])
        dist, ac = nearby[0]

        callsign = (ac.get("flight") or ac.get("hex", "?")).strip()
        alt      = ac.get("alt_baro") or ac.get("altitude")
        speed    = ac.get("gs") or ac.get("speed")
        track    = ac.get("track")
        bearing  = _bearing(lat, lon, ac.get("lat"), ac.get("lon"))
        direction = _card8(bearing)

        parts = [callsign]
        if alt is not None:
            parts.append(f"{int(alt)}ft")
        if speed is not None:
            parts.append(f"{int(speed)}kt")
        parts.append(f"{direction} {dist:.1f}nm")
        if track is not None:
            parts.append(f"hdg{int(track)}")

        count_str = f" (+{len(nearby)-1} more)" if len(nearby) > 1 else ""
        return "OVHD: " + " ".join(parts) + count_str

    def _read_dump1090(self) -> str:
        with open(self._dump1090, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    # ── Satellite passes ──────────────────────────────────────────────────────

    async def _cmd_satpass(self, args: str) -> str:
        lat, lon = self._resolve_coords()
        if lat is None or lon is None:
            return "satpass: observer position not set (set base location in Settings or mesh_bot.lat/lon in config)"
        # Determine which satellite to predict
        want = args.strip().upper() if args.strip() else None
        if self._tle_cache.expired():
            await self._prefetch_tles()

        # Pick target: explicit name first, else first in configured list
        target_name: str | None = None
        target_lines: tuple[str, str] | None = None
        if want:
            for key in self._tle_cache._data:
                if want in key:
                    target_name = key
                    target_lines = self._tle_cache.get(key)
                    break
            if target_lines is None:
                return f"satpass: '{args}' not found in TLE cache"
        else:
            for t in self._tle_targets:
                target_lines = self._tle_cache.get(t)
                if target_lines:
                    target_name = t
                    break

        if target_lines is None or target_name is None:
            return "satpass: no TLE data — check internet connection"

        return await asyncio.to_thread(
            self._compute_pass, target_name, target_lines[0], target_lines[1]
        )

    def _compute_pass(self, name: str, line1: str, line2: str) -> str:
        try:
            from skyfield.api import load, wgs84, EarthSatellite
        except ImportError:
            return "satpass: skyfield not installed (pip install skyfield)"

        ts = load.timescale(builtin=True)
        sat = EarthSatellite(line1, line2, name, ts)
        observer = wgs84.latlon(lat, lon)

        t0 = ts.now()
        t1 = ts.tt_jd(t0.tt + 1.0)   # search up to 24h ahead

        times, events = sat.find_events(observer, t0, t1, altitude_degrees=10.0)

        # Group into passes: AOS(0) + TCA(1) + LOS(2)
        passes: list[dict] = []
        current: dict = {}
        for t, ev in zip(times, events):
            if ev == 0:
                current = {"aos": t}
            elif ev == 1 and current:
                current["tca"] = t
                diff = (sat - observer).at(t)
                alt, _, _ = diff.altaz()
                current["max_el"] = alt.degrees
            elif ev == 2 and "aos" in current:
                current["los"] = t
                dur = (t - current["aos"]) * 86400
                current["duration_s"] = int(dur)
                passes.append(current)
                current = {}

        if not passes:
            return f"{name[:12]}: no pass in next 24h (el>10)"

        p = passes[0]
        aos_dt = p["aos"].utc_datetime()
        aos_str = aos_dt.strftime("%H:%Mz")
        date_str = aos_dt.strftime("%d%b").upper()
        max_el  = p.get("max_el", 0)
        dur_min = p.get("duration_s", 0) // 60
        dur_sec = p.get("duration_s", 0) % 60
        short_name = name.split("(")[0].strip()[:10]
        return f"{short_name} {date_str} AOS {aos_str} El {max_el:.0f}deg {dur_min}m{dur_sec:02d}s"

    async def _prefetch_tles(self) -> None:
        client = self._client
        if client is None:
            return
        total = 0
        for src, url in CELESTRAK_URLS.items():
            try:
                r = await client.get(url)
                r.raise_for_status()
                n = self._tle_cache.load_text(r.text)
                total += n
                log.debug("MeshBot: loaded %d TLEs from %s", n, src)
            except Exception as exc:
                log.warning("MeshBot: TLE fetch failed (%s): %s", src, exc)
        log.info("MeshBot: %d TLEs cached total", total)

    # ── Solar / space weather ─────────────────────────────────────────────────

    async def _cmd_solar(self) -> str:
        now = time.monotonic()
        if self._solar_cache and now - self._solar_ts < self._solar_cache_sec:
            return self._solar_cache
        client = self._client
        assert client is not None
        try:
            r = await client.get(HAMQSL_URL, timeout=10.0)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            # hamqsl XML nests data inside <solardata> — use XPath .//<tag>
            sfi  = root.findtext(".//solarflux") or "?"
            ssn  = root.findtext(".//sunspots")  or "?"
            ki   = root.findtext(".//kindex")    or "?"
            ai   = root.findtext(".//aindex")    or "?"
            upd  = root.findtext(".//updated")   or ""
            result = f"Solar SFI:{sfi} SSN:{ssn} K:{ki} A:{ai}"
            if upd:
                result += f" ({upd[:12]})"
        except Exception as exc:
            log.warning("MeshBot/solar fetch error: %s", exc)
            result = f"solar: data unavailable ({type(exc).__name__})"
        self._solar_cache = result
        self._solar_ts = now
        return result

    # ── Nearby AIS ships ──────────────────────────────────────────────────────

    async def _cmd_ships(self) -> str:
        lat, lon = self._resolve_coords()
        radius = float((self._config.get("mesh_bot") or {}).get("ships_radius_nm", 50))
        vessels: list = []

        if self._router:
            for adapter in self._router._adapters.values():
                if not getattr(adapter, "_connected", False):
                    continue
                try:
                    nodes = await adapter.nodes()
                    for n in nodes:
                        if (n.node_id or "").startswith("mmsi:"):
                            vessels.append(n)
                except Exception:
                    pass

        if not vessels:
            return "ships: no AIS vessels visible (is ais_catcher adapter connected?)"

        def _dist(v) -> float:
            if lat is None or lon is None or v.lat is None or v.lon is None:
                return 9999.0
            return _haversine_nm(lat, lon, v.lat, v.lon)

        vessels.sort(key=_dist)
        if lat is not None:
            vessels = [v for v in vessels if _dist(v) <= radius]

        if not vessels:
            return f"ships: no vessels within {radius:.0f}nm"

        parts = []
        for v in vessels[:4]:
            d     = _dist(v)
            spd   = (v.meta or {}).get("speed_kts", "")
            label = v.display_name[:10]
            entry = f"{label} {d:.1f}nm"
            if spd:
                entry += f" {float(spd):.0f}kts"
            parts.append(entry)
        return f"Ships({len(vessels)}): " + " | ".join(parts)

    # ── FCC callsign lookup ───────────────────────────────────────────────────

    async def _cmd_fcc(self, args: str) -> str:
        call = args.strip().upper().split()[0] if args.strip() else ""
        if not call:
            return "Usage: fcc <callsign>  e.g. fcc W1ABC"
        client = self._client
        assert client is not None
        try:
            r = await client.get(
                FCC_URL,
                params={"searchvalue": call, "format": "json", "status": "A"},
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            lic_wrap = data.get("Licenses") or {}
            items = lic_wrap.get("License") or []
            if isinstance(items, dict):
                items = [items]
            # Filter to exact callsign match and amateur service
            hits = [
                x for x in items
                if x.get("callsign", "").upper() == call
                and "amateur" in x.get("serviceDesc", "").lower()
            ]
            if not hits:
                return f"FCC: {call} not found (active amateur)"
            h = hits[0]
            name = h.get("licName", "?")
            exp  = (h.get("expiredDate") or "")[:10]
            cls  = h.get("categoryDesc", "")
            return f"FCC {call}: {name} | {cls} | exp {exp}"
        except Exception as exc:
            log.warning("MeshBot/fcc error: %s", exc)
            return f"fcc: lookup failed ({type(exc).__name__})"

    # ── Trivia ───────────────────────────────────────────────────────────────

    async def _cmd_trivia(self) -> str:
        client = self._client
        assert client is not None
        try:
            r = await client.get(TRIVIA_URL, params={"amount": "1", "type": "multiple"}, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if data.get("response_code") != 0 or not data.get("results"):
                return "trivia: no question available, try again"
            q    = data["results"][0]
            # Decode HTML entities (opentdb encodes &amp; etc.)
            import html
            question = html.unescape(q["question"])
            answer   = html.unescape(q["correct_answer"])
            cat      = html.unescape(q.get("category", ""))
            diff     = q.get("difficulty", "")
            prefix   = f"[{cat}|{diff}] " if cat else ""
            return f"Q: {prefix}{question[:120]} | A: {answer}"
        except Exception as exc:
            log.warning("MeshBot/trivia error: %s", exc)
            return f"trivia: fetch failed ({type(exc).__name__})"

    # ── Dad jokes ────────────────────────────────────────────────────────────

    async def _cmd_dad(self) -> str:
        client = self._client
        assert client is not None
        try:
            r = await client.get(
                DADJOKE_URL,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=8.0,
            )
            r.raise_for_status()
            return r.json().get("joke", "I couldn't think of a joke.")[:200]
        except Exception as exc:
            log.warning("MeshBot/dadjoke error: %s", exc)
            return f"dad: fetch failed ({type(exc).__name__})"

    # ── NWS Alerts ───────────────────────────────────────────────────────────

    async def _cmd_alerts(self) -> str:
        lat, lon = self._resolve_coords()
        if lat is None:
            return "alerts: set base location in Settings to receive local alerts"
        client = self._client
        assert client is not None
        try:
            r = await client.get(
                f"{NWS_BASE}/alerts/active",
                params={"point": f"{lat:.4f},{lon:.4f}", "status": "actual", "limit": "5"},
                timeout=10.0,
            )
            r.raise_for_status()
            features = r.json().get("features", [])
            if not features:
                return "ALERTS: none active for your location"
            parts = []
            for f in features[:3]:
                props = f.get("properties", {})
                event    = props.get("event", "Alert")
                severity = props.get("severity", "")[:3].upper()
                headline = props.get("headline") or props.get("description", "")
                # truncate headline to fit in LoRa payload
                headline = headline.split("\n")[0][:60]
                parts.append(f"[{severity}] {event}: {headline}")
            total = len(features)
            header = f"ALERTS({total}): " if total > 1 else "ALERT: "
            return header + " | ".join(parts)
        except Exception as exc:
            log.warning("MeshBot/alerts error: %s", exc)
            return f"alerts: fetch failed ({type(exc).__name__})"

    # ── METAR ────────────────────────────────────────────────────────────────

    async def _cmd_metar(self, args: str) -> str:
        query = args.strip()
        if not query:
            # Use base location
            lat, lon = self._resolve_coords()
            if lat is None:
                return "Usage: metar <ICAO or city>  e.g. metar KPWM"
            return await self._fetch_metar_nearby(lat, lon)
        client = self._client
        assert client is not None
        # 4-letter all-alpha ICAO code?
        m = _ICAO_RE.search(query)
        if m and re.fullmatch(r'[A-Za-z]{4}', query.strip()):
            return await self._fetch_metar_icao(m.group(1).upper())
        # Zip code?
        zm = _ZIP_RE.search(query)
        if zm:
            geo = await client.get(NOMINATIM_URL,
                params={"postalcode": zm.group(1), "countrycodes": "us", "format": "json", "limit": "1"})
            hits = geo.json()
        else:
            # Free-text place name
            geo = await client.get(NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": "1"})
            hits = geo.json()
        if not hits:
            return f"metar: location '{query}' not found"
        lat = float(hits[0]["lat"])
        lon = float(hits[0]["lon"])
        return await self._fetch_metar_nearby(lat, lon)

    async def _fetch_metar_icao(self, icao: str) -> str:
        client = self._client
        assert client is not None
        try:
            r = await client.get(METAR_URL, params={"ids": icao, "format": "json", "taf": "false"}, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if not data:
                return f"METAR {icao}: no recent report"
            return self._format_metar(data[0])
        except Exception as exc:
            log.warning("MeshBot/metar error: %s", exc)
            return f"metar: fetch failed ({type(exc).__name__})"

    async def _fetch_metar_nearby(self, lat: float, lon: float) -> str:
        client = self._client
        assert client is not None
        try:
            # bbox: 1.5 deg radius (~100nm)
            r = await client.get(METAR_URL, params={
                "bbox": f"{lat-1.5},{lon-2.5},{lat+1.5},{lon+2.5}",
                "format": "json", "taf": "false",
            }, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if not data:
                return "METAR: no stations found nearby"
            # Sort by distance to query point
            def _d(obs):
                try:
                    return _haversine_nm(lat, lon, float(obs["lat"]), float(obs["lon"]))
                except Exception:
                    return 9999.0
            data.sort(key=_d)
            return self._format_metar(data[0])
        except Exception as exc:
            log.warning("MeshBot/metar nearby error: %s", exc)
            return f"metar: fetch failed ({type(exc).__name__})"

    def _format_metar(self, obs: dict) -> str:
        raw = obs.get("rawOb") or obs.get("raw_text", "")
        if raw:
            return f"METAR {raw[:180]}"
        icao = obs.get("icaoId") or obs.get("stationId", "?")
        tmp  = obs.get("temp")
        wspd = obs.get("wspd")
        wdir = obs.get("wdir")
        vis  = obs.get("visib")
        sky  = obs.get("skyCondition") or obs.get("clouds", "")
        parts = [icao]
        if tmp  is not None: parts.append(f"{tmp}C")
        if wdir is not None and wspd is not None: parts.append(f"{wdir:03.0f}/{wspd}kt")
        if vis  is not None: parts.append(f"vis {vis}sm")
        if sky: parts.append(str(sky)[:20])
        return "METAR " + " ".join(parts)

    # ── Sunrise / Sunset ─────────────────────────────────────────────────────

    async def _cmd_sun(self) -> str:
        lat, lon = self._resolve_coords()
        if lat is None:
            return "sun: set base location in Settings"
        try:
            from datetime import date
            today = date.today().isoformat()
            client = self._client
            assert client is not None
            r = await client.get(
                "https://api.sunrise-sunset.org/json",
                params={"lat": lat, "lng": lon, "formatted": "0", "date": today},
                timeout=8.0,
            )
            r.raise_for_status()
            res = r.json().get("results", {})
            # Convert UTC → server local time; server should be co-located with the station
            local_tz = datetime.now().astimezone().tzinfo
            def _fmt(iso: str) -> str:
                try:
                    from datetime import timezone as _tz
                    dt_utc = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    dt_loc = dt_utc.astimezone(local_tz)
                    return dt_loc.strftime("%-I:%M%p").lower()
                except Exception:
                    return iso[:5]
            rise   = _fmt(res.get("sunrise", ""))
            sset   = _fmt(res.get("sunset", ""))
            solar  = res.get("day_length", 0)
            h, rem = divmod(int(solar), 3600)
            m2     = rem // 60
            tz_abbr = datetime.now().astimezone().strftime("%Z")
            return f"Sun {today}: rise {rise} set {sset} {tz_abbr} ({h}h{m2:02d}m daylight)"
        except Exception as exc:
            log.warning("MeshBot/sun error: %s", exc)
            return f"sun: fetch failed ({type(exc).__name__})"

    # ── Active mesh nodes ────────────────────────────────────────────────────

    async def _cmd_nodes(self) -> str:
        if not self._router:
            return "nodes: router not available"
        from datetime import datetime, timezone
        now_ts  = datetime.now(timezone.utc).timestamp()
        stale   = 3600.0   # show nodes heard in last hour
        all_nodes: list = []

        for adapter in self._router._adapters.values():
            if not getattr(adapter, "_connected", False):
                continue
            try:
                nodes = await adapter.nodes()
                for n in nodes:
                    nid = n.node_id or ""
                    # Skip ADS-B and AIS position-only nodes
                    if nid.startswith("icao:") or nid.startswith("mmsi:"):
                        continue
                    if n.last_heard and now_ts - n.last_heard.timestamp() <= stale:
                        all_nodes.append(n)
            except Exception:
                pass

        if not all_nodes:
            return "nodes: none heard in last hour"

        # Deduplicate by node_id (same node may appear on multiple adapters)
        seen_ids: set[str] = set()
        unique = []
        for n in all_nodes:
            if n.node_id not in seen_ids:
                seen_ids.add(n.node_id)
                unique.append(n)

        # Sort most-recently-heard first
        unique.sort(key=lambda n: n.last_heard.timestamp() if n.last_heard else 0, reverse=True)

        parts = []
        for n in unique[:8]:
            age_m = int((now_ts - n.last_heard.timestamp()) / 60) if n.last_heard else 0
            label = (n.display_name or n.node_id or "?")[:12]
            parts.append(f"{label}({age_m}m)")

        return f"Nodes({len(unique)}): " + " ".join(parts)

    # ── APRS position lookup ──────────────────────────────────────────────────

    async def _cmd_aprs(self, args: str) -> str:
        call = args.strip().upper().split()[0] if args.strip() else ""
        if not call:
            return "Usage: aprs <callsign>  e.g. aprs W1ABC"

        # 1. Check our own connected APRS adapter nodes first (no external API needed)
        if self._router:
            for adapter in self._router._adapters.values():
                aname = adapter.name.lower()
                if "aprs" not in aname:
                    continue
                if not getattr(adapter, "_connected", False):
                    continue
                try:
                    nodes = await adapter.nodes()
                    for n in nodes:
                        nid = (n.node_id or "").upper()
                        # APRS node IDs are typically the callsign or callsign-SSID
                        if nid == call or nid.startswith(call + "-") or nid.startswith(call + ">"):
                            lat_s = f"{n.lat:.4f}" if n.lat is not None else "?"
                            lon_s = f"{n.lon:.4f}" if n.lon is not None else "?"
                            age_m = ""
                            if n.last_heard:
                                from datetime import datetime, timezone
                                age_s = int((datetime.now(timezone.utc) - n.last_heard).total_seconds())
                                age_m = f" ({age_s//60}m ago)"
                            comment = (n.meta or {}).get("comment", "")
                            return f"APRS {nid}: {lat_s},{lon_s}{age_m}{' — ' + comment[:40] if comment else ''}"
                except Exception:
                    pass

        # 2. Fall back to aprs.fi API if a key is configured
        aprs_key = (self._config.get("mesh_bot") or {}).get("aprs_fi_key", "")
        if not aprs_key:
            return f"APRS {call}: not heard locally. Add aprs_fi_key to mesh_bot config for remote lookup."
        client = self._client
        assert client is not None
        try:
            r = await client.get(APRS_FI_URL, params={
                "name": call, "what": "loc", "apikey": aprs_key, "format": "json",
            }, timeout=8.0)
            r.raise_for_status()
            data = r.json()
            if data.get("result") != "ok" or not data.get("entries"):
                return f"APRS {call}: not found on aprs.fi"
            e    = data["entries"][0]
            lat_s = e.get("lat", "?")
            lon_s = e.get("lng", "?")
            name  = e.get("name", call)
            lasttime = int(e.get("lasttime", 0))
            age_m = ""
            if lasttime:
                import time
                age_s = int(time.time()) - lasttime
                age_m = f" ({age_s//60}m ago)"
            comment = e.get("comment", "")
            return f"APRS {name}: {lat_s},{lon_s}{age_m}{' — ' + comment[:40] if comment else ''}"
        except Exception as exc:
            log.warning("MeshBot/aprs error: %s", exc)
            return f"aprs: lookup failed ({type(exc).__name__})"

    # ── Anomalies ─────────────────────────────────────────────────────────────

    async def _cmd_anomalies(self) -> str:
        anomaly_engine = getattr(self._router, "_anomaly_engine", None) if self._router else None
        if anomaly_engine is None:
            return "anomalies: anomaly engine not available"
        findings = anomaly_engine.active_findings()
        if not findings:
            return "ANOMALIES: none active"
        # Sort by severity (highest first), then recency
        sev_order = {"ALERT": 0, "WARN": 1, "INFO": 2}
        findings = sorted(findings,
            key=lambda f: (sev_order.get(f.severity.value.upper(), 9),
                           -(f.timestamp.timestamp() if hasattr(f.timestamp, "timestamp") else 0)))
        parts = []
        for f in findings[:4]:
            sev  = f.severity.value.upper()[:4]
            rule = f.rule.replace("_", " ")[:14]
            node = (f.node_id or f.adapter or "?")[:10]
            summ = f.summary[:40]
            parts.append(f"[{sev}] {node} {rule}: {summ}")
        total = len(findings)
        header = f"ANOMALIES({total}): " if total > 1 else "ANOMALY: "
        return header + " | ".join(parts)


# Backwards-compat alias so any code that imported WeatherBot still works
WeatherBot = MeshBot
