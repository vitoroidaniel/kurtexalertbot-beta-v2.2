"""
storage/fleet_store.py — Truck/trailer/reefer registry.

Each unit is just: unit_number + notes. A registry that lets cases reference
a *specific* unit by ID instead of matching on a free-text unit number
string (which breaks the moment someone types "TRL204" one day and
"TRL-204" the next).

Units are auto-registered the first time someone reports an issue against
them (called from handlers/report_handler.py) — the registry builds itself
from real reports, no manual data entry required to get started.

Only get_or_create_unit is here — that's the only piece anything currently
calls. Search/history-by-unit queries (top_problem_units,
get_case_history_for_unit, etc.) are a couple of indexed one-liners against
cases.truck_id/trailer_id/reefer_id whenever the dashboard is ready to use
them — see README for the query shape.
"""

import logging
import re

from storage.db import get_conn, run

logger = logging.getLogger(__name__)

_TABLES = {"truck": "trucks", "trailer": "trailers", "reefer": "reefers"}


def normalize_unit_number(raw: str) -> str:
    """Uppercase, trim, keep only letters/digits/dashes — so 'trl 204',
    'TRL204', and 'trl-204' all resolve to the same unit."""
    return re.sub(r"[^A-Za-z0-9\-]", "", (raw or "").strip().upper())


def get_or_create_unit(unit_number: str, vtype: str) -> int | None:
    """Returns the fleet row id for this unit, creating it if it doesn't
    exist yet. Returns None if unit_number is empty or vtype is unknown."""
    table = _TABLES.get(vtype)
    unit_number = normalize_unit_number(unit_number)
    if not table or not unit_number:
        return None

    conn = get_conn()
    row = conn.execute(f"SELECT id FROM {table} WHERE unit_number = ?", (unit_number,)).fetchone()
    if row:
        return row["id"]

    cur = conn.execute(f"INSERT INTO {table} (unit_number) VALUES (?)", (unit_number,))
    conn.commit()
    logger.info(f"Fleet unit registered: {vtype} {unit_number}")
    return cur.lastrowid


async def async_get_or_create_unit(unit_number, vtype):
    return await run(get_or_create_unit, unit_number, vtype)
