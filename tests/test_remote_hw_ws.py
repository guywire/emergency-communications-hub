"""
tests/test_remote_hw_ws.py
--------------------------
Regression: /ws/remote-hw crashed with NameError (asyncio not imported in
ech/api/app.py) right after the hello, so the /remote-hw page reported
"Server closed the bridge" instantly and the session was never unregistered.
"""

import struct
from unittest.mock import MagicMock

from starlette.testclient import TestClient


def test_remote_hw_audio_bridge_stays_open_and_feeds_samples():
    from ech.api.app import create_app
    from ech.core.remote_hw import registry

    app = create_app(MagicMock(), MagicMock())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/remote-hw") as ws:
            ws.send_json({"role": "audio", "adapter": "cw-test"})
            assert ws.receive_json() == {"type": "ready", "adapter": "cw-test", "role": "audio"}
            sess = registry.get("cw-test")
            assert sess is not None
            ws.send_bytes(struct.pack("<4f", 0.0, 0.5, -0.5, 0.0))
            ws.send_text('{"type": "status"}')   # round-trip so the bytes frame is processed
        # Clean disconnect unregisters the session
    assert registry.get("cw-test") is None
    assert sess.bytes_in == 16
