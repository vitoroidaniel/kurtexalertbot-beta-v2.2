"""
storage/case_store.py — SQLite-backed case storage (Railway Volume).

This replaces the old whole-file JSON store, which read+rewrote the ENTIRE
case history on every single create/assign/close/report — the main cause of
the bot freezing as history grew. Now every operation is a single indexed
SQL statement, and every DB call runs through asyncio.to_thread() so it can
never block the event loop no matter how large the table gets.

Two layers:
  - Plain functions (`create_case`, `assign_case`, ...): synchronous, do the
    real SQL work. Safe to call from a worker thread or from a script.
  - `async_*` functions: thin async wrappers used by handlers — these are
    what you `await` from bot code.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from storage.db import get_conn, run

logger = logging.getLogger(__name__)

_CASE_COLUMNS = [
    "id", "driver_name", "driver_username", "group_name", "description",
    "opened_at", "assigned_at", "closed_at", "agent_id", "agent_name",
    "agent_username", "status", "notes", "report_msg_id", "response_secs",
    "resolution_secs", "reassigned", "vehicle_type", "unit_number",
    "report_driver", "issue_text", "load_type", "location", "priority",
    "pickup", "delivery", "comments", "setpoint", "current_temp", "temp_recorder",
    "truck_id", "trailer_id", "reefer_id",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["reassigned"] = bool(d.get("reassigned"))
    return d


# ── Cases — write ─────────────────────────────────────────────────────────────

def create_case(
    case_id: str,
    driver_name: str,
    driver_username: Optional[str],
    group_name: str,
    description: str,
) -> dict:
    case = {
        "id":              case_id,
        "driver_name":     driver_name,
        "driver_username": driver_username,
        "group_name":      group_name,
        "description":     description,
        "opened_at":       now_iso(),
        "status":          "open",
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO cases (id, driver_name, driver_username, group_name, description, "
        "opened_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (case_id, driver_name, driver_username, group_name, description,
         case["opened_at"], "open"),
    )
    conn.commit()
    logger.info(f"Case {case_id} created")
    return case


def assign_case(case_id: str, agent_id: int, agent_name: str, agent_username: Optional[str]) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT opened_at, response_secs FROM cases WHERE id = ?", (case_id,)
    ).fetchone()
    if not row:
        logger.warning(f"assign_case: {case_id} not found")
        return None
    assigned_at = now_iso()

    # response_secs is "time to FIRST response" — don't let a later
    # reassignment overwrite it with the time-to-reassignment instead.
    if row["response_secs"] is None:
        response_secs = int(
            (datetime.fromisoformat(assigned_at) - datetime.fromisoformat(row["opened_at"])).total_seconds()
        )
        conn.execute(
            "UPDATE cases SET assigned_at=?, agent_id=?, agent_name=?, agent_username=?, "
            "status='assigned', response_secs=? WHERE id=?",
            (assigned_at, agent_id, agent_name, agent_username, response_secs, case_id),
        )
    else:
        conn.execute(
            "UPDATE cases SET assigned_at=?, agent_id=?, agent_name=?, agent_username=?, "
            "status='assigned' WHERE id=?",
            (assigned_at, agent_id, agent_name, agent_username, case_id),
        )

    # Close out whoever held it before (if anyone), then open a new
    # assignment row for the new agent — this is the full ownership trail,
    # separate from cases.agent_id which only ever shows the current holder.
    conn.execute(
        "UPDATE case_assignments SET unassigned_at=?, unassign_reason='reassigned' "
        "WHERE case_id=? AND unassigned_at IS NULL",
        (assigned_at, case_id),
    )
    conn.execute(
        "INSERT INTO case_assignments (case_id, agent_id, agent_name, agent_username, assigned_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (case_id, agent_id, agent_name, agent_username, assigned_at),
    )
    conn.commit()
    logger.info(f"Case {case_id} assigned to {agent_name}")
    return get_case(case_id)


def report_case(case_id: str, notes: Optional[str] = "case reported") -> Optional[dict]:
    conn = get_conn()
    cur = conn.execute("UPDATE cases SET status='reported', notes=? WHERE id=?", (notes, case_id))
    conn.commit()
    if not cur.rowcount:
        return None
    return get_case(case_id)


def close_case(case_id: str, notes: Optional[str] = None) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT assigned_at FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        return None
    closed_at       = now_iso()
    resolution_secs = None
    if row["assigned_at"]:
        resolution_secs = int(
            (datetime.fromisoformat(closed_at) - datetime.fromisoformat(row["assigned_at"])).total_seconds()
        )
    conn.execute(
        "UPDATE cases SET closed_at=?, status='done', notes=?, resolution_secs=? WHERE id=?",
        (closed_at, notes, resolution_secs, case_id),
    )
    conn.execute(
        "UPDATE case_assignments SET unassigned_at=?, unassign_reason='closed' "
        "WHERE case_id=? AND unassigned_at IS NULL",
        (closed_at, case_id),
    )
    conn.commit()
    logger.info(f"Case {case_id} closed")
    return get_case(case_id)


def mark_missed(case_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE cases SET status='missed' WHERE id=? AND status IN ('open','assigned')",
        (case_id,),
    )
    conn.commit()


def set_report_msg_id(case_id: str, msg_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE cases SET report_msg_id=? WHERE id=?", (msg_id, case_id))
    conn.commit()


def mark_reassigned(case_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE cases SET reassigned=1 WHERE id=?", (case_id,))
    conn.commit()


# ── Active-case card tracking ────────────────────────────────────────────────
# The "📋 Active Case" DM card (Solve / Report / Reassign buttons) can be sent
# to an admin from several places (assignment, /mycases, reassign-accept...).
# We remember the *latest* copy sent so that once a report actually goes
# through (either the old chat flow or the Report Mini App), we can strip its
# buttons — otherwise the same card sits there inviting a second, duplicate
# report for the same case.

def set_active_card(case_id: str, chat_id: int, message_id: int) -> None:
    import json
    conn = get_conn()
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"active_card:{case_id}", json.dumps({"chat_id": chat_id, "message_id": message_id})),
    )
    conn.commit()


def get_active_card(case_id: str) -> Optional[dict]:
    import json
    conn = get_conn()
    row = conn.execute("SELECT value FROM kv WHERE key=?", (f"active_card:{case_id}",)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def clear_active_card(case_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM kv WHERE key=?", (f"active_card:{case_id}",))
    conn.commit()


async def async_set_active_card(case_id, chat_id, message_id):
    return await run(set_active_card, case_id, chat_id, message_id)


def update_case_fields(case_id: str, **fields) -> Optional[dict]:
    """Generic column update — used for the fleet-report fields collected
    in the /report conversation (vehicle_type, unit_number, location, ...)."""
    fields = {k: v for k, v in fields.items() if k in _CASE_COLUMNS}
    if not fields:
        return get_case(case_id)
    conn = get_conn()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    cur = conn.execute(
        f"UPDATE cases SET {set_clause} WHERE id=?", (*fields.values(), case_id)
    )
    conn.commit()
    if not cur.rowcount:
        return None
    return get_case(case_id)


# ── Cases — read ──────────────────────────────────────────────────────────────

def get_case(case_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_cases_for_agent_today(agent_id: int) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM cases WHERE agent_id=? AND assigned_at LIKE ?",
        (agent_id, f"{today}%"),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_open_cases_for_agent(agent_id: int) -> list[dict]:
    """Just this agent's currently active cases — an indexed range scan on
    (agent_id, status), not a fetch of their whole history filtered in
    Python. Stays fast no matter how much case history piles up."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM cases WHERE agent_id=? AND status IN ('assigned','reported') "
        "ORDER BY opened_at",
        (agent_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_closed_cases_for_agent_page(
    agent_id: int, offset: int, limit: int,
    start_iso: Optional[str] = None, end_iso: Optional[str] = None,
) -> list[dict]:
    """One page of this agent's closed cases, newest first — LIMIT/OFFSET at
    the SQL level, optionally scoped to a date range. Each page turn only
    ever reads `limit` rows, not the agent's whole history (that's the
    difference from fetching everything and slicing in
    Python — this stays flat-cost no matter how many pages deep you go, or
    how many years of history pile up)."""
    conn   = get_conn()
    query  = "SELECT * FROM cases WHERE agent_id=? AND status='done'"
    params = [agent_id]
    if start_iso:
        query += " AND closed_at >= ?"
        params.append(start_iso)
    if end_iso:
        query += " AND closed_at < ?"
        params.append(end_iso)
    query += " ORDER BY closed_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_closed_cases_for_agent(
    agent_id: int, start_iso: Optional[str] = None, end_iso: Optional[str] = None,
) -> int:
    """Indexed COUNT for the pagination footer ('Page X of Y') — same WHERE
    clause as get_closed_cases_for_agent_page, no LIMIT."""
    conn   = get_conn()
    query  = "SELECT COUNT(*) AS n FROM cases WHERE agent_id=? AND status='done'"
    params = [agent_id]
    if start_iso:
        query += " AND closed_at >= ?"
        params.append(start_iso)
    if end_iso:
        query += " AND closed_at < ?"
        params.append(end_iso)
    return conn.execute(query, params).fetchone()["n"]


def get_agent_stats(agent_id: int) -> dict:
    """Today/this-week/all-time counts in a single indexed pass over just
    this agent's rows — one aggregate query instead of pulling every case
    this agent has ever touched into Python and counting it four different
    ways (what /mystats used to do)."""
    today      = datetime.now(timezone.utc).date().isoformat()
    week_start = (datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())).date().isoformat()
    conn = get_conn()
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN assigned_at LIKE ? THEN 1 ELSE 0 END) AS today_assigned,
            SUM(CASE WHEN assigned_at LIKE ? AND status='done' THEN 1 ELSE 0 END) AS today_closed,
            SUM(CASE WHEN assigned_at >= ? THEN 1 ELSE 0 END) AS week_assigned,
            SUM(CASE WHEN assigned_at >= ? AND status='done' THEN 1 ELSE 0 END) AS week_closed,
            COUNT(*) AS all_total,
            SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS all_closed
        FROM cases WHERE agent_id=?
        """,
        (f"{today}%", f"{today}%", week_start, week_start, agent_id),
    ).fetchone()
    return {
        "today_assigned": row["today_assigned"] or 0,
        "today_closed":   row["today_closed"] or 0,
        "week_assigned":  row["week_assigned"] or 0,
        "week_closed":    row["week_closed"] or 0,
        "all_total":      row["all_total"] or 0,
        "all_closed":     row["all_closed"] or 0,
    }


def get_cases_today() -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    conn = get_conn()
    rows = conn.execute("SELECT * FROM cases WHERE opened_at LIKE ?", (f"{today}%",)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_cases_this_week() -> list[dict]:
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(days=now.weekday())).date().isoformat()
    conn = get_conn()
    rows = conn.execute("SELECT * FROM cases WHERE opened_at >= ?", (start,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_cases() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM cases ORDER BY opened_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Active alerts — small JSON blob, kept in the kv table ─────────────────────
# This dict only ever holds *currently unassigned* alerts (a handful at a
# time), never the full history, so writing it as one blob per write is fine.

def save_active_alerts(alerts: dict) -> None:
    import json
    serialisable = {}
    for aid, record in alerts.items():
        r = dict(record)
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
        if isinstance(r.get("last_escalated_at"), datetime):
            r["last_escalated_at"] = r["last_escalated_at"].isoformat()
        serialisable[aid] = r
    conn = get_conn()
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('active_alerts', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(serialisable, default=str),),
    )
    conn.commit()


def load_active_alerts() -> dict:
    import json
    conn = get_conn()
    row = conn.execute("SELECT value FROM kv WHERE key='active_alerts'").fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"Failed to parse active_alerts: {e}")
        return {}


# ── Async wrappers — use these from handlers ──────────────────────────────────

async def async_create_case(case_id, driver_name, driver_username, group_name, description):
    return await run(create_case, case_id, driver_name, driver_username, group_name, description)

async def async_assign_case(case_id, agent_id, agent_name, agent_username):
    return await run(assign_case, case_id, agent_id, agent_name, agent_username)

async def async_report_case(case_id, notes=None):
    return await run(report_case, case_id, notes)

async def async_close_case(case_id, notes=None):
    return await run(close_case, case_id, notes)

async def async_mark_missed(case_id):
    return await run(mark_missed, case_id)

async def async_get_case(case_id):
    return await run(get_case, case_id)

async def async_get_cases_for_agent_today(agent_id):
    return await run(get_cases_for_agent_today, agent_id)

async def async_get_open_cases_for_agent(agent_id):
    return await run(get_open_cases_for_agent, agent_id)

async def async_get_closed_cases_for_agent_page(agent_id, offset, limit, start_iso=None, end_iso=None):
    return await run(get_closed_cases_for_agent_page, agent_id, offset, limit, start_iso, end_iso)

async def async_count_closed_cases_for_agent(agent_id, start_iso=None, end_iso=None):
    return await run(count_closed_cases_for_agent, agent_id, start_iso, end_iso)

async def async_get_agent_stats(agent_id):
    return await run(get_agent_stats, agent_id)

async def async_get_cases_today():
    return await run(get_cases_today)

async def async_get_cases_this_week():
    return await run(get_cases_this_week)

async def async_set_report_msg_id(case_id, msg_id):
    return await run(set_report_msg_id, case_id, msg_id)

async def async_mark_reassigned(case_id):
    return await run(mark_reassigned, case_id)

async def async_update_case_fields(case_id, **fields):
    return await run(update_case_fields, case_id, **fields)

async def async_save_active_alerts(alerts):
    return await run(save_active_alerts, alerts)

async def async_load_active_alerts():
    return await run(load_active_alerts)
