"""
ech/core/auth.py
-----------------
Session-based authentication for ECH.

- Username/password stored as bcrypt hashes in SQLite
- Three roles: admin (full access), operator (no settings/user management),
  viewer (read-only: every page is viewable and Ham Log is writable, but
  settings, user management, and sending messages over any adapter are
  blocked — see ADMIN_PATHS/ADMIN_PREFIXES and the viewer check in
  ech/api/app.py's auth_middleware)
- Session tokens stored in SQLite with expiry
- Default admin/admin created on first run if no users exist
- Login page at /login, protected routes check session cookie

No OAuth, no LDAP - offline-first emergency system.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Optional

log = logging.getLogger(__name__)

SESSION_EXPIRE_HOURS = 12
SESSION_COOKIE = "ech_session"


class AuthManager:
    def __init__(self, db):
        self._db = db

    async def init(self) -> None:
        """Create default admin user if no users exist."""
        users = await self._db.get_users()
        if not users:
            await self.create_user("admin", "admin", "admin", must_change_pw=True)
            log.warning(
                "AUTH: Created default admin/admin account — "
                "CHANGE THIS PASSWORD immediately in Settings → Users"
            )

    # ── Users ─────────────────────────────────────────────────────────────

    async def create_user(self, username: str, password: str, role: str = "operator",
                          must_change_pw: bool = False, color: str = "#8b949e") -> bool:
        import bcrypt
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        try:
            await self._db.upsert_user({
                "username": username,
                "pw_hash": pw_hash,
                "role": role,
                "must_change_pw": must_change_pw,
                "color": color,
            })
            log.info("AUTH: user '%s' created with role '%s'", username, role)
            return True
        except Exception as exc:
            log.error("AUTH: create_user error: %s", exc)
            return False

    async def change_password(self, username: str, new_password: str) -> bool:
        import bcrypt
        pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        await self._db.update_user_password(username, pw_hash)
        log.info("AUTH: password changed for '%s'", username)
        return True

    async def admin_reset_password(self, username: str, new_password: str) -> bool:
        """Admin resets another user's password — forces a change at next login
        (see database.admin_reset_user_password) instead of trusting the admin's
        temporary password indefinitely."""
        import bcrypt
        pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        await self._db.admin_reset_user_password(username, pw_hash)
        log.info("AUTH: admin reset password for '%s' — must change at next login", username)
        return True

    async def delete_user(self, username: str) -> None:
        await self._db.delete_user(username)

    # ── Login / logout ────────────────────────────────────────────────────

    async def login(self, username: str, password: str) -> Optional[str]:
        """
        Validate credentials. Returns session token on success, None on failure.
        """
        import bcrypt
        user = await self._db.get_user(username)
        if not user:
            return None
        try:
            if not bcrypt.checkpw(password.encode(), user["pw_hash"].encode()):
                log.warning("AUTH: failed login for '%s'", username)
                return None
        except Exception:
            return None

        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_EXPIRE_HOURS)
        await self._db.create_session(token, username, user["role"], expires)
        log.info("AUTH: '%s' logged in", username)
        return token

    async def logout(self, token: str) -> None:
        await self._db.delete_session(token)

    async def get_session(self, token: str) -> Optional[dict]:
        """Return session dict {username, role, must_change_pw} or None if invalid/expired."""
        if not token:
            return None
        session = await self._db.get_session(token)
        if not session:
            return None
        expires = datetime.fromisoformat(session["expires"])
        if datetime.now(timezone.utc) > expires:
            await self._db.delete_session(token)
            return None
        user = await self._db.get_user(session["username"])
        must_change = bool(user and user.get("must_change_pw"))
        return {"username": session["username"], "role": session["role"],
                "must_change_pw": must_change}

    async def require_session(self, request) -> Optional[dict]:
        """Extract and validate session from request cookie."""
        token = request.cookies.get(SESSION_COOKIE)
        return await self.get_session(token)
