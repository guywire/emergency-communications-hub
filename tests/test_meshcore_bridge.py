"""
tests/test_meshcore_bridge.py
------------------------------
Tests for the MeshCore → MeshMapper MQTT bridge.
No actual MQTT broker or MeshCore hardware needed.
"""

import pytest
from ech.core.meshcore_bridge import MeshCoreMQTTBridge
from ech.core.models import NormalizedMessage, Priority


def make_config(enabled=False, **kwargs):
    cfg = {"meshcore_bridge": {"enabled": enabled, **kwargs}}
    return cfg


def make_mesh_msg(**kwargs):
    defaults = dict(
        source_adapter="meshcore-mock",
        source_channel="TAC-1",
        from_id="A1B2C3D4E5F6",
        from_display="W1ABC Node",
        body="Test message",
        priority=Priority.NORMAL,
        raw={"snr": 8.0, "rssi": -90},
    )
    defaults.update(kwargs)
    return NormalizedMessage(**defaults)


# ── Config and init ───────────────────────────────────────────────────────

def test_bridge_disabled_by_default():
    bridge = MeshCoreMQTTBridge({"meshcore_bridge": {}})
    assert not bridge.enabled


def test_bridge_enabled():
    bridge = MeshCoreMQTTBridge(make_config(enabled=True))
    assert bridge.enabled


def test_bridge_config_host():
    bridge = MeshCoreMQTTBridge(make_config(
        enabled=True, mqtt_host="192.168.1.10", mqtt_port=1884
    ))
    assert bridge._host == "192.168.1.10"
    assert bridge._port == 1884


def test_bridge_topic_prefix():
    bridge = MeshCoreMQTTBridge(make_config(topic_prefix="mymesh"))
    assert bridge._prefix == "mymesh"


def test_bridge_adapter_name():
    bridge = MeshCoreMQTTBridge(make_config(adapter_name="meshcore-usb"))
    assert bridge._adapter_name == "meshcore-usb"


def test_bridge_auto_adapter():
    bridge = MeshCoreMQTTBridge(make_config())
    assert bridge._adapter_name is None  # auto-detect


# ── Device ID normalization ───────────────────────────────────────────────

def test_device_id_plain_hex():
    did = MeshCoreMQTTBridge._device_id("A1B2C3D4E5F6")
    assert did == "A1B2C3D4E5F6"


def test_device_id_strips_exclamation():
    did = MeshCoreMQTTBridge._device_id("!a1b2c3d4")
    assert "!" not in did


def test_device_id_uppercase():
    did = MeshCoreMQTTBridge._device_id("a1b2c3d4e5f6")
    assert did == did.upper()


def test_device_id_strips_colons():
    did = MeshCoreMQTTBridge._device_id("A1:B2:C3:D4:E5:F6")
    assert ":" not in did


def test_device_id_truncates_long():
    did = MeshCoreMQTTBridge._device_id("A1B2C3D4E5F6112233")
    assert len(did) <= 12


def test_device_id_short():
    did = MeshCoreMQTTBridge._device_id("!test")
    assert len(did) > 0


# ── Queue and enqueue ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_disabled_does_nothing():
    bridge = MeshCoreMQTTBridge(make_config(enabled=False))
    msg = make_mesh_msg()
    await bridge.enqueue(msg)   # should not raise
    assert bridge._queue.qsize() == 0


@pytest.mark.asyncio
async def test_enqueue_enabled_adds_to_queue():
    bridge = MeshCoreMQTTBridge(make_config(enabled=True))
    msg = make_mesh_msg()
    await bridge.enqueue(msg)
    assert bridge._queue.qsize() == 1


@pytest.mark.asyncio
async def test_enqueue_only_meshcore():
    """Non-MeshCore messages are not enqueued (router filters by adapter name)."""
    bridge = MeshCoreMQTTBridge(make_config(enabled=True))
    aprs_msg = NormalizedMessage(
        source_adapter="aprs-mock",
        source_channel="144.390",
        from_id="W1ABC-9",
        body="APRS test",
    )
    # The router would filter this — bridge.enqueue would not be called
    # but if called directly, it still queues (filtering is router's job)
    await bridge.enqueue(aprs_msg)
    assert bridge._queue.qsize() == 1  # bridge itself doesn't filter


@pytest.mark.asyncio
async def test_queue_full_drops_gracefully():
    """Queue full should drop without raising."""
    bridge = MeshCoreMQTTBridge(make_config(enabled=True))
    # Fill the queue
    for _ in range(bridge._queue.maxsize):
        await bridge.enqueue(make_mesh_msg())
    # This should not raise
    await bridge.enqueue(make_mesh_msg())
    assert bridge._queue.qsize() == bridge._queue.maxsize


# ── Status ────────────────────────────────────────────────────────────────

def test_status_structure():
    bridge = MeshCoreMQTTBridge(make_config(
        enabled=True,
        mqtt_host="localhost",
        mqtt_port=1883,
        topic_prefix="meshcore",
        adapter_name="meshcore-usb",
    ))
    s = bridge.status()
    assert "enabled" in s
    assert "connected" in s
    assert "broker" in s
    assert "topic_prefix" in s
    assert "published" in s
    assert "queue_depth" in s


def test_status_disabled():
    bridge = MeshCoreMQTTBridge({})
    s = bridge.status()
    assert not s["enabled"]
    assert not s["connected"]


# ── Start/stop (no real MQTT) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_disabled_does_not_create_task():
    bridge = MeshCoreMQTTBridge(make_config(enabled=False))
    await bridge.start(router=None)
    assert bridge._task is None


@pytest.mark.asyncio
async def test_stop_when_not_started():
    bridge = MeshCoreMQTTBridge(make_config(enabled=False))
    await bridge.stop()   # should not raise


# ── Integration: router hook ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_router_sets_bridge_attribute():
    """
    Verify that start() installs the bridge as _meshcore_bridge on the router.
    """
    from unittest.mock import MagicMock
    bridge = MeshCoreMQTTBridge(make_config(enabled=True, mqtt_host="127.0.0.1"))
    mock_router = MagicMock()
    mock_router._adapters = {}
    # start() sets router._meshcore_bridge and creates a task
    # The task will fail immediately (no MQTT broker) but that's expected
    await bridge.start(mock_router)
    assert mock_router._meshcore_bridge is bridge
    # Clean up task
    if bridge._task:
        bridge._task.cancel()
        try:
            import asyncio
            await asyncio.shield(bridge._task)
        except (asyncio.CancelledError, Exception):
            pass
