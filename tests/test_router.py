"""
tests/test_router.py
--------------------
Basic smoke tests — confirm mock adapters produce messages
and the router receives them.
"""
import asyncio
import pytest
from ech.adapters.mock_meshtastic import MockMeshtasticAdapter
from ech.adapters.mock_aprs import MockAPRSAdapter
from ech.adapters.mock_meshcore import MockMeshCoreAdapter
from ech.core.models import NormalizedMessage, Priority


@pytest.mark.asyncio
async def test_mock_meshtastic_connects_and_produces_messages():
    adapter = MockMeshtasticAdapter({"name": "test-mesh", "interval_sec": 0.05})
    await adapter.connect()
    assert adapter._connected

    # Collect a few messages
    msgs = []
    async def collect():
        async for m in adapter.receive():
            msgs.append(m)
            if len(msgs) >= 2:
                break

    await asyncio.wait_for(collect(), timeout=5.0)
    await adapter.disconnect()

    assert len(msgs) >= 2
    for m in msgs:
        assert isinstance(m, NormalizedMessage)
        assert m.source_adapter == "test-mesh"
        assert m.body


@pytest.mark.asyncio
async def test_mock_aprs_produces_messages():
    adapter = MockAPRSAdapter({"name": "test-aprs", "interval_sec": 0.05})
    await adapter.connect()

    msgs = []
    async def collect():
        async for m in adapter.receive():
            msgs.append(m)
            if len(msgs) >= 2:
                break

    await asyncio.wait_for(collect(), timeout=5.0)
    await adapter.disconnect()

    assert len(msgs) >= 2
    assert all(m.source_adapter == "test-aprs" for m in msgs)


@pytest.mark.asyncio
async def test_mock_meshcore_nodes():
    adapter = MockMeshCoreAdapter({"name": "test-mc", "interval_sec": 60.0})
    await adapter.connect()
    nodes = await adapter.nodes()
    await adapter.disconnect()
    assert len(nodes) > 0
    for n in nodes:
        assert n.node_id
        assert n.display_name


@pytest.mark.asyncio
async def test_adapter_health():
    adapter = MockMeshtasticAdapter({"name": "test-health", "interval_sec": 60.0})
    await adapter.connect()
    health = await adapter.health()
    await adapter.disconnect()
    assert health.state == "connected"
    assert health.adapter == "test-health"


@pytest.mark.asyncio
async def test_send_returns_true():
    adapter = MockMeshtasticAdapter({"name": "test-send", "interval_sec": 60.0})
    await adapter.connect()
    msg = NormalizedMessage(
        source_adapter="test-send",
        source_channel="ch0",
        from_id="!local",
        body="Test message",
        priority=Priority.NORMAL,
    )
    result = await adapter.send(msg)
    await adapter.disconnect()
    assert result is True


@pytest.mark.asyncio
async def test_router_receives_from_adapter():
    from unittest.mock import AsyncMock, MagicMock
    from ech.core.router import Router

    db = MagicMock()
    db.save_message = AsyncMock()

    router = Router(db)
    adapter = MockMeshtasticAdapter({"name": "test-router", "interval_sec": 0.05})
    router.register(adapter)

    await router.start()
    await asyncio.sleep(1.0)  # let messages flow through asyncio queues
    await router.stop()

    assert db.save_message.call_count >= 1
