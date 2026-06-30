"""
ech/core/mesh_bot.py
---------------------
General-purpose mesh channel bot.  Replaces weather_bot.py.

Commands (case-insensitive, detected anywhere in the message body):
  ping              — round-trip echo with SNR / hop metadata
  weather <zip5>    — current NWS conditions + forecast (US zip code)
  wx <zip5>         — alias for weather
  overhead          — aircraft within radius via local dump1090 JSON
  satpass [name]    — next satellite pass (ISS, NOAA 19, …)
  solar / space     — NOAA space weather: SFI, SSN, K-index
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
    lat: null                         # observer lat; falls back to weather_service.nws_lat
    lon: null                         # observer lon; falls back to weather_service.nws_lon
    dump1090_path: "/run/dump1090-fa/aircraft.json"
    overhead_radius_nm: 20            # nautical miles
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
CELESTRAK_URLS = {
    "stations": "https://celestrak.org/GP/TLE/stations.txt",
    "noaa":     "https://celestrak.org/GP/TLE/noaa.txt",
    "weather":  "https://celestrak.org/GP/TLE/weather.txt",
}
USER_AGENT = "(ECH Emergency Communications Hub, ech@emergency.local)"

_WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
              "S","SSW","SW","WSW","W","WNW","NW","NNW"]
_CARD8 = ["N","NE","E","SE","S","SW","W","NW"]

# Matches: "weather? 04101", "wx 90210", "ping", "overhead", "satpass iss", "solar", "help"
_CMD_RE = re.compile(
    r'(?<![a-z0-9])'
    r'(ping|weather\??|wx|overhead|satpass|sat|solar|space|help)'
    r'(?:\s+([^\s].{0,40}))?'
    r'(?=\s|$|[^a-z0-9])',
    re.IGNORECASE,
)
_ZIP_RE = re.compile(r'\b(\d{5})\b')

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
    def __init__(self, config: dict, router=None):
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

        # Observer coordinates — bot-specific override or fall back to weather_service lat/lon
        wx_cfg = config.get("weather_service", {})
        self._lat: float | None = cfg.get("lat") or wx_cfg.get("nws_lat")
        self._lon: float | None = cfg.get("lon") or wx_cfg.get("nws_lon")

        self._router = router
        self._user_ts:  dict[str, float] = {}   # from_id → last reply time
        self._global_ts: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._tle_cache = _TleCache()
        self._solar_cache: str = ""
        self._solar_ts: float = 0.0

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
        return "Cmds: ping | weather <zip> | overhead | satpass | solar"

    # ── Weather ───────────────────────────────────────────────────────────────

    async def _cmd_weather(self, args: str) -> str:
        m = _ZIP_RE.search(args)
        if not m:
            return "Usage: weather <zip5>  e.g. weather 04101"
        zip5 = m.group(1)
        return await self._fetch_weather(zip5)

    async def _fetch_weather(self, zip5: str) -> str:
        client = self._client
        assert client is not None
        geo = await client.get(
            NOMINATIM_URL,
            params={"postalcode": zip5, "countrycodes": "us", "format": "json", "limit": "1"},
        )
        geo.raise_for_status()
        hits = geo.json()
        if not hits:
            return f"zip {zip5} not found"
        lat = float(hits[0]["lat"])
        lon = float(hits[0]["lon"])
        place = hits[0].get("display_name", "").split(",")[0].strip()

        pts = await client.get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
        pts.raise_for_status()
        nws = pts.json()["properties"]
        forecast_url = nws["forecast"]
        stations_url = nws["observationStations"]

        current = "obs unavail"
        try:
            st = await client.get(stations_url)
            st.raise_for_status()
            sid = st.json()["features"][0]["properties"]["stationIdentifier"]
            obs_r = await client.get(f"{NWS_BASE}/stations/{sid}/observations/latest")
            obs_r.raise_for_status()
            obs = obs_r.json()["properties"]
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
            p = fc.json()["properties"]["periods"][0]
            forecast = f"{p['name']}: {p['temperature']}F {p['shortForecast']} wind {p['windSpeed']}"
        except Exception as exc:
            log.debug("MeshBot/weather fcst error: %s", exc)

        return f"WX {zip5} {place}|Now:{current}|{forecast}"

    # ── Aircraft overhead ─────────────────────────────────────────────────────

    async def _cmd_overhead(self) -> str:
        if self._lat is None or self._lon is None:
            return "overhead: observer lat/lon not configured (set mesh_bot.lat/lon in config)"
        try:
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
            dist = _haversine_nm(self._lat, self._lon, ac_lat, ac_lon)
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
        bearing  = _bearing(self._lat, self._lon, ac.get("lat"), ac.get("lon"))
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
        if self._lat is None or self._lon is None:
            return "satpass: observer lat/lon not configured"
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
        observer = wgs84.latlon(self._lat, self._lon)

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
            sfi  = root.findtext("solarflux") or "?"
            ssn  = root.findtext("sunspots")  or "?"
            ki   = root.findtext("kindex")    or "?"
            ai   = root.findtext("aindex")    or "?"
            upd  = root.findtext("updated")   or ""
            result = f"Solar SFI:{sfi} SSN:{ssn} K:{ki} A:{ai}"
            if upd:
                result += f" ({upd[:12]})"
        except Exception as exc:
            log.warning("MeshBot/solar fetch error: %s", exc)
            result = f"solar: data unavailable ({type(exc).__name__})"
        self._solar_cache = result
        self._solar_ts = now
        return result


# Backwards-compat alias so any code that imported WeatherBot still works
WeatherBot = MeshBot
