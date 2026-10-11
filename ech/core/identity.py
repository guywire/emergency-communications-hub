"""
ech/core/identity.py
--------------------
This station's identity for outbound requests to third-party data services.

Every install identifies as ITS OWN operator: the callsign and contact email
come from that install's config.yaml (`operator.callsign`,
`operator.contact_email`, falling back to the older
`pskreporter.contact_email`) and can be changed live from Settings. Services
such as the NWS API and PSKReporter ask for a real contact in the
User-Agent so they can reach the operator instead of blocking them — so no
shared/placeholder address is ever sent, and nothing about the original
developer's station is baked into the code.
"""

from __future__ import annotations

_callsign = "N0CALL"
_email = ""


def configure(cfg: dict | None) -> None:
    """Load identity from a parsed config.yaml dict."""
    cfg = cfg or {}
    op = cfg.get("operator") or {}
    set_callsign(op.get("callsign") or "N0CALL")
    set_email(op.get("contact_email") or (cfg.get("pskreporter") or {}).get("contact_email") or "")


def set_callsign(callsign: str) -> None:
    global _callsign
    _callsign = (str(callsign or "").strip().upper() or "N0CALL")


def set_email(email: str) -> None:
    global _email
    _email = str(email or "").strip()


def callsign() -> str:
    return _callsign


def email() -> str:
    return _email


def user_agent() -> str:
    """'(SignalMatrix/<CALL>, <email>)' — the NWS-recommended '(app, contact)'
    form. Without an email the callsign is still sent, and the contact slot
    says it isn't configured rather than inventing an address."""
    contact = _email or "contact email not configured"
    return f"(SignalMatrix/{_callsign}, {contact})"
