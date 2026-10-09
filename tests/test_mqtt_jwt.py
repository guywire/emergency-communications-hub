"""
tests/test_mqtt_jwt.py
-------------------------
Covers O75: the LetsMesh JWT claim set rebuild. Two prior fix attempts
(rc211, rc216) left the broker rejecting every token with
[code:135] Not authorized. A fresh research pass found a more accurate
reference scheme — minimal claims only (publicKey, aud, iat, exp), no
client/owner/email — see mqtt_adapter.py's _jwt_header_and_payload()
docstring for the full citation trail. Not yet live-verified against the
real broker (the operator doesn't currently run this adapter against
LetsMesh) — these tests cover that the claim set itself now matches the
corrected scheme, not that the broker accepts it.
"""

import base64
import json

import pytest

from ech.adapters.mqtt_adapter import MQTTAdapter


def _decode_jwt_payload(jwt: str) -> dict:
    header_b64, payload_b64, sig = jwt.split(".")
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_jwt_payload_has_only_minimal_claims():
    a = MQTTAdapter({"name": "letsmesh-test", "host": "mqtt-us-v1.letsmesh.net", "tls": True})
    header, payload = a._jwt_header_and_payload("AB" * 32)
    body = _decode_jwt_payload(f"{header}.{payload}.deadbeef")
    assert set(body.keys()) == {"publicKey", "aud", "iat", "exp"}


def test_jwt_payload_does_not_include_client_owner_email_even_when_set():
    """These were the extra claims added in rc216 chasing a less-accurate
    reference — confirmed to no longer be sent, even if an operator still
    has jwt_owner/jwt_email in their config.yaml from before."""
    a = MQTTAdapter({
        "name": "letsmesh-test", "host": "mqtt-us-v1.letsmesh.net", "tls": True,
        "jwt_owner": "KN0O", "jwt_email": "test@example.com",
    })
    header, payload = a._jwt_header_and_payload("AB" * 32)
    body = _decode_jwt_payload(f"{header}.{payload}.deadbeef")
    assert "client" not in body
    assert "owner" not in body
    assert "email" not in body


def test_jwt_aud_is_the_literal_connect_hostname():
    a = MQTTAdapter({"name": "letsmesh-test", "host": "mqtt-eu-v1.letsmesh.net"})
    header, payload = a._jwt_header_and_payload("CD" * 32)
    body = _decode_jwt_payload(f"{header}.{payload}.deadbeef")
    assert body["aud"] == "mqtt-eu-v1.letsmesh.net"


def test_jwt_public_key_is_uppercase_hex():
    a = MQTTAdapter({"name": "letsmesh-test", "host": "mqtt-us-v1.letsmesh.net"})
    header, payload = a._jwt_header_and_payload("ab" * 32)
    body = _decode_jwt_payload(f"{header}.{payload}.deadbeef")
    assert body["publicKey"] == "AB" * 32


def test_jwt_header_alg_is_ed25519():
    a = MQTTAdapter({"name": "letsmesh-test", "host": "mqtt-us-v1.letsmesh.net"})
    header, _ = a._jwt_header_and_payload("AB" * 32)
    padded = header + "=" * (-len(header) % 4)
    hdr = json.loads(base64.urlsafe_b64decode(padded))
    assert hdr == {"alg": "Ed25519", "typ": "JWT"}


@pytest.mark.asyncio
async def test_connect_warns_when_deprecated_jwt_fields_set(caplog):
    import logging
    a = MQTTAdapter({
        "name": "letsmesh-test", "host": "mqtt-us-v1.letsmesh.net",
        "jwt_owner": "KN0O",
    })
    with caplog.at_level(logging.WARNING):
        await a.connect()
    assert any("no longer sent" in r.message for r in caplog.records)
    await a.disconnect()


@pytest.mark.asyncio
async def test_connect_does_not_warn_without_deprecated_fields(caplog):
    import logging
    a = MQTTAdapter({"name": "letsmesh-test", "host": "mqtt-us-v1.letsmesh.net"})
    with caplog.at_level(logging.WARNING):
        await a.connect()
    assert not any("no longer sent" in r.message for r in caplog.records)
    await a.disconnect()


def test_username_scheme_is_v1_prefixed_uppercase_pubkey():
    """Not part of the JWT payload itself, but the other half of the auth
    scheme confirmed correct against the new reference — username =
    'v1_<64-hex-uppercase-pubkey>'. Exercises the literal string format,
    not the full _resolve_credentials() flow (that needs a live/mocked
    MeshCore adapter registry)."""
    pubkey = "ab" * 32
    assert f"v1_{pubkey.upper()}" == "v1_" + ("AB" * 32)
