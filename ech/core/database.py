"""
ech/core/database.py
--------------------
Async SQLite database layer (aiosqlite + raw SQL — no ORM overhead).
Schema is append-only for the message log (records are never deleted,
matching records management best practice for operational logs).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from ech.core.models import NormalizedMessage

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    source_adapter  TEXT NOT NULL,
    source_channel  TEXT NOT NULL,
    from_id         TEXT NOT NULL,
    from_display    TEXT NOT NULL,
    to_id           TEXT,
    body            TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    lat             REAL,
    lon             REAL,
    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp
    ON messages (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_messages_adapter
    ON messages (source_adapter, timestamp DESC);

-- Hourly message-count rollup, written alongside every save_message() insert.
-- Survives purge_old_messages() deleting the raw rows, so analytics (message
-- volume by adapter) still has history well past each adapter's retention
-- window — e.g. "100 APRS messages yesterday" even though APRS bodies are
-- purged after 12h.
CREATE TABLE IF NOT EXISTS message_stats_hourly (
    bucket   TEXT NOT NULL,   -- '2026-07-13T14:00:00Z'
    adapter  TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, adapter)
);

CREATE TABLE IF NOT EXISTS contacts (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    sms_number      TEXT,
    aprs_callsign   TEXT,
    meshtastic_id   TEXT,
    meshcore_id     TEXT,
    tags            TEXT DEFAULT '',   -- comma-separated
    notes           TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username       TEXT PRIMARY KEY,
    pw_hash        TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'operator',
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL,
    expires     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv_store (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sim_nodes (
    id          TEXT PRIMARY KEY,
    adapter     TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    config_json TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sim_messages (
    id          TEXT PRIMARY KEY,
    adapter     TEXT NOT NULL,
    from_id     TEXT NOT NULL,
    body        TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    interval_sec REAL NOT NULL DEFAULT 30.0,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS log_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    level       TEXT NOT NULL,
    logger      TEXT NOT NULL,
    message     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_timestamp ON log_entries (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions (token);

CREATE TABLE IF NOT EXISTS mesh_nodes (
    node_id      TEXT NOT NULL,
    adapter      TEXT NOT NULL,
    display_name TEXT NOT NULL,
    lat          REAL,
    lon          REAL,
    last_heard   TEXT,
    meta_json    TEXT,
    PRIMARY KEY (node_id, adapter)
);

CREATE TABLE IF NOT EXISTS anomaly_findings (
    id              TEXT PRIMARY KEY,
    adapter         TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    rule            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    evidence_json   TEXT,
    timestamp       TEXT NOT NULL,
    acknowledged    INTEGER NOT NULL DEFAULT 0,
    broadcast_sent  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_anomaly_timestamp ON anomaly_findings (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_rule ON anomaly_findings (rule, acknowledged);

CREATE TABLE IF NOT EXISTS broadcast_log (
    id              TEXT PRIMARY KEY,
    operator        TEXT NOT NULL,
    template_id     TEXT,
    body            TEXT NOT NULL,
    group_id        TEXT,
    adapters        TEXT NOT NULL,   -- JSON list
    recipient_count INTEGER NOT NULL DEFAULT 0,
    sent_at         TEXT NOT NULL,
    delivery_json   TEXT             -- {adapter: bool, ...}
);

CREATE TABLE IF NOT EXISTS qso_log (
    id              TEXT PRIMARY KEY,
    station_id      TEXT NOT NULL DEFAULT '1',
    callsign        TEXT NOT NULL,
    band            TEXT NOT NULL,
    mode            TEXT NOT NULL,
    freq_mhz        REAL,
    sent_rst        TEXT NOT NULL DEFAULT '59',
    rcvd_rst        TEXT NOT NULL DEFAULT '59',
    sent_exch       TEXT NOT NULL DEFAULT '',
    rcvd_exch       TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    timestamp       TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual',
    source_adapter  TEXT,
    contest         TEXT NOT NULL DEFAULT 'GENERAL',
    pota_ref        TEXT NOT NULL DEFAULT '',
    sota_ref        TEXT NOT NULL DEFAULT '',
    uploaded_qrz    INTEGER NOT NULL DEFAULT 0,
    uploaded_clublog INTEGER NOT NULL DEFAULT 0,
    uploaded_lotw   INTEGER NOT NULL DEFAULT 0,
    uploaded_pota   INTEGER NOT NULL DEFAULT 0,
    uploaded_sota   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_qso_timestamp ON qso_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_qso_contest   ON qso_log (contest, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_qso_callsign  ON qso_log (callsign);

CREATE TABLE IF NOT EXISTS hamlog_stations (
    station_id TEXT PRIMARY KEY,
    operator TEXT NOT NULL DEFAULT '',
    band TEXT NOT NULL DEFAULT '20M',
    mode TEXT NOT NULL DEFAULT 'PH',
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hamlog_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    station_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    from_id     TEXT NOT NULL,
    from_display TEXT NOT NULL,
    command     TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '',
    adapter     TEXT NOT NULL DEFAULT '',
    response    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_bot_activity_ts ON bot_activity (timestamp DESC);

CREATE TABLE IF NOT EXISTS skywarn_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT NOT NULL,
    from_id        TEXT NOT NULL,
    from_display   TEXT NOT NULL,
    callsign       TEXT NOT NULL,
    adapter        TEXT NOT NULL DEFAULT '',
    source_channel TEXT NOT NULL DEFAULT '',
    spotter_id     TEXT,
    location       TEXT,
    event_type     TEXT,
    temp_f         TEXT,
    wind_mph       TEXT,
    wind_dir       TEXT,
    hail_size      TEXT,
    precip         TEXT,
    notes          TEXT,
    completed      INTEGER NOT NULL DEFAULT 0,
    raw_text       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skywarn_ts ON skywarn_reports (timestamp DESC);
"""


class Database:
    def __init__(self, path: str | Path = "ech.db"):
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()
        log.info("Database: connected (%s)", self._path)

    async def _migrate(self) -> None:
        # Add must_change_pw to users table if upgrading from older schema
        try:
            await self._db.execute("ALTER TABLE users ADD COLUMN must_change_pw INTEGER NOT NULL DEFAULT 0")
            await self._db.commit()
        except Exception:
            pass  # column already exists

        new_qso_cols = [
            ("name",     "TEXT NOT NULL DEFAULT ''"),
            ("power",    "TEXT NOT NULL DEFAULT ''"),
            ("state",    "TEXT NOT NULL DEFAULT ''"),
            ("country",  "TEXT NOT NULL DEFAULT ''"),
            ("county",   "TEXT NOT NULL DEFAULT ''"),
            ("time_off", "TEXT NOT NULL DEFAULT ''"),
            ("grid",     "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, defn in new_qso_cols:
            try:
                await self._db.execute(f"ALTER TABLE qso_log ADD COLUMN {col} {defn}")
                await self._db.commit()
            except Exception:
                pass  # column already exists

        # Add newer skywarn_reports columns if upgrading from an older schema
        for col in ("spotter_id", "location", "event_type", "hail_size"):
            try:
                await self._db.execute(f"ALTER TABLE skywarn_reports ADD COLUMN {col} TEXT")
                await self._db.commit()
            except Exception:
                pass  # column already exists (or table doesn't exist yet — CREATE below adds it)
        try:
            await self._db.execute("ALTER TABLE skywarn_reports ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
            await self._db.commit()
        except Exception:
            pass  # column already exists

        # Prune expired sessions on startup
        await self.prune_expired_sessions()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Messages ──────────────────────────────────────────────────────────

    async def save_message(self, msg: NormalizedMessage) -> None:
        import json
        # Merge routing fields into raw_json so they survive the round-trip
        raw = dict(msg.raw or {})
        if msg.hop_count is not None:
            raw["_hop_count"] = msg.hop_count
        if msg.hop_start is not None:
            raw["_hop_start"] = msg.hop_start
        if msg.path:
            raw["_path"] = msg.path
        if msg.via_mqtt:
            raw["_via_mqtt"] = True
        if msg.msg_type and msg.msg_type != "text":
            raw["_msg_type"] = msg.msg_type
        cur = await self._db.execute(
            """
            INSERT OR IGNORE INTO messages
                (id, source_adapter, source_channel, from_id, from_display,
                 to_id, body, timestamp, priority, lat, lon, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                msg.id, msg.source_adapter, msg.source_channel,
                msg.from_id, msg.from_display or msg.from_id,
                msg.to_id, msg.body,
                msg.timestamp.isoformat(),
                int(msg.priority),
                msg.lat, msg.lon,
                json.dumps(raw) if raw else None,
            ),
        )
        # Only bump the rollup when this id wasn't already stored — the id-based
        # dedup above (belt-and-suspenders behind Router._is_duplicate) would
        # otherwise let a retried/duplicate save double-count the same message.
        if cur.rowcount:
            bucket = msg.timestamp.strftime("%Y-%m-%dT%H:00:00Z")
            await self._db.execute(
                """
                INSERT INTO message_stats_hourly (bucket, adapter, count) VALUES (?, ?, 1)
                ON CONFLICT(bucket, adapter) DO UPDATE SET count = count + 1
                """,
                (bucket, msg.source_adapter),
            )
        await self._db.commit()

    async def get_messages(
        self,
        limit: int = 100,
        offset: int = 0,
        adapter: str | None = None,
        since: str | None = None,
        priority_min: int | None = None,
        from_id: str | None = None,
        per_adapter_limit: int | None = None,
    ) -> list[dict]:
        clauses, params = [], []
        if adapter:
            clauses.append("source_adapter = ?")
            params.append(adapter)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        if priority_min is not None:
            clauses.append("priority >= ?")
            params.append(priority_min)
        if from_id:
            clauses.append("from_id = ?")
            params.append(from_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        if per_adapter_limit and not adapter:
            # A flat ORDER BY timestamp DESC LIMIT lets one high-volume adapter
            # (an internet APRS-IS gateway can run 8000+/day) crowd every other
            # adapter's messages out of the window entirely — the "most recent
            # N" can silently be 100% one adapter's noise. Cap each adapter's
            # contribution first (window function), so every adapter is
            # guaranteed representation regardless of relative volume, then
            # take the most recent `limit` of that balanced set.
            query = (
                f"SELECT * FROM ("
                f"  SELECT *, ROW_NUMBER() OVER ("
                f"    PARTITION BY source_adapter ORDER BY timestamp DESC"
                f"  ) AS _rn FROM messages {where}"
                f") WHERE _rn <= ? ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            )
            run_params = params + [per_adapter_limit, limit, offset]
        else:
            query = f"SELECT * FROM messages {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            run_params = params + [limit, offset]

        async with self._db.execute(query, run_params) as cur:
            rows = await cur.fetchall()
        import json as _json
        result = []
        for r in rows:
            row = dict(r)
            row.pop("_rn", None)  # window-function ordinal from the per-adapter-fairness query; not real data
            # Extract routing fields saved by save_message
            raw = _json.loads(row.get("raw_json") or "{}")
            row["hop_count"]        = raw.pop("_hop_count", None)
            row["hop_start"]        = raw.pop("_hop_start", None)
            row["path"]             = raw.pop("_path", None)
            row["via_mqtt"]         = bool(raw.pop("_via_mqtt", False))
            row["delivery_status"]  = raw.pop("_delivery_status", None)
            row["delivery_path"]    = raw.pop("_delivery_path", None)
            row["msg_type"]         = raw.pop("_msg_type", "text")
            row["snr"]              = raw.get("snr")
            row["rssi"]             = raw.get("rssi")
            result.append(row)
        return result

    async def update_delivery_status(self, msg_id: str, status: str,
                                      path: list | None = None) -> None:
        """Persist delivery status (and optional relay path) into raw_json."""
        import json as _json
        async with self._db.execute(
            "SELECT raw_json FROM messages WHERE id = ?", (msg_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        raw = _json.loads(row[0] or "{}")
        raw["_delivery_status"] = status
        if path is not None:
            raw["_delivery_path"] = path
        await self._db.execute(
            "UPDATE messages SET raw_json = ? WHERE id = ?",
            (_json.dumps(raw), msg_id),
        )
        await self._db.commit()

    async def message_count(self) -> int:
        async with self._db.execute("SELECT COUNT(*) FROM messages") as cur:
            row = await cur.fetchone()
        return row[0]

    # ── Analytics ────────────────────────────────────────────────────────────

    async def get_message_time_buckets(self, hours: int = 48) -> list[dict]:
        """Message counts per hour per adapter, from the message_stats_hourly
        rollup rather than the raw messages table — this survives per-adapter
        retention purging, so a 7-day view still shows e.g. APRS volume even
        though APRS bodies are purged after 12h."""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:00:00Z")
        async with self._db.execute(
            """
            SELECT bucket, adapter, count
            FROM message_stats_hourly
            WHERE bucket >= ?
            ORDER BY bucket ASC
            """,
            (since,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_command_usage_stats(self, hours: int | None = None) -> list[dict]:
        clause, params = "", []
        if hours is not None:
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            clause = "WHERE timestamp >= ?"
            params.append(since)
        async with self._db.execute(
            f"""
            SELECT command, COUNT(*) AS count
            FROM bot_activity
            {clause}
            GROUP BY command
            ORDER BY count DESC
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_anomaly_time_buckets(self, hours: int = 48) -> list[dict]:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with self._db.execute(
            """
            SELECT strftime('%Y-%m-%dT%H:00:00Z', timestamp) AS bucket,
                   severity, COUNT(*) AS count
            FROM anomaly_findings
            WHERE timestamp >= ?
            GROUP BY bucket, severity
            ORDER BY bucket ASC
            """,
            (since,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def clear_messages(self) -> int:
        async with self._db.execute("SELECT COUNT(*) FROM messages") as cur:
            row = await cur.fetchone()
        count = row[0]
        await self._db.execute("DELETE FROM messages")
        await self._db.commit()
        log.info("Database: cleared %d messages", count)
        return count

    # ── Contacts ──────────────────────────────────────────────────────────

    async def get_contacts(self, search: str | None = None) -> list[dict]:
        if search:
            q = f"%{search}%"
            async with self._db.execute(
                "SELECT * FROM contacts WHERE display_name LIKE ? OR aprs_callsign LIKE ? ORDER BY display_name",
                (q, q),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._db.execute("SELECT * FROM contacts ORDER BY display_name") as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def upsert_contact(self, contact: dict) -> None:
        import uuid
        now = datetime.now(timezone.utc).isoformat()
        contact.setdefault("id", str(uuid.uuid4()))
        contact.setdefault("created_at", now)
        contact["updated_at"] = now
        await self._db.execute(
            """
            INSERT INTO contacts
                (id, display_name, sms_number, aprs_callsign, meshtastic_id,
                 meshcore_id, tags, notes, created_at, updated_at)
            VALUES (:id, :display_name, :sms_number, :aprs_callsign, :meshtastic_id,
                    :meshcore_id, :tags, :notes, :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                sms_number=excluded.sms_number,
                aprs_callsign=excluded.aprs_callsign,
                meshtastic_id=excluded.meshtastic_id,
                meshcore_id=excluded.meshcore_id,
                tags=excluded.tags,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            contact,
        )
        await self._db.commit()

    async def delete_contact(self, contact_id: str) -> None:
        await self._db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        await self._db.commit()

    # ── Anomaly findings ──────────────────────────────────────────────────

    async def save_anomaly(self, finding) -> None:
        import json
        await self._db.execute(
            """INSERT OR IGNORE INTO anomaly_findings
                (id, adapter, node_id, rule, severity, summary,
                 evidence_json, timestamp, acknowledged, broadcast_sent)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                finding.id, finding.adapter, finding.node_id,
                finding.rule, finding.severity.value, finding.summary,
                json.dumps(finding.evidence),
                finding.timestamp.isoformat(),
                int(finding.acknowledged),
                int(finding.broadcast_sent),
            ),
        )
        await self._db.commit()

    async def get_anomalies(self, acknowledged: bool | None = None,
                             limit: int = 200) -> list[dict]:
        if acknowledged is None:
            clause = ""
        elif acknowledged:
            clause = "WHERE acknowledged = 1"
        else:
            clause = "WHERE acknowledged = 0"
        async with self._db.execute(
            f"SELECT * FROM anomaly_findings {clause} ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        import json as _json
        results = []
        for r in rows:
            d = dict(r)
            raw = d.pop("evidence_json", None)
            try:
                d["evidence"] = _json.loads(raw) if raw else {}
            except Exception:
                d["evidence"] = {}
            results.append(d)
        return results

    async def acknowledge_anomaly(self, finding_id: str) -> None:
        await self._db.execute(
            "UPDATE anomaly_findings SET acknowledged = 1 WHERE id = ?",
            (finding_id,),
        )
        await self._db.commit()

    async def get_anomaly(self, finding_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM anomaly_findings WHERE id = ?", (finding_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def merge_anomaly_evidence(self, finding_id: str, extra: dict) -> dict | None:
        """Merge keys into a finding's evidence_json (used by anomaly verification
        to attach live-trace results to an existing finding)."""
        import json as _json
        row = await self.get_anomaly(finding_id)
        if row is None:
            return None
        try:
            evidence = _json.loads(row.get("evidence_json") or "{}")
        except Exception:
            evidence = {}
        evidence.update(extra)
        await self._db.execute(
            "UPDATE anomaly_findings SET evidence_json = ? WHERE id = ?",
            (_json.dumps(evidence), finding_id),
        )
        await self._db.commit()
        row["evidence_json"] = _json.dumps(evidence)
        return row

    async def get_hop_stats(self, adapter: str, from_id: str | None = None,
                            hours: int = 168) -> dict:
        """Hop-count distribution from persisted messages (raw_json $._hop_count) —
        the durable baseline the anomaly engine compares against, instead of its
        20-message in-memory window that resets on every restart."""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        clauses = ["source_adapter = ?", "timestamp >= ?",
                   "json_extract(raw_json, '$._hop_count') IS NOT NULL"]
        params: list = [adapter, since]
        if from_id:
            clauses.append("from_id = ?")
            params.append(from_id)
        async with self._db.execute(
            f"SELECT CAST(json_extract(raw_json, '$._hop_count') AS INTEGER) AS hc "
            f"FROM messages WHERE {' AND '.join(clauses)} ORDER BY hc",
            params,
        ) as cur:
            rows = await cur.fetchall()
        hops = [r["hc"] for r in rows if r["hc"] is not None and 0 <= r["hc"] <= 64]
        if not hops:
            return {"count": 0, "median": None, "p95": None, "max": None}
        return {
            "count": len(hops),
            "median": hops[len(hops) // 2],
            "p95": hops[min(int(len(hops) * 0.95), len(hops) - 1)],
            "max": hops[-1],
        }

    # ── KV store ─────────────────────────────────────────────────────────

    async def get_kv(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM kv_store WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        from datetime import datetime, timezone
        await self._db.execute(
            "INSERT INTO kv_store(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat())
        )
        await self._db.commit()

    # ── Users ─────────────────────────────────────────────────────────────

    async def get_users(self) -> list[dict]:
        async with self._db.execute("SELECT username,role,created_at FROM users") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_user(self, username: str) -> dict | None:
        async with self._db.execute("SELECT * FROM users WHERE username=?", (username,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_user(self, user: dict) -> None:
        must_change = 1 if user.get("must_change_pw") else 0
        await self._db.execute(
            "INSERT INTO users(username,pw_hash,role,must_change_pw,created_at) "
            "VALUES(:username,:pw_hash,:role,:must_change_pw,datetime('now')) "
            "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, role=excluded.role, "
            "must_change_pw=excluded.must_change_pw",
            {**user, "must_change_pw": must_change},
        )
        await self._db.commit()

    async def update_user_password(self, username: str, pw_hash: str) -> None:
        await self._db.execute(
            "UPDATE users SET pw_hash=?, must_change_pw=0 WHERE username=?",
            (pw_hash, username),
        )
        # SEC-06: invalidate all existing sessions so old credentials stop working
        await self._db.execute("DELETE FROM sessions WHERE username=?", (username,))
        await self._db.commit()

    async def delete_user(self, username: str) -> None:
        await self._db.execute("DELETE FROM users WHERE username=?", (username,))
        await self._db.commit()

    # ── Sessions ──────────────────────────────────────────────────────────

    async def create_session(self, token: str, username: str, role: str, expires) -> None:
        await self._db.execute(
            "INSERT INTO sessions(token,username,role,expires) VALUES(?,?,?,?)",
            (token, username, role, expires.isoformat())
        )
        await self._db.commit()

    async def get_session(self, token: str) -> dict | None:
        async with self._db.execute("SELECT * FROM sessions WHERE token=?", (token,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_session(self, token: str) -> None:
        await self._db.execute("DELETE FROM sessions WHERE token=?", (token,))
        await self._db.commit()

    async def delete_sessions_for_user(self, username: str) -> int:
        """Revoke all active sessions for a user. Returns number of rows deleted."""
        cur = await self._db.execute("DELETE FROM sessions WHERE username=?", (username,))
        await self._db.commit()
        return cur.rowcount

    async def get_active_sessions(self) -> list[dict]:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        async with self._db.execute(
            "SELECT username, role, expires FROM sessions WHERE expires > ? ORDER BY username",
            (now,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def prune_expired_sessions(self) -> int:
        """Delete sessions whose expiry timestamp has passed. Returns rows deleted."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
        await self._db.commit()
        if cur.rowcount:
            log.info("Database: pruned %d expired session(s)", cur.rowcount)
        return cur.rowcount

    async def purge_old_messages(self, retention: dict[str, int]) -> int:
        """Delete messages older than per-adapter retention windows.

        retention: {adapter_pattern: hours}  e.g. {"aprs": 12, "meshcore": 36}
        Patterns are prefix-matched against source_adapter (case-insensitive).
        Returns total rows deleted.
        """
        from datetime import datetime, timezone, timedelta
        total = 0
        for pattern, hours in retention.items():
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            cur = await self._db.execute(
                "DELETE FROM messages WHERE LOWER(source_adapter) LIKE ? AND timestamp < ?",
                (f"{pattern.lower()}%", cutoff),
            )
            if cur.rowcount:
                log.info("Database: purged %d messages from %s* (>%dh)", cur.rowcount, pattern, hours)
            total += cur.rowcount
        if total:
            await self._db.commit()
            await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return total

    async def purge_old_stats(self, days: int = 90) -> int:
        """Trim message_stats_hourly beyond `days`. Independent of (and much
        longer-lived than) per-adapter message retention — one row per
        adapter per hour is negligible, so history is kept far longer than
        the raw message bodies it summarizes."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:00:00Z")
        cur = await self._db.execute(
            "DELETE FROM message_stats_hourly WHERE bucket < ?", (cutoff,)
        )
        if cur.rowcount:
            await self._db.commit()
            log.info("Database: purged %d old message_stats_hourly row(s) (>%dd)", cur.rowcount, days)
        return cur.rowcount

    # ── Simulation ────────────────────────────────────────────────────────

    async def upsert_mesh_node(self, adapter: str, node) -> None:
        """Persist a MeshNode to the mesh_nodes table."""
        import json as _json
        meta = _json.dumps(node.meta) if getattr(node, "meta", None) else None
        lh = node.last_heard.isoformat() if getattr(node, "last_heard", None) else None
        await self._db.execute(
            """INSERT INTO mesh_nodes(node_id, adapter, display_name, lat, lon, last_heard, meta_json)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(node_id, adapter) DO UPDATE SET
                 display_name = excluded.display_name,
                 lat          = COALESCE(excluded.lat, lat),
                 lon          = COALESCE(excluded.lon, lon),
                 last_heard   = COALESCE(excluded.last_heard, last_heard),
                 meta_json    = COALESCE(excluded.meta_json, meta_json)""",
            (node.node_id, adapter, node.display_name,
             getattr(node, "lat", None), getattr(node, "lon", None), lh, meta),
        )
        await self._db.commit()

    async def get_mesh_nodes(self, adapter: str) -> list[dict]:
        """Return all persisted nodes for a given adapter."""
        async with self._db.execute(
            "SELECT node_id, display_name, lat, lon, last_heard, meta_json "
            "FROM mesh_nodes WHERE adapter=?", (adapter,)
        ) as cur:
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    async def get_sim_nodes(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM sim_nodes ORDER BY adapter,display_name") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def upsert_sim_node(self, node: dict) -> None:
        import uuid, json as _json
        node.setdefault("id", str(uuid.uuid4()))
        node["config_json"] = _json.dumps(node.get("config_json", {}))
        await self._db.execute(
            "INSERT INTO sim_nodes(id,adapter,node_id,display_name,lat,lon,config_json,enabled) "
            "VALUES(:id,:adapter,:node_id,:display_name,:lat,:lon,:config_json,:enabled) "
            "ON CONFLICT(id) DO UPDATE SET "
            "display_name=excluded.display_name, lat=excluded.lat, lon=excluded.lon, "
            "config_json=excluded.config_json, enabled=excluded.enabled",
            node
        )
        await self._db.commit()

    async def delete_sim_node(self, node_id: str) -> None:
        await self._db.execute("DELETE FROM sim_nodes WHERE id=?", (node_id,))
        await self._db.commit()

    async def get_sim_messages(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM sim_messages ORDER BY adapter,from_id") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def upsert_sim_message(self, msg: dict) -> None:
        import uuid
        msg.setdefault("id", str(uuid.uuid4()))
        await self._db.execute(
            "INSERT INTO sim_messages(id,adapter,from_id,body,priority,interval_sec,enabled) "
            "VALUES(:id,:adapter,:from_id,:body,:priority,:interval_sec,:enabled) "
            "ON CONFLICT(id) DO UPDATE SET "
            "body=excluded.body, priority=excluded.priority, "
            "interval_sec=excluded.interval_sec, enabled=excluded.enabled",
            msg
        )
        await self._db.commit()

    async def delete_sim_message(self, msg_id: str) -> None:
        await self._db.execute("DELETE FROM sim_messages WHERE id=?", (msg_id,))
        await self._db.commit()

    # ── Log entries ───────────────────────────────────────────────────────

    _log_insert_count: int = 0

    async def save_log_entry(self, level: str, logger: str, message: str) -> None:
        from datetime import datetime, timezone
        await self._db.execute(
            "INSERT INTO log_entries(timestamp,level,logger,message) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), level, logger, message[:1000])
        )
        self._log_insert_count += 1
        # Rotate every 200 inserts — keep only the newest 5000 rows
        if self._log_insert_count % 200 == 0:
            await self._db.execute(
                "DELETE FROM log_entries WHERE rowid NOT IN "
                "(SELECT rowid FROM log_entries ORDER BY rowid DESC LIMIT 5000)"
            )
        await self._db.commit()

    async def get_log_entries(self, limit: int = 500, level: str | None = None) -> list[dict]:
        if level:
            async with self._db.execute(
                "SELECT * FROM log_entries WHERE level=? ORDER BY timestamp DESC LIMIT ?",
                (level.upper(), limit)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._db.execute(
                "SELECT * FROM log_entries ORDER BY timestamp DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def execute_raw(self, sql: str, params: tuple = ()) -> int:
        """Execute a raw SQL statement and return the number of rows affected."""
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return cur.rowcount

    # ── Bot activity ──────────────────────────────────────────────────────

    async def save_bot_activity(self, from_id: str, from_display: str,
                                command: str, args: str,
                                adapter: str, response: str) -> None:
        from datetime import datetime, timezone
        await self._db.execute(
            """INSERT INTO bot_activity(timestamp,from_id,from_display,command,args,adapter,response)
               VALUES(?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(),
             from_id, from_display, command, args[:200], adapter, response[:500])
        )
        # Keep last 2000 rows
        await self._db.execute(
            "DELETE FROM bot_activity WHERE id NOT IN "
            "(SELECT id FROM bot_activity ORDER BY id DESC LIMIT 2000)"
        )
        await self._db.commit()

    async def get_bot_activity(self, limit: int = 200, command: str | None = None) -> list[dict]:
        if command:
            async with self._db.execute(
                "SELECT * FROM bot_activity WHERE command=? ORDER BY timestamp DESC LIMIT ?",
                (command.lower(), limit)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._db.execute(
                "SELECT * FROM bot_activity ORDER BY timestamp DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_bot_stats(self) -> dict:
        """Return total count, unique callers, and per-command counts."""
        async with self._db.execute("SELECT COUNT(*) as total FROM bot_activity") as cur:
            total = (await cur.fetchone())["total"]
        async with self._db.execute("SELECT COUNT(DISTINCT from_id) as u FROM bot_activity") as cur:
            unique = (await cur.fetchone())["u"]
        async with self._db.execute(
            "SELECT command, COUNT(*) as cnt FROM bot_activity GROUP BY command ORDER BY cnt DESC"
        ) as cur:
            by_cmd = [dict(r) for r in await cur.fetchall()]
        async with self._db.execute(
            "SELECT * FROM bot_activity ORDER BY timestamp DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            last = dict(row) if row else None
        return {"total": total, "unique_callers": unique, "by_command": by_cmd, "last": last}

    # ── SKYWARN spotter reports ─────────────────────────────────────────────

    async def save_skywarn_report(self, from_id: str, from_display: str, callsign: str,
                                  adapter: str, source_channel: str, raw_text: str,
                                  spotter_id: str | None = None, location: str | None = None,
                                  event_type: str | None = None,
                                  temp_f: str | None = None, wind_mph: str | None = None,
                                  wind_dir: str | None = None, hail_size: str | None = None,
                                  precip: str | None = None, notes: str | None = None) -> None:
        from datetime import datetime, timezone
        await self._db.execute(
            """INSERT INTO skywarn_reports(timestamp,from_id,from_display,callsign,adapter,
               source_channel,spotter_id,location,event_type,temp_f,wind_mph,wind_dir,hail_size,precip,notes,raw_text)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), from_id, from_display, callsign.upper(),
             adapter, source_channel, spotter_id, location, event_type, temp_f, wind_mph,
             wind_dir, hail_size, precip, notes, raw_text[:500])
        )
        await self._db.commit()

    async def update_skywarn_report(self, report_id: int, fields: dict) -> dict | None:
        allowed = {
            "callsign", "spotter_id", "location", "event_type", "temp_f",
            "wind_mph", "wind_dir", "hail_size", "precip", "notes", "raw_text",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if updates:
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            updates["_id"] = report_id
            await self._db.execute(
                f"UPDATE skywarn_reports SET {set_clause} WHERE id = :_id", updates
            )
            await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM skywarn_reports WHERE id = ?", (report_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def set_skywarn_completed(self, report_id: int, completed: bool) -> dict | None:
        await self._db.execute(
            "UPDATE skywarn_reports SET completed = ? WHERE id = ?", (int(completed), report_id)
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM skywarn_reports WHERE id = ?", (report_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_skywarn_report(self, report_id: int) -> bool:
        cur = await self._db.execute("DELETE FROM skywarn_reports WHERE id = ?", (report_id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def get_skywarn_reports(self, limit: int = 200, from_id: str | None = None) -> list[dict]:
        if from_id:
            async with self._db.execute(
                "SELECT * FROM skywarn_reports WHERE from_id=? ORDER BY timestamp DESC LIMIT ?",
                (from_id, limit)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._db.execute(
                "SELECT * FROM skywarn_reports ORDER BY timestamp DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── QSO log ───────────────────────────────────────────────────────────

    async def save_qso(self, qso: dict) -> None:
        await self._db.execute(
            """INSERT OR IGNORE INTO qso_log
               (id, station_id, callsign, band, mode, freq_mhz,
                sent_rst, rcvd_rst, sent_exch, rcvd_exch, notes,
                timestamp, source, source_adapter, contest, pota_ref, sota_ref,
                name, power, state, country, county, time_off, grid)
               VALUES
               (:id, :station_id, :callsign, :band, :mode, :freq_mhz,
                :sent_rst, :rcvd_rst, :sent_exch, :rcvd_exch, :notes,
                :timestamp, :source, :source_adapter, :contest, :pota_ref, :sota_ref,
                :name, :power, :state, :country, :county, :time_off, :grid)""",
            qso,
        )
        await self._db.commit()

    async def get_qsos(
        self,
        contest: str | None = None,
        limit: int = 1000,
        since: str | None = None,
        source: str | None = None,
    ) -> list[dict]:
        clauses = []
        params: list = []
        if contest:
            clauses.append("contest = ?")
            params.append(contest)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with self._db.execute(
            f"SELECT * FROM qso_log {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_qso(self, qso_id: str) -> bool:
        cur = await self._db.execute("DELETE FROM qso_log WHERE id = ?", (qso_id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def update_qso(self, qso_id: str, fields: dict) -> dict | None:
        allowed = {
            "callsign", "band", "mode", "freq_mhz",
            "sent_rst", "rcvd_rst", "sent_exch", "rcvd_exch",
            "notes", "timestamp", "station_id", "pota_ref", "sota_ref",
            "name", "power", "state", "country", "county", "time_off", "grid",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            async with self._db.execute(
                "SELECT * FROM qso_log WHERE id = ?", (qso_id,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        updates["_id"] = qso_id
        await self._db.execute(
            f"UPDATE qso_log SET {set_clause} WHERE id = :_id", updates
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM qso_log WHERE id = ?", (qso_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def mark_qso_uploaded(self, qso_id: str, service: str) -> None:
        col_map = {
            "qrz": "uploaded_qrz",
            "clublog": "uploaded_clublog",
            "lotw": "uploaded_lotw",
            "pota": "uploaded_pota",
            "sota": "uploaded_sota",
        }
        col = col_map.get(service)
        if col:
            await self._db.execute(
                f"UPDATE qso_log SET {col} = 1 WHERE id = ?", (qso_id,)
            )
            await self._db.commit()

    async def get_qso_count(self, contest: str | None = None) -> int:
        where = "WHERE contest = ?" if contest else ""
        params = (contest,) if contest else ()
        async with self._db.execute(
            f"SELECT COUNT(*) FROM qso_log {where}", params
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    # ── Hamlog stations ───────────────────────────────────────────────────────

    async def upsert_station(self, station_id: str, operator: str, band: str, mode: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO hamlog_stations (station_id, operator, band, mode, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(station_id) DO UPDATE SET
                   operator=excluded.operator, band=excluded.band,
                   mode=excluded.mode, last_seen=excluded.last_seen""",
            (station_id, operator, band, mode, now),
        )
        await self._db.commit()
        return {"station_id": station_id, "operator": operator, "band": band, "mode": mode, "last_seen": now}

    async def get_stations(self, active_minutes: int = 5) -> list[dict]:
        async with self._db.execute(
            """SELECT * FROM hamlog_stations
               WHERE last_seen >= datetime('now', ?)
               ORDER BY last_seen DESC""",
            (f"-{active_minutes} minutes",),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Hamlog chat ───────────────────────────────────────────────────────────

    async def save_chat_msg(self, station_id: str, operator: str, text: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._db.execute(
            "INSERT INTO hamlog_chat (timestamp, station_id, operator, text) VALUES (?, ?, ?, ?)",
            (now, station_id, operator, text),
        )
        await self._db.commit()
        return {"id": cur.lastrowid, "timestamp": now, "station_id": station_id, "operator": operator, "text": text}

    async def get_chat_msgs(self, since_id: int = 0, limit: int = 200) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM hamlog_chat WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
