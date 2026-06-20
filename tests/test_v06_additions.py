"""
tests/test_v06_additions.py
----------------------------
Tests for v0.6 additions:
  - Reticulum mock adapter
  - Anomaly detection engine
  - Weather service (mocked HTTP)
  - Prometheus metrics module
  - Database anomaly table
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from ech.adapters.reticulum_adapter import MockReticulumAdapter
from ech.core.anomaly import AnomalyEngine, Severity, _haversine_km
from ech.core.models import NormalizedMessage, Priority
from ech.core import metrics as M


# ── Haversine ─────────────────────────────────────────────────────────────

def test_haversine_zero():
    assert _haversine_km(44.1, -69.1, 44.1, -69.1) == 0.0


def test_haversine_known():
    # Portland ME to Bangor ME ≈ 130km
    d = _haversine_km(43.65, -70.26, 44.80, -68.78)
    assert 160 < d < 190


def test_haversine_short():
    d = _haversine_km(44.1, -69.1, 44.11, -69.1)
    assert 0 < d < 2


# ── AnomalyEngine ─────────────────────────────────────────────────────────

def _make_mesh_msg(**kwargs):
    defaults = dict(
        source_adapter="meshtastic-mock",
        source_channel="ch0",
        from_id="!a1b2c3d4",
        from_display="TestNode",
        body="Test",
        priority=Priority.NORMAL,
        raw={},
    )
    defaults.update(kwargs)
    return NormalizedMessage(**defaults)


@pytest.fixture
def engine():
    return AnomalyEngine({
        "anomaly_detection": {
            "enabled": True,
            "altitude_threshold_m": 500,
            "speed_threshold_kmh": 200,
            "position_stale_minutes": 30,
            "ducting_snr_margin_db": 5,
        }
    })


@pytest.mark.asyncio
async def test_no_findings_normal_message(engine):
    msg = _make_mesh_msg(raw={"snr": 8.0, "hop_count": 2})
    findings = await engine.process(msg)
    assert findings == []


@pytest.mark.asyncio
async def test_high_altitude_detection(engine):
    msg = _make_mesh_msg(lat=44.1, lon=-69.1, raw={"altitude": 1200.0})
    findings = await engine.process(msg)
    assert any(f.rule == "high_altitude" for f in findings)
    f = next(f for f in findings if f.rule == "high_altitude")
    assert f.severity == Severity.WARN
    assert "1200" in f.summary


@pytest.mark.asyncio
async def test_high_altitude_below_threshold(engine):
    msg = _make_mesh_msg(lat=44.1, lon=-69.1, raw={"altitude": 100.0})
    findings = await engine.process(msg)
    assert not any(f.rule == "high_altitude" for f in findings)


@pytest.mark.asyncio
async def test_impossible_jump_detection(engine):
    # First position
    msg1 = _make_mesh_msg(lat=44.1, lon=-69.1, raw={})
    msg1.timestamp = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    await engine.process(msg1)

    # Jump 500km in 10 seconds
    msg2 = _make_mesh_msg(lat=48.5, lon=-69.1, raw={})
    msg2.timestamp = datetime(2024, 5, 1, 12, 0, 10, tzinfo=timezone.utc)
    findings = await engine.process(msg2)
    assert any(f.rule == "impossible_jump" for f in findings)
    f = next(f for f in findings if f.rule == "impossible_jump")
    assert f.severity == Severity.ALERT
    assert f.evidence["speed_kmh"] > 200


@pytest.mark.asyncio
async def test_normal_movement_no_finding(engine):
    msg1 = _make_mesh_msg(lat=44.1, lon=-69.1, raw={})
    msg1.timestamp = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    await engine.process(msg1)

    # 1km in 60 seconds → 60 km/h, well within threshold
    msg2 = _make_mesh_msg(lat=44.109, lon=-69.1, raw={})
    msg2.timestamp = datetime(2024, 5, 1, 12, 1, 0, tzinfo=timezone.utc)
    findings = await engine.process(msg2)
    assert not any(f.rule == "impossible_jump" for f in findings)


@pytest.mark.asyncio
async def test_stale_position_detection(engine):
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    msg = _make_mesh_msg(lat=44.1, lon=-69.1, raw={"timestamp": stale_ts})
    findings = await engine.process(msg)
    assert any(f.rule == "stale_position" for f in findings)


@pytest.mark.asyncio
async def test_ducting_detection(engine):
    # Use unique node to avoid mqtt_injection rule interfering
    for i in range(6):
        msg = _make_mesh_msg(raw={"snr": 5.0})
        msg.from_id = "!duct_node"
        # Mark node as known by pre-seeding first_seen
        engine._node_first_seen[("meshtastic-mock", "!duct_node")] = msg.timestamp
        await engine.process(msg)

    # Sudden SNR jump to +15 on same node
    jump_msg = _make_mesh_msg(raw={"snr": 15.0})
    jump_msg.from_id = "!duct_node"
    findings = await engine.process(jump_msg)
    assert any(f.rule == "ducting_event" for f in findings)
    f = next(f for f in findings if f.rule == "ducting_event")
    assert f.severity == Severity.INFO
    assert f.evidence["snr_jump_db"] >= 5


@pytest.mark.asyncio
async def test_nonmesh_adapter_ignored(engine):
    msg = NormalizedMessage(
        source_adapter="aprs-mock",
        source_channel="144.390",
        from_id="W1ABC-9",
        body="Test",
        lat=44.1, lon=-69.1,
        raw={"altitude": 9999},
    )
    findings = await engine.process(msg)
    assert findings == []  # APRS not a mesh adapter


def test_acknowledge_finding(engine):
    import asyncio
    from ech.core.anomaly import AnomalyFinding
    f = AnomalyFinding(
        id="F000001", adapter="meshtastic-mock", node_id="!test",
        rule="high_altitude", severity=Severity.WARN,
        summary="Test finding", evidence={}
    )
    engine.findings.append(f)
    assert engine.acknowledge("F000001") is True
    assert engine.active_findings() == []


# ── Weather service ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_weather_service_disabled():
    from ech.core.weather import WeatherService
    ws = WeatherService({"weather_service": {"enabled": False}})
    await ws.start()
    assert ws._poll_task is None


@pytest.mark.asyncio
async def test_weather_service_status():
    from ech.core.weather import WeatherService
    ws = WeatherService({"weather_service": {"enabled": False}})
    status = ws.status()
    assert "enabled" in status
    assert "active_alerts" in status


def test_severity_to_priority():
    from ech.core.weather import SEVERITY_MAP
    from ech.core.models import Priority
    assert SEVERITY_MAP["Extreme"] == Priority.EMERGENCY
    assert SEVERITY_MAP["Severe"] == Priority.ELEVATED
    assert SEVERITY_MAP["Minor"] == Priority.NORMAL


# ── Prometheus metrics ────────────────────────────────────────────────────

def test_metrics_record_received():
    before = M.msg_received.labels(adapter="test-adapter", priority="0")._value.get()
    M.record_message_received("test-adapter", 0)
    after = M.msg_received.labels(adapter="test-adapter", priority="0")._value.get()
    assert after == before + 1


def test_metrics_record_sent():
    M.record_message_sent("test-adapter-tx")
    # Just verify no exception


def test_metrics_update_adapter_health():
    from datetime import datetime, timezone
    M.update_adapter_health("test-health", True,
                            last_rx=datetime.now(timezone.utc),
                            last_tx=datetime.now(timezone.utc))
    val = M.adapter_connected.labels(adapter="test-health")._value.get()
    assert val == 1.0


def test_metrics_record_anomaly():
    M.record_anomaly("meshtastic-test", "high_altitude", "warn")


def test_metrics_output_bytes():
    body, content_type = M.get_metrics_output()
    assert isinstance(body, bytes)
    assert "prometheus" in content_type.lower() or "text/plain" in content_type
    assert b"ech_uptime_seconds" in body


# ── Reticulum mock adapter ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mock_reticulum_connects():
    a = MockReticulumAdapter({"name": "rns-test", "interval_sec": 60.0})
    await a.connect()
    assert a._connected
    await a.disconnect()


@pytest.mark.asyncio
async def test_mock_reticulum_produces_messages():
    a = MockReticulumAdapter({"name": "rns-msg-test", "interval_sec": 0.05})
    await a.connect()
    msgs = []
    async def collect():
        async for m in a.receive():
            msgs.append(m)
            if len(msgs) >= 2:
                break
    await asyncio.wait_for(collect(), timeout=20.0)
    await a.disconnect()
    assert len(msgs) >= 2
    assert all(m.source_channel == "LXMF" for m in msgs)


@pytest.mark.asyncio
async def test_mock_reticulum_send_requires_to_id():
    a = MockReticulumAdapter({"name": "rns-send-test"})
    await a.connect()
    msg = NormalizedMessage(
        source_adapter="rns-send-test",
        source_channel="LXMF",
        from_id="local",
        body="test",
    )
    result = await a.send(msg)
    await a.disconnect()
    assert result is False


@pytest.mark.asyncio
async def test_mock_reticulum_nodes():
    a = MockReticulumAdapter({"name": "rns-nodes-test"})
    await a.connect()
    nodes = await a.nodes()
    await a.disconnect()
    assert len(nodes) == 3


def test_reticulum_registered_in_main():
    from ech.main import build_adapter
    a = build_adapter({"type": "mock_reticulum", "display_name": "Test"})
    assert isinstance(a, MockReticulumAdapter)


# ── API send endpoint ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_send_message():
    """POST /api/messages must accept JSON body and return results dict."""
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from ech.adapters.mock_meshtastic import MockMeshtasticAdapter
    from httpx import AsyncClient, ASGITransport

    db = Database(":memory:")
    await db.connect()
    engine = AnomalyEngine({})
    router = Router(db, anomaly_engine=engine)
    adapter = MockMeshtasticAdapter({"name": "meshtastic-mock", "interval_sec": 999})
    router.register(adapter)
    await router.start()

    app = create_app(router, db, anomaly_engine=engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/messages",
            json={"body": "Test", "adapters": ["meshtastic-mock"], "to_id": None, "priority": 0}
        )
        assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
        data = r.json()
        assert "results" in data
        assert "meshtastic-mock" in data["results"]

    await router.stop()
    await db.close()
