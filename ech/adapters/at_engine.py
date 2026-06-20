"""
ech/adapters/at_engine.py
--------------------------
Async AT command engine for Hayes-compatible GSM/LTE modems.
Used by sms.py — extracted as a separate module so it can be reused
for future adapters that need raw modem access (e.g. voice calls, USSD).

Protocol basics:
  - Commands are sent as  "AT+CMD\r"
  - Responses are lines terminated by \r\n
  - Final response line is "OK" or "ERROR" or "+CME ERROR: n"
  - Unsolicited Result Codes (URCs) arrive at any time between commands
    e.g.  +CMTI: "SM",3     (new SMS stored at index 3)
          +CMT: ...         (SMS delivered directly if CNMI mode 2)
          RING              (incoming call)

Threading model:
  A background reader task continuously consumes bytes from the serial
  port and dispatches lines as either:
    (a) a response to the current pending command  → put on _resp_queue
    (b) an unsolicited result code                 → call _urc_handler

Locking:
  Only one AT command may be in-flight at a time.  _cmd_lock is an
  asyncio.Lock that callers must hold for the duration of send+wait.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

log = logging.getLogger(__name__)

# Lines that terminate an AT response
FINAL_OK    = {"OK", "CONNECT", "SEND OK"}
FINAL_ERROR = {"ERROR", "NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"}


def _is_final(line: str) -> bool:
    s = line.strip().upper()
    if s in FINAL_OK or s in FINAL_ERROR:
        return True
    if s.startswith("+CME ERROR") or s.startswith("+CMS ERROR"):
        return True
    return False


class ATEngine:
    """
    Async AT command engine.

    Usage:
        engine = ATEngine("/dev/ttyUSB0", 115200)
        await engine.connect()
        lines = await engine.cmd("AT+CMGF=1")   # ["OK"]
        await engine.disconnect()
    """

    def __init__(self, port: str, baud: int = 115200, urc_handler: Callable | None = None):
        self._port    = port
        self._baud    = baud
        self._urc_cb  = urc_handler   # called with each URC line (or multi-line URC block)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._cmd_lock  = asyncio.Lock()
        self._resp_queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending_lines: list[str] = []   # accumulate current response
        self._urc_buf: list[str] = []         # accumulate multi-line URCs
        self._connected = False

    async def connect(self) -> None:
        try:
            import serial_asyncio
        except ImportError as exc:
            raise ImportError(
                "SMS modem requires serial_asyncio: pip install pyserial-asyncio"
            ) from exc
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baud
        )
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop(), name="at-reader")
        log.debug("ATEngine: opened %s @ %d", self._port, self._baud)

    async def disconnect(self) -> None:
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()

    async def cmd(self, command: str, timeout: float = 5.0) -> list[str]:
        """
        Send an AT command and return all response lines (including final OK/ERROR).
        Raises TimeoutError if no final response within `timeout` seconds.
        """
        async with self._cmd_lock:
            # Drain any stale data from the queue
            while not self._resp_queue.empty():
                self._resp_queue.get_nowait()
            self._pending_lines = []

            raw = (command.rstrip("\r") + "\r").encode()
            self._writer.write(raw)
            await self._writer.drain()
            log.debug("AT>>> %s", command.strip())

            lines: list[str] = []
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"AT timeout waiting for response to: {command!r}")
                try:
                    line = await asyncio.wait_for(self._resp_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"AT timeout: {command!r}")
                if line:
                    lines.append(line)
                if _is_final(line):
                    break
            log.debug("AT<<< %s", lines)
            return lines

    async def cmd_prompt(self, command: str, data: bytes, timeout: float = 10.0) -> list[str]:
        """
        Send a command that expects a '>' prompt, then send data + Ctrl-Z (0x1A).
        Used for AT+CMGS (SMS send).
        """
        async with self._cmd_lock:
            while not self._resp_queue.empty():
                self._resp_queue.get_nowait()

            self._writer.write((command.rstrip("\r") + "\r").encode())
            await self._writer.drain()
            log.debug("AT>>> %s", command.strip())

            # Wait for '>' prompt
            deadline = asyncio.get_event_loop().time() + timeout
            prompt_seen = False
            while not prompt_seen:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"AT prompt timeout: {command!r}")
                try:
                    line = await asyncio.wait_for(self._resp_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"AT prompt timeout: {command!r}")
                if ">" in line:
                    prompt_seen = True

            # Send data + Ctrl-Z
            self._writer.write(data + b"\x1a")
            await self._writer.drain()

            # Read until final response
            lines = []
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"AT send timeout: {command!r}")
                try:
                    line = await asyncio.wait_for(self._resp_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"AT send timeout: {command!r}")
                if line:
                    lines.append(line)
                if _is_final(line):
                    break
            log.debug("AT<<< %s", lines)
            return lines

    # ── Background reader ─────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        buf = b""
        try:
            while self._connected:
                chunk = await asyncio.wait_for(self._reader.read(256), timeout=1.0)
                if not chunk:
                    raise ConnectionError("ATEngine: serial port closed")
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip("\r ")
                    if not line:
                        continue
                    log.debug("AT raw: %r", line)
                    await self._dispatch_line(line)
        except asyncio.TimeoutError:
            pass   # Normal idle — loop back
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self._connected:
                log.error("ATEngine reader error: %s", exc)

    async def _dispatch_line(self, line: str) -> None:
        """Route a line to either the pending command response or URC handler."""
        # While a command is in-flight (lock held), everything goes to response queue.
        # When no command is pending, non-final lines that look like URCs go to URC handler.
        if self._cmd_lock.locked():
            await self._resp_queue.put(line)
        else:
            # Unsolicited — dispatch to callback
            if self._urc_cb:
                try:
                    result = self._urc_cb(line)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    log.debug("ATEngine URC handler error: %s", exc)
