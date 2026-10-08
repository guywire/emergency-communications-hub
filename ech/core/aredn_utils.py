"""
ech/core/aredn_utils.py
-------------------------
O101: shared helper for ECH's AREDN-facing adapters (aredn_ami.py,
aredn_meshchat.py) to recognize whether a configured host actually looks
like it's on an AREDN mesh, rather than treating every host as an opaque
IP/hostname.

Researched against AREDN's own current docs (docs.arednmesh.org, fetched
2026-10-08) rather than assumed — this corrects two premises in this
project's own earlier backlog note (O101) that turned out to be wrong, and
explicitly rules out a third:

  - "44.x.x.x address space" is NOT the default/general AREDN mesh
    addressing scheme. Per the Node Admin Guide, standard node-to-node mesh
    addresses are "automatically calculated based on the unique MAC
    addresses on your node" — in practice this lands in the 10.x.x.x RFC
    1918 range. The real 44.0.0.0/9 + 44.128.0.0/10 AMPRnet space is only
    used in an OPT-IN "44Net LAN mode" for devices BEHIND a node, which
    supernodes then route — most mesh hosts won't be using it. Validating
    against 44.x.x.x as if it were the normal case would flag the common,
    correctly-configured case as wrong.
  - "Node naming" convention IS real and simple: AREDN nodes are reachable
    at `<nodename>.local.mesh` — already used correctly as the example in
    aredn_meshchat.py's own docstring/config example.
  - "VLAN-awareness" (AREDN's real single-port-node convention — untagged
    = LAN, VLAN 1 = WAN, VLAN 2 = DtD, custom VLANs ≥5) is real, but it's
    an AREDN NODE's own Ethernet-port configuration — one layer below
    anything a Python IP/HTTP client like ECH's adapters can see or act
    on. ECH never touches 802.1Q tags; it just opens a TCP/HTTP connection
    to whatever IP/hostname is configured. There is nothing for ECH to "be
    aware of" here — this candidate was checked against the real docs and
    is deliberately NOT implemented, recorded as checked-and-not-applicable
    rather than silently dropped.

What this module actually does: a cheap, non-blocking sanity check.
classify_mesh_address() recognizes the two real AREDN-ish address shapes
(the standard `*.local.mesh` node hostname, and the 10.x.x.x / 44Net
ranges) so an adapter can log/surface a warning if its configured host
doesn't look like it's actually on the mesh (e.g. a typo'd public IP) —
informational only, never a hard failure, since there are legitimate setups
(custom LAN ranges, NAT, a non-default AREDN build) this can't see.
"""

from __future__ import annotations

import ipaddress

_AMPRNET_NETS = [ipaddress.ip_network("44.0.0.0/9"), ipaddress.ip_network("44.128.0.0/10")]
_MESH_RFC1918_NET = ipaddress.ip_network("10.0.0.0/8")


def classify_mesh_address(host: str) -> str:
    """Best-effort classification of a configured AREDN-facing host.
    Returns a short human-readable label, never raises — unparseable/
    non-IP hostnames are checked for the '.local.mesh' convention instead
    of treated as an error."""
    host = (host or "").strip().lower()
    if not host:
        return "unset"
    if host.endswith(".local.mesh"):
        return "aredn-hostname (.local.mesh)"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "unrecognized-hostname"
    if addr in _MESH_RFC1918_NET:
        return "aredn-mesh (10.x.x.x, standard node range)"
    if any(addr in net for net in _AMPRNET_NETS):
        return "aredn-44net (AMPRnet LAN-side device)"
    if addr.is_loopback:
        return "loopback"
    if addr.is_private:
        return "private-ip (not AREDN's standard 10.x.x.x range)"
    return ("public-ip (not AREDN mesh-typical — verify this host is "
            "actually reachable only via the mesh, not the open internet)")


def is_mesh_typical(classification: str) -> bool:
    """True for the classifications that look like a normal AREDN setup —
    used to decide whether to log a one-time informational warning."""
    return classification.startswith(("aredn-hostname", "aredn-mesh", "aredn-44net", "unset"))
