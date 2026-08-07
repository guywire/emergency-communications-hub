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
    - type: psk31_audio
      name: psk31
      input_device: "USB Audio"
      freq: 1000            # carrier
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
        config.setdefault("freq", 2125.0)   # mark frequency
        super().__init__(config)

    def _make_decoder(self):
        return RTTYDecoder(sample_rate=self._sample_rate, mark=self._freq,
                           shift=self._rtty_shift, baud=self._rtty_baud)

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
        return PSK31Decoder(sample_rate=self._sample_rate, freq=self._freq)

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
