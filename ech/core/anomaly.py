"""
ech/core/anomaly.py
--------------------
RF Anomaly Detection Engine.

Analyzes NormalizedMessage traffic from Meshtastic and MeshCore adapters
for patterns that suggest:
  - MQTT injection / range cheating
  - High-altitude nodes (balloons, airborne relays)
  - Impossible position jumps
  - Atmospheric ducting signatures
  - Stale/replayed position data
  - Abnormal hop counts

Each detected anomaly produces an AnomalyFinding, stored in SQLite and
pushed to connected WebSocket clients as a JSON event with type "anomaly".

The engine is passive — it observes the router's inbound message stream
and writes findings without modifying messages.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Altitude text patterns: "36k feet", "36000 feet", "FL360", "10000m", "10km alt"
_ALT_FT_RE  = re.compile(r'\b(\d+(?:\.\d+)?)\s*k\s*f(?:eet|t)\b', re.IGNORECASE)
_ALT_FT2_RE = re.compile(r'\b(\d{4,6})\s*f(?:eet|t)\b', re.IGNORECASE)
_ALT_FL_RE  = re.compile(r'\bFL\s*(\d{2,3})\b', re.IGNORECASE)
_ALT_M_RE   = re.compile(r'\b(\d{4,6})\s*m(?:eters?)?\b', re.IGNORECASE)


def _extract_altitude_m_from_text(body: str) -> float | None:
    """Return altitude in metres if a recognisable altitude mention is found."""
    m = _ALT_FT_RE.search(body)
    if m:
        return float(m.group(1)) * 1000 * 0.3048
    m = _ALT_FT2_RE.search(body)
    if m:
        return float(m.group(1)) * 0.3048
    m = _ALT_FL_RE.search(body)
    if m:
        return float(m.group(1)) * 100 * 0.3048  # FL360 → 36000 ft → metres
    m = _ALT_M_RE.search(body)
    if m:
        return float(m.group(1))
    return None

log = logging.getLogger(__name__)


class Severity(str, Enum):
    INFO  = "info"
    WARN  = "warn"
    ALERT = "alert"


@dataclass
class AnomalyFinding:
    id: str
    adapter: str
    node_id: str
    rule: str
    severity: Severity
    summary: str
    evidence: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    acknowledged: bool = False
    broadcast_sent: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "adapter": self.adapter,
            "node_id": self.node_id,
            "rule": self.rule,
            "severity": self.severity.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "timestamp": self.timestamp.isoformat(),
            "acknowledged": self.acknowledged,
            "broadcast_sent": self.broadcast_sent,
        }


# ── Haversine distance ────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class AnomalyEngine:
    """
    Stateful anomaly detector.
    Call process(msg) for every inbound NormalizedMessage.
    New findings are placed on the findings_queue for the router to broadcast.
    """

    def __init__(self, config: dict, db=None):
        cfg = config.get("anomaly_detection", {})
        self.enabled               = cfg.get("enabled", True)
        self._alt_threshold_m      = float(cfg.get("altitude_threshold_m", 500))
        self._speed_threshold_kmh  = float(cfg.get("speed_threshold_kmh", 200))
        self._stale_minutes        = int(cfg.get("position_stale_minutes", 30))
        self._ducting_snr_margin   = float(cfg.get("ducting_snr_margin_db", 5))
        self._ducting_range_km     = float(cfg.get("ducting_range_km", 100))
        self._auto_broadcast       = bool(cfg.get("broadcast_on_alert", False))

        self._db = db

        # Per-node state: keyed by (adapter, node_id)
        self._node_last_pos: dict[tuple, dict]    = {}   # last lat/lon/alt/time
        self._node_hop_history: dict[tuple, list]  = {}   # recent hop counts
        self._node_snr_history: dict[tuple, list]  = {}   # recent SNR values
        self._node_battery_history: dict[tuple, list] = {}
        self._node_first_seen: dict[tuple, datetime] = {}
        self._node_path_history: dict[tuple, list] = {}   # recent relay/digipeater paths

        self.findings: list[AnomalyFinding] = []
        self.findings_queue: asyncio.Queue[AnomalyFinding] = asyncio.Queue()
        self._finding_id = 0

    def _next_id(self) -> str:
        self._finding_id += 1
        return f"F{self._finding_id:06d}"

    async def process(self, msg) -> list[AnomalyFinding]:
        """
        Analyze a NormalizedMessage. Returns list of new findings (may be empty).
        Only processes messages from mesh adapters (Meshtastic, MeshCore).
        """
        if not self.enabled:
            return []
        if not self._is_mesh_adapter(msg.source_adapter):
            return []

        key = (msg.source_adapter, msg.from_id)
        new_findings: list[AnomalyFinding] = []

        # Track first seen
        if key not in self._node_first_seen:
            self._node_first_seen[key] = msg.timestamp

        raw = msg.raw or {}
        # Prefer top-level routing fields; fall back to raw dict for older messages
        hop_count  = msg.hop_count if msg.hop_count is not None else (raw.get("hop_count") or raw.get("hops"))
        snr        = raw.get("snr")
        rssi       = raw.get("rssi")
        lat        = msg.lat
        lon        = msg.lon
        altitude   = raw.get("altitude") or raw.get("alt")
        pkt_ts     = raw.get("timestamp") or raw.get("packet_timestamp")

        # ── Update SNR history ────────────────────────────────────────────
        # Compute avg from EXISTING history before appending current value
        _snr_avg_before = None
        if snr is not None:
            hist = self._node_snr_history.setdefault(key, [])
            if len(hist) >= 5:
                _snr_avg_before = sum(hist) / len(hist)
            hist.append(float(snr))
            if len(hist) > 20:
                hist.pop(0)

        # ── Update hop history ────────────────────────────────────────────
        if hop_count is not None:
            hist = self._node_hop_history.setdefault(key, [])
            hist.append(int(hop_count))
            if len(hist) > 20:
                hist.pop(0)

        # ── RULE: high altitude (structured GPS field) ────────────────────
        if altitude is not None and float(altitude) > self._alt_threshold_m:
            f = self._make_finding(
                msg, "high_altitude", Severity.WARN,
                f"Node at {altitude:.0f}m — above {self._alt_threshold_m:.0f}m threshold",
                {"altitude_m": altitude, "lat": lat, "lon": lon},
            )
            new_findings.append(f)

        # ── RULE: high altitude mentioned in message text ─────────────────
        if altitude is None and msg.body:
            text_alt = _extract_altitude_m_from_text(msg.body)
            if text_alt is not None and text_alt > self._alt_threshold_m:
                f = self._make_finding(
                    msg, "high_altitude_reported", Severity.WARN,
                    f"Message reports ~{text_alt:.0f}m ({text_alt/0.3048:.0f}ft) — "
                    f"above {self._alt_threshold_m:.0f}m threshold (from message text)",
                    {"altitude_m_estimated": round(text_alt), "text_snippet": msg.body[:80]},
                )
                new_findings.append(f)

        # ── RULE: impossible position jump ────────────────────────────────
        if lat is not None and lon is not None:
            last = self._node_last_pos.get(key)
            if last:
                dist_km = _haversine_km(last["lat"], last["lon"], lat, lon)
                dt_s = (msg.timestamp - last["time"]).total_seconds()
                if dt_s > 0:
                    speed_kmh = (dist_km / dt_s) * 3600
                    if speed_kmh > self._speed_threshold_kmh and dist_km > 1.0:
                        f = self._make_finding(
                            msg, "impossible_jump", Severity.ALERT,
                            f"Position jumped {dist_km:.1f}km in {dt_s:.0f}s "
                            f"({speed_kmh:.0f}km/h > {self._speed_threshold_kmh:.0f}km/h limit)",
                            {
                                "from_lat": last["lat"], "from_lon": last["lon"],
                                "to_lat": lat, "to_lon": lon,
                                "distance_km": round(dist_km, 2),
                                "speed_kmh": round(speed_kmh, 1),
                                "elapsed_sec": round(dt_s, 1),
                            },
                        )
                        new_findings.append(f)

            self._node_last_pos[key] = {
                "lat": lat, "lon": lon,
                "alt": altitude,
                "time": msg.timestamp,
            }

        # ── RULE: stale position timestamp ────────────────────────────────
        if pkt_ts and lat is not None:
            try:
                pkt_dt = datetime.fromtimestamp(float(pkt_ts), tz=timezone.utc)
                staleness_min = (msg.timestamp - pkt_dt).total_seconds() / 60
                if staleness_min > self._stale_minutes:
                    f = self._make_finding(
                        msg, "stale_position", Severity.WARN,
                        f"Position timestamp {staleness_min:.0f} min behind receive time "
                        f"(>{self._stale_minutes} min threshold) — possible MQTT replay",
                        {"staleness_minutes": round(staleness_min, 1),
                         "packet_timestamp": pkt_dt.isoformat()},
                    )
                    new_findings.append(f)
            except (ValueError, TypeError):
                pass

        # ── RULE: MQTT injection heuristic ────────────────────────────────
        # viaMqtt flag is definitive; hop_count=0 with position is circumstantial.
        if msg.via_mqtt and lat is not None:
            f = self._make_finding(
                msg, "mqtt_injection", Severity.ALERT,
                f"Node position arrived via MQTT gateway (viaMqtt flag set) — "
                f"not a direct RF contact",
                {"via_mqtt": True, "lat": lat, "lon": lon, "snr": snr},
            )
            new_findings.append(f)
        elif hop_count == 0 and lat is not None:
            hop_hist = self._node_hop_history.get(key, [])
            if len(hop_hist) <= 1:
                f = self._make_finding(
                    msg, "mqtt_injection", Severity.WARN,
                    f"Node appeared at hop_count=0 with position on first contact — "
                    f"possible MQTT injection rather than direct RF",
                    {"hop_count": hop_count, "lat": lat, "lon": lon, "snr": snr},
                )
                new_findings.append(f)

        # ── RULE: ducting event ───────────────────────────────────────────
        if snr is not None and _snr_avg_before is not None:
            current_snr = float(snr)
            snr_jump = current_snr - _snr_avg_before
            if True:  # placeholder structure preserved
                avg_snr = _snr_avg_before
                if snr_jump >= self._ducting_snr_margin:
                    f = self._make_finding(
                        msg, "ducting_event", Severity.INFO,
                        f"SNR jumped {snr_jump:+.1f}dB above recent average "
                        f"({avg_snr:.1f}→{current_snr:.1f}) — possible atmospheric ducting",
                        {"snr_current": current_snr, "snr_avg": round(avg_snr, 1),
                         "snr_jump_db": round(snr_jump, 1), "rssi": rssi},
                    )
                    new_findings.append(f)

        # ── RULE: relay path change ───────────────────────────────────────
        # A node suddenly routing via a different digipeater chain may indicate
        # a spoofed packet, a moved node, or a new/failed relay.
        if msg.path:
            normalized_path = re.sub(r'\*', '', msg.path).strip()
            if normalized_path:
                path_hist = self._node_path_history.setdefault(key, [])
                if len(path_hist) >= 3:
                    recent_paths = set(path_hist[-5:])
                    if normalized_path not in recent_paths:
                        prev = path_hist[-1]
                        f = self._make_finding(
                            msg, "path_change", Severity.WARN,
                            f"Relay path changed: '{prev}' → '{normalized_path}' — "
                            f"possible spoofing, node movement, or relay failure",
                            {"previous_path": prev, "current_path": normalized_path,
                             "path_history": list(path_hist[-3:])},
                        )
                        new_findings.append(f)
                path_hist.append(normalized_path)
                if len(path_hist) > 20:
                    path_hist.pop(0)

        # ── RULE: abnormal hop count increase ─────────────────────────────
        if hop_count is not None:
            hop_hist = self._node_hop_history.get(key, [])
            if len(hop_hist) >= 4:
                avg_hops = sum(hop_hist[:-1]) / len(hop_hist[:-1])
                if hop_count > 5 and avg_hops < 2.0:
                    f = self._make_finding(
                        msg, "abnormal_hops", Severity.WARN,
                        f"Hop count {hop_count} is unusually high for a node "
                        f"with average {avg_hops:.1f} hops",
                        {"hop_count": hop_count, "avg_hops": round(avg_hops, 1)},
                    )
                    new_findings.append(f)

        # Enqueue and store findings
        for f in new_findings:
            self.findings.append(f)
            await self.findings_queue.put(f)
            if self._db:
                await self._db.save_anomaly(f)
            log.info(
                "Anomaly [%s] %s on %s:%s — %s",
                f.severity.value.upper(), f.rule, f.adapter, f.node_id[:12], f.summary[:80],
            )

        return new_findings

    def _make_finding(self, msg, rule: str, severity: Severity,
                      summary: str, evidence: dict) -> AnomalyFinding:
        return AnomalyFinding(
            id=self._next_id(),
            adapter=msg.source_adapter,
            node_id=msg.from_id,
            rule=rule,
            severity=severity,
            summary=summary,
            evidence=evidence,
        )

    def acknowledge(self, finding_id: str) -> bool:
        for f in self.findings:
            if f.id == finding_id:
                f.acknowledged = True
                return True
        return False

    def clear_all(self) -> int:
        count = sum(1 for f in self.findings if not f.acknowledged)
        for f in self.findings:
            f.acknowledged = True
        return count

    def active_findings(self) -> list[AnomalyFinding]:
        return [f for f in self.findings if not f.acknowledged]

    def all_findings(self) -> list[AnomalyFinding]:
        return list(self.findings)

    @staticmethod
    def _is_mesh_adapter(adapter_name: str) -> bool:
        # APRS excluded: bots and internet digipeaters generate too much positional noise
        if "aprs" in adapter_name.lower():
            return False
        return any(x in adapter_name for x in ("meshtastic", "meshcore", "mesh", "reticulum"))
