"""
ech/adapters/meshtastic_mqtt_codec.py
-------------------------------------
Decode Meshtastic MQTT protobuf uplinks (``msh/<region>/2/e/<channel>/<gateway>``).

Gateways publish a ``ServiceEnvelope`` protobuf. Its ``packet`` is either already
``decoded`` (gateway has "encryption enabled" off) or ``encrypted`` with the
channel PSK using AES-CTR. This module turns one payload into a plain dict the
MQTT adapter can map onto NormalizedMessage / MeshNode.

Channel keys follow the firmware rules:
  - 1-byte PSK: index 0 = no crypto, index N = default key with last byte + (N-1)
    ("AQ==" == index 1 == the well-known LongFast key)
  - 16/32-byte PSK: AES-128 / AES-256 key used as-is
Nonce = packet_id (uint64 LE) + from_node (uint32 LE) + 4 zero bytes.
"""

from __future__ import annotations

import base64
import binascii
import logging

log = logging.getLogger(__name__)

DEFAULT_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
BROADCAST = 0xFFFFFFFF


def expand_psk(key: str | bytes | None) -> bytes | None:
    """Turn a configured channel key (base64 or hex, firmware short form allowed) into AES key bytes.

    Returns None for "no encryption" (index 0 / empty)."""
    if key is None:
        return None
    if isinstance(key, str):
        k = key.strip()
        if not k:
            return None
        raw = None
        if len(k) in (32, 64):
            try:
                raw = bytes.fromhex(k)
            except ValueError:
                raw = None
        if raw is None:
            try:
                raw = base64.b64decode(k, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"channel key is neither base64 nor hex: {exc}") from None
    else:
        raw = bytes(key)
    if len(raw) == 0:
        return None
    if len(raw) == 1:
        idx = raw[0]
        if idx == 0:
            return None
        return DEFAULT_PSK[:-1] + bytes([(DEFAULT_PSK[-1] + idx - 1) & 0xFF])
    if len(raw) in (16, 32):
        return raw
    raise ValueError(f"channel key must be 1, 16 or 32 bytes (got {len(raw)})")


def _aes_ctr(key: bytes, nonce16: bytes, data: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES  # pycryptodome
        return AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=nonce16).decrypt(data)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CTR(nonce16)).decryptor()
        return dec.update(data) + dec.finalize()


def decrypt_packet(packet, key: bytes):
    """Decrypt ``packet.encrypted`` into a ``mesh_pb2.Data``; None if it isn't valid under this key."""
    from meshtastic.protobuf import mesh_pb2
    from_node = getattr(packet, "from")
    nonce = packet.id.to_bytes(8, "little") + from_node.to_bytes(4, "little") + b"\x00" * 4
    plain = _aes_ctr(key, nonce, packet.encrypted)
    data = mesh_pb2.Data()
    try:
        data.ParseFromString(plain)
    except Exception:
        return None
    # A wrong key still often "parses" — portnum 0 (UNKNOWN_APP) is the tell.
    if data.portnum == 0:
        return None
    return data


def node_id_str(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"


def parse_node_id(node_id: str) -> int | None:
    """'!1234abcd' / '1234abcd' / '0x1234abcd' / decimal -> node number."""
    s = str(node_id or "").strip().lower()
    try:
        if s.startswith("!"):
            return int(s[1:], 16)
        if s.startswith("0x"):
            return int(s, 16)
        if len(s) == 8 and all(c in "0123456789abcdef" for c in s):
            return int(s, 16)
        return int(s)
    except ValueError:
        return None


def _xor(b: bytes) -> int:
    h = 0
    for x in b:
        h ^= x
    return h


def channel_hash(name: str, key: bytes | None) -> int:
    """Firmware Channels::generateHash: xor(name bytes) ^ xor(expanded key bytes)."""
    return _xor(name.encode("utf-8")) ^ _xor(key or b"")


def encode_envelope(*, from_num: int, to_num: int, packet_id: int, channel: str, key: bytes | None,
                    gateway_id: str, portnum: int, payload: bytes, hop_limit: int = 3) -> bytes:
    """Build a ServiceEnvelope the way a firmware gateway uplinks one (encrypted with the channel PSK)."""
    from meshtastic.protobuf import mesh_pb2, mqtt_pb2
    data = mesh_pb2.Data(portnum=portnum, payload=payload, bitfield=1)  # bit0 = ok_to_mqtt
    pkt = mesh_pb2.MeshPacket(to=to_num & 0xFFFFFFFF, id=packet_id & 0xFFFFFFFF,
                              hop_limit=hop_limit, hop_start=hop_limit)
    setattr(pkt, "from", from_num & 0xFFFFFFFF)
    if key:
        nonce = (packet_id & 0xFFFFFFFF).to_bytes(8, "little") + (from_num & 0xFFFFFFFF).to_bytes(4, "little") + b"\x00" * 4
        pkt.encrypted = _aes_ctr(key, nonce, data.SerializeToString())  # CTR: same op both ways
        pkt.channel = channel_hash(channel, key)
    else:
        pkt.decoded.CopyFrom(data)
    return mqtt_pb2.ServiceEnvelope(packet=pkt, channel_id=channel, gateway_id=gateway_id).SerializeToString()


def decode_envelope(payload: bytes, keys: dict[str, bytes | None], fallback_keys: list[bytes] | None = None) -> dict | None:
    """Decode one ServiceEnvelope payload.

    keys:          channel name -> expanded AES key (None = channel is unencrypted)
    fallback_keys: keys to try when the envelope's channel isn't in ``keys``

    Returns None for undecodable / undecryptable payloads, else a dict:
      packet_id, from_num, from_id, to_num, to_id, is_broadcast, channel, gateway_id,
      portnum (name), payload (bytes), hop_start, hop_limit, rx_snr, rx_rssi, rx_time, decoded (protobuf obj)
    """
    from meshtastic.protobuf import mqtt_pb2, portnums_pb2

    env = mqtt_pb2.ServiceEnvelope()
    try:
        env.ParseFromString(payload)
    except Exception:
        return None
    if not env.HasField("packet"):
        return None
    pkt = env.packet
    channel = env.channel_id or ""

    data = None
    if pkt.HasField("decoded"):
        data = pkt.decoded
    elif pkt.encrypted:
        candidates: list[bytes] = []
        if channel in keys:
            if keys[channel]:
                candidates.append(keys[channel])
        else:
            candidates.extend(k for k in (fallback_keys or []) if k)
        for k in candidates:
            data = decrypt_packet(pkt, k)
            if data is not None:
                break
        if data is None:
            return None
    else:
        return None

    try:
        portnum = portnums_pb2.PortNum.Name(data.portnum)
    except ValueError:
        portnum = str(data.portnum)

    from_num = getattr(pkt, "from")
    to_num = pkt.to
    return {
        "packet_id": pkt.id,
        "from_num": from_num,
        "from_id": node_id_str(from_num),
        "to_num": to_num,
        "to_id": node_id_str(to_num),
        "is_broadcast": to_num == BROADCAST,
        "channel": channel,
        "gateway_id": env.gateway_id or "",
        "portnum": portnum,
        "payload": bytes(data.payload),
        "hop_start": pkt.hop_start or None,
        "hop_limit": pkt.hop_limit if pkt.hop_start else None,
        "rx_snr": pkt.rx_snr or None,
        "rx_rssi": pkt.rx_rssi or None,
        "rx_time": pkt.rx_time or None,
        "decoded": data,
    }


def parse_user(payload: bytes) -> dict | None:
    from meshtastic.protobuf import mesh_pb2
    u = mesh_pb2.User()
    try:
        u.ParseFromString(payload)
    except Exception:
        return None
    try:
        hw = mesh_pb2.HardwareModel.Name(u.hw_model) if u.hw_model else ""
    except ValueError:
        hw = str(u.hw_model)
    try:
        from meshtastic.protobuf import config_pb2
        role = config_pb2.Config.DeviceConfig.Role.Name(u.role) if u.role else ""
    except Exception:
        role = ""
    return {"long_name": u.long_name, "short_name": u.short_name, "hw_model": hw, "role": role}


def parse_map_report(payload: bytes) -> dict | None:
    """MAP_REPORT_APP payload (gateways publish these on <root>/2/map/): name + rough position."""
    from meshtastic.protobuf import mesh_pb2, mqtt_pb2
    r = mqtt_pb2.MapReport()
    try:
        r.ParseFromString(payload)
    except Exception:
        return None
    try:
        hw = mesh_pb2.HardwareModel.Name(r.hw_model) if r.hw_model else ""
    except ValueError:
        hw = str(r.hw_model)
    try:
        from meshtastic.protobuf import config_pb2
        role = config_pb2.Config.DeviceConfig.Role.Name(r.role) if r.role else ""
    except Exception:
        role = ""
    has_pos = bool(r.latitude_i or r.longitude_i)
    return {"long_name": r.long_name, "short_name": r.short_name, "hw_model": hw, "role": role,
            "firmware": r.firmware_version,
            "lat": r.latitude_i / 1e7 if has_pos else None,
            "lon": r.longitude_i / 1e7 if has_pos else None}


def parse_position(payload: bytes) -> dict | None:
    from meshtastic.protobuf import mesh_pb2
    p = mesh_pb2.Position()
    try:
        p.ParseFromString(payload)
    except Exception:
        return None
    if not p.latitude_i and not p.longitude_i:
        return None
    return {"lat": p.latitude_i / 1e7, "lon": p.longitude_i / 1e7, "alt": p.altitude or None}
