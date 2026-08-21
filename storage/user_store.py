"""
storage/user_store.py — Dynamic user/role/shift management, SQLite-backed.

Roles:
  developer    — full bot access, NO alert notifications
  super_admin  — full bot access + alerts + report commands
  agent        — standard agent commands + alerts

Shifts (who gets paged when a group's alert fires):
  ALL — always on shift, regardless of time of day (default)
  R   — Regular / day shift
  AH  — After hours
  M   — Morning / overnight

No user IDs, names, or usernames are hardcoded anywhere in the code — every
admin/agent is added at runtime via /adduser or the forward-to-add flow, and
lives only in the database on your Railway Volume.

Reads are served from an in-memory cache (refreshed on every write) because
is_authorized()/has_role() run on *every* private-chat message — the hottest
path in the bot. Writes (adding/editing a user) are rare admin actions, so
they hit SQLite directly; they're small and fast and were never the source
of the freezing (that was the whole-file cases.json rewrite — see case_store.py).
"""

import logging

from storage.db import get_conn

logger = logging.getLogger(__name__)

VALID_ROLES  = {"developer", "super_admin", "agent"}
VALID_SHIFTS = {"ALL", "R", "AH", "M"}

SHIFT_LABELS = {
    "ALL": "All shifts",
    "R":   "Regular (day)",
    "AH":  "After hours",
    "M":   "Morning / overnight",
}

# ── In-memory cache ──────────────────────────────────────────────────────────
_cache: dict[int, dict] | None = None


def _row_to_dict(row) -> dict:
    return {
        "name":     row["name"],
        "username": row["username"] or "",
        "role":     row["role"],
        "shift":    row["shift"] or "ALL",
    }


def _load_cache() -> dict[int, dict]:
    global _cache
    conn = get_conn()
    rows = conn.execute("SELECT id, name, username, role, shift FROM users").fetchall()
    _cache = {row["id"]: _row_to_dict(row) for row in rows}
    return _cache


def _cache_or_load() -> dict[int, dict]:
    if _cache is None:
        return _load_cache()
    return _cache


def prime_cache() -> None:
    """Call once at startup so the very first message doesn't pay for the load."""
    _load_cache()


# ── Reads (cache-backed, no disk I/O after first load) ───────────────────────

def get_user(user_id: int) -> dict | None:
    return _cache_or_load().get(user_id)


def get_all_users() -> dict:
    return {str(uid): u for uid, u in _cache_or_load().items()}


def has_role(user_id: int, *roles: str) -> bool:
    u = get_user(user_id)
    return u is not None and u["role"] in roles


def is_authorized(user_id: int) -> bool:
    """Any registered user can use the bot."""
    return get_user(user_id) is not None


def get_super_admin_ids() -> list[int]:
    return [uid for uid, u in _cache_or_load().items() if u["role"] == "super_admin"]


def get_all_user_dicts() -> list[dict]:
    """All users as list of dicts with id included, for shift_manager compatibility."""
    return [{"id": uid, **u} for uid, u in _cache_or_load().items()]


# ── Writes (hit SQLite directly, then refresh cache) ──────────────────────────

def add_user(user_id: int, name: str, username: str, role: str, shift: str = "ALL") -> bool:
    if role not in VALID_ROLES or shift not in VALID_SHIFTS:
        return False
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (id, name, username, role, shift) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, "
        "role=excluded.role, shift=excluded.shift",
        (user_id, name, username or "", role, shift),
    )
    conn.commit()
    _load_cache()
    logger.info(f"User added/updated: {user_id} ({name}) as {role}/{shift}")
    return True


def remove_user(user_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    if cur.rowcount:
        _load_cache()
        logger.info(f"User removed: {user_id}")
        return True
    return False


def edit_role(user_id: int, role: str) -> bool:
    if role not in VALID_ROLES:
        return False
    conn = get_conn()
    cur = conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    if cur.rowcount:
        _load_cache()
        logger.info(f"Role changed: {user_id} -> {role}")
        return True
    return False


def edit_shift(user_id: int, shift: str) -> bool:
    if shift not in VALID_SHIFTS:
        return False
    conn = get_conn()
    cur = conn.execute("UPDATE users SET shift = ? WHERE id = ?", (shift, user_id))
    conn.commit()
    if cur.rowcount:
        _load_cache()
        logger.info(f"Shift changed: {user_id} -> {shift}")
        return True
    return False


def bootstrap_developer(user_id: int, name: str = "Developer") -> None:
    """
    Called at startup. If DEVELOPER_ID env var is set and that user isn't in
    the store yet, add them as developer automatically. This is the ONLY
    identity that ever comes from an env var — everyone else is added at
    runtime via /adduser or the forward-to-add flow.
    """
    existing = get_user(user_id)
    if not existing:
        add_user(user_id, name, "", "developer", "ALL")
        logger.info(f"Bootstrapped developer account: {user_id}")
    elif existing["role"] != "developer":
        edit_role(user_id, "developer")
