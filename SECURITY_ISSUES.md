# ECH Security Issues & Vulnerability Report
# Prepared for Communications Director review
# Classification: INTERNAL — Do not share publicly

---

## CRITICAL

### SEC-01 — Default admin/admin credentials
**Risk:** Any attacker who reaches port 8765 can log in immediately.  
**Affected:** All fresh installs.  
**Fix:** Force password change on first login. Add a `password_changed` flag to the users table; if false and the user is `admin`, redirect every page to `/settings#users` with a mandatory change prompt. In the meantime, install.sh should generate a random first-run password and print it once.  
**Priority:** P0 — before any internet exposure.

### SEC-02 — No HTTPS / session hijacking risk
**Risk:** Session tokens transmitted in cleartext over HTTP are trivially sniffable on the LAN or over radio-linked IP networks (common in AREDN/mesh deployments). An attacker who intercepts one cookie owns the session for 12 hours.  
**Fix:** Generate a self-signed TLS cert at install time (`openssl req -x509 -newkey rsa:4096 -keyout /etc/ech/ech.key -out /etc/ech/ech.crt -days 3650 -nodes`) and configure uvicorn with `ssl_certfile`/`ssl_keyfile`. Or put nginx in front with TLS termination.  
**Priority:** P0 for any internet-facing or multi-hop radio deployment.

---

## HIGH

### SEC-03 — No role enforcement on sensitive API endpoints
**Risk:** Any authenticated user (even `operator` role) can call:
- `GET/POST/DELETE /api/users` — add or delete admin accounts
- `POST /api/system/services/{service}/restart` — restart services
- `POST/GET /api/adapter-config` — rewrite config.yaml
- `POST /api/base-location` — change config

**Fix:** Add a `require_role("admin")` decorator and apply it to admin-only endpoints:
```python
async def require_admin(request: Request):
    session = await auth.require_session(request)
    if not session or session["role"] != "admin":
        raise HTTPException(403, "Admin required")
```
**Priority:** P1

### SEC-04 — No brute-force protection on login
**Risk:** An attacker can make unlimited login attempts. At 100 req/s this cracks a 6-digit PIN in minutes.  
**Fix:** Add per-IP lockout after 10 failed attempts within 5 minutes using an in-memory counter (or Redis if available). Log all failures.  
**Priority:** P1

### SEC-05 — config.yaml may contain credentials readable by ech user
**Risk:** MQTT passwords, APRS passcodes, and Winlink passwords are stored in plaintext in `/etc/ech/config.yaml`. Any process running as the `ech` user can read these.  
**Fix:** For now, file permissions are already `root:ech 640`. Longer term, support `ECH_MQTT_PASSWORD` environment variable overrides and document that secrets should not be in config.yaml.  
**Priority:** P2

### SEC-06 — Session tokens not invalidated on password change
**Risk:** After a password change, all existing sessions remain valid for up to 12 hours. An attacker who stole a session token remains authenticated even after the victim changes their password.  
**Fix:** In `auth.change_password()`, call `db.delete_sessions_for_user(username)` to invalidate all outstanding sessions.  
**Priority:** P2

---

## MEDIUM

### SEC-07 — Logs endpoint exposes sensitive operational data
**Risk:** `GET /api/logs` returns all system log entries including callsigns, IP addresses, and error details. No role check.  
**Fix:** Restrict to authenticated operators, and consider redacting callsigns from publicly-accessible log downloads.  
**Priority:** P2

### SEC-08 — DBLogHandler uses deprecated asyncio.ensure_future loop parameter
**Risk:** `asyncio.ensure_future(..., loop=self._loop)` is deprecated since Python 3.8 and removed in Python 3.12. On Python 3.12+ the system crashes on first log write.  
**Fix:** Replace with:
```python
loop = asyncio.get_event_loop()
loop.call_soon_threadsafe(lambda: asyncio.ensure_future(
    self._db.save_log_entry(record.levelname, record.name, msg[:500])
))
```
**Priority:** P2 (becomes P0 on Python 3.12+)

### SEC-09 — No CSRF protection
**Risk:** A malicious page can submit forms to `/api/auth/login` or state-changing API endpoints if a victim visits it while logged in. SameSite=Lax mitigates most but not all cases.  
**Fix:** Add a CSRF token to forms and verify it server-side on all state-changing POST requests.  
**Priority:** P2 for internet-facing deployments; lower for LAN-only.

### SEC-10 — Message body has no length validation
**Risk:** A malicious operator can send a 10MB message body to `/api/messages`. The body gets stored in SQLite and broadcast to all WebSocket clients, potentially causing OOM on low-RAM Pi targets.  
**Fix:** Add `if len(body) > 1000: raise HTTPException(400, "Message too long")` in the send endpoint.  
**Priority:** P2

### SEC-11 — Adapter config API allows arbitrary file write
**Risk:** `POST /api/adapter-config` and `POST /api/base-location` write to config.yaml via YAML dump. A crafted request could write unexpected YAML keys, potentially enabling YAML deserialization gadgets.  
**Fix:** Validate the adapter list structure before writing (check that only expected keys are present). Use an allowlist of valid adapter types.  
**Priority:** P2

---

## LOW / INFORMATIONAL

### SEC-12 — PSKReporter callsign in spots exposes operator location
**Info:** Enabling the PSKReporter overlay reveals the operator's callsign and Maidenhead grid square (accurate to ~120 km × 110 km) to anyone with access to the ECH interface.  
**Fix:** Document this in the Settings page. Optionally add an option to not display own callsign on the map.  
**Priority:** P3

### SEC-13 — Login page shows "Default: admin / admin" hint
**Info:** The original login.html showed default credentials in plain sight. This has been removed in v1.0.0-rc2 and replaced with "Change default password in Settings → Users after first login."  
**Status:** Fixed in this release.

### SEC-14 — No audit log for authentication events
**Info:** Failed logins are logged at WARNING level but not to a separate tamper-evident audit trail. This matters for incident response and FCC Part 97 accountability.  
**Fix:** Write AUTH events (login success, failure, logout, password change) to a separate `auth_log` table in SQLite with IP address and timestamp.  
**Priority:** P3

### SEC-15 — Service restart over sudo without audit
**Info:** `POST /api/system/services/{service}/restart` runs `sudo systemctl restart {service}`. This is whitelisted, but there is no audit trail of who triggered a restart and when.  
**Fix:** Log service restarts to the database with username and timestamp.  
**Priority:** P3

---

## Deployment Checklist (for Communications Director)

Before activating for real incidents:

- [ ] SEC-01: Change admin password immediately
- [ ] SEC-02: Enable TLS if on any non-isolated network
- [ ] SEC-03: Create operator accounts; do not give operators admin role
- [ ] SEC-04: Verify the system is behind a firewall on 8765 (not internet-exposed)
- [ ] Verify `/etc/ech/config.yaml` contains no real passwords before GitHub backup
- [ ] Verify operator callsign is set (Settings → click "OP:" in header)
- [ ] Test PSKReporter rate limiting doesn't exceed 1 req/5 min per IP

---
*Generated by security review of ECH v1.0.0-rc2, 2026-06-20*
