from types import SimpleNamespace as NS

from ech.adapters.meshtastic_adapter import MeshtasticAdapter


def _adapter():
    return MeshtasticAdapter({"name": "mt", "transport": "tcp", "host": "x"})


def _ch(i, name, role):
    return NS(index=i, role=role, settings=NS(name=name))


def test_reads_all_enabled_channels_and_skips_disabled():
    a = _adapter()
    iface = NS(localNode=NS(channels=[_ch(0, "", 1), _ch(1, "TestNet", 2), _ch(2, "", 0)]))
    assert a._refresh_channel_list(iface=iface) is True
    assert a._channel_list == [{"index": 0, "name": ""}, {"index": 1, "name": "TestNet"}]


def test_new_channel_appearing_later_is_picked_up():
    a = _adapter()
    chans = [_ch(0, "", 1)]
    iface = NS(localNode=NS(channels=chans))
    a._refresh_channel_list(iface=iface)
    chans.append(_ch(3, "Drill", 2))
    assert a._refresh_channel_list(iface=iface) is True
    assert [c["index"] for c in a._channel_list] == [0, 3]
    assert a._refresh_channel_list(iface=iface) is False  # unchanged


def test_empty_library_table_keeps_existing_list():
    a = _adapter()
    a._channel_list = [{"index": 0, "name": ""}]
    assert a._refresh_channel_list(iface=NS(localNode=NS(channels=None))) is False
    assert a._channel_list == [{"index": 0, "name": ""}]


def test_tx_channel_label_includes_name_when_known():
    a = _adapter()
    a._channel_list = [{"index": 0, "name": ""}, {"index": 1, "name": "test"}]
    assert a._channel_label(0) == "ch0"
    assert a._channel_label(1) == "ch1:test"
    a._channel_idx = 1
    assert a.tx_channel == "ch1:test"
