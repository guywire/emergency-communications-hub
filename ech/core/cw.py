"""
ech/core/cw.py
--------------
CW (Morse code) audio DSP: a text→audio encoder and a streaming decoder.

Pure numpy — no audio-device dependency here, so the whole encode→decode
pipeline is unit-testable without hardware. Device I/O lives in
ech/adapters/cw_audio.py, which feeds raw sample blocks into CWDecoder.

Decoder design (why it's built this way):
  * Tone detection is a Goertzel filter at the (auto-tuned) CW pitch,
    evaluated per ~10 ms block — a narrow matched filter, so wideband noise
    mostly falls outside the detection bandwidth (~15 dB processing gain at
    8 kHz sample rate vs. broadband).
  * Mark/space gating uses an adaptive noise floor + peak tracker with
    hysteresis, so it self-adjusts to signal level and doesn't chatter.
  * Symbol classification is deferred until a full TRANSMISSION has been
    buffered (key-up silence ends it). Classifying dits vs dahs from the
    complete duration population (cluster split at the geometric midpoint of
    the observed range) is far more accurate than the classic greedy
    symbol-at-a-time approach, which mis-learns the unit length whenever the
    first mark happens to be a dah. The cost is that text appears at key-up
    rather than mid-transmission — for message-inbox semantics that's fine.
  * WPM is derived from the measured dit length (WPM = 1200 / dit_ms) and
    reported per transmission.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# ── Morse tables ─────────────────────────────────────────────────────────────

MORSE_TO_CHAR = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", "-..-.": "/", "-...-": "=",
    ".-.-.": "+", "-....-": "-", ".--.-.": "@", ".----.": "'", "-.-.--": "!",
    "-.--.": "(", "-.--.-": ")", ".-...": "&", "---...": ":", "-.-.-.": ";",
    ".-..-.": '"', "..--.-": "_", "...-.-": "<SK>",
}
CHAR_TO_MORSE = {v: k for k, v in MORSE_TO_CHAR.items()}
# Aliases that share a pattern with a printable char keep the printable one in
# MORSE_TO_CHAR; encoding accepts the prosign spellings too.
CHAR_TO_MORSE.update({"<AR>": ".-.-.", "<BT>": "-...-", "<KN>": "-.--."})


# ── CW abbreviation / Q-code expansion ───────────────────────────────────────
# Standard operating abbreviations. annotate_cw() appends the meaning in
# parentheses after the FIRST occurrence of each abbreviation in a decoded
# transmission — repeats stay bare so "CQ CQ CQ DE ..." doesn't triple up.

CW_ABBREVIATIONS = {
    "CQ": "calling any station", "DE": "from", "K": "over", "KN": "over, named station only",
    "R": "roger", "ES": "and", "TU": "thank you", "TNX": "thanks", "TKS": "thanks",
    "73": "best regards", "88": "love and kisses", "GM": "good morning",
    "GA": "good afternoon", "GE": "good evening", "GN": "good night",
    "OM": "old man", "YL": "young lady", "XYL": "wife", "OP": "operator",
    "WX": "weather", "QTH": "location", "RIG": "radio", "ANT": "antenna",
    "PWR": "power", "HW": "how copy", "CPY": "copy", "CPI": "copy",
    "FB": "fine business", "HI": "laughter", "AGN": "again", "PSE": "please",
    "SRI": "sorry", "BK": "break", "CL": "closing station", "DX": "long distance",
    "UR": "your", "RST": "signal report", "NR": "number", "ABT": "about",
    "B4": "before", "BCNU": "be seeing you", "CUL": "see you later",
    "HR": "here", "NW": "now", "VY": "very", "GL": "good luck", "GB": "goodbye",
    "DR": "dear", "FER": "for", "HPE": "hope", "GUD": "good", "NIL": "nothing",
    "MSG": "message", "RCVD": "received", "RCD": "received", "WKD": "worked",
    "WL": "will", "TMW": "tomorrow", "SIG": "signal", "RPT": "repeat",
    "SK": "end of contact", "<SK>": "end of contact", "<AR>": "end of message",
    "<BT>": "break", "<KN>": "over, named station only",
    "QRL": "is the frequency busy?", "QRM": "interference", "QRN": "static noise",
    "QRP": "low power", "QRO": "high power", "QRS": "send slower",
    "QRQ": "send faster", "QRT": "stopping transmission", "QRV": "ready",
    "QRX": "stand by", "QRZ": "who is calling me?", "QSB": "signal fading",
    "QSL": "confirmed", "QSO": "contact", "QSY": "change frequency",
    "QST": "general call to all amateurs", "QNI": "check in to net",
}


def annotate_cw(text: str) -> str:
    """Expand CW abbreviations in-line: 'TU 73 ES GL OM' →
    'TU (thank you) 73 (best regards) ES (and) GL (good luck) OM (old man)'.
    Only the first occurrence of each abbreviation is expanded."""
    seen: set[str] = set()
    out: list[str] = []
    for token in text.split(" "):
        key = token.upper().rstrip("?")
        if key in CW_ABBREVIATIONS and key not in seen:
            seen.add(key)
            out.append(f"{token} ({CW_ABBREVIATIONS[key]})")
        else:
            out.append(token)
    return " ".join(out)


# ── Encoder ──────────────────────────────────────────────────────────────────

def encode_cw(text: str, wpm: int = 20, freq: float = 600.0,
              sample_rate: int = 8000, amplitude: float = 0.8) -> np.ndarray:
    """Render text as keyed-sine CW audio (float32, mono).

    PARIS timing: dit = 1200/wpm ms; dah = 3 dits; intra-element gap 1 dit;
    inter-character gap 3 dits; word gap 7 dits. Key edges get a 4 ms
    raised-cosine ramp so the transmitted signal has no key clicks."""
    unit_s = 1.2 / wpm
    n_unit = int(round(unit_s * sample_rate))
    ramp_n = min(int(0.004 * sample_rate), max(n_unit // 4, 1))

    def tone(units: int) -> np.ndarray:
        n = n_unit * units
        t = np.arange(n, dtype=np.float32) / sample_rate
        out = (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)
        env = 0.5 * (1 - np.cos(np.linspace(0, math.pi, ramp_n, dtype=np.float32)))
        out[:ramp_n] *= env
        out[-ramp_n:] *= env[::-1]
        return out

    def silence(units: int) -> np.ndarray:
        return np.zeros(n_unit * units, dtype=np.float32)

    parts: list[np.ndarray] = [silence(2)]   # brief lead-in
    words = text.upper().split()
    for wi, word in enumerate(words):
        if wi:
            parts.append(silence(7))
        # Allow <AR>-style prosigns inside a word
        chars: list[str] = []
        i = 0
        while i < len(word):
            if word[i] == "<":
                j = word.find(">", i)
                if j > i:
                    chars.append(word[i:j + 1])
                    i = j + 1
                    continue
            chars.append(word[i])
            i += 1
        for ci, ch in enumerate(chars):
            code = CHAR_TO_MORSE.get(ch)
            if code is None:
                continue
            if ci:
                parts.append(silence(3))
            for ei, el in enumerate(code):
                if ei:
                    parts.append(silence(1))
                parts.append(tone(3 if el == "-" else 1))
    parts.append(silence(2))
    return np.concatenate(parts)


# ── Decoder ──────────────────────────────────────────────────────────────────

def _goertzel_power(block: np.ndarray, sample_rate: int, freq: float) -> float:
    """Single-bin DFT power at freq — cheaper than an FFT per block."""
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
class Transmission:
    text: str
    wpm: float
    freq: float
    snr_db: float
    morse: str = ""


@dataclass
class CWDecoder:
    sample_rate: int = 8000
    freq: float = 600.0            # CW pitch; auto-tuned per transmission when auto_tune
    auto_tune: bool = True
    block_ms: float = 5.0    # 5 ms blocks: a 35 wpm dit is ~34 ms, and 10 ms
                             # quantization plus edge-block widening biased the
                             # WPM estimate ~20 % low at high speeds
    end_of_tx_s: float = 1.2       # key-up silence that ends a transmission

    _buf: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _raw_recent: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _noise: float = 1e-7
    _peak: float = 1e-6
    _warmup_left: int = 12      # ~60 ms of floor-learning before gating starts —
                                # must stay shorter than the shortest realistic
                                # quiet lead-in, or the median learns signal
                                # power and locks the gate shut (bit us at 35
                                # wpm, whose lead-in is only ~68 ms)
    _wu_vals: list = field(default_factory=list)
    _open_pending: int = 0
    _key_down: bool = False
    _run_blocks: int = 0           # length of current mark/space run, in blocks
    _events: list = field(default_factory=list)   # (is_mark, blocks) for current tx
    _in_tx: bool = False
    _tx_freq: float = 0.0
    _tx_snr_num: float = 0.0
    _tx_snr_den: int = 0

    def __post_init__(self):
        self._block_n = max(int(self.sample_rate * self.block_ms / 1000.0), 8)

    # -- public API ----------------------------------------------------------

    def process(self, samples: np.ndarray) -> list[Transmission]:
        """Feed raw float32 samples; returns completed transmissions (if any)."""
        out: list[Transmission] = []
        self._buf = np.concatenate([self._buf, samples.astype(np.float32)])
        # Keep ~1 s of raw audio for the auto-tune FFT
        self._raw_recent = np.concatenate([self._raw_recent, samples.astype(np.float32)])[-self.sample_rate:]

        while len(self._buf) >= self._block_n:
            block, self._buf = self._buf[:self._block_n], self._buf[self._block_n:]
            tx = self._process_block(block)
            if tx is not None:
                out.append(tx)
        return out

    def flush(self) -> "Transmission | None":
        """Force end-of-transmission (e.g. stream closing)."""
        return self._end_transmission() if self._in_tx else None

    # -- internals -----------------------------------------------------------

    def _process_block(self, block: np.ndarray) -> "Transmission | None":
        p = _goertzel_power(block, self.sample_rate, self._tx_freq or self.freq)

        # Cold-start warmup: learn the ambient floor before any gating. Without
        # this, the floor init is arbitrary and in real noise the very first
        # blocks either false-open the gate or deadlock it. MEDIAN, not mean:
        # if a transmission is already in progress during warmup, a few mark
        # blocks in the window would drag a mean far above the true floor and
        # lock the gate shut for seconds; they can't move the median.
        if self._warmup_left > 0:
            self._warmup_left -= 1
            self._wu_vals.append(p)
            if self._warmup_left == 0:
                sv = sorted(self._wu_vals)
                self._noise = max(sv[len(sv) // 2], 1e-12)
                self._wu_vals = []
            return None

        # ORDER MATTERS: evaluate the gate from the PRE-update stats, then let
        # the trackers learn from the decision. Updating the noise floor first
        # let the opening block of a transmission poison its own detection —
        # the floor absorbed 2 % of the mark's power, the contrast test then
        # failed by a hair, and the loop deadlocked with the floor climbing on
        # every subsequent mark block (observed: gate never opened until a
        # louder-relative moment several characters in).
        contrast_ok = (self._peak > self._noise * 25) or (p > self._noise * 80)
        thr = math.sqrt(self._noise * max(self._peak, p if contrast_ok else self._peak))
        if self._key_down:
            key = contrast_ok and p > thr * 0.6
        else:
            key = contrast_ok and p > thr * 1.6

        # GATED tracking: the noise floor learns only from key-UP blocks and
        # the peak only from key-DOWN blocks. Letting the floor absorb signal
        # power during a sustained mark makes it climb until the contrast test
        # fails mid-element (truncating every dah to dit length). The floor
        # decays gently (5 %) rather than snapping to minima so it tracks a
        # low percentile of the noise, not the absolute minimum — an absolute-
        # minimum floor makes every "×N above floor" test meaninglessly easy.
        if key:
            if p > self._peak:
                self._peak = 0.5 * self._peak + 0.5 * p
            else:
                self._peak += (p - self._peak) * 0.01
        else:
            if p < self._noise:
                self._noise += (p - self._noise) * 0.05
            else:
                self._noise += (p - self._noise) * 0.02
            # Peak leaks toward the floor during silence so a stale peak from a
            # long-gone strong signal can't hold the gate hostage forever.
            self._peak += (self._noise - self._peak) * 0.001
        self._noise = max(self._noise, 1e-12)

        if not self._in_tx:
            # Require two consecutive gate-open blocks to start a transmission,
            # so a single noise spike can't open the decoder and mistune it.
            if key:
                self._open_pending += 1
                if self._open_pending >= 2:
                    self._open_pending = 0
                    self._start_transmission()
                    self._run_blocks = 2   # credit both confirmation blocks
            else:
                self._open_pending = 0
            return None

        if key:
            self._tx_snr_num += p
            self._tx_snr_den += 1

        if key == self._key_down:
            self._run_blocks += 1
            # End of transmission: long key-up silence
            if (not key) and self._run_blocks * self._block_ms_actual() >= self.end_of_tx_s * 1000:
                return self._end_transmission()
            return None

        # Edge: close out the previous run
        if self._run_blocks > 0:
            self._events.append((self._key_down, self._run_blocks))
        self._key_down = key
        self._run_blocks = 1
        return None

    def _block_ms_actual(self) -> float:
        return self._block_n * 1000.0 / self.sample_rate

    def _start_transmission(self) -> None:
        self._in_tx = True
        self._events = []
        self._key_down = True
        self._run_blocks = 1
        self._tx_snr_num = 0.0
        self._tx_snr_den = 0
        self._tx_freq = self._detect_freq() if self.auto_tune else self.freq

    def _detect_freq(self) -> float:
        """FFT peak over the recent raw window, constrained to CW pitch range."""
        w = self._raw_recent
        if len(w) < 512:
            return self.freq
        spec = np.abs(np.fft.rfft(w * np.hanning(len(w))))
        freqs = np.fft.rfftfreq(len(w), 1.0 / self.sample_rate)
        lo, hi = np.searchsorted(freqs, 300.0), np.searchsorted(freqs, 1200.0)
        if hi <= lo:
            return self.freq
        band = spec[lo:hi]
        peak_i = int(np.argmax(band))
        # Only retune when the peak clearly dominates the band — otherwise the
        # "peak" is just the tallest blade of grass in flat noise, and tuning
        # to it detunes the Goertzel away from the real (configured) pitch.
        med = float(np.median(band)) or 1e-12
        if band[peak_i] < 5.0 * med:
            return self.freq
        return float(freqs[lo + peak_i])

    def _end_transmission(self) -> "Transmission | None":
        events = self._events
        if self._key_down and self._run_blocks:
            events = events + [(True, self._run_blocks)]
        self._in_tx = False
        self._key_down = False
        self._run_blocks = 0
        self._events = []

        marks = [b for is_mark, b in events if is_mark]
        if not marks:
            return None

        # Debounce: drop 1-block glitch marks if real marks are much longer
        if len(marks) > 2 and max(marks) >= 4:
            events = [(m, b) for m, b in events if not (m and b == 1)]
            marks = [b for is_mark, b in events if is_mark]
            if not marks:
                return None

        # Cluster dits vs dahs from the whole population: if the spread is
        # < 2x everything is one kind (decide by comparing against the space
        # population); otherwise split at the geometric midpoint.
        mn, mx = min(marks), max(marks)
        if mx / mn >= 2.0:
            split = math.sqrt(mn * mx)
            dits = [m for m in marks if m < split]
            unit = (sum(dits) / len(dits)) if dits else mn
        else:
            # All marks same length. Compare with intra-character spaces if
            # any: spaces ≈ marks → they're dits; spaces ≈ marks/3 → dahs.
            spaces = [b for is_mark, b in events if not is_mark]
            if spaces and (sum(marks) / len(marks)) / (sum(spaces) / len(spaces)) >= 2.0:
                unit = (sum(marks) / len(marks)) / 3.0
            else:
                unit = sum(marks) / len(marks)
            split = unit * 2.0

        # Walk events → morse symbols
        morse: list[str] = []
        sym = ""
        for is_mark, blocks in events:
            if is_mark:
                sym += "-" if blocks >= split else "."
            else:
                if blocks < unit * 2.0:
                    continue                        # intra-element gap
                if sym:
                    morse.append(sym)
                    sym = ""
                if blocks >= unit * 5.0:
                    morse.append(" ")               # word gap
        if sym:
            morse.append(sym)

        text_parts: list[str] = []
        for m in morse:
            if m == " ":
                text_parts.append(" ")
            else:
                text_parts.append(MORSE_TO_CHAR.get(m, "▚"))
        text = "".join(text_parts).strip()
        if not text:
            return None

        unit_ms = unit * self._block_ms_actual()
        wpm = 1200.0 / unit_ms if unit_ms > 0 else 0.0
        snr_db = 0.0
        if self._tx_snr_den and self._noise > 0:
            snr_db = 10 * math.log10((self._tx_snr_num / self._tx_snr_den) / self._noise)
        return Transmission(
            text=text,
            wpm=round(wpm, 1),
            freq=round(self._tx_freq or self.freq, 1),
            snr_db=round(snr_db, 1),
            morse=" ".join(m for m in morse if m != " "),
        )
