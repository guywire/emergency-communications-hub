"""
ech/core/audit.py
-------------------
SEC-14/SEC-15: a tamper-evident audit trail for authentication and
admin/service actions, previously only logged at WARNING level to the app
log (not durable, not queryable, no tamper detection). Storage and the
hash-chain itself live in ech/core/database.py (add_audit_entry/
get_audit_log/verify_audit_chain) — this module is just a thin, easy-to-call
wrapper for app.py's endpoints so each call site doesn't repeat IP
extraction and error handling.

Deliberately fire-and-forget-safe: a failure to write an audit entry (e.g.
a transient DB issue) logs a warning but never raises — an audit log
that could break the action it's auditing would be worse than one that
occasionally misses an entry.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def client_ip(request) -> str:
    return request.client.host if request.client else "unknown"


async def record(db, request, username: str, role: str, action: str,
                  detail: str = "", success: bool = True) -> None:
    if db is None:
        return
    try:
        await db.add_audit_entry(
            username=username, role=role, ip=client_ip(request),
            action=action, detail=detail, success=success,
        )
    except Exception as exc:
        log.warning("AUDIT: failed to record '%s' for '%s': %s", action, username, exc)
