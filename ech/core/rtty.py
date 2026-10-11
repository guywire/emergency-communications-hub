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

_PROBE_CACHE: dict = {}


def _tone_power(block: np.ndarray, sample_rate: int, freq: float) -> float:
    """Power at EXACTLY `freq` (single-frequency DFT). The previous integer-bin
    Goertzel snapped to the block's bin grid — 182 Hz at 44 samples — so mark
    and space were measured up to ~90 Hz off and leaked into each other
    depending on where the signal sat (tone dominance 0.99 at 1277 Hz, 0.72
    at 2128 Hz), costing copy at many AFC-found frequencies."""
    n = len(block)
    key = (n, sample_rate, round(freq, 1))
    probe = _PROBE_CACHE.get(key)
    if probe is None:
        if len(_PROBE_CACHE) > 4096:
            _PROBE_CACHE.clear()
        probe = np.exp(-2j * math.pi * freq * np.arange(n) / sample_rate)
        _PROBE_CACHE[key] = probe
    z = np.dot(np.asarray(block, dtype=np.float64), probe)
    return float((z.real * z.real + z.imag * z.imag) / (n * n))


@dataclass
class RTTYDecoder:
    """Gate on combined mark+space power, buffer the transmission, decode
    offline at signal end (same architecture as CWDecoder, same reasons)."""
    sample_rate: int = 8000
    baud: float = 45.45
    mark: float = 2125.0
    shift: float = 170.0
    end_of_tx_s: float = 0.5
    afc: bool = True               # find the tone pair in the passband
    reverse: bool = False          # mark is the UPPER tone (USB / inverted)
    squelch: float = 1.0           # gate-threshold multiplier (<1 = more sensitive)

    _buf: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _raw_recent: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _afc_countdown: int = 0
    _p_hist: list = field(default_factory=list)
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
        if self.afc:
            self._raw_recent = np.concatenate([self._raw_recent, samples.astype(np.float32)])[-self.sample_rate:]
        while len(self._buf) >= self._block_n:
            block, self._buf = self._buf[:self._block_n], self._buf[self._block_n:]
            tx = self._process_block(block)
            if tx is not None:
                out.append(tx)
        return out

    def flush(self) -> "DecodedText | None":
        return self._end_tx() if self._in_tx else None

    def _afc_retune(self) -> None:
        """Between transmissions, move onto the strongest tone pair `shift`
        apart (~4x/s). `mark` here is always the LOWER audio tone; `reverse`
        decides which of the two carries mark at demod time."""
        self._afc_countdown -= 1
        if self._afc_countdown > 0 or len(self._raw_recent) < self.sample_rate // 2:
            return
        self._afc_countdown = int(0.25 * self.sample_rate / self._block_n)
        from ech.core.afc import find_fsk_pair
        f = find_fsk_pair(self._raw_recent, self.sample_rate, self.shift)
        if f is not None and abs(f - self.mark) > 10:
            self.mark = f

    def _process_block(self, block: np.ndarray) -> "DecodedText | None":
        pm = _tone_power(block, self.sample_rate, self.mark)
        ps = _tone_power(block, self.sample_rate, self.mark + self.shift)
        # Gate on power averaged over ~1 bit (4 blocks): single 5.5 ms blocks
        # swing so much in noise that the old per-block 60x gate had to sit
        # far above the signal level. Measured: 17/60 → 50/60 weak-signal
        # decodes at 8x, still zero junk from 200 s of pure noise.
        self._p_hist.append(pm + ps)
        del self._p_hist[:-4]
        p = sum(self._p_hist) / len(self._p_hist)

        if self._warmup_left > 0:
            self._warmup_left -= 1
            self._wu_vals.append(p)
            if self._warmup_left == 0:
                sv = sorted(self._wu_vals)
                self._noise = max(sv[len(sv) // 2], 1e-12)
                self._wu_vals = []
            return None

        thr = self._noise * 8 * self.squelch
        present = p > thr
        # Retune between transmissions, and ALSO mid-transmission when the
        # gate is only barely open: that means it opened on leakage from a
        # signal while parked on the previous one's frequency, and the
        # leakage can sag mid-message and split it (seen with CW → RTTY on a
        # shared stream). A solidly-received signal (≥3× threshold) never
        # hops, so another station on a busy band can't steal the lock — and
        # never while the gate is CLOSED mid-tx (that's this signal ending;
        # hopping then merged the next station into it and lost this one).
        if self.afc and ((not self._in_tx and not present)
                         or (self._in_tx and present and p < 3 * thr)):
            self._afc_retune()
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
        if self.afc:
            # Lock the pair from the whole transmission (finer than the 1 s
            # coarse window); fall back to a full-band search if the gate
            # opened before the coarse AFC moved.
            from ech.core.afc import find_fsk_pair
            f = find_fsk_pair(audio, self.sample_rate, self.shift,
                              lo=self.mark - 60, hi=self.mark + self.shift + 60)
            if f is None:
                f = find_fsk_pair(audio, self.sample_rate, self.shift)
            if f is not None:
                self.mark = f
        text = self._decode_offline(audio)
        if not text.strip():
            return None
        snr_db = 0.0
        if self._tx_snr_den and self._noise > 0:
            snr_db = 10 * math.log10((self._tx_snr_num / self._tx_snr_den) / self._noise)
        return DecodedText(text=text.strip(), freq=round(self.mark, 1), snr_db=round(snr_db, 1),
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
        pm_arr = np.zeros(n_blocks)
        ps_arr = np.zeros(n_blocks)
        for i in range(n_blocks):
            b = audio[i * n:(i + 1) * n]
            pm = _tone_power(b, self.sample_rate, self.mark)
            ps = _tone_power(b, self.sample_rate, self.mark + self.shift)
            pm_arr[i], ps_arr[i] = pm, ps
            is_mark[i] = (pm >= ps) != self.reverse
            power[i] = pm + ps
        # A real character has continuous tone power across its whole 7-bit
        # window; trailing noise blocks (buffered before the gate's idle
        # timeout fired) can fool the mark/space COMPARATOR but not the
        # power test — without this, noise tails decoded phantom characters.
        med_power = float(np.median(power)) or 1e-12

        # Is this really FSK? In a real RTTY bit one tone dominates (|pm-ps|/
        # (pm+ps) ≈ 1); broadband static crashes put equal energy on both
        # tones (≈ 0.5). Live band noise produced a steady trickle of 1–3
        # character junk decodes ("K", "V", "CMX") that this rejects.
        # Measured: real 0.93–0.99 (white noise to 0.25, band noise + crashes),
        # junk 0.45–0.57.
        strong = power > 0.5 * med_power
        if strong.any():
            dom = float(np.mean(np.abs(pm_arr[strong] - ps_arr[strong]) / (power[strong] + 1e-30)))
            if dom < 0.75:
                return ""

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
