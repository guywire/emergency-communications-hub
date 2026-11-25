"""
ech/core/tls.py
---------------
TLS certificate management for ECH.

Generates a persistent CA cert (stored once in the data dir) and a
per-startup server cert with all current IP addresses as Subject
Alternative Names.

Field-deployment workflow:
  1. First run generates ech-ca.crt + ech-ca.key in the data dir.
  2. Operators browse to http://<ip>:8765/ca.crt and trust that CA cert
     in their OS/browser once.
  3. Every subsequent ECH deployment — any IP — is trusted automatically
     because server certs are signed by the same CA.
  4. Web Serial API (SecureContext) works on https://<ip>:8766.
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _local_ips(all_interfaces: bool = False) -> list[str]:
    """Return this box's IPv4 addresses plus loopback. all_interfaces: every
    interface's address too (for the certificate only; mDNS must not offer LAN
    clients a mesh VLAN, docker or VPN address they can't reach)."""
    ips: list[str] = ["127.0.0.1"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            try:
                ipaddress.IPv4Address(addr)
                if addr not in ips:
                    ips.append(addr)
            except ValueError:
                pass
    except Exception:
        pass
    # Every interface's address (the hostname lookup above misses e.g. a mesh VLAN)
    if all_interfaces:
        try:
            import psutil
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family == socket.AF_INET and a.address not in ips and not a.address.startswith("169.254."):
                        ips.append(a.address)
        except Exception:
            pass
    # Default outbound interface
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addr = s.getsockname()[0]
            if addr not in ips:
                ips.append(addr)
    except Exception:
        pass
    return ips


# ---------------------------------------------------------------------------
# CA cert
# ---------------------------------------------------------------------------

def ensure_ca(data_dir: Path) -> Tuple[bytes, bytes]:
    """Load or generate the ECH CA key + cert.

    Returns (cert_pem, key_pem).  The CA lives for 10 years and is only
    generated once so operators trust it once and are done.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    ca_key_path = data_dir / "ech-ca.key"
    ca_crt_path = data_dir / "ech-ca.crt"

    if ca_key_path.exists() and ca_crt_path.exists():
        log.debug("TLS: CA cert loaded from %s", ca_crt_path)
        return ca_crt_path.read_bytes(), ca_key_path.read_bytes()

    log.info("TLS: generating new CA cert in %s", data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "SignalMatrix CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SignalMatrix"),
    ])
    now = datetime.datetime.utcnow()
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    key_pem  = ca_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    ca_crt_path.write_bytes(cert_pem)
    ca_key_path.write_bytes(key_pem)
    log.info("TLS: CA cert written to %s", ca_crt_path)
    return cert_pem, key_pem


# ---------------------------------------------------------------------------
# Server cert
# ---------------------------------------------------------------------------

def san_names(extra) -> list[str]:
    """Configured extra SAN names, cleaned: lower case, a .local.mesh name
    with its short form too, anything that isn't a host name or IP dropped."""
    import re
    out: list[str] = []
    for raw in extra or []:
        n = str(raw or "").strip().lower().rstrip(".")
        if not n:
            continue
        try:
            ipaddress.ip_address(n)
        except ValueError:
            label = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
            if not re.fullmatch(rf"(\*|{label})(\.{label})*", n) or n == "*":
                log.warning("TLS: ignoring extra name %r (not a host name or IP)", raw)
                continue
        for v in (n, n[: -len(".local.mesh")] if n.endswith(".local.mesh") else None):
            if v and v != "*" and v not in out:
                out.append(v)
    return out


def ensure_server_cert(
    data_dir: Path,
    ca_cert_pem: bytes,
    ca_key_pem: bytes,
    extra_names: list[str] | tuple[str, ...] = (),
) -> Tuple[Path, Path]:
    """Generate a server cert signed by the ECH CA.

    SANs include all current local IPv4 addresses plus ``localhost`` and
    ``ech.local`` so the cert is valid regardless of which IP the operator
    uses to reach the server.  Regenerated on every startup so the SAN list
    stays current.

    extra_names (config tls.extra_names): more host names or IPs, e.g. the
    name the AREDN node gives this box ("kn0o-pbx-ech.local.mesh"); a
    .local.mesh name also gets its short form, as mesh users type either.

    Returns (cert_path, key_path) suitable for uvicorn ssl_certfile/ssl_keyfile.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.x509.oid import NameOID

    server_key_path = data_dir / "ech-server.key"
    server_crt_path = data_dir / "ech-server.crt"

    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key  = load_pem_private_key(ca_key_pem, password=None)

    sans: list[x509.GeneralName] = []
    ips = _local_ips(all_interfaces=True)
    for ip_str in ips:
        try:
            sans.append(x509.IPAddress(ipaddress.IPv4Address(ip_str)))
        except ValueError:
            pass
    for hostname in ("localhost", "ech.local"):
        sans.append(x509.DNSName(hostname))
    try:
        sans.append(x509.DNSName(socket.gethostname()))
    except Exception:
        pass
    for name in san_names(extra_names):
        try:
            gn = x509.IPAddress(ipaddress.ip_address(name))
        except ValueError:
            gn = x509.DNSName(name)
        if gn not in sans:
            sans.append(gn)

    log.info("TLS: generating server cert with SANs: %s + localhost/ech.local %s", ips, san_names(extra_names))

    server_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.utcnow()
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "SignalMatrix"),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=False, crl_sign=False,
            content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    cert_pem = server_cert.public_bytes(serialization.Encoding.PEM)
    key_pem  = server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    server_crt_path.write_bytes(cert_pem)
    server_key_path.write_bytes(key_pem)
    return server_crt_path, server_key_path


# ---------------------------------------------------------------------------
# mDNS advertisement (optional — requires zeroconf)
# ---------------------------------------------------------------------------

def start_mdns(https_port: int, service_name: str = "SignalMatrix") -> object | None:
    """Advertise SignalMatrix via mDNS as ech.local so operators can use a stable hostname.

    Requires the ``zeroconf`` package.  Returns the ServiceInfo object (keep a
    reference so it isn't GC'd) or None if zeroconf is unavailable.
    """
    try:
        from zeroconf import ServiceInfo, Zeroconf
        import socket

        ips = [ip for ip in _local_ips() if ip != "127.0.0.1"]
        if not ips:
            return None

        addresses = [socket.inet_aton(ip) for ip in ips]
        info = ServiceInfo(
            "_https._tcp.local.",
            f"{service_name}._https._tcp.local.",
            addresses=addresses,
            port=https_port,
            properties={"path": "/"},
            server="ech.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        log.info("TLS: mDNS advertising %s at ech.local:%d", service_name, https_port)
        return (zc, info)
    except ImportError:
        log.debug("TLS: zeroconf not installed — mDNS advertisement skipped")
        return None
    except Exception as exc:
        log.warning("TLS: mDNS failed: %s", exc)
        return None


def stop_mdns(handle: object) -> None:
    if handle is None:
        return
    try:
        zc, info = handle
        zc.unregister_service(info)
        zc.close()
    except Exception:
        pass
