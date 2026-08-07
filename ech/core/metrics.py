"""
ech/core/metrics.py
--------------------
Prometheus metrics for ECH.

Exposes GET /metrics in Prometheus text format via prometheus-client.
The router, adapters, anomaly engine, and weather service all call
into this module to update metrics; the FastAPI app mounts /metrics.

Install: pip install prometheus-client
"""

from __future__ import annotations

import time
from prometheus_client import (
    Counter, Gauge, Histogram, CollectorRegistry,
    generate_latest, CONTENT_TYPE_LATEST,
)

# Use the default registry
_registry = CollectorRegistry(auto_describe=True)

# ── Message counters ──────────────────────────────────────────────────────

msg_received = Counter(
    "ech_messages_received_total",
    "Total inbound messages per adapter and priority",
    ["adapter", "priority"],
    registry=_registry,
)

msg_sent = Counter(
    "ech_messages_sent_total",
    "Total outbound messages per adapter",
    ["adapter"],
    registry=_registry,
)

msg_dropped = Counter(
    "ech_messages_dropped_total",
    "Total dropped messages (dedup, queue full) per adapter and reason",
    ["adapter", "reason"],
    registry=_registry,
)

# ── Adapter health ────────────────────────────────────────────────────────

adapter_connected = Gauge(
    "ech_adapter_connected",
    "Adapter connection state (1=connected, 0=disconnected)",
    ["adapter"],
    registry=_registry,
)

adapter_last_rx = Gauge(
    "ech_adapter_last_rx_seconds",
    "Unix timestamp of last received message per adapter",
    ["adapter"],
    registry=_registry,
)

adapter_last_tx = Gauge(
    "ech_adapter_last_tx_seconds",
    "Unix timestamp of last sent message per adapter",
    ["adapter"],
    registry=_registry,
)

# ── Mesh node statistics ──────────────────────────────────────────────────

node_snr = Gauge(
    "ech_node_snr_db",
    "Last reported SNR in dB for a mesh node",
    ["adapter", "node_id", "display_name"],
    registry=_registry,
)

node_rssi = Gauge(
    "ech_node_rssi_dbm",
    "Last reported RSSI in dBm for a mesh node",
    ["adapter", "node_id", "display_name"],
    registry=_registry,
)

node_battery = Gauge(
    "ech_node_battery_pct",
    "Last reported battery level (0-100) for a mesh node",
    ["adapter", "node_id", "display_name"],
    registry=_registry,
)

node_last_heard = Gauge(
    "ech_node_last_heard_seconds",
    "Unix timestamp when this node was last heard",
    ["adapter", "node_id"],
    registry=_registry,
)

# ── Anomaly detection ─────────────────────────────────────────────────────

anomaly_findings = Counter(
    "ech_anomaly_findings_total",
    "Total anomaly findings per adapter, rule, and severity",
    ["adapter", "rule", "severity"],
    registry=_registry,
)

anomaly_active = Gauge(
    "ech_anomaly_active",
    "Number of unacknowledged anomaly findings",
    ["rule"],
    registry=_registry,
)

# ── Weather service ───────────────────────────────────────────────────────

wx_alerts_active = Gauge(
    "ech_wx_alerts_active",
    "Number of active NWS alerts by severity",
    ["severity"],
    registry=_registry,
)

wx_broadcasts = Counter(
    "ech_wx_alerts_broadcast_total",
    "Total weather alert broadcasts sent",
    registry=_registry,
)

# ── System ────────────────────────────────────────────────────────────────

ech_uptime = Gauge(
    "ech_uptime_seconds",
    "ECH process uptime in seconds",
    registry=_registry,
)

msg_log_total = Gauge(
    "ech_message_log_total",
    "Total messages stored in SQLite log",
    registry=_registry,
)

db_size_bytes = Gauge(
    "ech_db_size_bytes",
    "SQLite database file size in bytes",
    registry=_registry,
)

# ── Message processing latency ────────────────────────────────────────────

msg_processing_seconds = Histogram(
    "ech_message_processing_seconds",
    "Time to process (route + persist + broadcast) an inbound message",
    ["adapter"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=_registry,
)

# ── Module-level state ────────────────────────────────────────────────────

_start_time = time.time()


def update_uptime() -> None:
    ech_uptime.set(time.time() - _start_time)


def record_message_received(adapter: str, priority: int) -> None:
    msg_received.labels(adapter=adapter, priority=str(priority)).inc()


def record_message_sent(adapter: str) -> None:
    msg_sent.labels(adapter=adapter).inc()


def record_message_dropped(adapter: str, reason: str) -> None:
    msg_dropped.labels(adapter=adapter, reason=reason).inc()


def update_adapter_health(adapter: str, connected: bool,
                          last_rx=None, last_tx=None) -> None:
    adapter_connected.labels(adapter=adapter).set(1 if connected else 0)
    if last_rx:
        adapter_last_rx.labels(adapter=adapter).set(last_rx.timestamp())
    if last_tx:
        adapter_last_tx.labels(adapter=adapter).set(last_tx.timestamp())


def update_node_stats(adapter: str, node_id: str, display_name: str = "",
                      snr: float | None = None, rssi: int | None = None,
                      battery: int | None = None, last_heard=None) -> None:
    short_id = node_id[:16] if len(node_id) > 16 else node_id
    dname = display_name[:32] if display_name else short_id
    if snr is not None:
        node_snr.labels(adapter=adapter, node_id=short_id, display_name=dname).set(snr)
    if rssi is not None:
        node_rssi.labels(adapter=adapter, node_id=short_id, display_name=dname).set(rssi)
    if battery is not None:
        node_battery.labels(adapter=adapter, node_id=short_id, display_name=dname).set(battery)
    if last_heard:
        node_last_heard.labels(adapter=adapter, node_id=short_id).set(last_heard.timestamp())


def record_anomaly(adapter: str, rule: str, severity: str) -> None:
    anomaly_findings.labels(adapter=adapter, rule=rule, severity=severity).inc()


def update_anomaly_active(counts: dict[str, int]) -> None:
    """counts: {rule: unacknowledged_count}"""
    for rule, count in counts.items():
        anomaly_active.labels(rule=rule).set(count)


def update_wx_alerts(severity_counts: dict[str, int]) -> None:
    for severity, count in severity_counts.items():
        wx_alerts_active.labels(severity=severity).set(count)


def get_metrics_output() -> tuple[bytes, str]:
    """Return (body_bytes, content_type) for the /metrics endpoint."""
    update_uptime()
    return generate_latest(_registry), CONTENT_TYPE_LATEST
