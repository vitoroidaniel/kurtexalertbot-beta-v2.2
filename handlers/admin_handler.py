"""
handlers/admin_handler.py — Admin commands + dynamic user management.

No user is ever hardcoded — every admin/agent/developer is added at runtime
via /adduser, /editrole, /editshift, or by forwarding one of their messages.
"""
import logging
from collections import defaultdict
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

from storage.case_store import (
    async_get_cases_today,
    async_get_cases_this_week,
)
from storage.user_store import (
    get_all_users, get_user, add_user, remove_user, edit_role, edit_shift,
    has_role, VALID_ROLES, VALID_SHIFTS, SHIFT_LABELS,
)


logger   = logging.getLogger(__name__)
BOT_NAME = "Kurtex Alert Bot"


# ── Role helpers ──────────────────────────────────────────────────────────────

def _is_main_admin(user_id: int) -> bool:
    """Super admins and developers can access report commands."""
    return has_role(user_id, "super_admin", "developer")


def _can_manage(user_id: int) -> bool:
    """Developer or super_admin can manage users."""
    return has_role(user_id, "developer", "super_admin")


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except Exception:
        return iso[:16]


def _build_daily_report(cases: list[dict], title: str) -> str:
    total    = len(cases)
    assigned = [c for c in cases if c["status"] in ("assigned", "reported", "done")]
    done     = [c for c in cases if c["status"] == "done"]
    missed   = [c for c in cases if c["status"] == "missed"]
    open_    = [c for c in cases if c["status"] == "open"]

    agent_counts: dict[str, int] = defaultdict(int)
    for c in assigned:
        if c.get("agent_name"):
            agent_counts[c["agent_name"]] += 1

    lines = [
        f"*{title}*\n",
        f"Total Alerts: {total}",
        f"Assigned: {len(assigned)}",
        f"Resolved: {len(done)}",
        f"Missed: {len(missed)}",
        f"Open: {len(open_)}",
    ]

    if agent_counts:
        lines.append("\n*Agent Activity:*")
        for agent, count in sorted(agent_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {agent}: {count} case(s)")

    if missed:
        lines.append("\n*Unresolved Alerts:*")
        for c in missed:
            lines.append(f"  {_fmt_dt(c.get('opened_at'))} — {c['driver_name']} ({c['group_name']})")

    return "\n".join(lines)


# ── /report ───────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_main_admin(user.id):
        await update.message.reply_text("Access denied.")
        return
    cases  = await async_get_cases_today()
    today  = datetime.now().strftime("%B %d, %Y")
    report = _build_daily_report(cases, f"Daily Report — {today}")
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)


# ── /leaderboard ──────────────────────────────────────────────────────────────

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_main_admin(user.id):
        await update.message.reply_text("Access denied.")
        return
    cases = await async_get_cases_this_week()
    if not cases:
        await update.message.reply_text("No activity recorded this week yet.")
        return

    agent_stats: dict[str, dict] = defaultdict(lambda: {"count": 0})
    for c in cases:
        if c.get("agent_name") and c["status"] in ("assigned", "reported", "done"):
            agent_stats[c["agent_name"]]["count"] += 1

    if not agent_stats:
        await update.message.reply_text("No assigned cases this week.")
        return

    sorted_agents = sorted(agent_stats.items(), key=lambda x: -x[1]["count"])
    medals = ["🥇", "🥈", "🥉"]
    lines  = ["*Weekly Leaderboard*\n"]
    for i, (name, stats) in enumerate(sorted_agents):
        medal = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{medal} *{_esc(name)}* — {stats['count']} cases")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /missed ───────────────────────────────────────────────────────────────────

async def cmd_missed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_main_admin(user.id):
        await update.message.reply_text("Access denied.")
        return
    cases  = await async_get_cases_today()
    missed = [c for c in cases if c["status"] == "missed"]
    if not missed:
        await update.message.reply_text("✅ All alerts handled today. Great job!")
        return
    lines = [f"*Missed Alerts — {len(missed)} today*\n"]
    for c in missed:
        lines.append(f"{_fmt_dt(c.get('opened_at'))} — {c['driver_name']}")
        lines.append(f"   {c['group_name']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Daily report (called by scheduler) ───────────────────────────────────────

async def send_daily_report(bot, chat_id: int) -> None:
    cases  = await async_get_cases_today()
    today  = datetime.now().strftime("%B %d, %Y")
    report = _build_daily_report(cases, f"End of Day Report — {today}")
    try:
        await bot.send_message(chat_id, report, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Daily report sent to {chat_id}")
    except TelegramError as e:
        logger.error(f"Failed to send daily report: {e}")


# ── /adduser ──────────────────────────────────────────────────────────────────

async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _can_manage(user.id):
        await update.message.reply_text("Access denied.")
        return
    args = ctx.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: `/adduser <user_id> <name> <role> [shift]`\n"
            f"Roles: `{' | '.join(sorted(VALID_ROLES))}`\n"
            f"Shifts: `{' | '.join(sorted(VALID_SHIFTS))}` (default `ALL`)\n\n"
            "Example: `/adduser 123456789 Alex agent AH`\n\n"
            "Or just *forward any message* from the user and I'll ask for the role.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user_id — must be a number.")
        return
    name  = args[1]
    role  = args[2].lower()
    shift = args[3].upper() if len(args) > 3 else "ALL"
    if shift not in VALID_SHIFTS:
        await update.message.reply_text(
            f"❌ Invalid shift. Use: `{' | '.join(sorted(VALID_SHIFTS))}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if add_user(uid, name, "", role, shift):
        await update.message.reply_text(
            f"✅ Added *{_esc(name)}* (ID: `{uid}`) as *{role}* — shift *{shift}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ Invalid role. Use: `{' | '.join(sorted(VALID_ROLES))}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /removeuser ───────────────────────────────────────────────────────────────

async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _can_manage(user.id):
        await update.message.reply_text("Access denied.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/removeuser <user_id>`", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user_id.")
        return
    existing = get_user(uid)
    if remove_user(uid):
        name = existing["name"] if existing else str(uid)
        await update.message.reply_text(
            f"✅ *{_esc(name)}* (ID: `{uid}`) removed.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ User `{uid}` not found.", parse_mode=ParseMode.MARKDOWN
        )


# ── /editrole ─────────────────────────────────────────────────────────────────

async def cmd_editrole(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _can_manage(user.id):
        await update.message.reply_text("Access denied.")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            f"Usage: `/editrole <user_id> <role>`\nRoles: `{' | '.join(sorted(VALID_ROLES))}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user_id.")
        return
    role     = ctx.args[1].lower()
    existing = get_user(uid)
    if not existing:
        await update.message.reply_text(f"❌ User `{uid}` not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if edit_role(uid, role):
        await update.message.reply_text(
            f"✅ *{existing['name']}* is now *{role}*.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ Invalid role. Use: `{' | '.join(sorted(VALID_ROLES))}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /editshift ────────────────────────────────────────────────────────────────

async def cmd_editshift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _can_manage(user.id):
        await update.message.reply_text("Access denied.")
        return
    if len(ctx.args) < 2:
        lines = "\n".join(f"  `{code}` — {label}" for code, label in SHIFT_LABELS.items())
        await update.message.reply_text(
            f"Usage: `/editshift <user_id> <shift>`\nShifts:\n{lines}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user_id.")
        return
    shift    = ctx.args[1].upper()
    existing = get_user(uid)
    if not existing:
        await update.message.reply_text(f"❌ User `{uid}` not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if edit_shift(uid, shift):
        await update.message.reply_text(
            f"✅ *{existing['name']}* is now on shift *{shift}* ({SHIFT_LABELS.get(shift, shift)}).",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ Invalid shift. Use: `{' | '.join(sorted(VALID_SHIFTS))}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /listusers ────────────────────────────────────────────────────────────────

async def cmd_listusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _can_manage(user.id):
        await update.message.reply_text("Access denied.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("No users in the system yet.")
        return

    role_order = {"developer": 0, "super_admin": 1, "agent": 2}
    sorted_users = sorted(users.items(), key=lambda x: role_order.get(x[1]["role"], 9))

    role_icons = {"developer": "🛠", "super_admin": "⭐", "agent": "👤"}
    lines = ["*User List*\n"]
    for uid, u in sorted_users:
        icon   = role_icons.get(u["role"], "•")
        handle = f"@{u['username']}" if u.get("username") else "—"
        shift  = u.get("shift", "ALL")
        lines.append(f"{icon} *{u['name']}* ({handle})\n   ID: `{uid}` — _{u['role']}_ — shift `{shift}`")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Forward a message → add user flow ────────────────────────────────────────

async def handle_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    When a manager/developer forwards any message from a user,
    bot extracts that user's info and asks for a role via inline buttons.
    Shift defaults to ALL and can be changed afterwards with /editshift.
    """
    user = update.effective_user
    if not _can_manage(user.id):
        return  # silently ignore forwards from non-managers

    msg      = update.message
    fwd_user = None

    # Telegram Bot API 7+: forward_origin replaces forward_from
    origin = getattr(msg, "forward_origin", None)
    if origin:
        fwd_user = getattr(origin, "sender_user", None)

    # Fallback for older clients / API versions
    if not fwd_user and getattr(msg, "forward_from", None):
        fwd_user = msg.forward_from

    if not fwd_user:
        await msg.reply_text(
            "⚠️ Couldn't read the user from that forward.\n\n"
            "The person may have *Forwarding Privacy* enabled in Telegram settings.\n"
            "Ask them to send `/start` to the bot directly — then try again.\n\n"
            "Or add manually: `/adduser <user_id> <name> <role> [shift]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    uid      = fwd_user.id
    name     = fwd_user.full_name
    username = fwd_user.username or ""
    handle   = f"@{username}" if username else f"ID: `{uid}`"

    existing     = get_user(uid)
    status_line  = ""
    if existing:
        status_line = f"\n\n⚠️ Already registered as *{existing['role']}* — selecting a role will update it."

    ctx.user_data["pending_add"] = {
        "uid":      uid,
        "name":     name,
        "username": username,
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Agent",      callback_data="addrole|agent"),
            InlineKeyboardButton("⭐ Super Admin", callback_data="addrole|super_admin"),
            InlineKeyboardButton("🛠 Developer",   callback_data="addrole|developer"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="addrole|cancel")],
    ])

    await msg.reply_text(
        f"👤 *{_esc(name)}* ({handle})\n\nSelect a role for this user:{status_line}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def cb_addrole(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback handler for the role selection buttons shown after a forward."""
    query = update.callback_query
    await query.answer()

    role = query.data.split("|")[1]

    if role == "cancel":
        ctx.user_data.pop("pending_add", None)
        await query.edit_message_text("Cancelled.")
        return

    pending = ctx.user_data.get("pending_add")
    if not pending:
        await query.edit_message_text("⚠️ Session expired. Forward the message again.")
        return

    uid, name, username = pending["uid"], pending["name"], pending["username"]
    add_user(uid, name, username, role, "ALL")
    ctx.user_data.pop("pending_add", None)

    handle     = f"@{username}" if username else f"ID: `{uid}`"
    role_icons = {"developer": "🛠", "super_admin": "⭐", "agent": "👤"}
    icon       = role_icons.get(role, "•")

    await query.edit_message_text(
        f"✅ {icon} *{_esc(name)}* ({handle}) added as *{role}* — shift `ALL`.\n"
        f"Use /editshift to set a specific shift.",
        parse_mode=ParseMode.MARKDOWN,
    )


def _esc(t: str) -> str:
    """Escape Markdown v1 special chars in dynamic content."""
    return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
