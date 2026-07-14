"""
ech/core/remote_hw.py
---------------------
Registry for BROWSER-HOSTED hardware sessions.

ECH adapters normally talk to hardware plugged into the server. This registry
lets the hardware live on the operator's computer instead: a browser page
opens the device with Web Serial (a MeshCore node on COM5) or Web Audio (a
radio's audio via the mic jack) and pumps raw bytes/samples over a WebSocket
to `/ws/remote-hw`. Server-side adapters configured with `transport: browser`
(MeshCore) or `input_device: browser` (CW/RTTY/PSK31) read and write through
this registry instead of a local device — the entire protocol/DSP stack above
the transport is unchanged.

Wire protocol on /ws/remote-hw (after the auth'd handshake):
  first message (JSON text):  {"role": "serial"|"audio", "adapter": "<name>"}
  then: binary frames both directions —
    serial: raw device bytes
    audio:  float32 little-endian PCM mono @ 8 kHz (browser resamples)
  optional JSON text frames: {"type": "status", ...} (logged only)

One live session per adapter name; a new connection replaces a dead one but
is refused while a healthy session exists.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class RemoteHWSession:
    def __init__(self, role: str, adapter: str):
        self.role = role
        self.adapter = adapter
        self.connected_at = time.time()
        self.rx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self.alive = True
        self.bytes_in = 0
        self.bytes_out = 0

    # Adapter side
    async def read(self, timeout: float | None = None) -> bytes:
        """Next chunk from the browser. Raises ConnectionError once dead."""
        if not self.alive and self.rx_queue.empty():
            raise ConnectionError("remote hardware session closed")
        if timeout is None:
            data = await self.rx_queue.get()
        else:
            data = await asyncio.wait_for(self.rx_queue.get(), timeout=timeout)
        if data == b"" and not self.alive:
            raise ConnectionError("remote hardware session closed")
        return data

    async def write(self, data: bytes) -> None:
        if not self.alive:
            raise ConnectionError("remote hardware session closed")
        self.bytes_out += len(data)
        await self._tx_queue.put(data)

    # WebSocket side
    def feed(self, data: bytes) -> None:
        self.bytes_in += len(data)
        try:
            self.rx_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass   # drop rather than stall the WS receive loop

    async def next_tx(self) -> bytes:
        return await self._tx_queue.get()

    def close(self) -> None:
        self.alive = False
        # Wake any blocked reader so it can raise ConnectionError
        try:
            self.rx_queue.put_nowait(b"")
        except asyncio.QueueFull:
            pass


class RemoteHWRegistry:
    def __init__(self):
        self._sessions: dict[str, RemoteHWSession] = {}

    def register(self, role: str, adapter: str) -> RemoteHWSession:
        old = self._sessions.get(adapter)
        if old is not None and old.alive:
            raise ValueError(f"adapter {adapter!r} already has a live remote session")
        sess = RemoteHWSession(role, adapter)
        self._sessions[adapter] = sess
        log.info("RemoteHW: %s session registered for adapter %r", role, adapter)
        return sess

    def unregister(self, sess: RemoteHWSession) -> None:
        sess.close()
        if self._sessions.get(sess.adapter) is sess:
            del self._sessions[sess.adapter]
        log.info("RemoteHW: %s session for %r closed (in=%dB out=%dB)",
                 sess.role, sess.adapter, sess.bytes_in, sess.bytes_out)

    def get(self, adapter: str) -> RemoteHWSession | None:
        s = self._sessions.get(adapter)
        return s if (s is not None and s.alive) else None

    async def wait_for(self, adapter: str, timeout: float = 0.0) -> RemoteHWSession | None:
        """Poll for a live session (adapter connect() path). timeout 0 = one check."""
        deadline = time.monotonic() + timeout
        while True:
            s = self.get(adapter)
            if s is not None:
                return s
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.5)

    def status(self) -> list[dict]:
        return [{"adapter": s.adapter, "role": s.role, "alive": s.alive,
                 "connected_at": s.connected_at,
                 "bytes_in": s.bytes_in, "bytes_out": s.bytes_out}
                for s in self._sessions.values()]


# Process-wide singleton — adapters and the API import this
registry = RemoteHWRegistry()
