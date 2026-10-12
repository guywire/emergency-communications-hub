"""
tests/test_multi_radio.py
-------------------------
O131: two radios of one protocol (local MeshCore + one in an operator's
browser) act as one station — a channel message heard on both is ONE feed
entry ("heard on" both), our own other radio's transmissions aren't new
messages, DMs are never merged, and a bridge between the two radios skips a
target that already heard the message itself.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ech.core.models import NormalizedMessage


class MeshCoreAdapter:          # same class name the router keys radio families on
    def __init__(self, name, device_name, node_id):
        self.name, self._device_name, self._self_node_id = name, device_name, node_id
        self._connected, self._paused = True, False
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)
        return True


def _router(*adapters):
    from ech.core.router import Router
    db = MagicMock()
    db.save_message = AsyncMock()
    r = Router(db)
    r._adapters = {a.name: a for a in adapters}
    r.broadcast_ws_event = AsyncMock()
    r._broadcast_ws = AsyncMock()
    return r


def _chan(adapter, ch, sender, text):
    return NormalizedMessage(source_adapter=adapter, source_channel=ch, from_id=sender.lower(),
                             from_display=sender, body=text)


@pytest.mark.asyncio
async def test_same_message_on_two_radios_is_one_entry():
    usb, remote = MeshCoreAdapter("meshcore-usb", "SignalMatrix", "C65FDBF8C77E"), \
        MeshCoreAdapter("meshcore-remote", "KN0O-Remote", "AB12CD34EF56")
    r = _router(usb, remote)
    await r._handle_inbound(_chan("meshcore-usb", "ch2:testing", "Bob", "net check"))
    await r._handle_inbound(_chan("meshcore-remote", "ch0:testing", "Bob", "net check"))  # other index, same channel name
    assert r._db.save_message.await_count == 1
    ev = r.broadcast_ws_event.await_args_list[-1]
    assert ev.args[0] == "heard_on" and ev.args[1]["adapters"] == ["meshcore-usb", "meshcore-remote"]


@pytest.mark.asyncio
async def test_own_radio_echo_dropped_and_dms_not_merged():
    usb, remote = MeshCoreAdapter("meshcore-usb", "SignalMatrix", "C65F"), MeshCoreAdapter("meshcore-remote", "KN0O-Remote", "AB12")
    r = _router(usb, remote)
    await r._handle_inbound(_chan("meshcore-remote", "ch0:testing", "SignalMatrix", "hello from usb radio"))
    assert r._db.save_message.await_count == 0                     # our own transmission
    dm1 = NormalizedMessage(source_adapter="meshcore-usb", source_channel="DM", from_id="bob", from_display="Bob",
                            body="hi", to_id="C65F")
    dm2 = NormalizedMessage(source_adapter="meshcore-remote", source_channel="DM", from_id="bob", from_display="Bob",
                            body="hi", to_id="AB12")
    await r._handle_inbound(dm1)
    await r._handle_inbound(dm2)
    assert r._db.save_message.await_count == 2                     # two different DMs


@pytest.mark.asyncio
async def test_single_radio_unaffected():
    r = _router(MeshCoreAdapter("meshcore-usb", "SignalMatrix", "C65F"))
    await r._handle_inbound(_chan("meshcore-usb", "ch2:testing", "Bob", "one"))
    await r._handle_inbound(_chan("meshcore-usb", "ch2:testing", "Bob", "two"))
    assert r._db.save_message.await_count == 2


@pytest.mark.asyncio
async def test_bridge_to_sibling_skips_radio_that_already_heard_it():
    usb, remote = MeshCoreAdapter("meshcore-usb", "SignalMatrix", "C65F"), MeshCoreAdapter("meshcore-remote", "Remote", "AB12")
    r = _router(usb, remote)
    r.BRIDGE_SIBLING_HOLD_S = 0.05
    r._bridge_rules = [{"name": "sites", "a": {"adapter": "meshcore-usb", "channel": 2},
                        "b": {"adapter": "meshcore-remote", "channel": 0}, "direction": "both", "rate_per_min": 60}]
    m1 = _chan("meshcore-usb", "ch2:testing", "Bob", "heard at both sites")
    m1.raw = {"channel_idx": 2}
    m2 = _chan("meshcore-remote", "ch0:testing", "Bob", "heard at both sites")
    m2.raw = {"channel_idx": 0}
    await r._handle_inbound(m1)
    await r._handle_inbound(m2)          # remote heard it too, within the hold
    await asyncio.sleep(0.2)
    assert remote.sent == [] and usb.sent == []
    m3 = _chan("meshcore-usb", "ch2:testing", "Carol", "only the usb site heard this")
    m3.raw = {"channel_idx": 2}
    await r._handle_inbound(m3)
    await asyncio.sleep(0.2)
    assert [m.body for m in remote.sent] == ["Carol: only the usb site heard this"]
