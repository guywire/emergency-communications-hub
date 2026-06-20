"""
tests/test_v08_additions.py
----------------------------
Tests for v0.8 additions:
  - Auth manager (bcrypt, sessions)
  - ECH state manager
  - MQTT adapter (mock)
  - AREDN AMI adapter (mock)
  - Database new tables
"""

import asyncio
import pytest
from ech.core.auth import AuthManager
from ech.core.state import ECHState
from ech.adapters.mqtt_adapter import MockMQTTAdapter, MQTTAdapter, _priority
from ech.adapters.aredn_ami import MockAREDNAMIAdapter
from ech.core.models import NormalizedMessage, Priority


# ── Auth ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def auth_db():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_auth_creates_default_admin(auth_db):
    auth = AuthManager(auth_db)
    await auth.init()
    users = await auth_db.get_users()
    assert any(u["username"] == "admin" for u in users)


@pytest.mark.asyncio
async def test_auth_login_success(auth_db):
    auth = AuthManager(auth_db)
    await auth.init()
    token = await auth.login("admin", "admin")
    assert token is not None
    assert len(token) > 20


@pytest.mark.asyncio
async def test_auth_login_wrong_password(auth_db):
    auth = AuthManager(auth_db)
    await auth.init()
    token = await auth.login("admin", "wrongpassword")
    assert token is None


@pytest.mark.asyncio
async def test_auth_session_valid(auth_db):
    auth = AuthManager(auth_db)
    await auth.init()
    token = await auth.login("admin", "admin")
    session = await auth.get_session(token)
    assert session is not None
    assert session["username"] == "admin"
    assert session["role"] == "admin"


@pytest.mark.asyncio
async def test_auth_logout(auth_db):
    auth = AuthManager(auth_db)
    await auth.init()
    token = await auth.login("admin", "admin")
    await auth.logout(token)
    session = await auth.get_session(token)
    assert session is None


@pytest.mark.asyncio
async def test_auth_create_operator(auth_db):
    auth = AuthManager(auth_db)
    await auth.init()
    ok = await auth.create_user("operator1", "pass123", "operator")
    assert ok
    token = await auth.login("operator1", "pass123")
    assert token is not None
    session = await auth.get_session(token)
    assert session["role"] == "operator"


@pytest.mark.asyncio
async def test_auth_change_password(auth_db):
    auth = AuthManager(auth_db)
    await auth.init()
    await auth.change_password("admin", "newpass")
    token = await auth.login("admin", "newpass")
    assert token is not None
    old_token = await auth.login("admin", "admin")
    assert old_token is None


# ── ECH State ─────────────────────────────────────────────────────────────

@pytest.fixture
async def state_db():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_state_default_mode(state_db):
    state = ECHState(state_db)
    await state.init()
    assert state.mode == "standard"
    assert not state.is_emergency


@pytest.mark.asyncio
async def test_state_set_emergency(state_db):
    state = ECHState(state_db)
    await state.init()
    await state.set_mode("emergency")
    assert state.is_emergency
    assert state.mode == "emergency"


@pytest.mark.asyncio
async def test_state_set_incident(state_db):
    state = ECHState(state_db)
    await state.init()
    await state.set_incident("INC-2024-001")
    assert state.incident_name == "INC-2024-001"


@pytest.mark.asyncio
async def test_state_persists(state_db):
    state = ECHState(state_db)
    await state.init()
    await state.set_mode("emergency")
    await state.set_incident("TEST-INC")
    # Re-init from same DB
    state2 = ECHState(state_db)
    await state2.init()
    assert state2.mode == "emergency"
    assert state2.incident_name == "TEST-INC"


@pytest.mark.asyncio
async def test_state_simulation_toggle(state_db):
    state = ECHState(state_db)
    await state.init()
    assert state.simulation_enabled  # default True
    await state.set_simulation(False)
    assert not state.simulation_enabled
    await state.set_simulation(True)
    assert state.simulation_enabled


# ── MQTT adapter ──────────────────────────────────────────────────────────

def test_mqtt_priority_detection():
    assert _priority("emergency shelter needed") == Priority.EMERGENCY
    assert _priority("urgent resource request") == Priority.ELEVATED
    assert _priority("net check-in all ok") == Priority.NORMAL


def test_mqtt_parse_meshtastic_text():
    a = MQTTAdapter({"host": "localhost", "name": "test-mqtt"})
    data = {
        "type": "text",
        "from": "!a1b2c3",
        "sender": "W1ABC",
        "payload": {"text": "Hello mesh"},
    }
    msg = a._parse_meshtastic("msh/US/ME/text", data, str(data))
    assert msg is not None
    assert msg.body == "Hello mesh"
    assert msg.from_id == "!a1b2c3"


def test_mqtt_parse_meshtastic_position():
    a = MQTTAdapter({"host": "localhost", "name": "test-mqtt"})
    data = {
        "type": "position",
        "from": "!a1b2c3",
        "payload": {"latitudeI": 441050000, "longitudeI": -691100000, "altitude": 50},
    }
    msg = a._parse_meshtastic("msh/US/ME/position", data, str(data))
    assert msg is not None
    assert msg.lat == pytest.approx(44.105, abs=0.001)
    assert "POS" in msg.body


def test_mqtt_parse_meshcore():
    a = MQTTAdapter({"host": "localhost", "name": "test-mqtt"})
    data = {"sender": "W1ABC", "text": "MeshCore message"}
    msg = a._parse_meshcore("meshcore/packets", data, str(data))
    assert msg is not None
    assert "MeshCore message" in msg.body


def test_mqtt_parse_generic_json():
    a = MQTTAdapter({"host": "localhost", "name": "test-mqtt"})
    data = {"message": "Generic MQTT message", "source": "sensor1"}
    msg = a._parse_generic_json("sensors/data", data, str(data))
    assert msg is not None
    assert "Generic MQTT message" in msg.body


def test_mqtt_parse_raw():
    a = MQTTAdapter({"host": "localhost", "name": "test-mqtt"})
    msg = a._parse_raw("raw/topic", "plain text payload")
    assert msg.body == "plain text payload"


def test_mqtt_registered_in_main():
    from ech.main import build_adapter
    a = build_adapter({"type": "mock_mqtt", "host": "localhost", "name": "mqtt-test"})
    assert isinstance(a, MockMQTTAdapter)


@pytest.mark.asyncio
async def test_mock_mqtt_produces_messages():
    a = MockMQTTAdapter({"name": "mqtt-test", "host": "localhost", "interval_sec": 0.05})
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
    assert all("mqtt:" in m.source_channel for m in msgs)


# ── AREDN AMI adapter ────────────────────────────────────────────────────

def test_aredn_ami_registered():
    from ech.main import build_adapter
    a = build_adapter({"type": "mock_aredn_ami", "name": "pbx-test"})
    assert isinstance(a, MockAREDNAMIAdapter)


@pytest.mark.asyncio
async def test_mock_aredn_produces_messages():
    a = MockAREDNAMIAdapter({"name": "pbx-test", "interval_sec": 0.05})
    await a.connect()
    msgs = []
    async def collect():
        async for m in a.receive():
            msgs.append(m)
            if len(msgs) >= 1:
                break
    await asyncio.wait_for(collect(), timeout=20.0)
    await a.disconnect()
    assert len(msgs) >= 1
    assert msgs[0].source_channel == "AREDN PBX"


# ── Database new tables ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_kv_store():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    await db.set_kv("test_key", "test_value")
    val = await db.get_kv("test_key")
    assert val == "test_value"
    await db.close()


@pytest.mark.asyncio
async def test_db_sim_nodes():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    node = {
        "adapter": "meshtastic-mock",
        "node_id": "!test123",
        "display_name": "Test Node",
        "lat": 44.1, "lon": -69.1,
        "enabled": 1,
    }
    await db.upsert_sim_node(node)
    nodes = await db.get_sim_nodes()
    assert len(nodes) == 1
    assert nodes[0]["display_name"] == "Test Node"
    await db.close()


@pytest.mark.asyncio
async def test_db_sim_messages():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    msg = {
        "adapter": "meshtastic-mock",
        "from_id": "!test",
        "body": "Test simulation message",
        "priority": 0,
        "interval_sec": 30.0,
        "enabled": 1,
    }
    await db.upsert_sim_message(msg)
    msgs = await db.get_sim_messages()
    assert len(msgs) == 1
    assert msgs[0]["body"] == "Test simulation message"
    await db.close()


@pytest.mark.asyncio
async def test_db_log_entries():
    from ech.core.database import Database
    db = Database(":memory:")
    await db.connect()
    await db.save_log_entry("INFO", "ech.test", "Test log message")
    await db.save_log_entry("ERROR", "ech.test", "Test error message")
    entries = await db.get_log_entries(limit=10)
    assert len(entries) == 2
    levels = {e["level"] for e in entries}
    assert "INFO" in levels
    assert "ERROR" in levels
    filtered = await db.get_log_entries(level="ERROR")
    assert all(e["level"] == "ERROR" for e in filtered)
    await db.close()


# ── System stats API ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_system_stats_returns_expected_keys():
    """GET /api/system/stats should return cpu, mem, net, disk fields."""
    from ech.core.database import Database
    from ech.core.router import Router
    from ech.core.anomaly import AnomalyEngine
    from ech.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    db = Database(":memory:")
    await db.connect()
    router = Router(db, anomaly_engine=AnomalyEngine({}))
    await router.start()
    app = create_app(router, db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/system/stats")
        assert r.status_code == 200
        data = r.json()
        # psutil available in test env
        if "error" not in data:
            assert "cpu_pct" in data
            assert "mem_pct" in data
            assert "mem_used_mb" in data
            assert "mem_total_mb" in data
            assert "net_bytes_sent" in data
            assert "net_bytes_recv" in data
            assert "disk_pct" in data
            assert isinstance(data["cpu_pct"], (int, float))
            assert 0 <= data["mem_pct"] <= 100
            assert 0 <= data["disk_pct"] <= 100

    await router.stop()
    await db.close()
