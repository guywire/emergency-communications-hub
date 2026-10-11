"""
ech/adapters/rtty_audio.py + psk31_audio counterpart
----------------------------------------------------
RTTY and PSK31 over a sound card. Both subclass CWAudioAdapter, reusing all
its sound-card plumbing (device resolution, PortAudio-thread→asyncio handoff,
health detail, playback) and swapping only the DSP core and message framing.

Config (adapters:):
    - type: rtty_audio
      name: rtty
      input_device: "USB Audio"
      output_device: null
      freq: 2125            # MARK frequency; space = freq + shift
      shift: 170
      baud: 45.45
      reverse: false        # true = mark is the upper tone
      auto_tune: true       # AFC: find the tone pair anywhere in the passband
      sensitivity: 3        # 1 strict .. 5 weak-signal (see CWAudioAdapter)
    - type: psk31_audio
      name: psk31
      input_device: "USB Audio"
      freq: 1000            # carrier (starting point when auto_tune is on)
      auto_tune: true       # AFC: find + lock the carrier anywhere in the passband
      sensitivity: 3

Both also accept input_device: browser + browser_session (see cw_audio.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np

from ech.adapters.cw_audio import CWAudioAdapter
from ech.core.models import NormalizedMessage, Priority
from ech.core.rtty import RTTYDecoder, encode_rtty
from ech.core.psk31 import PSK31Decoder, encode_psk31

log = logging.getLogger(__name__)


class RTTYAudioAdapter(CWAudioAdapter):
    MODE = "RTTY"

    def __init__(self, config: dict):
        self._rtty_shift = float(config.get("shift", 170.0))
        self._rtty_baud = float(config.get("baud", 45.45))
        self._rtty_reverse = bool(config.get("reverse", False))
        config.setdefault("freq", 2125.0)   # mark frequency
        super().__init__(config)

    def _make_decoder(self):
        return RTTYDecoder(sample_rate=self._sample_rate, mark=self._freq,
                           shift=self._rtty_shift, baud=self._rtty_baud,
                           afc=self._auto_tune, reverse=self._rtty_reverse,
                           squelch=self._squelch())

    def modem_settings(self) -> dict:
        s = super().modem_settings()
        s.pop("tx_wpm", None)
        s.pop("max_wpm", None)
        s.update(shift=self._rtty_shift, baud=self._rtty_baud, reverse=self._rtty_reverse,
                 tracking_freq=round(self._decoder.mark, 1))
        return s

    def _apply_mode_tuning(self, p: dict) -> None:
        if "shift" in p:
            self._rtty_shift = float(min(max(float(p["shift"]), 50.0), 1000.0))
        if "baud" in p:
            self._rtty_baud = float(min(max(float(p["baud"]), 20.0), 300.0))
        if "reverse" in p:
            self._rtty_reverse = bool(p["reverse"])

    TUNING_CONFIG_KEYS = {"freq": "freq", "afc": "auto_tune", "sensitivity": "sensitivity",
                          "shift": "shift", "baud": "baud", "reverse": "reverse"}

    def _encode_tx(self, text: str) -> np.ndarray:
        return encode_rtty(text, baud=self._rtty_baud, mark=self._freq,
                           shift=self._rtty_shift, sample_rate=self._sample_rate,
                           amplitude=self._tx_amplitude)

    async def _emit_transmission(self, tx) -> None:
        self._last_decode = {"text": tx.text, "baud": tx.baud, "freq": tx.freq,
                             "snr_db": tx.snr_db,
                             "ts": datetime.now(timezone.utc).isoformat()}
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"rtty {tx.freq:.0f}Hz",
            from_id="rtty-audio",
            from_display=f"RTTY {tx.baud:g}Bd",
            body=tx.text,
            priority=Priority.NORMAL,
            raw={"mode": "RTTY", "baud": tx.baud, "freq_hz": tx.freq,
                 "snr_db": tx.snr_db},
        )
        self._last_rx = datetime.now(timezone.utc)
        await self._enqueue(msg)
        log.info("RTTYAudio %s: decoded %gBd @ %.0f Hz (SNR %.0f dB): %s",
                 self.name, tx.baud, tx.freq, tx.snr_db, tx.text[:70])


class PSK31AudioAdapter(CWAudioAdapter):
    MODE = "PSK31"

    def __init__(self, config: dict):
        config.setdefault("freq", 1000.0)   # carrier
        super().__init__(config)

    def _make_decoder(self):
        return PSK31Decoder(sample_rate=self._sample_rate, freq=self._freq,
                            afc=self._auto_tune, squelch=self._squelch())

    def modem_settings(self) -> dict:
        s = super().modem_settings()
        s.pop("tx_wpm", None)
        s.pop("max_wpm", None)
        s["tracking_freq"] = round(self._decoder.freq, 1)
        return s

    def _apply_mode_tuning(self, p: dict) -> None:
        pass

    TUNING_CONFIG_KEYS = {"freq": "freq", "afc": "auto_tune", "sensitivity": "sensitivity"}

    def _encode_tx(self, text: str) -> np.ndarray:
        return encode_psk31(text, freq=self._freq, sample_rate=self._sample_rate,
                            amplitude=self._tx_amplitude)

    async def _emit_transmission(self, tx) -> None:
        self._last_decode = {"text": tx.text, "freq": tx.freq, "snr_db": tx.snr_db,
                             "ts": datetime.now(timezone.utc).isoformat()}
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"psk31 {tx.freq:.0f}Hz",
            from_id="psk31-audio",
            from_display="PSK31",
            body=tx.text,
            priority=Priority.NORMAL,
            raw={"mode": "PSK31", "freq_hz": tx.freq, "snr_db": tx.snr_db},
        )
        self._last_rx = datetime.now(timezone.utc)
        await self._enqueue(msg)
        log.info("PSK31Audio %s: decoded @ %.0f Hz (SNR %.0f dB): %s",
                 self.name, tx.freq, tx.snr_db, tx.text[:70])
