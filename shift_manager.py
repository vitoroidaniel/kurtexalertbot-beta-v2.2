"""
shift_manager.py - Returns which users are currently on shift.

Reads shift *windows* from shifts.py (just time ranges, no personal data) and
matches them against each user's `shift` code stored in the database
(storage/user_store.py). Users tagged "ALL" are always on shift.
"""

from datetime import datetime
import zoneinfo

from shifts import SHIFTS, TIMEZONE
from storage.user_store import get_all_user_dicts


def _tz():
    try:
        return zoneinfo.ZoneInfo(TIMEZONE)
    except Exception:
        return zoneinfo.ZoneInfo("America/New_York")


def _alert_eligible() -> list[dict]:
    """Pull alert-eligible users (super_admin + agent) from the DB."""
    return [u for u in get_all_user_dicts() if u["role"] in ("super_admin", "agent")]


def get_current_shift() -> dict | None:
    """Returns the active shift-window dict, or None if outside all windows."""
    now      = datetime.now(_tz())
    weekday  = now.weekday()
    now_time = now.time().replace(second=0, microsecond=0)

    for shift in SHIFTS:
        if weekday not in shift["days"]:
            continue
        s, e = shift["start"], shift["end"]
        if s <= e:
            if s <= now_time < e:
                return shift
        else:
            if now_time >= s or now_time < e:
                return shift
    return None


def get_current_shift_name() -> str:
    shift = get_current_shift()
    return shift["name"] if shift else "Off Hours"


def get_on_shift_admins() -> list[dict]:
    """Returns alert-eligible users currently on shift: those tagged ALL,
    plus those whose shift code matches the active window."""
    shift = get_current_shift()
    users = _alert_eligible()
    if not shift:
        return [u for u in users if u["shift"] == "ALL"]
    code = shift["code"]
    return [u for u in users if u["shift"] in ("ALL", code)]


def get_all_admins() -> list[dict]:
    """Returns all alert-eligible users regardless of shift (fallback)."""
    return _alert_eligible()
