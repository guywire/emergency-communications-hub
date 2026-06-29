"""
ech/core/weather_bot.py
------------------------
Weather request bot.

Listens for "weather? <zip>", "weather <zip>", or "wx <zip>" on configured
channels and replies with current NWS conditions + 12-hour forecast.

Data sources (no API key required):
  Geocoding : Nominatim (OpenStreetMap) — zip → lat/lon
  Conditions: NWS api.weather.gov — current obs + forecast

Config block (config.yaml):
  weather_bot:
    enabled: true
    channels: ["#weather"]    # list of channel names to watch; ["*"] = all channels
    adapters: []              # [] = all adapters; or ["meshcore-1", "meshtastic-1"]
    reply_dm: true            # true = DM the requester; false = channel broadcast
    rate_limit_sec: 60        # min seconds between replies to the same sender
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "(ECH Emergency Communications Hub, ech@emergency.local)"

# Matches: "weather? 04101", "weather 90210", "wx 10001-1234"
TRIGGER_RE = re.compile(
    r'\b(?:weather\?|weather|wx)\s+(\d{5}(?:-\d{4})?)\b', re.IGNORECASE
)

_WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
              "S","SSW","SW","WSW","W","WNW","NW","NNW"]


class WeatherBot:
    def __init__(self, config: dict, router=None):
        bot_cfg = config.get("weather_bot", {})
        self.enabled          = bool(bot_cfg.get("enabled", False))
        self._channels        = [c.lower() for c in bot_cfg.get("channels", ["#weather"])]
        self._adapter_filter  = bot_cfg.get("adapters", [])
        self._reply_dm        = bool(bot_cfg.get("reply_dm", True))
        self._rate_limit_sec  = int(bot_cfg.get("rate_limit_sec", 60))
        self._router          = router
        self._rate: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if not self.enabled:
            log.info("WeatherBot: disabled in config")
            return
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=20.0,
            follow_redirects=True,
        )
        log.info("WeatherBot: started, channels=%s, reply_dm=%s", self._channels, self._reply_dm)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def handle(self, msg: NormalizedMessage) -> None:
        """Called by router for every inbound message."""
        if not self.enabled or not self._client:
            return
        # Skip bot's own outbound messages and system sources
        if msg.from_id in ("local", "NWS", "wx-service", "weather-bot"):
            return
        if msg.source_adapter in ("wx-service",):
            return
        # Adapter filter (empty = all)
        if self._adapter_filter and msg.source_adapter not in self._adapter_filter:
            return
        # Channel filter — skip check if wildcard
        if self._channels != ["*"]:
            ch = (msg.source_channel or "").lower()
            if not any(want.lstrip("#") in ch for want in self._channels):
                return
        # Detect trigger keyword + zip code
        match = TRIGGER_RE.search(msg.body)
        if not match:
            return
        zip_code = match.group(1)[:5]   # use 5-digit base zip for lookup
        # Per-sender rate limit
        now = time.monotonic()
        if now - self._rate.get(msg.from_id, 0) < self._rate_limit_sec:
            log.debug("WeatherBot: rate-limiting reply to %s", msg.from_id)
            return
        self._rate[msg.from_id] = now
        # Dispatch without blocking the inbound pipeline
        asyncio.ensure_future(
            self._reply(msg, zip_code),
            loop=asyncio.get_event_loop(),
        )

    async def _reply(self, msg: NormalizedMessage, zip_code: str) -> None:
        try:
            wx_text = await self._fetch_weather(zip_code)
        except Exception as exc:
            log.warning("WeatherBot: fetch error for %s: %s", zip_code, exc)
            wx_text = f"weather data unavailable ({type(exc).__name__})"
        if not self._router:
            return
        body = f"WX {zip_code}: {wx_text}"[:200]
        to_id = msg.from_id if self._reply_dm else None
        await self._router.send(
            body=body,
            adapter_names=[msg.source_adapter],
            to_id=to_id,
            priority=Priority.NORMAL,
        )
        log.info("WeatherBot: replied to %s/%s for zip %s (dm=%s)",
                 msg.source_adapter, msg.from_id[:12], zip_code, self._reply_dm)

    async def _fetch_weather(self, zip_code: str) -> str:
        client = self._client
        assert client is not None

        # 1. Zip → lat/lon via Nominatim
        geo = await client.get(
            NOMINATIM_URL,
            params={"postalcode": zip_code, "countrycodes": "us",
                    "format": "json", "limit": "1"},
        )
        geo.raise_for_status()
        hits = geo.json()
        if not hits:
            return f"zip {zip_code} not found"
        lat = float(hits[0]["lat"])
        lon = float(hits[0]["lon"])
        place = hits[0].get("display_name", "").split(",")[0].strip()

        # 2. NWS grid metadata (returns forecast URL + station list URL)
        pts = await client.get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
        pts.raise_for_status()
        nws_props = pts.json()["properties"]
        forecast_url  = nws_props["forecast"]
        stations_url  = nws_props["observationStations"]

        # 3. Current observation from nearest station
        current = "obs unavail"
        try:
            st_resp = await client.get(stations_url)
            st_resp.raise_for_status()
            sid = st_resp.json()["features"][0]["properties"]["stationIdentifier"]
            obs_resp = await client.get(f"{NWS_BASE}/stations/{sid}/observations/latest")
            obs_resp.raise_for_status()
            obs = obs_resp.json()["properties"]

            temp_c = (obs.get("temperature") or {}).get("value")
            temp_f = round(temp_c * 9 / 5 + 32) if temp_c is not None else None
            desc   = obs.get("textDescription", "")
            w_mps  = (obs.get("windSpeed") or {}).get("value")
            w_mph  = round(w_mps * 2.237) if w_mps is not None else None
            w_deg  = (obs.get("windDirection") or {}).get("value")
            w_dir  = _WIND_DIRS[int((w_deg + 11.25) / 22.5) % 16] if w_deg is not None else ""

            parts: list[str] = []
            if temp_f is not None:
                parts.append(f"{temp_f}F")
            if desc:
                parts.append(desc)
            if w_mph is not None:
                parts.append(f"Wind {w_mph}mph {w_dir}".strip())
            if parts:
                current = ", ".join(parts)
        except Exception as exc:
            log.debug("WeatherBot: observation fetch failed: %s", exc)

        # 4. First forecast period (~6-12h)
        forecast = "fcst unavail"
        try:
            fc_resp = await client.get(forecast_url)
            fc_resp.raise_for_status()
            p = fc_resp.json()["properties"]["periods"][0]
            forecast = (
                f"{p['name']}: {p['temperature']}F "
                f"{p['shortForecast']} "
                f"wind {p['windSpeed']}"
            )
        except Exception as exc:
            log.debug("WeatherBot: forecast fetch failed: %s", exc)

        return f"{place} | Now:{current} | {forecast}"
