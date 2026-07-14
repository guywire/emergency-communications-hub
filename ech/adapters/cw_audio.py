"""
ech/adapters/cw_audio.py
------------------------
CW (Morse code) over a sound card — the first audio-path adapter.

RX: captures audio from a sound-card input (built-in, USB codec, or a rig's
    USB audio like an IC-7300) and runs it through ech.core.cw.CWDecoder;
    each completed transmission becomes a NormalizedMessage.
TX: send() renders the message body as keyed-sine CW audio (ech.core.cw
    .encode_cw) and plays it out the configured output device. PTT defaults to
    the radio's own VOX; set `ptt: cat` to have ECH key/unkey the rig itself via
    the server's rigctld CATController (wired in by main.py at startup) for the
    duration of each transmission — no VOX needed. If `ptt: cat` is set but CAT
    isn't connected, TX still plays audio but logs a warning every send (silent
    dead air is worse than a noisy log).

Config (adapters: - type: cw_audio):
    name:           cw-audio
    input_device:   null        # null = system default; int index; or name substring
    output_device:  null        # same semantics; TX disabled if no output resolves
    freq: 600                   # CW pitch Hz (decoder auto-tunes 300-1200 Hz around it)
    auto_tune: true
    wpm: 20                     # TX keying speed
    sample_rate: 8000
    tx_amplitude: 0.8
    ptt: vox                    # "vox" (default, radio keys itself) or "cat" (ECH keys via rigctld)

The audio-device dependency (sounddevice/PortAudio) is imported lazily in
connect(), matching the ADAPT-1 convention — the adapter can be configured on
a box without PortAudio and fails cleanly at connect time instead of import.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import numpy as np

from ech.adapters.base import Adapter
from ech.core.models import NormalizedMessage, Priority
from ech.core.cw import CWDecoder, encode_cw, annotate_cw

log = logging.getLogger(__name__)


def list_audio_devices() -> dict:
    """Enumerate sound cards (for /api/audio/devices and the settings UI)."""
    try:
        import sounddevice as sd
    except Exception as exc:
        return {"available": False, "detail": f"sounddevice not installed: {exc}", "devices": []}
    try:
        devs = []
        default_in, default_out = sd.default.device
        for i, d in enumerate(sd.query_devices()):
            devs.append({
                "index": i,
                "name": d["name"],
                "inputs": d["max_input_channels"],
                "outputs": d["max_output_channels"],
                "default_input": i == default_in,
                "default_output": i == default_out,
                "sample_rate": d["default_samplerate"],
            })
        return {"available": True, "devices": devs}
    except Exception as exc:
        return {"available": False, "detail": str(exc), "devices": []}


class CWAudioAdapter(Adapter):
    def __init__(self, config: dict):
        super().__init__(config)
        self._input_device = config.get("input_device")
        self._output_device = config.get("output_device")
        self._freq = float(config.get("freq", 600.0))
        self._auto_tune = bool(config.get("auto_tune", True))
        self._wpm = int(config.get("wpm", 20))
        self._sample_rate = int(config.get("sample_rate", 8000))
        self._tx_amplitude = float(config.get("tx_amplitude", 0.8))
        self._decoder = self._make_decoder()
        self._sample_q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._stream = None
        self._rx_task: asyncio.Task | None = None
        self._sd = None
        self._input_name = ""
        self._output_name = ""
        self._last_decode: dict | None = None
        self._dropped_blocks = 0
        self._hw_sess = None          # browser-hosted audio session (remote_hw)
        self._pump_task: asyncio.Task | None = None
        self._ptt_via_cat = str(config.get("ptt", "vox")).lower() == "cat"
        self._cat_ctrl = None          # wired in by main.py if ptt: cat and CAT is configured

    # ── mode hooks (overridden by RTTY/PSK31 subclasses, which reuse all the
    #    sound-card plumbing here and swap only the DSP) ─────────────────────

    MODE = "CW"

    def _make_decoder(self):
        return CWDecoder(sample_rate=self._sample_rate, freq=self._freq,
                         auto_tune=self._auto_tune)

    def _encode_tx(self, text: str) -> np.ndarray:
        return encode_cw(text, wpm=self._wpm, freq=self._freq,
                         sample_rate=self._sample_rate, amplitude=self._tx_amplitude)

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        # Browser-hosted audio: the operator's computer has the radio audio
        # (mic/line via Web Audio); the /remote-hw page streams float32 PCM
        # @8 kHz over /ws/remote-hw. No sounddevice/PortAudio needed at all —
        # the DSP consumes the same sample stream either way.
        if str(self._input_device).lower() == "browser":
            from ech.core.remote_hw import registry
            self._hw_sess = await registry.wait_for(self.name, timeout=10.0)
            if self._hw_sess is None:
                raise ConnectionError(
                    f"no browser audio session for {self.name!r} — open /remote-hw "
                    "and connect the radio audio")
            self._input_name = "browser (remote)"
            self._output_name = "browser (remote)"
            self._rx_task = asyncio.ensure_future(self._run())
            self._pump_task = asyncio.ensure_future(self._browser_pump())
            self._connected = True
            log.info("%sAudio %s: attached to remote browser audio", self.MODE, self.name)
            return

        import sounddevice as sd   # lazy: PortAudio may not be installed
        self._sd = sd
        loop = asyncio.get_running_loop()

        in_dev = self._resolve_device(self._input_device, want_input=True)
        self._input_name = self._device_name(in_dev, want_input=True)
        out_dev = self._resolve_device(self._output_device, want_input=False)
        self._output_name = self._device_name(out_dev, want_input=False)

        def _callback(indata, frames, time_info, status):
            # PortAudio thread → hand samples to the event loop. Drop (and
            # count) when the queue is full rather than blocking the audio
            # thread — a stalled callback breaks the whole capture stream.
            samples = indata[:, 0].copy()
            try:
                loop.call_soon_threadsafe(self._offer_samples, samples)
            except RuntimeError:
                pass   # loop shutting down

        self._stream = sd.InputStream(
            device=in_dev, channels=1, samplerate=self._sample_rate,
            dtype="float32", blocksize=int(self._sample_rate * 0.05),
            callback=_callback,
        )
        self._stream.start()
        self._rx_task = asyncio.ensure_future(self._run())
        self._connected = True
        log.info("CWAudio %s: listening on %r @ %d Hz (pitch %.0f Hz, auto_tune=%s); "
                 "TX device %r wpm=%d",
                 self.name, self._input_name, self._sample_rate, self._freq,
                 self._auto_tune, self._output_name or "none", self._wpm)

    def _offer_samples(self, samples: np.ndarray) -> None:
        try:
            self._sample_q.put_nowait(samples)
        except asyncio.QueueFull:
            self._dropped_blocks += 1

    async def _browser_pump(self) -> None:
        """Feed browser PCM chunks (float32 LE @ sample_rate) into the decoder queue."""
        try:
            while True:
                chunk = await self._hw_sess.read()
                if chunk:
                    self._offer_samples(np.frombuffer(chunk, dtype=np.float32))
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.error("%sAudio %s: browser pump error: %s", self.MODE, self.name, exc)

    async def _run(self) -> None:
        """Worker loop: drain captured sample blocks into the CW decoder."""
        try:
            while True:
                samples = await self._sample_q.get()
                for tx in self._decoder.process(samples):
                    await self._emit_transmission(tx)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("CWAudio %s: rx loop error: %s", self.name, exc)

    async def _emit_transmission(self, tx) -> None:
        self._last_decode = {"text": tx.text, "wpm": tx.wpm, "freq": tx.freq,
                             "snr_db": tx.snr_db,
                             "ts": datetime.now(timezone.utc).isoformat()}
        msg = NormalizedMessage(
            source_adapter=self.name,
            source_channel=f"cw {tx.freq:.0f}Hz",
            from_id="cw-audio",
            from_display=f"CW {tx.wpm:.0f}wpm",
            # Abbreviations expanded in parens ("73 (best regards)") so
            # non-CW operators can read the traffic; verbatim copy in raw.
            body=annotate_cw(tx.text),
            priority=Priority.NORMAL,
            raw={"verbatim": tx.text, "morse": tx.morse, "wpm": tx.wpm,
                 "freq_hz": tx.freq, "snr_db": tx.snr_db},
        )
        self._last_rx = datetime.now(timezone.utc)
        await self._enqueue(msg)
        log.info("CWAudio %s: decoded %.0f wpm @ %.0f Hz (SNR %.0f dB): %s",
                 self.name, tx.wpm, tx.freq, tx.snr_db, tx.text[:70])

    async def disconnect(self) -> None:
        self._connected = False
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        self._hw_sess = None
        if self._rx_task:
            self._rx_task.cancel()
            try:
                await self._rx_task
            except asyncio.CancelledError:
                pass
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        log.info("CWAudio %s: disconnected", self.name)

    # ── TX ────────────────────────────────────────────────────────────────

    async def _key_ptt(self, on: bool) -> None:
        """Key/unkey the rig via CAT for the duration of a TX, if ptt: cat is set."""
        if not self._ptt_via_cat:
            return
        if self._cat_ctrl is None or not getattr(self._cat_ctrl, "_connected", False):
            if on:
                log.warning("%sAudio %s: ptt=cat but CAT is not connected — "
                            "transmitting WITHOUT keying the rig (dead air unless VOX is also on)",
                            self.MODE, self.name)
            return
        ok = await self._cat_ctrl.set_ptt(on)
        if not ok:
            log.warning("%sAudio %s: CAT PTT %s failed", self.MODE, self.name, "on" if on else "off")

    async def send(self, message: NormalizedMessage) -> bool:
        # Browser-hosted: ship the rendered audio to the browser, which plays
        # it out the operator's sound card into the radio (VOX/CAT PTT still
        # applies, just on the remote end).
        if self._hw_sess is not None:
            audio = self._encode_tx(message.body)
            await self._key_ptt(True)
            try:
                await self._hw_sess.write(audio.astype(np.float32).tobytes())
                self._last_tx = datetime.now(timezone.utc)
                log.info("%sAudio %s: sent %.1fs of TX audio to browser",
                         self.MODE, self.name, len(audio) / self._sample_rate)
                return True
            except ConnectionError:
                log.warning("%sAudio %s: browser session gone — TX failed", self.MODE, self.name)
                return False
            finally:
                await self._key_ptt(False)
        if self._sd is None:
            return False
        out_dev = self._resolve_device(self._output_device, want_input=False)
        if out_dev is None and not self._output_name:
            log.warning("CWAudio %s: no output device — cannot key CW", self.name)
            return False
        audio = self._encode_tx(message.body)
        dur = len(audio) / self._sample_rate
        log.info("%sAudio %s: keying %d chars (%.1f s of audio)",
                 self.MODE, self.name, len(message.body), dur)

        def _play():
            self._sd.play(audio, samplerate=self._sample_rate, device=out_dev, blocking=True)

        await self._key_ptt(True)
        try:
            await asyncio.get_running_loop().run_in_executor(None, _play)
            self._last_tx = datetime.now(timezone.utc)
            return True
        except Exception as exc:
            log.error("CWAudio %s: playback failed: %s", self.name, exc)
            return False
        finally:
            await self._key_ptt(False)

    # ── helpers ───────────────────────────────────────────────────────────

    def _resolve_device(self, spec, want_input: bool):
        """None → default; int → index; str → case-insensitive name substring."""
        if spec is None or spec == "" or self._sd is None:
            return None
        if isinstance(spec, int):
            return spec
        want = str(spec).lower()
        for i, d in enumerate(self._sd.query_devices()):
            ch = d["max_input_channels"] if want_input else d["max_output_channels"]
            if ch > 0 and want in d["name"].lower():
                return i
        log.warning("CWAudio %s: no %s device matching %r — using system default",
                    self.name, "input" if want_input else "output", spec)
        return None

    def _device_name(self, dev, want_input: bool) -> str:
        if self._sd is None:
            return ""
        try:
            info = self._sd.query_devices(dev, "input" if want_input else "output")
            return info["name"]
        except Exception:
            return str(dev) if dev is not None else ""

    def _health_detail(self) -> dict:
        return {
            "input_device": self._input_name,
            "output_device": self._output_name or "none (RX only)",
            "pitch_hz": self._freq,
            "auto_tune": self._auto_tune,
            "tx_wpm": self._wpm,
            "dropped_blocks": self._dropped_blocks,
            "last_decode": self._last_decode,
            "ptt": "cat" if self._ptt_via_cat else "vox",
            "ptt_cat_connected": bool(self._cat_ctrl and getattr(self._cat_ctrl, "_connected", False))
                                 if self._ptt_via_cat else None,
        }
