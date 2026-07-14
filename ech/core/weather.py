"""
ech/core/weather.py
--------------------
Weather & Alert Service.

Polls NWS CAP alerts (api.weather.gov — no API key required).
Emits severe/extreme alerts as NormalizedMessages into the ECH router.
Supports scheduled broadcasts with template substitution.
Supports UTC time sync broadcast.

NWS Alert API:
  GET https://api.weather.gov/alerts/active?area=ME
  Returns GeoJSON FeatureCollection of active CAP alerts.
  No auth required. Rate limit: be polite, 5-minute poll is fine.
  User-Agent header required (identifies your application).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ech.core.models import NormalizedMessage, Priority

log = logging.getLogger(__name__)

NWS_API_BASE = "https://api.weather.gov"
NWS_USER_AGENT = "(SignalMatrix, sm@emergency.local)"

# NWS severity → ECH priority mapping
SEVERITY_MAP = {
    "Extreme":  Priority.EMERGENCY,
    "Severe":   Priority.ELEVATED,
    "Moderate": Priority.ELEVATED,
    "Minor":    Priority.NORMAL,
    "Unknown":  Priority.NORMAL,
}


class WeatherService:
    """
    Polls NWS CAP alerts and emits them as NormalizedMessages.
    Also provides time-sync broadcast and scheduled announcement support.
    """

    def __init__(self, config: dict, router=None):
        wx_cfg = config.get("weather_service", {})
        self.enabled           = wx_cfg.get("enabled", True)
        self._area             = wx_cfg.get("nws_area", "ME")
        self._lat              = wx_cfg.get("nws_lat")
        self._lon              = wx_cfg.get("nws_lon")
        self._poll_interval    = int(wx_cfg.get("poll_interval_sec", 300))
        self._severity_filter  = set(wx_cfg.get("severity_filter",
                                                  ["Extreme", "Severe", "Moderate", "Minor"]))

        # Auto-broadcast config
        # Severities that trigger auto-broadcast (list — or legacy bool for compat)
        _ab = wx_cfg.get("auto_broadcast_severities", None)
        if _ab is None:
            # Back-compat: auto_broadcast_extreme: true → ["Extreme"]
            _ab = ["Extreme"] if wx_cfg.get("auto_broadcast_extreme", False) else []
        self._auto_broadcast_severities: set[str] = set(_ab)
        self._auto_broadcast   = bool(self._auto_broadcast_severities)
        self._auto_adapters    = wx_cfg.get("auto_broadcast_adapters", [])
        self._auto_channel     = wx_cfg.get("auto_broadcast_channel", "")

        # Rate limiting
        self._auto_min_interval   = int(wx_cfg.get("auto_broadcast_min_interval_sec", 600))
        self._auto_event_cooldown = int(wx_cfg.get("auto_broadcast_event_cooldown_sec", 3600))
        self._auto_max_per_hour   = int(wx_cfg.get("auto_broadcast_max_per_hour", 4))

        self._router           = router
        self._db               = None   # set via set_db() so cooldown state can be persisted
        self._seen_alert_ids: set[str] = set()
        self._active_alerts: list[dict] = []
        self._last_auto_broadcast: float = 0.0
        # Per-event-type last broadcast time to prevent same-type spam
        self._last_event_broadcast: dict[str, float] = {}
        # Rolling hour broadcast timestamps for max_per_hour cap
        self._hour_broadcast_ts: list[float] = []
        self._poll_task: asyncio.Task | None = None
        self._schedule_tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = None
        self._last_poll: datetime | None = None
        self._poll_count = 0
        self._broadcast_count = 0

        # Scheduled broadcasts from config
        self._schedules = config.get("scheduled_broadcasts", [])

    def set_db(self, db) -> None:
        self._db = db

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.info("WeatherService: disabled in config")
            return
        self._client = httpx.AsyncClient(
            headers={"User-Agent": NWS_USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
        self._poll_task = asyncio.create_task(self._poll_loop(), name="wx-poll")
        log.info("WeatherService: started, area=%s, interval=%ds",
                 self._area, self._poll_interval)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    # ── Poll loop ─────────────────────────────────────────────────────────

    async def trigger_poll(self) -> None:
        """Trigger an immediate NWS alert poll (callable from API)."""
        await self._poll_alerts()

    async def _poll_loop(self) -> None:
        # Initial poll on startup
        await self._poll_alerts()
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._poll_alerts()
        except asyncio.CancelledError:
            pass

    async def _poll_alerts(self) -> None:
        try:
            self._poll_count += 1
            self._last_poll = datetime.now(timezone.utc)

            # Build query: prefer lat/lon point, fall back to area
            if self._lat is not None and self._lon is not None:
                url = f"{NWS_API_BASE}/alerts/active?point={self._lat},{self._lon}"
                log.info("WeatherService: polling NWS by coordinates (%s, %s)", self._lat, self._lon)
            else:
                url = f"{NWS_API_BASE}/alerts/active?area={self._area}"
                log.info("WeatherService: polling NWS by state area code %r (set lat/lon for better precision)", self._area)

            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()

            features = data.get("features", [])
            self._active_alerts = []
            new_count = 0

            for feature in features:
                props = feature.get("properties", {})
                alert_id  = props.get("id", "")
                status    = props.get("status", "Actual")
                severity  = props.get("severity", "Unknown")
                urgency   = props.get("urgency", "Unknown")
                event     = props.get("event", "")
                headline  = props.get("headline", "")
                desc      = props.get("description", "")
                area_desc = props.get("areaDesc", "")
                effective = props.get("effective", "")
                expires   = props.get("expires", "")

                # NWS test/exercise traffic (weekly/monthly required tests, drill
                # exercises) commonly carries severity=Extreme + urgency=Immediate
                # — the exact combination this code uses to force Priority.EMERGENCY.
                # Without a status check, a routine test gets broadcast to the mesh
                # and logged as a real emergency alert. Only "Actual" is real;
                # Exercise/System/Test/Draft must never reach the emergency path.
                is_test = status != "Actual" or "test" in event.lower() or "exercise" in event.lower()
                if is_test:
                    log.info("WeatherService: suppressing non-Actual alert %r (status=%s, event=%r)",
                              alert_id, status, event)
                    continue

                self._active_alerts.append({
                    "id": alert_id,
                    "severity": severity,
                    "urgency": urgency,
                    "event": event,
                    "headline": headline,
                    "area": area_desc,
                    "expires": expires,
                })

                # Only emit if severity passes filter and not already seen
                if severity not in self._severity_filter:
                    continue
                if alert_id in self._seen_alert_ids:
                    continue

                self._seen_alert_ids.add(alert_id)
                new_count += 1

                priority = SEVERITY_MAP.get(severity, Priority.NORMAL)
                # Immediate + Extreme → EMERGENCY regardless
                if severity == "Extreme" and urgency == "Immediate":
                    priority = Priority.EMERGENCY

                body = f"🌩 NWS {severity.upper()} ALERT — {event}: {headline}"
                if area_desc:
                    body += f" ({area_desc})"

                msg = NormalizedMessage(
                    source_adapter="wx-service",
                    source_channel=f"NWS {self._area}",
                    from_id="NWS",
                    from_display="NWS Alerts",
                    body=body,
                    priority=priority,
                    raw={
                        "alert_id": alert_id,
                        "severity": severity,
                        "urgency": urgency,
                        "event": event,
                        "description": desc[:500] if desc else "",
                        "expires": expires,
                    },
                )

                if self._router:
                    await self._router._handle_inbound(msg)

                # Auto-broadcast to mesh — severity-filtered and rate-limited
                if self._auto_broadcast and self._router and severity in self._auto_broadcast_severities:
                    now_ts = time.time()
                    skip_reason = None

                    # 1. Global minimum interval between any broadcasts
                    if now_ts - self._last_auto_broadcast < self._auto_min_interval:
                        skip_reason = f"global min interval {self._auto_min_interval}s"

                    # 2. Per-event-type cooldown (same event type won't spam)
                    elif now_ts - self._last_event_broadcast.get(event, 0) < self._auto_event_cooldown:
                        skip_reason = f"event cooldown {self._auto_event_cooldown}s for {event!r}"

                    # 3. Hourly cap
                    else:
                        cutoff = now_ts - 3600
                        self._hour_broadcast_ts = [t for t in self._hour_broadcast_ts if t > cutoff]
                        if len(self._hour_broadcast_ts) >= self._auto_max_per_hour:
                            skip_reason = f"hourly cap {self._auto_max_per_hour}/hr reached"

                    if skip_reason:
                        log.info("WeatherService: skipping auto-broadcast (%s)", skip_reason)
                    else:
                        raw_hint = {"channel_name": self._auto_channel} if self._auto_channel else None
                        if not self._auto_channel:
                            log.warning("WeatherService: auto_broadcast_channel not set — "
                                        "alert will go to each adapter's current default channel")
                        ch_label = f"channel={self._auto_channel!r}" if self._auto_channel else "channel=(adapter default — NOT CONFIGURED)"
                        log.info("WeatherService: auto-broadcasting %s %r → %s adapters=%s",
                                 severity, event, ch_label, self._auto_adapters or "all")
                        await self._router.send(
                            body=body[:200],
                            adapter_names=self._auto_adapters or None,
                            priority=priority,
                            raw=raw_hint,
                        )
                        self._last_auto_broadcast = now_ts
                        self._last_event_broadcast[event] = now_ts
                        self._hour_broadcast_ts.append(now_ts)
                        self._broadcast_count += 1
                        log.info("WeatherService: auto-broadcast sent (%d this hour)", len(self._hour_broadcast_ts))
                        if self._db:
                            import json as _json
                            try:
                                await self._db.set_kv("wx_broadcast_state", _json.dumps({
                                    "last_auto": self._last_auto_broadcast,
                                    "by_event": self._last_event_broadcast,
                                }))
                            except Exception as _e:
                                log.debug("WeatherService: failed to persist broadcast state: %s", _e)

            if new_count:
                log.info("WeatherService: %d new NWS alert(s) emitted", new_count)

        except httpx.HTTPError as exc:
            log.warning("WeatherService: NWS poll HTTP error: %s", exc)
        except Exception as exc:
            log.error("WeatherService: poll error: %s", exc)

    # ── Manual operations ─────────────────────────────────────────────────

    async def broadcast_time_sync(self, adapter_names: list[str] | None = None,
                                   incident_name: str = "") -> bool:
        """Broadcast current UTC time to specified adapters (or all)."""
        if not self._router:
            return False
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        body = f"TIME SYNC: {ts}"
        if incident_name:
            body += f" | INCIDENT: {incident_name}"
        results = await self._router.send(
            body=body,
            adapter_names=adapter_names,
            priority=Priority.NORMAL,
        )
        self._broadcast_count += 1
        log.info("WeatherService: time sync broadcast to %s", adapter_names or "all")
        return all(results.values()) if results else False

    async def broadcast_wx_summary(self, adapter_names: list[str] | None = None) -> bool:
        """Broadcast current active alert summary to adapters."""
        if not self._router:
            return False
        if not self._active_alerts:
            body = "NWS: No active alerts in your area"
        else:
            summaries = [
                f"{a['severity']} {a['event']} ({a['area'][:30]})"
                for a in self._active_alerts[:3]   # max 3 in a single SMS/mesh message
            ]
            body = "NWS ALERTS: " + " | ".join(summaries)
        body = body[:200]   # SMS-safe length
        results = await self._router.send(
            body=body,
            adapter_names=adapter_names,
            priority=Priority.ELEVATED if self._active_alerts else Priority.NORMAL,
        )
        self._broadcast_count += 1
        return all(results.values()) if results else False

    # ── Current conditions + forecast ────────────────────────────────────────

    async def fetch_conditions_and_forecast(self) -> str:
        """
        Fetch current conditions + 12h forecast from NWS for broadcast.
        Requires nws_lat/nws_lon configured (or set via settings).
        Returns a single mesh-safe string (~200 chars).
        """
        client = self._client
        if client is None:
            # Service not started — create a one-shot client
            client = httpx.AsyncClient(headers={"User-Agent": NWS_USER_AGENT}, timeout=10.0, follow_redirects=True)
            own_client = True
        else:
            own_client = False

        try:
            if self._lat is None or self._lon is None:
                return f"NWS {self._area}: lat/lon not configured — enable in Settings > Weather to get conditions"

            # Step 1: resolve grid + observation stations
            pts = await client.get(f"{NWS_API_BASE}/points/{self._lat},{self._lon}")
            pts.raise_for_status()
            props = pts.json()["properties"]
            forecast_url  = props["forecast"]
            stations_url  = props["observationStations"]

            # Step 2: current observation from nearest station
            current = "?"
            try:
                st_resp = await client.get(stations_url)
                st_resp.raise_for_status()
                station_id = st_resp.json()["features"][0]["properties"]["stationIdentifier"]
                obs_resp = await client.get(
                    f"{NWS_API_BASE}/stations/{station_id}/observations/latest"
                )
                obs_resp.raise_for_status()
                obs = obs_resp.json()["properties"]

                temp_c  = (obs.get("temperature") or {}).get("value")
                temp_f  = round(temp_c * 9 / 5 + 32) if temp_c is not None else None
                desc    = obs.get("textDescription", "")
                w_mps   = (obs.get("windSpeed") or {}).get("value")
                w_mph   = round(w_mps * 2.237) if w_mps is not None else None
                w_deg   = (obs.get("windDirection") or {}).get("value")
                _dirs   = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                           "S","SSW","SW","WSW","W","WNW","NW","NNW"]
                w_card  = _dirs[int((w_deg + 11.25) / 22.5) % 16] if w_deg is not None else ""

                parts = []
                if temp_f is not None:
                    parts.append(f"{temp_f}°F")
                if desc:
                    parts.append(desc)
                if w_mph is not None:
                    parts.append(f"Wind {w_mph}mph {w_card}".strip())
                current = ", ".join(parts) if parts else "conditions unavailable"
            except Exception as exc:
                log.warning("WeatherService: observation fetch error: %s", exc)
                current = "conditions unavailable"

            # Step 3: 12-hour forecast (first 2 periods ≈ 6h each)
            forecast = ""
            try:
                fc_resp = await client.get(forecast_url)
                fc_resp.raise_for_status()
                periods = fc_resp.json()["properties"]["periods"][:2]
                fc_parts = [
                    f"{p['name']}: {p['temperature']}°{p['temperatureUnit']} "
                    f"{p['shortForecast']} wind {p['windSpeed']}"
                    for p in periods
                ]
                forecast = " | ".join(fc_parts)
            except Exception as exc:
                log.warning("WeatherService: forecast fetch error: %s", exc)
                forecast = "forecast unavailable"

            result = f"NOW: {current} | 12H: {forecast}"
            return result[:200]

        except Exception as exc:
            log.warning("WeatherService: conditions/forecast error: %s", exc)
            return f"NWS {self._area}: weather fetch failed ({exc})"
        finally:
            if own_client:
                await client.aclose()

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        using_coords = self._lat is not None and self._lon is not None
        return {
            "enabled": self.enabled,
            "area": self._area,
            "poll_source": f"point:{self._lat},{self._lon}" if using_coords else f"area:{self._area}",
            "poll_count": self._poll_count,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "active_alerts": len(self._active_alerts),
            "broadcast_count": self._broadcast_count,
            "active_alert_list": self._active_alerts[:10],
        }
