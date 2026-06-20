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
    username    TEXT PRIMARY KEY,
    pw_hash     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'operator',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
"""


class Database:
    def __init__(self, path: str | Path = "ech.db"):
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("Database: connected (%s)", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Messages ──────────────────────────────────────────────────────────

    async def save_message(self, msg: NormalizedMessage) -> None:
        import json
        await self._db.execute(
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
                json.dumps(msg.raw) if msg.raw else None,
            ),
        )
        await self._db.commit()

    async def get_messages(
        self,
        limit: int = 100,
        offset: int = 0,
        adapter: str | None = None,
        since: str | None = None,
        priority_min: int | None = None,
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

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params += [limit, offset]

        async with self._db.execute(
            f"SELECT * FROM messages {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def message_count(self) -> int:
        async with self._db.execute("SELECT COUNT(*) FROM messages") as cur:
            row = await cur.fetchone()
        return row[0]

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
        return [dict(r) for r in rows]

    async def acknowledge_anomaly(self, finding_id: str) -> None:
        await self._db.execute(
            "UPDATE anomaly_findings SET acknowledged = 1 WHERE id = ?",
            (finding_id,),
        )
        await self._db.commit()

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
        from datetime import datetime, timezone
        await self._db.execute(
            "INSERT INTO users(username,pw_hash,role,created_at) VALUES(:username,:pw_hash,:role,datetime('now')) "
            "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, role=excluded.role",
            user
        )
        await self._db.commit()

    async def update_user_password(self, username: str, pw_hash: str) -> None:
        await self._db.execute("UPDATE users SET pw_hash=? WHERE username=?", (pw_hash, username))
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

    # ── Simulation ────────────────────────────────────────────────────────

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

    async def save_log_entry(self, level: str, logger: str, message: str) -> None:
        from datetime import datetime, timezone
        await self._db.execute(
            "INSERT INTO log_entries(timestamp,level,logger,message) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), level, logger, message[:1000])
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

    async def execute_raw(self, sql: str, params: tuple = ()) -> None:
        await self._db.execute(sql, params)
        await self._db.commit()
