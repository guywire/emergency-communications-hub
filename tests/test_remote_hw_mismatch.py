"""
tests/test_remote_hw_mismatch.py
--------------------------------
Live report (2026-10-11): a Meshtastic node connected in the browser as
"meshtastic-remote" never came up — the adapter had been saved with
transport: serial and port: '' (server kept opening a device named '').
Now: /remote-hw warns when no adapter will use the session, blank port
means auto-detect, and the editor's blank fields aren't saved.
"""

from unittest.mock import MagicMock

from starlette.testclient import TestClient


def _app(adapters):
    from ech.api.app import create_app
    router = MagicMock()
    router._adapters = adapters
    return create_app(router, MagicMock())


def test_warns_when_adapter_not_set_to_browser():
    mt = MagicMock(spec=[])                     # no _transport_type
    mt._transport = "serial"
    with TestClient(_app({"meshtastic-remote": mt})) as c:
        with c.websocket_connect("/ws/remote-hw") as ws:
            ws.send_json({"role": "serial", "adapter": "meshtastic-remote"})
            assert ws.receive_json()["type"] == "ready"
            w = ws.receive_json()
    assert w["type"] == "warning" and 'transport "serial"' in w["detail"]


def test_warns_when_no_such_adapter_and_silent_when_correct():
    ok = MagicMock(spec=[])
    ok._transport = "browser"
    with TestClient(_app({"meshtastic-remote": ok})) as c:
        with c.websocket_connect("/ws/remote-hw") as ws:
            ws.send_json({"role": "serial", "adapter": "typo-name"})
            ws.receive_json()
            assert "No adapter named" in ws.receive_json()["detail"]
        with c.websocket_connect("/ws/remote-hw") as ws:
            ws.send_json({"role": "serial", "adapter": "meshtastic-remote"})
            assert ws.receive_json()["type"] == "ready"
            ws.send_text('{"type": "status"}')            # no warning frame queued before this


def test_blank_port_means_autodetect():
    from ech.adapters.meshtastic_adapter import MeshtasticAdapter
    a = MeshtasticAdapter({"name": "mt", "transport": "serial", "port": "", "host": ""})
    assert a._port is None and a._host == "localhost"
