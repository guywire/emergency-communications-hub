"""
tests/test_meshtastic_channel_hint.py
---------------------------------------
Covers O87 (mesh bot replies land on the origin channel, not the adapter's
tuned default) and the follow-up compose-bar channel picker: send() must
honor an explicit raw["channel_idx"] hint over self._channel_idx.
"""

import asyncio

import pytest

from ech.adapters.meshtastic_adapter import MeshtasticAdapter
from ech.core.models import NormalizedMessage


class _FakeIface:
    def __init__(self):
        self.calls = []

    def sendText(self, text, destinationId=None, channelIndex=None, wantAck=False):
        self.calls.append({"text": text, "destinationId": destinationId,
                            "channelIndex": channelIndex, "wantAck": wantAck})
        return None


def _ready_adapter(channel_idx=0):
    a = MeshtasticAdapter({"transport": "tcp", "host": "10.0.0.50", "channel_idx": channel_idx})
    a._iface = _FakeIface()
    a._connected = True
    a._loop = asyncio.get_event_loop()
    return a


@pytest.mark.asyncio
async def test_send_uses_adapter_default_channel_without_hint():
    a = _ready_adapter(channel_idx=2)
    msg = NormalizedMessage(source_adapter="meshtastic-usb", source_channel="ch2", from_id="local", body="hi")
    ok = await a.send(msg)
    assert ok is True
    assert a._iface.calls[0]["channelIndex"] == 2


@pytest.mark.asyncio
async def test_send_honors_explicit_channel_idx_hint():
    """The compose-bar channel picker (and mesh-bot replies) pass
    raw={'channel_idx': N} to target a specific channel for one message
    without changing the adapter's persistently-tuned default."""
    a = _ready_adapter(channel_idx=0)
    msg = NormalizedMessage(
        source_adapter="meshtastic-usb", source_channel="outbound", from_id="local", body="hi",
        raw={"channel_idx": 3},
    )
    ok = await a.send(msg)
    assert ok is True
    assert a._iface.calls[0]["channelIndex"] == 3
    # The adapter's own tuned default must be unaffected by a one-off hint.
    assert a._channel_idx == 0
