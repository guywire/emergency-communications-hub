"""
tests/test_pbx_watch.py
-----------------------
PBX security / watched-extension findings raised by the Asterisk adapter from
AMI events (ech/adapters/asterisk_adapter.py). Addresses are made up.
"""

import asyncio

from ech.adapters import asterisk_adapter as aa
from ech.core.anomaly import AnomalyEngine


def _adapter(**cfg):
    a = aa.AsteriskAdapter({"name": "pbx", "watch_extensions": ["107"], **cfg})
    a._anomaly_engine = AnomalyEngine({})
    return a


def _run(coro):
    return asyncio.run(coro)


def _rules(a):
    return [(f.rule, f.severity.value, f.summary) for f in a._anomaly_engine.findings]


def test_helpers():
    assert aa.remote_ip("IPV4/UDP/10.1.2.3/5060") == "10.1.2.3"
    assert aa.remote_ip("") == ""
    assert aa.contact_ip("sip:107@10.9.8.7:5060;ob") == "10.9.8.7"
    assert aa.contact_ip("sip:107@10.9.8.7") == "10.9.8.7"
    assert aa.channel_endpoint("PJSIP/107-0000001a") == "107"
    assert aa.channel_endpoint("IAX2/HURRICANE-123") == ""


def test_auth_failures_threshold_and_doubling():
    a = _adapter()
    pkt = {"RemoteAddress": "IPV4/UDP/10.66.0.5/5060", "AccountID": "admin", "Service": "PJSIP"}

    async def go(n):
        for _ in range(n):
            await a._on_security_failure("InvalidAccountID", pkt)
    _run(go(2))
    assert a._anomaly_engine.findings == []          # under the threshold of 3
    _run(go(1))
    assert len(a._anomaly_engine.findings) == 1      # 3rd failure
    _run(go(2))
    assert len(a._anomaly_engine.findings) == 1      # 4, 5: quiet
    _run(go(1))
    assert len(a._anomaly_engine.findings) == 2      # 6 = doubled
    f = a._anomaly_engine.findings[-1]
    assert f.rule == "pbx_auth_failure" and f.node_id == "10.66.0.5" and f.severity.value == "warn"
    assert "admin" in f.summary


def test_auth_failure_on_watched_extension_is_alert():
    a = _adapter(security_alert_threshold=1)
    _run(a._on_security_failure("ChallengeResponseFailed",
                                {"RemoteAddress": "IPV4/UDP/10.66.0.9/5060", "AccountID": "107"}))
    assert a._anomaly_engine.findings[0].severity.value == "alert"


def test_security_alerts_can_be_disabled():
    a = _adapter(security_alerts=False, security_alert_threshold=1)
    _run(a._on_security_failure("InvalidPassword", {"RemoteAddress": "IPV4/UDP/10.1.1.1/5060"}))
    assert a._anomaly_engine.findings == []


def test_watched_registration():
    a = _adapter()

    async def go():
        await a._on_contact_status({"EndpointName": "107", "ContactStatus": "Created",
                                    "URI": "sip:107@10.50.1.2:5060"})
        await a._on_contact_status({"EndpointName": "107", "ContactStatus": "Reachable",
                                    "URI": "sip:107@10.50.1.2:5060"})   # ignored
        await a._on_contact_status({"EndpointName": "107", "ContactStatus": "Created",
                                    "URI": "sip:107@10.60.3.4:5060"})   # moved
        await a._on_contact_status({"EndpointName": "107", "ContactStatus": "Created",
                                    "URI": "sip:107@203.0.113.9:5060"})  # not mesh
        await a._on_contact_status({"EndpointName": "101", "ContactStatus": "Created",
                                    "URI": "sip:101@192.168.1.40:5060"})  # not watched
    _run(go())
    r = _rules(a)
    assert [x[1] for x in r] == ["info", "warn", "alert"]
    assert "registered from 10.50.1.2" in r[0][2] and "was 10.50.1.2" in r[1][2]
    assert "not an AREDN mesh address" in r[2][2]


def test_watched_calls_both_directions():
    a = _adapter()

    async def go():
        await a._watch_new_channel({"Channel": "PJSIP/107-00000001", "Exten": "785104397",
                                    "Context": "aredn-guest"})
        await a._watch_new_channel({"Channel": "PJSIP/107-00000002", "Exten": "s"})   # callee leg
        await a._watch_new_channel({"Channel": "PJSIP/101-00000003", "Exten": "102"})  # not watched
        await a._watch_dial_begin({"Channel": "PJSIP/101-00000004", "CallerIDNum": "101",
                                   "DestChannel": "PJSIP/107-00000005"})
    _run(go())
    r = _rules(a)
    assert [x[2] for x in r] == ["Ext 107 dialed 785104397", "Ext 107 called by 101"]


def test_guest_blocked():
    a = _adapter()
    _run(a._on_guest_blocked({"Channel": "PJSIP/107-0000000a", "Exten": "44000"}))
    (rule, sev, summary), = _rules(a)
    assert rule == "pbx_guest_blocked" and sev == "warn" and "44000" in summary


def test_no_engine_is_harmless():
    a = aa.AsteriskAdapter({"name": "pbx", "watch_extensions": ["107"]})
    _run(a._on_guest_blocked({"Channel": "PJSIP/107-0000000a", "Exten": "44000"}))


def test_invalid_watch_entries_dropped():
    a = aa.AsteriskAdapter({"name": "pbx", "watch_extensions": ["107", "101&PJSIP/x", ""]})
    assert a._watch_exts == {"107"}


def test_text_message_base64_fields_resist_header_injection():
    import base64
    a = _adapter()
    got = []

    async def enq(msg):
        got.append(msg)
    a._enqueue = enq
    b = lambda s: base64.b64encode(s.encode()).decode()
    # A body with a comma used to become a second "From:" header after UserEvent() split it.
    pkt = {"Event": "UserEvent", "UserEvent": "ECHTextMessage",
           "From64": b("sip:107@aredn-guest"), "To64": b("101"),
           "Body64": b("hi,From: sip:101@x\r\n\r\nEvent: Hangup"), "From": "sip:101@x"}
    _run(a._on_text_message(pkt))
    m = got[0]
    assert m.from_id == "107" and m.to_id == "101" and m.body.startswith("hi,From: sip:101@x")


def test_text_message_plain_fields_still_accepted():
    a = _adapter()
    got = []

    async def enq(msg):
        got.append(msg)
    a._enqueue = enq
    _run(a._on_text_message({"From": "<sip:102@192.168.1.5>", "To": "101", "Body": "hello"}))
    assert got[0].from_id == "102" and got[0].body == "hello"
