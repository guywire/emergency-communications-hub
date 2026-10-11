"""
tests/test_meshcore_browser_transport.py
----------------------------------------
MeshCore `transport: browser` must wait for the /remote-hw session however
long it takes (no 10 s window + router back-off gap), read across it, raise
ConnectionError when the browser drops, and attach to the next session.
"""

import asyncio

import pytest

from ech.adapters.meshcore import BrowserTransport
from ech.core.remote_hw import registry


@pytest.mark.asyncio
async def test_browser_transport_waits_reads_and_reattaches():
    t = BrowserTransport(adapter_name="mc-browser-test")
    task = asyncio.ensure_future(t.connect())
    await asyncio.sleep(0.8)
    assert not task.done()                     # still waiting, not failed

    s1 = registry.register("serial", "mc-browser-test")
    await asyncio.wait_for(task, timeout=2)    # attaches as soon as it appears
    s1.feed(b"\x3e\x05")
    s1.feed(b"\x00abc")
    assert await t.readexactly(3) == b"\x3e\x05\x00"
    assert await t.readexactly(3) == b"abc"

    await t.write(b"\x01\x02")
    assert await s1.next_tx() == b"\x01\x02"

    registry.unregister(s1)                    # browser tab closed
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(t.readexactly(1), timeout=2)

    s2 = registry.register("serial", "mc-browser-test")
    await asyncio.wait_for(t.connect(), timeout=2)
    s2.feed(b"xy")
    assert await t.readexactly(2) == b"xy"
    registry.unregister(s2)
