"""
handlers/agent_handler.py
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler, CommandHandler,
)
from telegram.error import TelegramError

from storage.case_store import (
    async_get_cases_for_agent_today          as get_cases_for_agent_today,
    async_get_open_cases_for_agent           as get_open_cases_for_agent,
    async_get_closed_cases_for_agent_page    as get_closed_cases_for_agent_page,
    async_count_closed_cases_for_agent       as count_closed_cases_for_agent,
    async_get_agent_stats                    as get_agent_stats,
    async_close_case                         as close_case,
    async_get_case                           as get_case,
)

from storage.user_store import has_role


def _esc(t: str) -> str:
    """Escape Markdown v1 special chars in dynamic content."""
    return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


logger         = logging.getLogger(__name__)
CASES_PER_PAGE = 5
# State constants kept for ConversationHandler compat (no active states used)
AWAITING_SOLUTION     = 1
AWAITING_CLOSE_REASON = 2


def _busy_agents(ctx) -> set:
    if "busy_agents" not in ctx.bot_data:
        ctx.bot_data["busy_agents"] = set()
    return ctx.bot_data["busy_agents"]


def _is_admin(user_id):
    return has_role(user_id, "agent", "super_admin", "developer")


def _fmt_dt(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%b %d %H:%M")
    except Exception:
        return iso[:16]


def _active_case_text(case):
    badge    = "📋 *Reported Case*" if case.get("status") == "reported" else "📋 *Active Case*"
    opened   = _fmt_dt(case.get("opened_at"))
    return (
        f"{badge}\n\n"
        f"⏱ *Opened:* {opened}\n"
        f"📌 *Group:* {_esc(case['group_name'])}\n"
        f"👤 *Reported by:* {_esc(case['driver_name'])}\n"
        f"📝 *Issue:* {_esc((case.get('description') or '—')[:200])}"
    )


def _active_case_keyboard(case_id, status="assigned"):
    if status == "reported":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Solve",    callback_data=f"close_ask|{case_id}"),
            InlineKeyboardButton("🔁 Reassign", callback_data=f"reassign_{case_id}"),
        ]])
    from handlers.webapp_utils import report_button
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Solve",    callback_data=f"close_ask|{case_id}"),
        report_button(case_id),
        InlineKeyboardButton("🔁 Reassign", callback_data=f"reassign_{case_id}"),
    ]])


async def _delete_after(bot, chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


# ── /mycases ──────────────────────────────────────────────────────────────────

async def cmd_mycases(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("Not authorized.")
        return

    active_only = await get_open_cases_for_agent(user.id)

    if not active_only:
        await update.message.reply_text("No active cases. You are free! ✅")
        return

    for case in active_only:
        await update.message.reply_text(
            _active_case_text(case),
            parse_mode="Markdown",
            reply_markup=_active_case_keyboard(case["id"], case.get("status", "assigned")),
        )


# ── /mystats ──────────────────────────────────────────────────────────────────

async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("Not authorized.")
        return

    s = await get_agent_stats(user.id)

    text = (
        f"📊 *Your Stats*\n\n"
        f"Today:      {s['today_assigned']} assigned  |  {s['today_closed']} closed\n"
        f"This week:  {s['week_assigned']} assigned  |  {s['week_closed']} closed\n"
        f"All time:   {s['all_total']} total  |  {s['all_closed']} closed"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── /casehistory ──────────────────────────────────────────────────────────────

def _range_bounds(kind: str):
    """(start_iso, end_iso, label) for a quick date-range filter — start/end
    are None for 'all' (no filter, matches every closed case)."""
    now         = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    upper       = (today_start + timedelta(days=1)).isoformat()  # through end of today
    if kind == "today":
        return today_start.isoformat(), upper, "Today"
    if kind == "week":
        week_start = today_start - timedelta(days=now.weekday())
        return week_start.isoformat(), upper, "This week"
    if kind == "month":
        month_start = today_start.replace(day=1)
        return month_start.isoformat(), upper, "This month"
    return None, None, "All time"


async def cmd_casehistory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("Not authorized.")
        return

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Today",      callback_data="histrange|today"),
            InlineKeyboardButton("This week",  callback_data="histrange|week"),
        ],
        [
            InlineKeyboardButton("This month", callback_data="histrange|month"),
            InlineKeyboardButton("All time",   callback_data="histrange|all"),
        ],
    ])
    await update.message.reply_text("Which cases would you like to see?", reply_markup=kb)


async def cb_histrange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    kind     = query.data.split("|")[1]
    agent_id = query.from_user.id

    start_iso, end_iso, label = _range_bounds(kind)
    total = await count_closed_cases_for_agent(agent_id, start_iso, end_iso)
    if not total:
        await query.edit_message_text(f"No closed cases — {label}.")
        return

    ctx.user_data["hist_range"]      = {"start": start_iso, "end": end_iso, "label": label}
    ctx.user_data["history_msg_ids"] = []
    await query.edit_message_text(f"📋 {label} — {total} closed case(s)")
    await _send_history_page(query, agent_id, page=0, ctx=ctx)


async def _send_history_page(target, agent_id, page, ctx=None):
    # Reads the range picked in cb_histrange. Falls back to "all time" if
    # it's missing (e.g. bot restarted and in-memory user_data was cleared)
    # rather than erroring — Next/Prev still works, just unfiltered.
    rng       = (ctx.user_data.get("hist_range") if ctx else None) or {}
    start_iso = rng.get("start")
    end_iso   = rng.get("end")
    label     = rng.get("label", "All time")

    # Each page turn fetches only CASES_PER_PAGE rows via SQL LIMIT/OFFSET —
    # not the agent's whole history — so this stays fast no matter how many
    # pages deep you go or how many years of cases pile up.
    offset = page * CASES_PER_PAGE
    total  = await count_closed_cases_for_agent(agent_id, start_iso, end_iso)
    batch  = await get_closed_cases_for_agent_page(agent_id, offset, CASES_PER_PAGE, start_iso, end_iso)
    end          = min(offset + CASES_PER_PAGE, total)
    is_last_page = end >= total

    async def _send(text, reply_markup=None):
        if hasattr(target, "reply_text"):
            sent = await target.reply_text(text, reply_markup=reply_markup)
        else:
            sent = await target.get_bot().send_message(agent_id, text, reply_markup=reply_markup)
        if ctx and "history_msg_ids" in ctx.user_data:
            ctx.user_data["history_msg_ids"].append(sent.message_id)
        return sent

    if not batch:
        await _send(f"No closed cases — {label}.")
        return

    for i, case in enumerate(batch):
        num  = offset + i + 1
        text = (
            f"Case {num}\n\n"
            f"Group: {_esc(case['group_name'])}\n"
            f"Reported by: {_esc(case['driver_name'])}\n"
            f"Issue: {(case.get('description') or '')[:80]}\n"
            f"Closed: {_fmt_dt(case.get('closed_at'))}"
            + (f"\nNote: {_esc(case['notes'])}" if case.get("notes") else "")
        )
        nav = []
        if i == len(batch) - 1:
            if page > 0:
                nav.append(InlineKeyboardButton("← Prev", callback_data=f"histpage|{page - 1}"))
            if end < total:
                nav.append(InlineKeyboardButton("Next →", callback_data=f"histpage|{page + 1}"))
        kb = InlineKeyboardMarkup([nav]) if nav else None
        await _send(text, reply_markup=kb)

    total_pages = max(1, ((total - 1) // CASES_PER_PAGE) + 1)
    footer      = f"Page {page + 1} of {total_pages}  ({total} closed — {label})"
    if is_last_page:
        delete_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Clear from chat", callback_data="hist_delete_chat"),
        ]])
        await _send(footer, reply_markup=delete_kb)
    else:
        await _send(footer)


async def cb_histpage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    try:
        page = int(query.data.split("|")[1])
    except (ValueError, IndexError):
        page = 0
    agent_id = query.from_user.id
    if "history_msg_ids" not in ctx.user_data:
        ctx.user_data["history_msg_ids"] = []
    await _send_history_page(query, agent_id, page, ctx=ctx)


async def cb_hist_delete_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    msg_ids = ctx.user_data.pop("history_msg_ids", [])
    msg_ids.append(query.message.message_id)
    for mid in msg_ids:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=mid)
        except TelegramError:
            pass


# ── Solve (close with note) ───────────────────────────────────────────────────

async def cb_solve_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    case_id = query.data.split("|")[1]
    case    = await get_case(case_id)

    if not case or case["status"] not in ("assigned", "reported"):
        await query.edit_message_text("This case is no longer active.", reply_markup=None)
        return ConversationHandler.END

    existing = ctx.user_data.get("solving_case_id")
    if existing and existing != case_id:
        existing_case = await get_case(existing)
        if existing_case and existing_case["status"] in ("assigned", "reported"):
            await query.answer("Finish your current case first.", show_alert=True)
            return ConversationHandler.END

    ctx.user_data.pop("pending_solution", None)
    ctx.user_data["solving_case_id"] = case_id

    user         = update.effective_user
    handler_name = f"{user.first_name} {user.last_name or ''}".strip()
    ctx.user_data["report_case_id"] = case_id
    ctx.user_data["report_handler"] = handler_name
    _busy_agents(ctx).add(user.id)

    await query.edit_message_text(
        f"📋 *Report*\n\n"
        f"Reported by: {_esc(case['driver_name'])} — {_esc(case['group_name'])}\n"
        f"Issue: {(case.get('description') or '')[:80]}\n\n"
        "Select vehicle type:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚛 Truck",   callback_data="rpt_type|truck"),
            InlineKeyboardButton("🚚 Trailer", callback_data="rpt_type|trailer"),
            InlineKeyboardButton("❄️ Reefer",  callback_data="rpt_type|reefer"),
        ]]),
    )
    return ConversationHandler.END


async def cb_solve_receive_solution(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Legacy fallback — should not be reached in normal flow
    await update.message.reply_text("Use /mycases to manage your cases.")
    return ConversationHandler.END


# stubs kept so bot.py callback registrations don't break
async def cb_solve_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Use /mycases to manage your cases.", reply_markup=None)


async def cb_solve_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    ctx.user_data.pop("solving_case_id", None)
    case_id = query.data.split("|")[1]
    case    = await get_case(case_id)
    if case and case["status"] in ("assigned", "reported"):
        await query.edit_message_text(
            _active_case_text(case), parse_mode="Markdown",
            reply_markup=_active_case_keyboard(case["id"], case.get("status", "assigned")),
        )
    else:
        await query.edit_message_text("Case not found.", reply_markup=None)


async def cmd_solve_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("solving_case_id", None)
    ctx.user_data.pop("report_case_id", None)
    ctx.user_data.pop("report_handler", None)
    _busy_agents(ctx).discard(update.effective_user.id)
    await update.message.reply_text("Cancelled. Use /mycases to see your cases.")
    return ConversationHandler.END


# ── Close — show details + buttons, no text input ────────────────────────────

async def cb_close_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tap ✅ Solve → shows case details with Close / Cancel buttons. No note required."""
    query   = update.callback_query
    await query.answer()
    case_id = query.data.split("|")[1]
    case    = await get_case(case_id)

    if not case or case["status"] not in ("assigned", "reported"):
        await query.edit_message_text("This case is no longer active.", reply_markup=None)
        return ConversationHandler.END

    await query.edit_message_text(
        f"📋 *Close Case*\n\n"
        f"📌 *Group:* {_esc(case['group_name'])}\n"
        f"👤 *Driver:* {_esc(case['driver_name'])}\n"
        f"📝 *Issue:* {(case.get('description') or '—')[:200]}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Close",  callback_data=f"close_confirm|{case_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"close_cancel|{case_id}"),
        ]]),
    )
    return ConversationHandler.END


async def cb_close_receive_reason(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Not used — kept so ConversationHandler doesn't error
    await update.message.reply_text("Use /mycases to manage your cases.")
    return ConversationHandler.END


async def cb_close_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tap ✅ Close → closes instantly, no note, no confirmation."""
    query   = update.callback_query
    await query.answer()
    case_id = query.data.split("|")[1]
    case    = await get_case(case_id)

    if not case or case["status"] not in ("assigned", "reported"):
        await query.edit_message_text("This case is no longer active.", reply_markup=None)
        return

    await close_case(case_id, notes=None)
    await query.edit_message_text(
        f"✅ *Case closed!*\n\n"
        f"Group: {_esc(case['group_name'])}\n"
        f"Driver: {_esc(case['driver_name'])}",
        parse_mode="Markdown",
        reply_markup=None,
    )
    asyncio.create_task(_show_remaining_after(update.effective_user.id, 0))


async def cb_close_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    ctx.user_data.pop("solving_case_id", None)
    case_id = query.data.split("|")[1]
    case    = await get_case(case_id)
    if case and case["status"] in ("assigned", "reported"):
        await query.edit_message_text(
            _active_case_text(case), parse_mode="Markdown",
            reply_markup=_active_case_keyboard(case["id"], case.get("status", "assigned")),
        )
    else:
        await query.edit_message_text("Case not found.", reply_markup=None)


_bot_ref = None  # set in bot.py after app.build()


async def _show_remaining_after(agent_id: int, delay: int = 0):
    if delay:
        await asyncio.sleep(delay)
    bot = _bot_ref
    if not bot:
        return
    active_only = await get_open_cases_for_agent(agent_id)
    if active_only:
        for c in active_only:
            try:
                await bot.send_message(
                    agent_id, _active_case_text(c),
                    parse_mode="Markdown",
                    reply_markup=_active_case_keyboard(c["id"], c.get("status", "assigned")),
                )
            except TelegramError:
                pass
    else:
        try:
            await bot.send_message(agent_id, "✅ No more active cases. You are free!")
        except TelegramError:
            pass


# ── /done ─────────────────────────────────────────────────────────────────────

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("Not authorized.")
        return

    today_cases  = await get_cases_for_agent_today(user.id)
    closed_today = [c for c in today_cases if c["status"] == "done"]

    if not closed_today:
        await update.message.reply_text("No cases closed today yet.")
        return

    lines = [f"Today's closed cases: {len(closed_today)}\n"]
    for i, c in enumerate(closed_today, 1):
        note = f"\n   Note: {c['notes']}" if c.get("notes") else ""
        lines.append(
            f"{i}. {c['driver_name']} — {c['group_name']}\n"
            f"   Closed: {_fmt_dt(c.get('closed_at'))}"
            f"{note}"
        )
    await update.message.reply_text("\n".join(lines))


async def cb_done_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    case_id = query.data.split("|")[1]
    case    = await get_case(case_id)

    if not case:
        await query.edit_message_text("Case not found.", reply_markup=None)
        return ConversationHandler.END

    await query.edit_message_text(
        f"📋 *Close Case*\n\n"
        f"📌 *Group:* {_esc(case['group_name'])}\n"
        f"👤 *Driver:* {_esc(case['driver_name'])}\n"
        f"📝 *Issue:* {(case.get('description') or '—')[:200]}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Close",  callback_data=f"close_confirm|{case_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"close_cancel|{case_id}"),
        ]]),
    )
    return ConversationHandler.END


# ── Backward-compat stubs ─────────────────────────────────────────────────────

async def cb_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    case_id = query.data.split("|")[1]
    query.data = f"close_ask|{case_id}"
    return await cb_close_ask(update, ctx)


async def cb_delete_do(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Cancelled.", reply_markup=None)


async def cb_delete_keep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    case_id = query.data.split("|")[1]
    case    = await get_case(case_id)
    if case and case["status"] in ("assigned", "reported"):
        await query.edit_message_text(
            _active_case_text(case), parse_mode="Markdown",
            reply_markup=_active_case_keyboard(case["id"], case.get("status", "assigned")),
        )
    else:
        await query.edit_message_text("Case not found.", reply_markup=None)


# ── Conversation handler ──────────────────────────────────────────────────────

def get_solve_conversation():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_done_pick, pattern=r'^done_pick\|'),
            CallbackQueryHandler(cb_close_ask, pattern=r'^close_ask\|'),
        ],
        states={},
        fallbacks=[CommandHandler("cancel", cmd_solve_cancel)],
        per_message=False,
        allow_reentry=True,
    )
