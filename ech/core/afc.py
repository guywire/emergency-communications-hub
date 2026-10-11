"""
ech/core/afc.py
---------------
Automatic frequency control helpers for the sound-card modems.

With a hand-tuned radio feeding audio, a digital signal lands wherever the
operator's VFO puts it in the passband — not on a configured audio
frequency. CW tolerates that (wide Goertzel bin + its own auto-tune), but
PSK31 needs the carrier within a few Hz and RTTY within ~20 Hz, so both
search the passband here instead of listening on one fixed frequency.

All functions take a mono float audio window and return a frequency in Hz,
or None when nothing clearly stands out of the noise (so callers keep their
current frequency rather than chasing noise).
"""

from __future__ import annotations

import numpy as np


def _spectrum(audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(audio, dtype=np.float64)
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    freqs = np.fft.rfftfreq(len(a), 1.0 / sample_rate)
    return freqs, spec


def _parabolic(y: np.ndarray, i: int) -> float:
    """Sub-bin peak offset from three points around index i."""
    if 0 < i < len(y) - 1:
        a, b, c = y[i - 1], y[i], y[i + 1]
        d = a - 2 * b + c
        if d != 0:
            return 0.5 * (a - c) / d
    return 0.0


def _smoothed_peak(freqs, power, lo, hi, width_hz, min_ratio):
    df = freqs[1] - freqs[0]
    k = max(int(round(width_hz / df)), 1)
    sm = np.convolve(power, np.ones(k) / k, mode="same")
    i0, i1 = np.searchsorted(freqs, lo), np.searchsorted(freqs, hi)
    if i1 - i0 < 3:
        return None
    band = sm[i0:i1]
    pk = int(np.argmax(band))
    med = float(np.median(band)) or 1e-30
    if band[pk] < min_ratio * med:
        return None
    return float(freqs[i0 + pk] + _parabolic(band, pk) * df)


def find_psk_center(audio: np.ndarray, sample_rate: int, lo: float = 300.0,
                    hi: float = 2700.0, min_ratio: float = 4.0) -> float | None:
    """Coarse PSK31 centre: peak of the spectrum smoothed over the signal's
    ~60 Hz occupied bandwidth (idle reversals put the energy at f±15.6 Hz,
    not on f itself, so a raw-bin peak would land beside the carrier)."""
    if len(audio) < 1024:
        return None
    freqs, spec = _spectrum(audio, sample_rate)
    return _smoothed_peak(freqs, spec, lo, hi, 62.5, min_ratio)


def refine_psk_carrier(audio: np.ndarray, sample_rate: int, coarse: float,
                       search_hz: float = 40.0) -> float | None:
    """Exact BPSK carrier: squaring strips the 180° phase flips and leaves a
    clean spectral line. Done at complex BASEBAND (mixed down by `coarse`,
    low-passed to the signal's bandwidth) so the line sits at 2*(f-coarse)
    near DC — squaring the raw audio instead puts it at 2f, which aliases
    past Nyquist for any carrier above sample_rate/4 (2 kHz at 8 kHz)."""
    n = len(audio)
    if n < 2048:
        return None
    t = np.arange(n, dtype=np.float64)
    z = np.asarray(audio, dtype=np.float64) * np.exp(-2j * np.pi * coarse * t / sample_rate)
    taps = max(int(sample_rate / 62.5), 1)          # ≈ ±60 Hz low-pass
    z = np.convolve(z, np.ones(taps) / taps, mode="same")
    z2 = z * z
    spec = np.abs(np.fft.fftshift(np.fft.fft(z2 * np.hanning(n)))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / sample_rate))
    i0 = np.searchsorted(freqs, -2 * search_hz)
    i1 = np.searchsorted(freqs, 2 * search_hz)
    if i1 - i0 < 3:
        return None
    band = spec[i0:i1]
    pk = int(np.argmax(band))
    med = float(np.median(band)) or 1e-30
    if band[pk] < 8.0 * med:
        return None
    df = freqs[1] - freqs[0]
    return coarse + float(freqs[i0 + pk] + _parabolic(band, pk) * df) / 2.0


def find_fsk_pair(audio: np.ndarray, sample_rate: int, shift: float,
                  lo: float = 300.0, hi: float = 2900.0,
                  min_ratio: float = 6.0) -> float | None:
    """Lower tone of the strongest pair of tones `shift` Hz apart. Scoring
    the PAIR (not the single loudest tone) keeps a CW carrier or a birdie
    from being mistaken for an RTTY signal."""
    if len(audio) < 1024:
        return None
    freqs, spec = _spectrum(audio, sample_rate)
    df = freqs[1] - freqs[0]
    k = max(int(round(10.0 / df)), 1)              # ±5 Hz tone smear
    sm = np.convolve(spec, np.ones(k) / k, mode="same")
    s_bins = int(round(shift / df))
    i0 = np.searchsorted(freqs, lo)
    i1 = np.searchsorted(freqs, hi - shift)
    if i1 - i0 < 3 or i1 + s_bins >= len(sm):
        return None
    lower = sm[i0:i1]
    upper = sm[i0 + s_bins:i1 + s_bins]
    # min() of the two tones: both must be present; a lone carrier scores ~0
    score = np.minimum(lower, upper)
    pk = int(np.argmax(score))
    med = float(np.median(sm[i0:i1 + s_bins])) or 1e-30
    if score[pk] < min_ratio * med:
        return None
    return float(freqs[i0 + pk] + _parabolic(lower, pk) * df)
