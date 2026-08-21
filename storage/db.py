"""
storage/db.py — Single SQLite database for everything (cases, users, active alerts).

Why SQLite instead of whole-file JSON:
  - Every write used to rewrite cases.json / users.json in full. As history grew,
    that full read+json.dumps+write happened synchronously ON THE EVENT LOOP for
    every single message — a bot with a year of case history could stall the
    entire bot (all chats, not just one) for a noticeable moment on every alert.
  - SQLite gives indexed, incremental reads/writes — a write only touches the
    rows that changed, not the whole file.
  - Still just one file on the Railway Volume. No separate DB server needed.

Concurrency model:
  - All access goes through asyncio.to_thread() (see the `run` helper) so DB
    I/O never blocks the event loop, no matter how big the table gets.
  - Each worker thread gets its own connection (threading.local) — sqlite3
    connections aren't safe to share across threads.
  - WAL mode lets reads and writes happen concurrently without blocking each
    other, and a busy_timeout means concurrent writers wait briefly instead
    of raising "database is locked".
"""

import asyncio
import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / os.getenv("DB_FILE", "kurtex.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    username    TEXT DEFAULT '',
    role        TEXT NOT NULL,
    shift       TEXT NOT NULL DEFAULT 'ALL',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cases (
    id                TEXT PRIMARY KEY,
    driver_name       TEXT,
    driver_username   TEXT,
    group_name        TEXT,
    description       TEXT,
    opened_at         TEXT,
    assigned_at       TEXT,
    closed_at         TEXT,
    agent_id          INTEGER,
    agent_name        TEXT,
    agent_username    TEXT,
    status            TEXT DEFAULT 'open',
    notes             TEXT,
    report_msg_id     INTEGER,
    response_secs     INTEGER,
    resolution_secs   INTEGER,
    reassigned        INTEGER DEFAULT 0,
    vehicle_type      TEXT,
    unit_number       TEXT,
    report_driver     TEXT,
    issue_text        TEXT,
    load_type         TEXT,
    location          TEXT,
    priority          TEXT,
    pickup            TEXT,
    delivery          TEXT,
    comments          TEXT,
    setpoint          TEXT,
    current_temp      TEXT,
    temp_recorder     TEXT,
    truck_id          INTEGER REFERENCES trucks(id),
    trailer_id        INTEGER REFERENCES trailers(id),
    reefer_id         INTEGER REFERENCES reefers(id)
);
CREATE INDEX IF NOT EXISTS idx_cases_agent   ON cases(agent_id);
CREATE INDEX IF NOT EXISTS idx_cases_status  ON cases(status);
CREATE INDEX IF NOT EXISTS idx_cases_opened  ON cases(opened_at);
CREATE INDEX IF NOT EXISTS idx_cases_agent_status ON cases(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_cases_agent_status_closed ON cases(agent_id, status, closed_at);
-- NOTE: indexes on truck_id/trailer_id/reefer_id are created separately,
-- AFTER _migrate_missing_columns() runs — see init_db(). On a fresh DB the
-- CREATE TABLE above already includes those columns so it wouldn't matter,
-- but on an existing DB from before the fleet tables existed, `cases`
-- already exists and CREATE TABLE IF NOT EXISTS is a no-op, so those
-- columns don't exist yet at this point in the script.

-- Fleet registry: one row per physical unit. No "status" column on purpose —
-- whether a unit is currently down is always derivable from cases (WHERE
-- truck_id=? AND status IN ('assigned','reported')), so there's nothing here
-- that could drift out of sync with the case data. Just a registry.
-- Just a registry: unit_number + optional notes. All three tables are the
-- same shape — every unit (truck, trailer, reefer) is identified by its
-- unit number, nothing else.
CREATE TABLE IF NOT EXISTS trucks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_number TEXT NOT NULL UNIQUE,
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trailers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_number TEXT NOT NULL UNIQUE,
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reefers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_number TEXT NOT NULL UNIQUE,
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- Reassignment / ownership history. cases.agent_id stays the CURRENT holder
-- (fast, denormalized, used by /mycases etc.) — this table is the full trail:
-- who had it before, how long they had it, and why they lost it.
CREATE TABLE IF NOT EXISTS case_assignments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    agent_id        INTEGER NOT NULL,
    agent_name      TEXT,
    agent_username  TEXT,
    assigned_at     TEXT NOT NULL,
    unassigned_at   TEXT,
    unassign_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignments_case  ON case_assignments(case_id);
CREATE INDEX IF NOT EXISTS idx_assignments_agent ON case_assignments(agent_id);

-- Small generic key/value table for the active-alerts snapshot (in-flight,
-- not-yet-assigned alerts) so a restart can recover them. This table stays
-- tiny (a handful of open alerts at any time), unlike cases which grows
-- forever, so it's fine to write it as one JSON blob per write.
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread (threads come from asyncio.to_thread's pool)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_missing_columns(conn)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_cases_truck   ON cases(truck_id);
        CREATE INDEX IF NOT EXISTS idx_cases_trailer ON cases(trailer_id);
        CREATE INDEX IF NOT EXISTS idx_cases_reefer  ON cases(reefer_id);
    """)
    conn.commit()
    logger.info(f"Database ready at {DB_PATH}")
    _migrate_legacy_json()


def _migrate_missing_columns(conn: sqlite3.Connection) -> None:
    """
    SQLite has no 'ADD COLUMN IF NOT EXISTS', so on a DB created by an earlier
    version of this schema, add any new columns by hand. Safe to run on every
    startup — it's a no-op once the columns exist.

    Note: if you deployed an earlier version that had make/model/year/vin,
    or status, on the fleet tables, those columns are left in place (SQLite's
    DROP COLUMN support is version-dependent and not worth the risk) — they
    just sit there unused going forward. Doesn't affect anything.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(cases)")}
    additions = {
        "truck_id":   "INTEGER REFERENCES trucks(id)",
        "trailer_id": "INTEGER REFERENCES trailers(id)",
        "reefer_id":  "INTEGER REFERENCES reefers(id)",
    }
    for col, decl in additions.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {decl}")
            logger.info(f"Migrated schema: added cases.{col}")
    conn.commit()


async def run(fn, *args, **kwargs):
    """Run a blocking DB function off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ── One-time migration from the old whole-file JSON storage ──────────────────

def _migrate_legacy_json() -> None:
    """
    If this volume still has the old cases.json / users.json / active_alerts.json
    from before the SQLite rework, import them once, then rename them to .imported
    so this never runs again (and the raw JSON stays around as a backup).
    """
    import json

    conn = get_conn()

    users_file = DATA_DIR / "users.json"
    if users_file.exists():
        try:
            data = json.loads(users_file.read_text(encoding="utf-8"))
            n = 0
            for uid, u in data.items():
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, name, username, role, shift) "
                    "VALUES (?, ?, ?, ?, 'ALL')",
                    (int(uid), u.get("name", ""), u.get("username", ""), u.get("role", "agent")),
                )
                n += 1
            conn.commit()
            users_file.rename(users_file.with_suffix(".json.imported"))
            logger.info(f"Migrated {n} users from legacy users.json into SQLite")
        except Exception as e:
            logger.error(f"Legacy users.json migration failed: {e}")

    cases_file = DATA_DIR / "cases.json"
    if cases_file.exists():
        try:
            cases = json.loads(cases_file.read_text(encoding="utf-8"))
            n = 0
            for c in cases:
                cols = [
                    "id", "driver_name", "driver_username", "group_name", "description",
                    "opened_at", "assigned_at", "closed_at", "agent_id", "agent_name",
                    "agent_username", "status", "notes", "report_msg_id", "response_secs",
                    "resolution_secs", "reassigned", "vehicle_type", "unit_number",
                    "report_driver", "issue_text", "load_type", "location", "priority",
                    "pickup", "delivery", "comments", "setpoint", "current_temp", "temp_recorder",
                ]
                values = [c.get(col) for col in cols]
                if "reassigned" in c:
                    idx = cols.index("reassigned")
                    values[idx] = 1 if c.get("reassigned") else 0
                placeholders = ", ".join(["?"] * len(cols))
                conn.execute(
                    f"INSERT OR IGNORE INTO cases ({', '.join(cols)}) VALUES ({placeholders})",
                    values,
                )
                n += 1
            conn.commit()
            cases_file.rename(cases_file.with_suffix(".json.imported"))
            logger.info(f"Migrated {n} cases from legacy cases.json into SQLite")
        except Exception as e:
            logger.error(f"Legacy cases.json migration failed: {e}")

    alerts_file = DATA_DIR / "active_alerts.json"
    if alerts_file.exists():
        try:
            raw = alerts_file.read_text(encoding="utf-8")
            conn.execute(
                "INSERT INTO kv (key, value) VALUES ('active_alerts', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (raw,),
            )
            conn.commit()
            alerts_file.rename(alerts_file.with_suffix(".json.imported"))
            logger.info("Migrated active_alerts.json into SQLite")
        except Exception as e:
            logger.error(f"Legacy active_alerts.json migration failed: {e}")


# ── Resilience: periodic checkpoint + backup ──────────────────────────────────
# WAL mode auto-checkpoints on its own once the WAL file grows large enough,
# but that's opportunistic, not guaranteed on any schedule. This does two
# things on a schedule (see handlers/scheduler.py's daily maintenance job):
#   1. Force a WAL checkpoint so the WAL file doesn't grow unbounded over a
#      long uptime.
#   2. Write a full backup to a second file on the same volume via
#      VACUUM INTO (a consistent, defragmented snapshot, safe to run while
#      the bot keeps writing to the live DB). If the primary file is ever
#      corrupted, this is a same-day fallback — not a substitute for real
#      off-volume backups, but better than nothing living only in one file.

def checkpoint_and_backup() -> None:
    conn = get_conn()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    backup_path = DATA_DIR / "kurtex_backup.db"
    tmp_path    = DATA_DIR / "kurtex_backup.db.tmp"
    if tmp_path.exists():
        tmp_path.unlink()
    conn.execute(f"VACUUM INTO '{tmp_path}'")
    tmp_path.replace(backup_path)  # atomic on the same filesystem
    logger.info(f"DB checkpoint + backup complete -> {backup_path}")


async def async_checkpoint_and_backup():
    return await run(checkpoint_and_backup)
