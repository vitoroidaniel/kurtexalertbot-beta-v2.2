# Kurtex Alert Bot

Truck Maintenance Command Center — Telegram bot for managing driver alerts and cases.

## What changed in this rework

**Root cause of the freezing / missed tags:** every case create/assign/close
rewrote the *entire* `cases.json` file, synchronously, directly on the
event loop. As case history grew, that read-modify-write got slower — and
because it ran on the loop (not a background thread), it could stall the
**whole bot** for a moment on every single alert, not just the chat that
triggered it. A tag that "landed" 8–9 minutes after a previous one could
simply arrive while the loop was busy rewriting a large JSON file (or while
the dashboard's dev server was doing the same on a separate thread and
contending for disk/GIL time).

Fixes in this version:

1. **SQLite instead of whole-file JSON** (`storage/db.py`, `storage/case_store.py`,
   `storage/user_store.py`) — every write is now a single indexed SQL
   statement, not a full-file rewrite. Still one file on your Railway Volume,
   no separate DB server.
2. **Nothing blocks the event loop** — all DB calls go through
   `asyncio.to_thread()`. User lookups (checked on every private message)
   are served from an in-memory cache that's invalidated on writes, so the
   hottest path does zero disk I/O after startup.
3. **Handlers can't wedge** — `alert_handler.handle()` and friends are
   wrapped so one malformed update logs an error instead of leaving a lock
   or driver-cooldown state stuck.
4. **Timeouts on every Telegram API call** — set once on the
   `Application.builder()` in `bot.py` (`connect_timeout`, `read_timeout`,
   `write_timeout`, `pool_timeout`), so a slow/hung call to Telegram can't
   hang a handler indefinitely.
5. **A watchdog** — logs a warning if the event loop falls more than a few
   seconds behind schedule, plus a heartbeat log line every 5 minutes, so a
   stall is visible in Railway logs instead of silent.
6. **Zero hardcoded people** — no Telegram IDs, names, or usernames in the
   source anymore (see "Users, roles, and shifts" below).
7. **Removed dead files and code** — see "Dead code" below.

`dashboard.py` was intentionally **not** touched in this pass — see
"About the dashboard" below for what that means for you right now.

## Users, roles, and shifts

Nobody is hardcoded. Every person who can use the bot is a row in the
database, added at runtime:

```
/adduser <user_id> <name> <role> [shift]
```

- **role**: `developer` | `super_admin` | `agent`
- **shift**: `ALL` (default, always on duty) | `R` (regular/day) |
  `AH` (after hours) | `M` (morning/overnight)

Example: `/adduser 123456789 Alex agent AH`

Other management commands (developer/super_admin only):
- `/editrole <user_id> <role>` — change someone's role
- `/editshift <user_id> <shift>` — change someone's shift
- `/removeuser <user_id>` — remove someone
- `/listusers` — see everyone and their role/shift
- Forward any message from someone to the bot in a private chat and it'll
  offer role buttons (shift defaults to `ALL`, editable after with `/editshift`)

Shift *time windows* (what hours `R`/`AH`/`M` actually mean) live in
`shifts.py` as plain numbers — no personal data, safe to edit and commit.

### First deploy / bootstrapping yourself

Since there are no hardcoded admins, you need one way in. Set `DEVELOPER_ID`
in Railway env vars to your own Telegram user ID before first deploy — the
bot will register you as `developer` automatically on startup. From there,
add everyone else with `/adduser` or by forwarding their messages.

## Migrating existing data

If your Railway Volume already has `cases.json` / `users.json` /
`active_alerts.json` from the old version, **just deploy this version as-is**.
On first startup it detects those files, imports everything into the new
SQLite database, and renames the originals to `*.json.imported` (kept as a
backup, not read again). Check the Railway logs after the first deploy for
lines like `Migrated N cases from legacy cases.json into SQLite`.

## Fleet registry & reassignment history

Two additions on top of the core rework:

**`trucks` / `trailers` / `reefers`** — a lightweight registry, one row per
physical unit: just `unit_number` + `notes`, identical shape across all
three. Units are auto-registered the first time someone reports an issue
against them in `/report` (unit number is normalized — uppercased, trimmed,
punctuation stripped — so `"trl 204"` and `"TRL-204"` resolve to the same
unit). Cases link to a specific unit via `truck_id`/`trailer_id`/`reefer_id`
(exact match) rather than only the free-text `unit_number` column (which is
still kept, for display and backward compatibility with the dashboard's
existing free-text search).

**`case_assignments`** — full ownership history per case: who had it, when,
and why they lost it (`reassigned` or `closed`). `cases.agent_id` still shows
only the *current* holder (fast, denormalized, what `/mycases` etc. use);
this table is what lets you answer "how many cases has agent X had taken
away from them" or "how long did the previous agent sit on this before it
got reassigned."

Also fixed along the way: `response_secs` (time to first response) used to
get silently overwritten with the time-to-*reassignment* every time a case
changed hands — it's now only ever set on the first assignment.

## About the dashboard

`dashboard.py` is otherwise unchanged from what you uploaded — only
`load_cases()` was touched, swapped from reading the now-dead `cases.json`
to calling `storage.case_store.get_all_cases()`. Since that function returns
dicts with the same field names the old JSON had, everything else in the
2900+ lines — `/api/stats`, `/api/cases`, `/api/unit`, `/api/issue_search`,
`/api/fleet`, `/api/agent`, `/api/my_profile` — works exactly as before,
now backed by real data again instead of a file that no longer gets written.

`bot.py` also now calls `init_db()` once, synchronously, before starting the
dashboard thread — the dashboard runs in its own thread independent of the
bot's async startup sequence, so without this a request could theoretically
race in before the schema existed.

The `/api/unit`, `/api/issue_search`, and `/api/fleet` endpoints still do
their own free-text grouping by `unit_number`/`vehicle_type` on the cases
table — that continues to work as-is. The new `truck_id`/`trailer_id`/`reefer_id`
FKs and `storage/fleet_store.py` (`top_problem_units()`, `get_case_history_for_unit()`)
are there whenever you want to move those endpoints to exact-match joins
instead of string matching — not required for what you already have to work.

Splitting the dashboard into its own repo/service with separate HTML/CSS/JS
is still the next step whenever you're ready — not done in this pass.

## A note on git history

The old `shifts.py` had real Telegram user IDs, names, and usernames
hardcoded in it. Removing them from the current file (done here) does
**not** remove them from git history — if this repo was ever pushed to
GitHub, those values are still recoverable from old commits, even in a
private repo. If that matters to you, options are: make a fresh repo from
this cleaned-up code (simplest), or rewrite history with something like
`git filter-repo` on the existing one.

## Project structure

```
bot.py                    — entry point, handler registration, watchdog
config.py                 — env-var driven config (token, group IDs, etc.)
shifts.py                 — shift time windows only, no personal data
shift_manager.py          — resolves current shift + on-duty users
storage/
  db.py                   — SQLite engine, schema, one-time JSON migration
  case_store.py            — case CRUD + assignment history (async, off the event loop)
  user_store.py            — user/role/shift CRUD (cached reads)
  fleet_store.py            — truck/trailer/reefer registry
handlers/
  alert_handler.py         — driver-group trigger words → alerts
  admin_handler.py         — /report, /adduser, /editrole, /editshift, ...
  agent_handler.py         — /mycases, /done, /casehistory, /mystats
  report_handler.py        — the guided fleet-report conversation
  scheduler.py             — daily report, escalation, heartbeat jobs
dashboard.py               — Flask dashboard (unchanged in this pass)
```

## Data storage

All data lives in one SQLite file on a Railway Volume:

| Path | Contents |
|---|---|
| `/app/data/kurtex.db` | Users, cases, and the active-alerts snapshot |

## Railway deployment

### 1. Add a Volume

In your Railway project → **New** → **Volume**
- Mount path: `/app/data`
- Attach to your bot service

### 2. Required environment variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `DRIVER_GROUP_ID` | Telegram group ID where drivers post alerts |
| `REPORTS_GROUP_ID` | Telegram group ID where case reports are sent |
| `DEVELOPER_ID` | Your own Telegram user ID — bootstraps you as `developer` on first boot |
| `AI_ALERTS_CHANNEL_ID` | Optional — channel ID for AI-detected alerts |
| `DATA_DIR` | Optional — defaults to `/app/data` (matches volume mount) |

### 3. Deploy

Push to GitHub, connect to Railway, add env vars, deploy.
Railway uses `python bot.py` as the start command per `railway.json`.

## Trigger words

Post any of these in the driver group to create an alert:
- `#maintenance`
- `#repairs`
- `#repair`

## Commands (private chat only)

| Command | Who |
|---|---|
| `/start` | Register with the bot |
| `/mycases` | Your active cases |
| `/done` | Today's closed cases |
| `/casehistory` | Full case history |
| `/mystats` | Your personal stats |
| `/shifts` | Current shift roster |
| `/help` | All commands |
| `/report` | Daily summary (super admin / developer) |
| `/leaderboard` | Weekly top performers (super admin / developer) |
| `/missed` | Unhandled alerts (super admin / developer) |
| `/adduser` | Add a user by ID, role, and shift (super admin / developer) |
| `/removeuser` | Remove a user (super admin / developer) |
| `/editrole` | Change a user's role (super admin / developer) |
| `/editshift` | Change a user's shift (super admin / developer) |
| `/listusers` | List all users, roles, shifts (super admin / developer) |

## Performance — verified, not assumed

Every claim below was actually measured against a seeded SQLite database (5000
historical cases across 12 agents, one agent given 15 concurrently open
cases), not estimated:

- **`/mycases`** (and the "remaining cases" nudge after closing one) used to
  fetch an agent's *entire case history* and filter it in Python for active
  ones — meaning it got slower every year, forever. Now `get_open_cases_for_agent()`
  does an indexed `WHERE agent_id=? AND status IN ('assigned','reported')`
  scan directly. Measured: **11.77ms → 0.79ms** on the same dataset, and
  critically, it now scales with *how many cases an agent has open right
  now* (stays flat around 10-15), not with total history.
- **`/mystats`** used to pull every case an agent has ever touched into
  Python and run four separate list comprehensions over it. Now
  `get_agent_stats()` computes today/week/all-time counts in one indexed
  SQL aggregate query. Measured: **~1ms**.
- **`/casehistory`** used to refetch an agent's *entire closed-case history*
  on every single Next/Prev click, just to slice out 5 rows for that page —
  the same "grows slower forever" bug as `/mycases` had, but worse, since
  every page turn paid the full cost, not just the first load. Now:
  - `/casehistory` opens with a quick date-range picker (Today / This week /
    This month / All time) instead of dumping everything at once.
  - Pagination uses SQL `LIMIT`/`OFFSET` (`get_closed_cases_for_agent_page()`)
    scoped to the chosen range, backed by a composite
    `(agent_id, status, closed_at)` index — each page turn only ever reads
    the ~5 rows it displays.
  - Measured on 8000 seeded cases spread across 400 days: paging to a deep
    page (page 500) went from **324ms** (old: fetch all 8000, sort, slice in
    Python) to **0.93ms** (new: indexed `LIMIT`/`OFFSET`) — and stayed flat
    (0.6-0.9ms) at every depth tested, from page 0 to page 1000. The old
    approach gets worse every year as history grows; this one doesn't.
- A composite index `(agent_id, status)` backs all of the above.

Everything else that made the original bot freeze is covered earlier in
this README: SQLite instead of whole-file JSON, `asyncio.to_thread()` on
every DB call, a cached user-auth lookup (checked on every private message),
connect/read/write timeouts on every Telegram API call, try/except around
every handler entry point, and the event-loop watchdog.

## Dead code

Ran an actual call-graph audit (not a guess) — every function checked for
real callers across the whole codebase, tracing through the async-wrapper
pattern so nothing was flagged just for being called indirectly.

**Files removed:**
- `clear_commands.py` — unused standalone script, not imported anywhere
- `crash_report.py` — not imported/used anywhere; sent crash alerts to an
  unrelated external web service (`KURTEX_WEB_URL`) that doesn't exist in
  this project. Reintroduce it if you actually stand that service up.
- `user_tracker.py` — not imported/used anywhere; superseded by the users
  table (a "registered" user already implies "started").
- `handlers/alert_handler.py.bak`, `handlers/report_handler.py.bak` — stale
  backup copies.
- `download` — a stray one-line file (accidental duplicate of `.gitignore`
  content), not referenced anywhere.

**Code removed:**
- `shifts.py`'s hardcoded `ADMINS` / `SUPER_ADMINS` / `MAIN_ADMIN_ID` — see
  "Users, roles, and shifts" above.
- `get_active_case_for_agent()` / `async_get_active_case_for_agent()` —
  `/mycases` never actually used it (it fetches + filters differently)
- `get_all_cases_for_agent()` / its async wrapper — dead now that
  `/mycases`, `/mystats`, `/casehistory`, and the post-close notification
  all use the indexed open/closed queries above instead
- `get_alert_recipients()` in `user_store.py` — superseded by
  `shift_manager`'s on-shift filtering, never called
- Several forward-looking `fleet_store.py` functions (`get_unit`,
  `list_units`, `update_unit`, `top_problem_units`, `get_case_history_for_unit`)
  that had no caller yet — trimmed down to just `get_or_create_unit()`,
  which is what `/report` actually calls. If/when the dashboard needs
  unit search or "most reported unit," it's a couple of indexed one-liners
  against `cases.truck_id`/`trailer_id`/`reefer_id` — same shape as the
  `get_open_cases_for_agent()` queries above.
- Unused imports (`MessageHandler`, `filters`, `uuid`) left over from the
  original codebase

Re-ran the audit after each removal to confirm nothing was left dangling,
and a full regression test (every remaining public function in `storage/`,
exercised end to end) still passes.

## Reliability — self-healing, not just self-reporting

The watchdog from the first pass only *warned* about a stalled event loop —
useful for diagnosis, but it didn't actually fix anything, and Railway's
`ON_FAILURE` restart policy only fires on a crash (non-zero exit), not on
"still running but stuck." A handful of real gaps closed here, each found
by actually tracing what happens on failure, not assumed:

- **Silent error handler → fixed.** Any unhandled exception used to just get
  logged — the person got no response at all, which from their side is
  *indistinguishable from the original freezing complaint*, even when the
  bot is perfectly healthy. It now sends a short "something went wrong,
  try again" message in private chats (never into a driver group), wrapped
  so a failure to notify can't itself cause a second exception.
- **Watchdog now self-heals, not just logs.** A single severe stall
  (~20s+) or four consecutive moderate lag warnings now force an
  immediate process exit — handing off to Railway's restart policy on
  purpose, since a compromised event loop shouldn't try to repair itself.
  `WATCHDOG_LAG_SEVERE_SECONDS` and `WATCHDOG_CHRONIC_STREAK` in `bot.py`
  are the two knobs if you want to tune sensitivity.
- **`/report` had no timeout.** An agent who started a report and walked
  away stayed stuck in that conversation state forever, unable to cleanly
  start over without knowing to type `/cancel`. Added
  `conversation_timeout=1800` (30 min) with a dedicated cleanup handler
  that does the same state cleanup as `/cancel`, but automatically.
- **One malformed alert record could silently block escalation for every
  other pending alert, forever.** The escalation loop had no per-alert
  isolation — an exception on one record (bad timestamp, missing field,
  whatever) would raise out of the whole job, meaning the *next* run would
  hit the exact same record and fail the exact same way, every 5 minutes,
  never checking any of the other genuinely pending alerts in the
  meantime. Each alert is now checked in its own try/except — verified
  with a test using one deliberately corrupted record alongside one valid
  one; the valid one still escalated correctly.
- **One unguarded `int()` on callback data** (`cb_histpage`) — the only
  callback parser across the whole codebase that wasn't defensive. Now
  falls back to page 0 instead of raising.
- **Daily DB checkpoint + backup**, off-peak (4am ET). WAL mode
  checkpoints opportunistically on its own, but not on any guaranteed
  schedule — this forces one, and also writes a full consistent snapshot
  to a second file (`kurtex_backup.db`) via `VACUUM INTO`, so a corrupted
  primary file has a same-day fallback. Verified the backup is a valid,
  independently queryable SQLite file with the correct row counts, not
  just "a file got created."

Every fix above was actually exercised, not just written and assumed
correct: the malformed-record test, the backup-file verification, and the
existing full regression suite were all re-run after these changes.

**What's intentionally still Railway's job, not the bot's:** process-level
crash recovery (`restartPolicyType: ON_FAILURE`, 10 retries — already set
in `railway.json`), and network-level retry during Telegram polling
(handled internally by `python-telegram-bot`, not something to reinvent).

**One known residual risk, out of scope for this pass:** `dashboard.py`
still runs Flask's development server in a background thread — fine for
now since it's read-only and lightweight, but Flask's dev server is
explicitly not meant for production and doesn't have the same crash
isolation as the bot's own error handling. Splitting it into its own
service behind a real WSGI server (gunicorn/waitress) is still the
recommended next step, deferred per earlier discussion.
