"""
ech/core/rtty.py
----------------
RTTY (Baudot/ITA2 FSK) encode + decode, pure numpy — same design as cw.py:
device-free DSP core, transmissions buffered while the signal gate is open and
decoded offline at key-up, so the whole encode→decode path unit-tests without
hardware.

Standard amateur RTTY: 45.45 baud, 170 Hz shift (mark 2125 / space 2295 Hz),
5-bit ITA2 with LTRS/FIGS shift, 1 start bit (space), 1.5+ stop bits (mark).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# ── ITA2 (US-TTY) tables, indexed by 5-bit code ─────────────────────────────

_LTRS = ["\x00", "E", "\n", "A", " ", "S", "I", "U", "\r", "D", "R", "J",
         "N", "F", "C", "K", "T", "Z", "L", "W", "H", "Y", "P", "Q", "O",
         "B", "G", "FIGS", "M", "X", "V", "LTRS"]
_FIGS = ["\x00", "3", "\n", "-", " ", "\x07", "8", "7", "\r", "$", "4", "'",
         ",", "!", ":", "(", "5", '"', ")", "2", "#", "6", "0", "1", "9",
         "?", "&", "FIGS", ".", "/", ";", "LTRS"]
_LTRS_CODE = 31
_FIGS_CODE = 27
_CHAR_TO_LTRS = {c: i for i, c in enumerate(_LTRS) if len(c) == 1}
_CHAR_TO_FIGS = {c: i for i, c in enumerate(_FIGS) if len(c) == 1 and c not in _CHAR_TO_LTRS}


@dataclass
class DecodedText:
    text: str
    freq: float          # mark frequency
    snr_db: float
    mode: str = "RTTY"
    baud: float = 45.45


# ── Encoder ──────────────────────────────────────────────────────────────────

def encode_rtty(text: str, baud: float = 45.45, mark: float = 2125.0,
                shift: float = 170.0, sample_rate: int = 8000,
                amplitude: float = 0.8) -> np.ndarray:
    """Render text as continuous-phase FSK RTTY audio (float32 mono).
    Leads with LTRS×3 (the traditional 'diddle' preamble that also settles the
    receiver's shift state) and idles on mark before/after."""
    space = mark + shift
    bit_s = 1.0 / baud

    # Build the bit stream: True = mark(1), False = space(0)
    bits: list[tuple[bool, float]] = [(True, 20 * bit_s)]     # mark idle lead-in
    shift_state = "LTRS"

    def push_code(code: int) -> None:
        bits.append((False, bit_s))                 # start bit = space
        for k in range(5):                          # LSB first
            bits.append((bool((code >> k) & 1), bit_s))
        bits.append((True, 1.5 * bit_s))            # stop = 1.5 mark bits

    for _ in range(3):
        push_code(_LTRS_CODE)
    for ch in text.upper():
        if ch in _CHAR_TO_LTRS:
            if shift_state != "LTRS":
                push_code(_LTRS_CODE)
                shift_state = "LTRS"
            push_code(_CHAR_TO_LTRS[ch])
        elif ch in _CHAR_TO_FIGS:
            if shift_state != "FIGS":
                push_code(_FIGS_CODE)
                shift_state = "FIGS"
            push_code(_CHAR_TO_FIGS[ch])
        # unsupported chars are dropped
    bits.append((True, 8 * bit_s))                  # mark idle tail

    # Continuous-phase synthesis: accumulate phase across tone switches so the
    # FSK has no clicks, with cumulative (not per-bit) sample rounding so the
    # 176.06-samples-per-bit at 8 kHz never drifts.
    total_t = 0.0
    phase = 0.0
    out: list[np.ndarray] = []
    written = 0
    for is_mark, dur in bits:
        total_t += dur
        end_sample = int(round(total_t * sample_rate))
        n = end_sample - written
        if n <= 0:
            continue
        f = mark if is_mark else space
        t = np.arange(n, dtype=np.float64)
        seg = amplitude * np.sin(phase + 2 * math.pi * f * t / sample_rate)
        phase = (phase + 2 * math.pi * f * n / sample_rate) % (2 * math.pi)
        out.append(seg.astype(np.float32))
        written = end_sample
    return np.concatenate(out)


# ── Decoder ──────────────────────────────────────────────────────────────────

def _tone_power(block: np.ndarray, sample_rate: int, freq: float) -> float:
    n = len(block)
    k = int(0.5 + n * freq / sample_rate)
    w = 2 * math.pi * k / n
    coeff = 2 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for x in block:
        s0 = x + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / (n * n)


@dataclass
class RTTYDecoder:
    """Gate on combined mark+space power, buffer the transmission, decode
    offline at signal end (same architecture as CWDecoder, same reasons)."""
    sample_rate: int = 8000
    baud: float = 45.45
    mark: float = 2125.0
    shift: float = 170.0
    end_of_tx_s: float = 0.5

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
        # Block = 1/4 bit so edge timing resolves to ±12.5% of a bit
        self._block_n = max(int(self.sample_rate / self.baud / 4), 8)

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

    def _process_block(self, block: np.ndarray) -> "DecodedText | None":
        pm = _tone_power(block, self.sample_rate, self.mark)
        ps = _tone_power(block, self.sample_rate, self.mark + self.shift)
        p = pm + ps

        if self._warmup_left > 0:
            self._warmup_left -= 1
            self._wu_vals.append(p)
            if self._warmup_left == 0:
                sv = sorted(self._wu_vals)
                self._noise = max(sv[len(sv) // 2], 1e-12)
                self._wu_vals = []
            return None

        present = p > self._noise * 60
        if not present:
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
        return DecodedText(text=text.strip(), freq=self.mark, snr_db=round(snr_db, 1),
                           baud=self.baud)

    def _decode_offline(self, audio: np.ndarray) -> str:
        """Per-block FSK discrimination, then a start-bit hunting bit clock.
        Each character re-syncs on its own start bit, so small timing drift
        never accumulates across the transmission."""
        n = self._block_n
        n_blocks = len(audio) // n
        if n_blocks < 8:
            return ""
        is_mark = np.zeros(n_blocks, dtype=bool)
        power = np.zeros(n_blocks)
        for i in range(n_blocks):
            b = audio[i * n:(i + 1) * n]
            pm = _tone_power(b, self.sample_rate, self.mark)
            ps = _tone_power(b, self.sample_rate, self.mark + self.shift)
            is_mark[i] = pm >= ps
            power[i] = pm + ps
        # A real character has continuous tone power across its whole 7-bit
        # window; trailing noise blocks (buffered before the gate's idle
        # timeout fired) can fool the mark/space COMPARATOR but not the
        # power test — without this, noise tails decoded phantom characters.
        med_power = float(np.median(power)) or 1e-12

        blocks_per_bit = self.sample_rate / self.baud / n   # ≈ 4.0
        chars: list[str] = []
        shift_state = "LTRS"
        i = 0
        while i < n_blocks - int(7 * blocks_per_bit):
            if is_mark[i] or not (i == 0 or is_mark[i - 1]):
                i += 1
                continue
            # mark→space edge at block i = start bit. Sample each data bit at
            # its centre (majority over the middle 2 blocks of the 4-block cell).
            def bit_at(bit_idx: float) -> bool:
                c = i + (bit_idx + 0.5) * blocks_per_bit
                lo = max(int(c) - 1, 0)
                votes = is_mark[lo:lo + 2]
                return bool(np.sum(votes) >= 1)
            # Validate the start bit itself is space at centre
            if bit_at(0.0):
                i += 1
                continue
            # Power continuity across the character window (see note above)
            win_end = min(i + int(7 * blocks_per_bit), n_blocks)
            if float(np.min(power[i:win_end])) < 0.2 * med_power:
                i += 1
                continue
            code = 0
            for k in range(5):
                if bit_at(1.0 + k):
                    code |= (1 << k)
            if not bit_at(6.0):        # stop bit must be mark — else false sync
                i += 1
                continue
            sym = (_LTRS if shift_state == "LTRS" else _FIGS)[code]
            if sym == "LTRS":
                shift_state = "LTRS"
            elif sym == "FIGS":
                shift_state = "FIGS"
            elif sym not in ("\x00", "\x07"):
                chars.append(sym)
            i += int(round(7 * blocks_per_bit)) - 1   # jump past stop bit
        return "".join(chars).replace("\r", "")
