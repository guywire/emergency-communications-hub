"""
tests/test_modem_shared_session.py
----------------------------------
CW + RTTY + PSK31 adapters sharing ONE /remote-hw browser audio session
(browser_session), each decoding its own mode from the same stream, with
off-frequency signals found by AFC — plus live tuning via apply_tuning.
"""

import asyncio

import numpy as np
import pytest

from ech.adapters.cw_audio import CWAudioAdapter
from ech.adapters.rtty_audio import PSK31AudioAdapter, RTTYAudioAdapter
from ech.core.cw import encode_cw
from ech.core.psk31 import encode_psk31
from ech.core.remote_hw import registry
from ech.core.rtty import encode_rtty

SR = 8000


def _gap(s):
    return np.zeros(int(SR * s), dtype=np.float32)


async def _feed(sess, audio):
    for i in range(0, len(audio), 682):
        sess.feed(audio[i:i + 682].astype(np.float32).tobytes())
        await asyncio.sleep(0)


async def _drain(adapter, timeout=15.0):
    """Wait for the adapter's DSP backlog to clear, then collect everything
    it enqueued (base Adapter._enqueue → _rx_queue)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not adapter._sample_q.empty() and loop.time() < end:
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    msgs = []
    while not adapter._rx_queue.empty():
        msgs.append(adapter._rx_queue.get_nowait())
    return msgs


@pytest.mark.asyncio
async def test_three_modems_one_session_each_decode_own_mode():
    common = {"input_device": "browser", "browser_session": "radio-audio-test"}
    cw = CWAudioAdapter({"type": "cw_audio", "name": "t-cw", **common})
    rt = RTTYAudioAdapter({"type": "rtty_audio", "name": "t-rtty", **common})
    pk = PSK31AudioAdapter({"type": "psk31_audio", "name": "t-psk", **common})
    for a in (cw, rt, pk):
        await a.connect()
    sess = registry.register("audio", "radio-audio-test")
    try:
        await asyncio.sleep(0.7)
        assert cw._hw_sess is sess and rt._hw_sess is sess and pk._hw_sess is sess

        rng = np.random.default_rng(5)
        stream = np.concatenate([
            _gap(1.0),
            encode_cw("CQ DE KN0O", wpm=18, freq=700, sample_rate=SR, amplitude=0.4),
            _gap(2.5),
            encode_rtty("RYRY DE KN0O", mark=1275.0, shift=170, sample_rate=SR, amplitude=0.4),
            _gap(2.5),
            encode_psk31("PSK DE KN0O", freq=1437.0, sample_rate=SR, amplitude=0.4),
            _gap(2.5),
        ]).astype(np.float32)
        stream += 0.03 * rng.standard_normal(len(stream)).astype(np.float32)
        await _feed(sess, stream)
        await asyncio.sleep(1.0)

        got = {}
        for a in (cw, rt, pk):
            got[a.name] = [m.body for m in await _drain(a, timeout=5.0)]
        assert any("KN0O" in b for b in got["t-cw"]), got
        assert any("KN0O" in b for b in got["t-rtty"]), got
        assert any("KN0O" in b for b in got["t-psk"]), got
        # Cross-talk: each decoder should emit only its own transmission
        assert len(got["t-cw"]) == 1, got
        assert len(got["t-rtty"]) == 1, got
        assert len(got["t-psk"]) == 1, got
    finally:
        registry.unregister(sess)
        for a in (cw, rt, pk):
            await a.disconnect()


def test_apply_tuning_updates_decoder_and_settings():
    cw = CWAudioAdapter({"type": "cw_audio", "name": "t-cw2"})
    s = cw.apply_tuning({"tx_wpm": 13, "sensitivity": 5, "freq": 650, "afc": False})
    assert s["tx_wpm"] == 13 and s["sensitivity"] == 5 and s["freq"] == 650 and s["afc"] is False
    assert cw._decoder.squelch == CWAudioAdapter.SENS_SQUELCH[5]
    assert cw._decoder.auto_tune is False

    rt = RTTYAudioAdapter({"type": "rtty_audio", "name": "t-rtty2"})
    s = rt.apply_tuning({"reverse": True, "shift": 425, "sensitivity": 1})
    assert s["reverse"] is True and s["shift"] == 425
    assert rt._decoder.reverse is True and rt._decoder.shift == 425
    assert "tx_wpm" not in s

    pk = PSK31AudioAdapter({"type": "psk31_audio", "name": "t-psk2"})
    s = pk.apply_tuning({"afc": False, "freq": 1500})
    assert pk._decoder.afc is False and pk._decoder.freq == 1500
