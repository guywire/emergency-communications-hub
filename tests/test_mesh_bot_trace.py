"""
tests/test_mesh_bot_trace.py
-----------------------------
Covers O86: a user-invokable 'trace <node>' mesh bot command that fires a
real active trace probe (MeshCoreAdapter.ping()/ping_and_wait()), distinct
from the existing passive 'path' command which only reports the relay chain
of the sender's own last message.
"""

import pytest

from ech.core.mesh_bot import MeshBot
from ech.core.models import MeshNode, NormalizedMessage


class _FakeAdapter:
    def __init__(self, nodes, ping_and_wait_result=None, ping_result=None, has_wait=True):
        self._nodes_list = nodes
        self._ping_and_wait_result = ping_and_wait_result
        self._ping_result = ping_result
        if has_wait:
            self.ping_and_wait = self._ping_and_wait
        self.ping = self._ping

    async def nodes(self):
        return self._nodes_list

    async def _ping_and_wait(self, node_id, timeout=20.0):
        return self._ping_and_wait_result

    async def _ping(self, node_id):
        return self._ping_result


class _FakeRouter:
    def __init__(self, adapters):
        self._adapters = adapters


def _bot(router):
    return MeshBot({"mesh_bot": {"enabled": True}}, router=router)


def _msg(body, adapter="meshcore-test"):
    return NormalizedMessage(source_adapter=adapter, source_channel="ch0", from_id="N0CALL", body=body)


@pytest.mark.asyncio
async def test_trace_no_args_usage():
    adapter = _FakeAdapter(nodes=[])
    bot = _bot(_FakeRouter({"meshcore-test": adapter}))
    reply = await bot._cmd_trace(_msg("trace"), "")
    assert "usage" in reply.lower()


@pytest.mark.asyncio
async def test_trace_unknown_node():
    node = MeshNode(node_id="AABBCC", display_name="RKDRPTMON")
    adapter = _FakeAdapter(nodes=[node])
    bot = _bot(_FakeRouter({"meshcore-test": adapter}))
    reply = await bot._cmd_trace(_msg("trace NoSuchNode"), "NoSuchNode")
    assert "no node matching" in reply.lower()


@pytest.mark.asyncio
async def test_trace_success_with_wait():
    node = MeshNode(node_id="AABBCC", display_name="RKDRPTMON")
    adapter = _FakeAdapter(
        nodes=[node],
        ping_and_wait_result={"status": "ok", "hops": 2, "named": ["Relay1", "RKDRPTMON"], "snrs": [1.0, 2.0]},
    )
    bot = _bot(_FakeRouter({"meshcore-test": adapter}))
    reply = await bot._cmd_trace(_msg("trace RKDRPTMON"), "RKDRPTMON")
    assert "Relay1" in reply and "RKDRPTMON" in reply and "2 hops" in reply


@pytest.mark.asyncio
async def test_trace_timeout_with_wait():
    node = MeshNode(node_id="AABBCC", display_name="RKDRPTMON")
    adapter = _FakeAdapter(
        nodes=[node],
        ping_and_wait_result={"status": "timeout", "detail": "no TRACE_DATA reply within 20s"},
    )
    bot = _bot(_FakeRouter({"meshcore-test": adapter}))
    reply = await bot._cmd_trace(_msg("trace RKDRPTMON"), "RKDRPTMON")
    assert "RKDRPTMON" in reply and "no TRACE_DATA reply" in reply


@pytest.mark.asyncio
async def test_trace_fire_and_forget_without_wait_support():
    node = MeshNode(node_id="AABBCC", display_name="Repeater1")
    adapter = _FakeAdapter(
        nodes=[node],
        ping_result={"status": "sent", "tag": 1},
        has_wait=False,
    )
    bot = _bot(_FakeRouter({"meshcore-test": adapter}))
    reply = await bot._cmd_trace(_msg("trace Repeater1"), "Repeater1")
    assert "Repeater1" in reply and "message feed" in reply.lower()


@pytest.mark.asyncio
async def test_trace_partial_name_match():
    node = MeshNode(node_id="AABBCC", display_name="RKDRPTMON")
    adapter = _FakeAdapter(
        nodes=[node],
        ping_and_wait_result={"status": "ok", "hops": 0, "named": []},
    )
    bot = _bot(_FakeRouter({"meshcore-test": adapter}))
    reply = await bot._cmd_trace(_msg("trace rkd"), "rkd")
    assert "direct" in reply.lower()
