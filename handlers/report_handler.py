"""
handlers/report_handler.py
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler,
    CallbackQueryHandler, CommandHandler, MessageHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from config import config
from storage.user_store import get_super_admin_ids


def _esc(t: str) -> str:
    """Escape Markdown v1 special chars in dynamic content."""
    return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


logger = logging.getLogger(__name__)

(
    ASK_TYPE,
    ASK_UNIT,
    ASK_DRIVER,
    ASK_ISSUE,
    ASK_LOAD,
    ASK_PICKUP,
    ASK_DELIVERY,
    ASK_LOCATION,
    ASK_SETPOINT,
    ASK_CURRENT_TEMP,
    ASK_TEMP_RECORDER,
    ASK_COMMENTS,
    ASK_MEDIA,
    ASK_PRIORITY,
    CONFIRM,
    ASK_EDIT_FIELD,
    ASK_EDIT_VALUE,
) = range(17)

SKIP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="rpt_skip")]])

LOAD_TYPE_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("JBS Load",    callback_data="rpt_loadtype|jbs"),
        InlineKeyboardButton("Broker Load", callback_data="rpt_loadtype|broker"),
    ],
    [
        InlineKeyboardButton("Meijer Load", callback_data="rpt_loadtype|meijer"),
        InlineKeyboardButton("Empty",       callback_data="rpt_loadtype|empty"),
    ],
])

VTYPE_LABELS = {
    "truck":   "Truck",
    "trailer": "Trailer",
    "reefer":  "Reefer",
}

PRIORITY_META = {
    "low":    {"icon": "🟢", "level": "Low"},
    "medium": {"icon": "🟡", "level": "Medium"},
    "high":   {"icon": "🔴", "level": "High"},
}


def _type_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚛 Truck",   callback_data="rpt_type|truck"),
        InlineKeyboardButton("🚜 Trailer", callback_data="rpt_type|trailer"),
        InlineKeyboardButton("❄️ Reefer",  callback_data="rpt_type|reefer"),
    ]])


def _priority_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 Low",    callback_data="rpt_priority|low"),
        InlineKeyboardButton("🟡 Medium", callback_data="rpt_priority|medium"),
        InlineKeyboardButton("🔴 High",   callback_data="rpt_priority|high"),
    ]])


def _confirm_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Send Report", callback_data="rpt_confirm|yes"),
            InlineKeyboardButton("❌ Cancel",      callback_data="rpt_confirm|no"),
        ],
        [InlineKeyboardButton("✏️ Edit", callback_data="rpt_edit")],
    ])


def _esc(text) -> str:
    if not text or text == "—":
        return "—"
    for ch in ['_', '*', '`', '[']:
        text = str(text).replace(ch, f'\\{ch}')
    return text


def _build_report(d: dict) -> str:
    vtype        = d.get("vehicle_type", "truck")
    priority_key = d.get("priority", "low")
    p            = PRIORITY_META.get(priority_key, PRIORITY_META["low"])
    unit         = d.get("unit_number", "")
    load         = d.get("load", "")

    if vtype == "truck":
        unit_line = f"*Truck:* {_esc(unit)}" if unit else "*Truck:* —"
    else:
        unit_line = f"*Trailer:* {_esc(unit)}" if unit else "*Trailer:* —"

    lines = [
        f"{p['icon']} *Case Report — {VTYPE_LABELS.get(vtype, vtype.title())}*",
        f"Priority: *{p['level']}*",
        "",
        unit_line,
        f"*Driver:* {_esc(d.get('driver', '—'))}",
        "",
        f"*Issue:* {_esc(d.get('issue', '—'))}",
        "",
    ]

    # Load label — just "JBS Load" or "Broker Load" or "Empty", no colon, no "Load:" prefix
    # Empty only shows Current Location
    if load and load != "—":
        lines.append(f"*{_esc(load)}*")
        if load.lower() != "empty":
            lines.append(f"Pick up Location/Time: {_esc(d.get('pickup', '—'))}")
            lines.append(f"Delivery Location/Time: {_esc(d.get('delivery', '—'))}")
        lines.append(f"Current Location: {_esc(d.get('location', '—'))}")
    else:
        lines.append(f"Current Location: {_esc(d.get('location', '—'))}")

    if vtype == "reefer" and load.lower() != "empty":
        lines += [
            "",
            f"*Setpoint:* {_esc(d.get('setpoint', '—'))}",
            f"*Current temp:* {_esc(d.get('current_temp', '—'))}",
            f"*Temp recorder:* {_esc(d.get('temp_recorder', '—'))}",
        ]

    if d.get("comments"):
        lines += ["", f"*Comments:* {_esc(d.get('comments'))}"]

    lines += ["", f"*Reported by:* {_esc(d.get('handler', '—'))}"]
    return "\n".join(lines)



async def cb_report_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point from Report button (solve|case_id). Stores handler info and shows vehicle selector."""
    query   = update.callback_query
    await query.answer()
    case_id = query.data.split("|")[1]

    from storage.case_store import get_case
    case = get_case(case_id)
    if not case or case["status"] not in ("assigned", "reported"):
        await query.edit_message_text("This case is no longer active.", reply_markup=None)
        return ConversationHandler.END

    # Block if agent is mid-report on another case
    existing_case_id = ctx.user_data.get("report_case_id")
    if existing_case_id and existing_case_id != case_id:
        existing = get_case(existing_case_id)
        if existing and existing["status"] in ("assigned", "reported"):
            await query.answer("Finish your current report first.", show_alert=True)
            return ConversationHandler.END

    # Store handler name and case_id
    user = update.effective_user
    handler_name = f"{user.first_name} {user.last_name or ''}".strip()
    ctx.user_data["report_case_id"] = case_id
    ctx.user_data["report_handler"] = handler_name
    if "busy_agents" not in ctx.bot_data:
        ctx.bot_data["busy_agents"] = set()
    ctx.bot_data["busy_agents"].add(user.id)

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
        ]])
    )
    return ASK_TYPE

async def cb_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    vtype = query.data.split("|")[1]
    report_case_id = ctx.user_data.get("report_case_id")
    report_handler = ctx.user_data.get("report_handler")
    ctx.user_data["report"] = {"media": [], "vehicle_type": vtype}
    if report_case_id:
        ctx.user_data["report_case_id"] = report_case_id
    if report_handler:
        ctx.user_data["report"]["handler"] = report_handler
    unit_prompt = "Truck number:" if vtype == "truck" else "Trailer number:"
    label = {"truck": "🚛 Truck", "trailer": "🚚 Trailer", "reefer": "❄️ Reefer"}[vtype]
    await query.edit_message_text(
        f"Type: *{label}*\n\n{unit_prompt}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=None
    )
    return ASK_UNIT


async def recv_unit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["unit_number"] = update.message.text.strip()
    await update.message.reply_text("Driver:")
    return ASK_DRIVER


async def recv_driver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["driver"] = update.message.text.strip()
    await update.message.reply_text("Issue description:")
    return ASK_ISSUE


async def recv_issue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["issue"] = update.message.text.strip()
    await update.message.reply_text("Load type:", reply_markup=LOAD_TYPE_KB)
    return ASK_LOAD


async def cb_loadtype(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ltype = query.data.split("|")[1]
    label_map = {"jbs": "JBS Load", "broker": "Broker Load", "meijer": "Meijer Load", "empty": "Empty"}
    label = label_map.get(ltype, ltype.title())
    ctx.user_data["report"]["load"] = label

    if ltype == "empty":
        # Empty — no PU or Delivery, jump straight to current location
        ctx.user_data["report"]["pickup"]   = "—"
        ctx.user_data["report"]["delivery"] = "—"
        await query.edit_message_text(
            f"*{label}*\n\nCurrent Location:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=SKIP_KB
        )
        return ASK_LOCATION
    else:
        await query.edit_message_text(
            f"*{label}*\n\nPick up Location / Time:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=SKIP_KB
        )
        return ASK_PICKUP


async def recv_load(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["load"] = update.message.text.strip()
    await update.message.reply_text("Pick up Location / Time:", reply_markup=SKIP_KB)
    return ASK_PICKUP


async def recv_pickup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["pickup"] = update.message.text.strip()
    await update.message.reply_text("Delivery Location / Time:", reply_markup=SKIP_KB)
    return ASK_DELIVERY


async def recv_delivery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["delivery"] = update.message.text.strip()
    await update.message.reply_text("Current Location:", reply_markup=SKIP_KB)
    return ASK_LOCATION


async def recv_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["location"] = update.message.text.strip()
    report = ctx.user_data["report"]
    vtype  = report.get("vehicle_type", "truck")
    load   = report.get("load", "")
    if vtype == "reefer" and load.lower() != "empty":
        await update.message.reply_text("Setpoint temperature (e.g. -10°C):", reply_markup=SKIP_KB)
        return ASK_SETPOINT
    # truck/trailer, or reefer with Empty load — skip straight to comments, no temp questions
    await update.message.reply_text("Comments:", reply_markup=SKIP_KB)
    return ASK_COMMENTS


async def recv_setpoint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["setpoint"] = update.message.text.strip()
    await update.message.reply_text("Current temperature:", reply_markup=SKIP_KB)
    return ASK_CURRENT_TEMP


async def recv_current_temp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["current_temp"] = update.message.text.strip()
    await update.message.reply_text(
        "Temp recorder: Y or N?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Y", callback_data="rpt_temprec|Y"),
            InlineKeyboardButton("N", callback_data="rpt_temprec|N"),
        ]])
    )
    return ASK_TEMP_RECORDER


async def cb_temp_recorder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["report"]["temp_recorder"] = query.data.split("|")[1]
    await query.edit_message_text("Comments:", reply_markup=None)
    return ASK_COMMENTS


async def recv_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["report"]["comments"] = update.message.text.strip()
    await update.message.reply_text(
        "Send photo(s) or video(s). Press Done when finished:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Done (no media)", callback_data="rpt_mediadone")
        ]])
    )
    return ASK_MEDIA


async def recv_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg    = update.message
    report = ctx.user_data.setdefault("report", {"media": []})
    media  = report.setdefault("media", [])
    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            if not any(fid == file_id for _, fid in media):
                media.append(("photo", file_id))
            kind = "Photo"
        elif msg.video:
            media.append(("video", msg.video.file_id))
            kind = "Video"
        elif msg.document:
            media.append(("document", msg.document.file_id))
            kind = "File"
        else:
            await msg.reply_text("Please send a photo, video, or file.")
            return ASK_MEDIA
        await msg.reply_text(
            f"{kind} received ({len(media)} total). Send more or press Done:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Done ✅", callback_data="rpt_mediadone")]])
        )
    except Exception as e:
        logger.error(f"recv_media error: {e}")
        await msg.reply_text(
            "Something went wrong. Try again or press Done:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Done ✅", callback_data="rpt_mediadone")]])
        )
    return ASK_MEDIA


async def cb_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    report = ctx.user_data.get("report", {})
    vtype  = report.get("vehicle_type", "truck")

    # Step order: pickup → delivery → location → (reefer only: setpoint → current_temp → temp_recorder) → comments → media
    if "pickup" not in report:
        report["pickup"] = "—"
        await query.edit_message_text("Delivery Location / Time:", reply_markup=SKIP_KB)
        return ASK_DELIVERY
    elif "delivery" not in report:
        report["delivery"] = "—"
        await query.edit_message_text("Current Location:", reply_markup=SKIP_KB)
        return ASK_LOCATION
    elif "location" not in report:
        report["location"] = "—"
        if vtype == "reefer" and report.get("load", "").lower() != "empty":
            await query.edit_message_text("Setpoint temperature:", reply_markup=SKIP_KB)
            return ASK_SETPOINT
        # truck / trailer, or reefer with Empty load — straight to comments, never ask temp
        await query.edit_message_text("Comments:", reply_markup=SKIP_KB)
        return ASK_COMMENTS
    elif vtype == "reefer" and "setpoint" not in report:
        report["setpoint"] = "—"
        await query.edit_message_text("Current temperature:", reply_markup=SKIP_KB)
        return ASK_CURRENT_TEMP
    elif vtype == "reefer" and "current_temp" not in report:
        report["current_temp"] = "—"
        await query.edit_message_text(
            "Temp recorder: Y or N?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Y", callback_data="rpt_temprec|Y"),
                InlineKeyboardButton("N", callback_data="rpt_temprec|N"),
            ]])
        )
        return ASK_TEMP_RECORDER
    elif "comments" not in report:
        report["comments"] = None
        await query.edit_message_text(
            "Send photo(s) or video(s), or press Done:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Done (no media)", callback_data="rpt_mediadone")]])
        )
        return ASK_MEDIA
    return ASK_COMMENTS


async def cb_media_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query       = update.callback_query
    await query.answer()
    vtype       = ctx.user_data.get("report", {}).get("vehicle_type", "truck")
    vtype_label = VTYPE_LABELS.get(vtype, vtype.title())
    await query.edit_message_text(
        f"Vehicle: *{vtype_label}*\n\nSelect priority:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_priority_kb()
    )
    return ASK_PRIORITY


async def cb_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    ctx.user_data["report"]["priority"] = query.data.split("|")[1]
    preview = _build_report(ctx.user_data["report"])
    media   = ctx.user_data["report"].get("media", [])
    note    = f"\n\n📎 {len(media)} media file(s) attached" if media else ""
    await query.edit_message_text(
        f"*Preview — confirm and send?*\n\n{preview}{note}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_kb()
    )
    return CONFIRM


async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data.split("|")[1]

    if action == "no":
        ctx.user_data.pop("report", None)
        await query.edit_message_text("Report cancelled.", reply_markup=None)
        return ConversationHandler.END

    data    = ctx.user_data.pop("report", {})
    dest_id = config.REPORTS_GROUP_ID or next(iter(get_super_admin_ids()), None)
    if not dest_id:
        await query.edit_message_text("No reports group configured.", reply_markup=None)
        return ConversationHandler.END

    report_text = _build_report(data)
    media       = data.get("media", [])

    try:
        if media:
            kind, file_id = media[0]
            try:
                if kind == "photo":
                    await ctx.bot.send_photo(dest_id, photo=file_id, caption=report_text, parse_mode=ParseMode.MARKDOWN, read_timeout=30, write_timeout=30)
                elif kind == "video":
                    await ctx.bot.send_video(dest_id, video=file_id, caption=report_text, parse_mode=ParseMode.MARKDOWN, read_timeout=60, write_timeout=60)
                else:
                    await ctx.bot.send_document(dest_id, document=file_id, caption=report_text, parse_mode=ParseMode.MARKDOWN, read_timeout=30, write_timeout=30)
            except TelegramError as e:
                logger.error(f"First media failed: {e}")
                await ctx.bot.send_message(dest_id, report_text, parse_mode=ParseMode.MARKDOWN)
            for kind, file_id in media[1:]:
                try:
                    if kind == "photo":   await ctx.bot.send_photo(dest_id, photo=file_id, read_timeout=30, write_timeout=30)
                    elif kind == "video": await ctx.bot.send_video(dest_id, video=file_id, read_timeout=60, write_timeout=60)
                    else:                 await ctx.bot.send_document(dest_id, document=file_id, read_timeout=30, write_timeout=30)
                except TelegramError as e:
                    logger.error(f"Media item failed: {e}")
        else:
            await ctx.bot.send_message(dest_id, report_text, parse_mode=ParseMode.MARKDOWN)

        await query.edit_message_text("✅ Report sent!", reply_markup=None)
        logger.info(f"Report sent to {dest_id}")

        case_id = ctx.user_data.pop("report_case_id", None)
        report_data = dict(data)  # save BEFORE any pops
        ctx.user_data.pop("report_handler", None)
        if "busy_agents" in ctx.bot_data:
            ctx.bot_data["busy_agents"].discard(update.effective_user.id)
        if case_id:
            try:
                from storage.case_store import async_report_case, async_update_case_fields
                from storage.fleet_store import async_get_or_create_unit
                await async_report_case(case_id)
                # Save all report fields to the case for dashboard analytics —
                # one indexed UPDATE instead of rewriting the whole case table.
                logger.info(f"Saving report data for case {case_id}: vtype={report_data.get('vehicle_type')}, unit={report_data.get('unit_number')}, issue={report_data.get('issue')}")

                # Register/look up the fleet unit so this case links to it by
                # ID (exact match) rather than only by free-text unit number.
                vtype        = report_data.get("vehicle_type", "")
                unit_number  = report_data.get("unit_number", "")
                fleet_fk     = {"truck": "truck_id", "trailer": "trailer_id", "reefer": "reefer_id"}.get(vtype)
                fleet_fields = {}
                if fleet_fk and unit_number:
                    unit_id = await async_get_or_create_unit(unit_number, vtype)
                    if unit_id:
                        fleet_fields[fleet_fk] = unit_id

                updated = await async_update_case_fields(
                    case_id,
                    vehicle_type=vtype,
                    unit_number=unit_number,
                    report_driver=report_data.get("driver", ""),
                    issue_text=report_data.get("issue", ""),
                    load_type=report_data.get("load", ""),
                    location=report_data.get("location", ""),
                    priority=report_data.get("priority", ""),
                    pickup=report_data.get("pickup", ""),
                    delivery=report_data.get("delivery", ""),
                    comments=report_data.get("comments", ""),
                    setpoint=report_data.get("setpoint", ""),
                    current_temp=report_data.get("current_temp", ""),
                    temp_recorder=report_data.get("temp_recorder", ""),
                    **fleet_fields,
                )
                if updated:
                    logger.info(f"Case {case_id} fleet data saved: vtype={report_data.get('vehicle_type')}")
                else:
                    logger.warning(f"Case {case_id} not found for fleet data save")
            except Exception as e:
                logger.error(f"Failed to mark case reported: {e}")

    except TelegramError as e:
        logger.error(f"Failed to send report: {e}")
        await query.edit_message_text("Failed to send report. Please try again.", reply_markup=None)

    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("report", None)
    ctx.user_data.pop("report_case_id", None)
    ctx.user_data.pop("report_handler", None)
    if "busy_agents" in ctx.bot_data:
        ctx.bot_data["busy_agents"].discard(update.effective_user.id)
    await update.message.reply_text("Report cancelled. Use /mycases to continue.")
    return ConversationHandler.END


async def cmd_report_timeout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Fires if a /report session sits inactive past conversation_timeout — an
    agent who starts a report and never finishes shouldn't be stuck unable
    to start a clean one later. Same cleanup as /cancel, but self-triggered
    instead of requiring the agent to notice and type /cancel themselves.
    """
    ctx.user_data.pop("report", None)
    ctx.user_data.pop("report_case_id", None)
    ctx.user_data.pop("report_handler", None)
    ctx.user_data.pop("editing_field", None)
    uid = update.effective_user.id if update.effective_user else None
    if uid and "busy_agents" in ctx.bot_data:
        ctx.bot_data["busy_agents"].discard(uid)
    try:
        if update.callback_query:
            await update.callback_query.answer()
        chat_id = update.effective_chat.id if update.effective_chat else uid
        if chat_id:
            await ctx.bot.send_message(
                chat_id,
                "⏱ Your report session timed out from inactivity and was cancelled.\n"
                "Use /mycases to start again when you're ready.",
            )
    except Exception as e:
        logger.warning(f"report timeout cleanup notify failed: {e}")
    return ConversationHandler.END



async def _show_preview(target, ctx, edit=True):
    report  = ctx.user_data.get("report", {})
    preview = _build_report(report)
    media   = report.get("media", [])
    note    = f"\n\n📎 {len(media)} media file(s) attached" if media else ""
    text    = f"*Preview — confirm and send?*\n\n{preview}{note}"
    if edit and hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_confirm_kb())
    else:
        msg = target.message if hasattr(target, "message") else target
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_confirm_kb())


def _edit_fields_kb(vtype: str, load: str = "") -> InlineKeyboardMarkup:
    fields = [
        ("Unit number",   "rpt_editfield|unit"),
        ("Driver name",   "rpt_editfield|driver"),
        ("Issue",         "rpt_editfield|issue"),
        ("Load type",     "rpt_editfield|load"),
        ("Pick up",       "rpt_editfield|pickup"),
        ("Delivery",      "rpt_editfield|delivery"),
        ("Location",      "rpt_editfield|location"),
        ("Comments",      "rpt_editfield|comments"),
        ("Priority",      "rpt_editfield|priority"),
    ]
    if vtype in ("trailer", "reefer") and load.lower() != "empty":
        fields[7:7] = [
            ("Setpoint",      "rpt_editfield|setpoint"),
            ("Current temp",  "rpt_editfield|current_temp"),
            ("Temp recorder", "rpt_editfield|temp_recorder"),
        ]
    rows = [[InlineKeyboardButton(label, callback_data=cb)] for label, cb in fields]
    rows.append([InlineKeyboardButton("↩️ Back to preview", callback_data="rpt_edit_back")])
    return InlineKeyboardMarkup(rows)


async def cb_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    report = ctx.user_data.get("report", {})
    vtype  = report.get("vehicle_type", "truck")
    load   = report.get("load", "")
    await query.edit_message_text("Which field would you like to edit?", reply_markup=_edit_fields_kb(vtype, load))
    return ASK_EDIT_FIELD


async def cb_edit_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _show_preview(query, ctx, edit=True)
    return CONFIRM


async def cb_edit_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split("|")[1]
    ctx.user_data["editing_field"] = field
    prompts = {
        "unit": "Enter new unit number:", "driver": "Enter new driver name:",
        "issue": "Enter new issue description:", "load": "Enter load type:",
        "pickup": "Enter new pick up location/time:", "delivery": "Enter new delivery location/time:",
        "location": "Enter new current location:", "setpoint": "Enter new setpoint temperature:",
        "current_temp": "Enter new current temperature:", "temp_recorder": "Temp recorder: Y or N?",
        "comments": "Enter new comments:", "priority": "Select new priority:",
    }
    if field == "load":
        await query.edit_message_text("Select load type:", reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("JBS Load",    callback_data="rpt_editval|JBS Load"),
                InlineKeyboardButton("Broker Load", callback_data="rpt_editval|Broker Load"),
            ],
            [
                InlineKeyboardButton("Meijer Load", callback_data="rpt_editval|Meijer Load"),
                InlineKeyboardButton("Empty",       callback_data="rpt_editval|Empty"),
            ],
        ]))
        return ASK_EDIT_VALUE
    if field == "temp_recorder":
        await query.edit_message_text(prompts[field], reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Y", callback_data="rpt_editval|Y"),
            InlineKeyboardButton("N", callback_data="rpt_editval|N"),
        ]]))
        return ASK_EDIT_VALUE
    if field == "priority":
        await query.edit_message_text(prompts[field], reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🟢 Low",    callback_data="rpt_editval|low"),
            InlineKeyboardButton("🟡 Medium", callback_data="rpt_editval|medium"),
            InlineKeyboardButton("🔴 High",   callback_data="rpt_editval|high"),
        ]]))
        return ASK_EDIT_VALUE
    await query.edit_message_text(prompts.get(field, "Enter new value:"))
    return ASK_EDIT_VALUE


async def cb_edit_val_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = ctx.user_data.pop("editing_field", None)
    value = query.data.split("|")[1]
    if field:
        ctx.user_data["report"]["unit_number" if field == "unit" else field] = value
    await _show_preview(query, ctx, edit=True)
    return CONFIRM


async def recv_edit_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    field = ctx.user_data.pop("editing_field", None)
    if field:
        ctx.user_data["report"]["unit_number" if field == "unit" else field] = value
    await _show_preview(update.message, ctx, edit=False)
    return CONFIRM

def get_report_conversation():
    private_text = filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
    private_media = filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL)

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_report_entry, pattern=r'^solve\|'),
            CallbackQueryHandler(cb_type,         pattern=r'^rpt_type\|'),
        ],
        states={
            ASK_TYPE:          [CallbackQueryHandler(cb_type,          pattern=r'^rpt_type\|')],
            ASK_UNIT:          [MessageHandler(private_text,           recv_unit)],
            ASK_DRIVER:        [MessageHandler(private_text,           recv_driver)],
            ASK_ISSUE:         [MessageHandler(private_text,           recv_issue)],
            ASK_LOAD:          [
                                CallbackQueryHandler(cb_loadtype,      pattern=r'^rpt_loadtype\|'),
                                CallbackQueryHandler(cb_skip,          pattern=r'^rpt_skip$'),
                                MessageHandler(private_text,           recv_load),
                               ],
            ASK_PICKUP:        [MessageHandler(private_text,           recv_pickup),
                                CallbackQueryHandler(cb_skip,          pattern=r'^rpt_skip$')],
            ASK_DELIVERY:      [MessageHandler(private_text,           recv_delivery),
                                CallbackQueryHandler(cb_skip,          pattern=r'^rpt_skip$')],
            ASK_LOCATION:      [MessageHandler(private_text,           recv_location),
                                CallbackQueryHandler(cb_skip,          pattern=r'^rpt_skip$')],
            ASK_SETPOINT:      [MessageHandler(private_text,           recv_setpoint),
                                CallbackQueryHandler(cb_skip,          pattern=r'^rpt_skip$')],
            ASK_CURRENT_TEMP:  [MessageHandler(private_text,           recv_current_temp),
                                CallbackQueryHandler(cb_skip,          pattern=r'^rpt_skip$')],
            ASK_TEMP_RECORDER: [CallbackQueryHandler(cb_temp_recorder, pattern=r'^rpt_temprec\|')],
            ASK_COMMENTS:      [MessageHandler(private_text,           recv_comments),
                                CallbackQueryHandler(cb_skip,          pattern=r'^rpt_skip$')],
            ASK_MEDIA:         [MessageHandler(private_media,          recv_media),
                                CallbackQueryHandler(cb_media_done,    pattern=r'^rpt_mediadone$')],
            ASK_PRIORITY:      [CallbackQueryHandler(cb_priority,      pattern=r'^rpt_priority\|')],
            CONFIRM:           [
                                CallbackQueryHandler(cb_confirm,         pattern=r'^rpt_confirm\|'),
                                CallbackQueryHandler(cb_edit,            pattern=r'^rpt_edit$'),
                               ],
            ASK_EDIT_FIELD:    [
                                CallbackQueryHandler(cb_edit_field,      pattern=r'^rpt_editfield\|'),
                                CallbackQueryHandler(cb_edit_back,       pattern=r'^rpt_edit_back$'),
                               ],
            ASK_EDIT_VALUE:    [
                                MessageHandler(private_text,             recv_edit_value),
                                CallbackQueryHandler(cb_edit_val_button, pattern=r'^rpt_editval\|'),
                               ],
            ConversationHandler.TIMEOUT: [
                                CallbackQueryHandler(cmd_report_timeout),
                                MessageHandler(filters.ALL,              cmd_report_timeout),
                               ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
        per_chat=False,
        per_user=True,
        allow_reentry=True,
        conversation_timeout=1800,  # 30 min inactive -> auto-cancel, see cmd_report_timeout
    )
