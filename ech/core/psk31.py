"""
ech/core/psk31.py
-----------------
PSK31 (BPSK 31.25 baud, G3PLX varicode) encode + decode, pure numpy — same
device-free, offline-per-transmission architecture as cw.py/rtty.py.

At 8 kHz sample rate one symbol is exactly 256 samples, which makes symbol
alignment clean. Bit convention: phase REVERSAL = 0, no reversal = 1; idle is
a stream of reversals; characters are separated by "00" (varicode words never
contain "00" internally and always start/end with 1 — a property the test
suite asserts over the whole table).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# ── G3PLX varicode, indexed by ASCII code 0-127 ──────────────────────────────

_VARICODE = [
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011",
    "1101011111", "1011101111", "1011111101", "1011111111", "11101111",
    "11101", "1101101111", "1011011101", "11111", "1101110101", "1110101011",
    "1011110111", "1011110101", "1110101101", "1110101111", "1101011011",
    "1101101011", "1101101101", "1101010111", "1101111011", "1101111101",
    "1110110111", "1101010101", "1101011101", "1110111011", "1011111011",
    "1101111111",
    "1", "111111111", "101011111", "111110101", "111011011", "1011010101",
    "1010111011", "101111111", "11111011", "11110111", "101101111",
    "111011111", "1110101", "110101", "1010111", "110101111",
    "10110111", "10111101", "11101101", "11111111", "101110111", "101011011",
    "101101011", "110101101", "110101011", "110110111",
    "11110101", "110111101", "111101101", "1010101", "111010111", "1010101111",
    "1010111101",
    "1111101", "11101011", "10101101", "10110101", "1110111", "11011011",
    "11111101", "101010101", "1111111", "111111101", "101111101", "11010111",
    "10111011", "11011101", "10101011", "11010101", "111011101", "10101111",
    "1101111", "1101101", "101010111", "110110101", "101011101", "101110101",
    "101111011", "1010101101",
    "111110111", "111101111", "111111011", "1010111111", "101101101",
    "1011011111",
    "1011", "1011111", "101111", "101101", "11", "111101", "1011011",
    "101011", "1101", "111101011", "10111111", "11011", "111011", "1111",
    "111", "111111", "110111111", "10101", "10111", "101", "110111",
    "1111011", "1101011", "11011111", "1011101", "111010101",
    "1010110111", "110111011", "1010110101", "1011010111", "1110110101",
]
_CODE_TO_CHAR = {code: chr(i) for i, code in enumerate(_VARICODE)}


@dataclass
class DecodedText:
    text: str
    freq: float
    snr_db: float
    mode: str = "PSK31"
    baud: float = 31.25


# ── Encoder ──────────────────────────────────────────────────────────────────

def encode_psk31(text: str, freq: float = 1000.0, sample_rate: int = 8000,
                 amplitude: float = 0.8) -> np.ndarray:
    """Shaped BPSK: on a phase reversal the amplitude follows a raised-cosine
    null across the symbol boundary (standard PSK31 shaping — hard 180° flips
    splatter across the band). Idle preambles/tails of reversals bracket the
    text, exactly like real PSK31 software idles."""
    sym_n = int(sample_rate / 31.25)          # 256 @ 8 kHz

    bits: list[int] = [0] * 32                # idle preamble: reversals
    for ch in text:
        code = _VARICODE[ord(ch)] if ord(ch) < 128 else None
        if code is None:
            continue
        bits += [int(b) for b in code] + [0, 0]
    bits += [0] * 32                           # idle tail

    # Phase per symbol from the bit stream (0 = flip, 1 = hold)
    phases: list[int] = [0]
    for b in bits:
        phases.append(phases[-1] ^ (0 if b else 1))

    n_total = len(phases) * sym_n
    t = np.arange(n_total, dtype=np.float64)
    carrier = np.sin(2 * math.pi * freq * t / sample_rate)
    env = np.ones(n_total, dtype=np.float64)
    sign = np.ones(n_total, dtype=np.float64)

    half = sym_n // 2
    ramp = 0.5 * (1 + np.cos(np.linspace(0, math.pi, half)))   # 1 → 0
    for i, ph in enumerate(phases):
        s = i * sym_n
        sign[s:s + sym_n] = 1.0 if ph == 0 else -1.0
        if i + 1 < len(phases) and phases[i + 1] != ph:
            env[s + sym_n - half:s + sym_n] = ramp                 # fade out
        if i > 0 and phases[i - 1] != ph:
            env[s:s + half] = ramp[::-1]                           # fade in
    return (amplitude * env * sign * carrier).astype(np.float32)


# ── Decoder ──────────────────────────────────────────────────────────────────

@dataclass
class PSK31Decoder:
    sample_rate: int = 8000
    freq: float = 1000.0
    end_of_tx_s: float = 0.6

    _buf: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _tx_samples: list = field(default_factory=list)
    _noise: float = 1e-7
    _warmup_left: int = 12
    _wu_vals: list = field(default_factory=list)
    _in_tx: bool = False
    _idle_blocks: int = 0
    _tx_snr_num: float = 0.0
    _tx_snr_den: int = 0

    def __post_init__(self):
        self._sym_n = int(self.sample_rate / 31.25)
        self._block_n = self._sym_n // 4

    def process(self, samples: np.ndarray) -> list[DecodedText]:
        out: list[DecodedText] = []
        self._buf = np.concatenate([self._buf, samples.astype(np.float32)])
        while len(self._buf) >= self._block_n:
            block, self._buf = self._buf[:self._block_n], self._buf[self._block_n:]
            tx = self._process_block(block)
            if tx is not None:
                out.append(tx)
        return out

    def flush(self) -> "DecodedText | None":
        return self._end_tx() if self._in_tx else None

    def _band_power(self, block: np.ndarray) -> float:
        n = len(block)
        k = int(0.5 + n * self.freq / self.sample_rate)
        w = 2 * math.pi * k / n
        coeff = 2 * math.cos(w)
        s0 = s1 = s2 = 0.0
        for x in block:
            s0 = x + coeff * s1 - s2
            s2 = s1
            s1 = s0
        return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / (n * n)

    def _process_block(self, block: np.ndarray) -> "DecodedText | None":
        p = self._band_power(block)

        if self._warmup_left > 0:
            self._warmup_left -= 1
            self._wu_vals.append(p)
            if self._warmup_left == 0:
                sv = sorted(self._wu_vals)
                self._noise = max(sv[len(sv) // 2], 1e-12)
                self._wu_vals = []
            return None

        # PSK31 amplitude nulls on every reversal, so gate with a LOW open
        # threshold and a generous idle timeout rather than per-block strictness.
        present = p > self._noise * 40
        if not present and not self._in_tx:
            if p < self._noise:
                self._noise += (p - self._noise) * 0.05
            else:
                self._noise += (p - self._noise) * 0.02
            self._noise = max(self._noise, 1e-12)

        if not self._in_tx:
            if present:
                self._in_tx = True
                self._tx_samples = [block]
                self._idle_blocks = 0
                self._tx_snr_num, self._tx_snr_den = p, 1
            return None

        self._tx_samples.append(block)
        if present:
            self._idle_blocks = 0
            self._tx_snr_num += p
            self._tx_snr_den += 1
        else:
            self._idle_blocks += 1
            blocks_per_s = self.sample_rate / self._block_n
            if self._idle_blocks >= self.end_of_tx_s * blocks_per_s:
                return self._end_tx()
        return None

    def _end_tx(self) -> "DecodedText | None":
        audio = np.concatenate(self._tx_samples) if self._tx_samples else np.zeros(0, dtype=np.float32)
        self._in_tx = False
        self._tx_samples = []
        text = self._decode_offline(audio)
        if not text.strip():
            return None
        snr_db = 0.0
        if self._tx_snr_den and self._noise > 0:
            snr_db = 10 * math.log10((self._tx_snr_num / self._tx_snr_den) / self._noise)
        return DecodedText(text=text.strip(), freq=self.freq, snr_db=round(snr_db, 1))

    def _decode_offline(self, audio: np.ndarray) -> str:
        """Coherent mix to baseband, symbol-sync by envelope, differential
        detection: correlation sign between adjacent symbol vectors gives the
        bit (negative = reversal = 0)."""
        sym_n = self._sym_n
        if len(audio) < 8 * sym_n:
            return ""
        t = np.arange(len(audio), dtype=np.float64)
        lo = np.exp(-2j * math.pi * self.freq * t / self.sample_rate)
        base = audio.astype(np.float64) * lo

        # Symbol sync: idle reversals put an amplitude null at every symbol
        # boundary — choose the offset whose per-symbol integration magnitude
        # is largest (boundary-aligned integration never straddles a null).
        best_off, best_metric = 0, -1.0
        n_probe = min(len(base) // sym_n - 1, 40)
        for off in range(0, sym_n, sym_n // 32):
            segs = base[off:off + n_probe * sym_n].reshape(n_probe, sym_n)
            metric = float(np.sum(np.abs(segs.mean(axis=1))))
            if metric > best_metric:
                best_metric, best_off = metric, off

        usable = (len(base) - best_off) // sym_n
        segs = base[best_off:best_off + usable * sym_n].reshape(usable, sym_n)
        vecs = segs.mean(axis=1)

        # Differential bits between adjacent symbols
        corr = vecs[1:] * np.conj(vecs[:-1])
        mags = np.abs(vecs)
        active = mags > (np.max(mags) * 0.15) if len(mags) else mags
        bits: list[str] = []
        for i, c in enumerate(corr):
            if not (active[i] or active[i + 1]):
                continue                        # dead air (pre/post gate slop)
            bits.append("1" if c.real > 0 else "0")
        bitstr = "".join(bits)

        # Characters are "00"-delimited varicode words; idle runs vanish
        out: list[str] = []
        for word in bitstr.split("00"):
            w = word.strip("0")   # idle padding around the word
            if w and w in _CODE_TO_CHAR:
                out.append(_CODE_TO_CHAR[w])
        return "".join(out)
