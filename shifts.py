"""
shifts.py — Shift time windows only.

There are NO hardcoded admins/users/IDs in this file (or anywhere else in the
bot) anymore. Who's on duty is entirely data-driven: every user is added at
runtime via /adduser or the forward-to-add flow and stored in the database
(storage/user_store.py) with a `shift` code (see VALID_SHIFTS there).

This file only defines the wall-clock windows for each shift code and which
days they apply to. Edit the times below to match your operation — they're
just numbers, not secrets, so it's fine for them to live in code. Everything
that used to be personal (names, Telegram IDs, usernames) now lives only in
the database on your Railway Volume.

All times are in TIMEZONE below.
"""

from datetime import time

TIMEZONE = "America/Chicago"

# `code` must match one of the VALID_SHIFTS in storage/user_store.py
# (ALL / R / AH / M). Users tagged "ALL" are always considered on shift,
# regardless of which window is currently active (handled in shift_manager.py).
SHIFTS = [
    {
        "name":  "Regular (day)",
        "code":  "R",
        "start": time(6, 30),    # 6:30 AM
        "end":   time(16, 0),    # 4:00 PM
        "days":  [0, 1, 2, 3, 4],  # Mon-Fri
    },
    {
        "name":  "After hours",
        "code":  "AH",
        "start": time(16, 0),    # 4:00 PM
        "end":   time(23, 0),    # 11:00 PM
        "days":  [0, 1, 2, 3, 4],
    },
    {
        "name":  "Morning / overnight",
        "code":  "M",
        "start": time(23, 0),    # 11:00 PM
        "end":   time(7, 0),     # 7:00 AM (next day)
        "days":  [0, 1, 2, 3, 4, 5, 6],  # every day
    },
]
