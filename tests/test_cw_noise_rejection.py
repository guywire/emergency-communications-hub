"""
tests/test_cw_noise_rejection.py
--------------------------------
Live report (2026-10-10, noisy band via browser audio): real copy decoded
fine at 17-18 wpm, but static crashes produced streams of E/T at 60-240
"wpm". The decoder must drop noise-only transmissions and glitch marks
without hurting real copy.
"""

import numpy as np

from ech.core.cw import CWDecoder, encode_cw

SR = 8000
rng = np.random.default_rng(1234)


def _decode(audio):
    d = CWDecoder(sample_rate=SR)
    out = []
    for i in range(0, len(audio), 400):
        out += d.process(audio[i:i + 400])
    tx = d.flush()
    if tx:
        out.append(tx)
    return out


def _crashes(n_samples, n_crashes):
    """Static crashes: short (2-12 ms) bursts of 600 Hz-ish energy."""
    a = np.zeros(n_samples, dtype=np.float32)
    t = np.arange(int(0.012 * SR)) / SR
    for _ in range(n_crashes):
        at = int(rng.integers(0, n_samples - len(t)))
        ln = int(rng.integers(int(0.002 * SR), len(t)))
        f = rng.uniform(500, 700)
        a[at:at + ln] += 0.6 * np.sin(2 * np.pi * f * t[:ln]).astype(np.float32)
    return a


def test_static_crashes_alone_decode_nothing():
    n = SR * 8
    audio = 0.01 * rng.standard_normal(n).astype(np.float32) + _crashes(n, 120)
    assert _decode(audio) == []


def test_real_copy_survives_crashes():
    cw = encode_cw("CQ CQ DE KN0O K", wpm=18, freq=600, sample_rate=SR, amplitude=0.5)
    lead = np.zeros(SR // 2, dtype=np.float32)
    tail = np.zeros(SR * 2, dtype=np.float32)
    clean = np.concatenate([lead, cw.astype(np.float32), tail])
    audio = clean + 0.01 * rng.standard_normal(len(clean)).astype(np.float32) \
        + _crashes(len(clean), 15)
    texts = " ".join(t.text for t in _decode(audio))
    assert "KN0O" in texts
    assert "CQ" in texts


def test_lone_dit_or_dah_is_dropped():
    for word in ("E", "T", "E T E"):
        cw = encode_cw(word, wpm=20, freq=600, sample_rate=SR, amplitude=0.5)
        audio = np.concatenate([np.zeros(SR // 2, np.float32), cw.astype(np.float32),
                                np.zeros(SR * 2, np.float32)])
        assert _decode(audio) == []


def test_short_dit_dah_groups_dropped_but_real_words_kept():
    def run(text):
        cw = encode_cw(text, wpm=20, freq=600, sample_rate=SR, amplitude=0.5)
        audio = np.concatenate([np.zeros(SR // 2, np.float32), cw.astype(np.float32),
                                np.zeros(SR * 2, np.float32)])
        return " ".join(t.text for t in _decode(audio))
    assert run("E E EE E I") == ""          # live junk signature
    assert run("NAME") == "NAME"            # real word from the same letters
    assert "TEAM" in run("TEAM")
