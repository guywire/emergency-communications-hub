"""
tests/test_cw_browser_audio.py
------------------------------
Browser-hosted CW audio (input_device: browser) must:
  - connect without a browser present (no router backoff → deaf windows),
  - attach to a /remote-hw session whenever it appears,
  - re-attach after the browser drops and reconnects,
and decode CW fed through the same float32 @ 8 kHz chunk framing the
/remote-hw page sends.
"""

import asyncio

import numpy as np
import pytest

from ech.adapters.cw_audio import CWAudioAdapter
from ech.core.cw import encode_cw
from ech.core.remote_hw import registry


async def _feed_cw(sess, text: str) -> None:
    audio = encode_cw(text, wpm=20, freq=600, sample_rate=8000, amplitude=0.5)
    pad = np.zeros(8000 * 2, dtype=np.float32)          # trailing silence ends the transmission
    stream = np.concatenate([pad[:4000], audio.astype(np.float32), pad])
    for i in range(0, len(stream), 682):                # ≈ the page's per-callback chunk size
        sess.feed(stream[i:i + 682].tobytes())
        await asyncio.sleep(0)


async def _wait_decode(adapter, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while adapter._last_decode is None:
        if asyncio.get_running_loop().time() > deadline:
            return None
        await asyncio.sleep(0.05)
    return adapter._last_decode


@pytest.mark.asyncio
async def test_browser_cw_attaches_late_and_reattaches():
    a = CWAudioAdapter({"type": "cw_audio", "name": "cw-browser-test", "input_device": "browser"})
    await a.connect()                                   # no browser yet — must not raise
    assert a._connected
    try:
        await asyncio.sleep(0.2)
        s1 = registry.register("audio", "cw-browser-test")
        await asyncio.sleep(0.7)                        # registry polls every 0.5 s
        assert a._hw_sess is s1
        await _feed_cw(s1, "CQ TEST")
        d = await _wait_decode(a)
        assert d is not None and "TEST" in d["text"]

        registry.unregister(s1)                         # browser tab closed
        await asyncio.sleep(0.1)
        assert a._hw_sess is None and a._connected

        a._last_decode = None
        s2 = registry.register("audio", "cw-browser-test")
        await asyncio.sleep(0.7)
        assert a._hw_sess is s2
        await _feed_cw(s2, "DE KN0O")
        d = await _wait_decode(a)
        assert d is not None and "KN0O" in d["text"]
        registry.unregister(s2)
    finally:
        await a.disconnect()
