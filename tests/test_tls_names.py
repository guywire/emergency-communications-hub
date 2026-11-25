"""Extra certificate names (tls.extra_names): the AREDN name of this box."""
import ipaddress

from cryptography import x509

from ech.core import tls


def test_san_names_cleans_and_adds_the_short_mesh_name():
    assert tls.san_names(["KN0O-PBX-ECH.local.mesh.", "10.13.53.44", "", "bad name!", "kn0o-pbx-ech"]) == \
        ["kn0o-pbx-ech.local.mesh", "kn0o-pbx-ech", "10.13.53.44"]


def test_server_cert_carries_the_extra_names(tmp_path):
    ca_pem, ca_key = tls.ensure_ca(tmp_path)
    crt, _ = tls.ensure_server_cert(tmp_path, ca_pem, ca_key, ["kn0o-pbx-ech.local.mesh", "10.13.53.44"])
    san = x509.load_pem_x509_certificate(crt.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    dns = san.get_values_for_type(x509.DNSName)
    assert "kn0o-pbx-ech.local.mesh" in dns and "kn0o-pbx-ech" in dns and "localhost" in dns
    assert ipaddress.ip_address("10.13.53.44") in san.get_values_for_type(x509.IPAddress)
