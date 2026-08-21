"""
handlers/scheduler.py
- Daily report at 06:50 America/New_York
- Escalation: first ping after 10 min, repeat every 30 min, max 5 rounds
- Heartbeat: logs "alive" periodically so a frozen event loop is visible in
  Railway logs as a gap, instead of silence you only notice when someone
  complains.
"""

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telegram.ext import Application

from config import config
from storage.case_store import async_mark_missed
from storage.db import async_checkpoint_and_backup
from storage.user_store import get_super_admin_ids
from handlers.admin_handler import send_daily_report


def _esc(t: str) -> str:
    """Escape Markdown v1 special chars in dynamic content."""
    return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


logger = logging.getLogger(__name__)

ESCALATION_FIRST_MINUTES  = 10
ESCALATION_REPEAT_MINUTES = 30
ESCALATION_MAX_ROUNDS     = 5
HEARTBEAT_MINUTES         = 5
ET = ZoneInfo("America/New_York")


async def job_daily_report(ctx) -> None:
    try:
        dest = config.REPORTS_GROUP_ID or next(iter(get_super_admin_ids()), None)
        if not dest:
            logger.warning("No REPORTS_GROUP_ID and no super_admin registered — skipping daily report.")
            return
        await send_daily_report(ctx.bot, dest)
    except Exception as e:
        logger.error(f"job_daily_report failed: {e}", exc_info=e)


async def job_heartbeat(ctx) -> None:
    """If this stops showing up in the logs at its expected interval, the
    event loop has stalled — that's the signal to check Railway logs/metrics
    or restart the service."""
    logger.info("heartbeat: bot event loop alive")


async def job_db_maintenance(ctx) -> None:
    try:
        await async_checkpoint_and_backup()
    except Exception as e:
        logger.error(f"job_db_maintenance failed: {e}", exc_info=e)


async def job_escalation_check(ctx) -> None:
    alert_handler = ctx.bot_data.get("alert_handler")
    if not alert_handler:
        return

    for alert_id, record in list(alert_handler._alerts.items()):
        try:
            await _check_one_alert(alert_id, record, ctx)
        except Exception as e:
            # A single malformed/unexpected record must never stop this job
            # from checking every OTHER genuinely pending alert — without
            # this, one bad record would silently block escalation for
            # everyone, every 5 minutes, forever, with nothing in the logs
            # pointing at why.
            logger.error(f"Escalation check failed for alert {alert_id}: {e}", exc_info=e)


async def _check_one_alert(alert_id, record, ctx) -> None:
    from shift_manager import get_all_admins

    now    = datetime.now(timezone.utc)
    first  = timedelta(minutes=ESCALATION_FIRST_MINUTES)
    repeat = timedelta(minutes=ESCALATION_REPEAT_MINUTES)
    alert_handler = ctx.bot_data.get("alert_handler")

    if record.get("taken_by"):
        return

    created_at = record.get("created_at")
    if not created_at:
        return
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    age = now - created_at
    if age < first:
        return

    last_esc = record.get("last_escalated_at")
    if last_esc:
        if isinstance(last_esc, str):
            last_esc = datetime.fromisoformat(last_esc)
        if last_esc.tzinfo is None:
            last_esc = last_esc.replace(tzinfo=timezone.utc)
        if (now - last_esc) < repeat:
            return

    count = record.get("escalation_count", 0)
    if count >= ESCALATION_MAX_ROUNDS:
        return

    age_str     = f"{int(age.total_seconds() // 60)}m"
    short_id    = alert_handler._register_alert(alert_id)
    kb          = alert_handler._make_kb(short_id)
    group_name  = record.get("group_name", "Driver Group")
    driver_name = record.get("driver_name", "a driver")
    description = record.get("text", "")

    msg = (
        f"🔔 *Unassigned Alert — {age_str} old* (reminder {count + 1}/{ESCALATION_MAX_ROUNDS})\n\n"
        f"📌 *Group:* {_esc(group_name)}\n"
        f"👤 *Driver:* {_esc(driver_name)}\n"
        f"📝 {description[:200]}\n\n"
        "⚠️ *Please respond!*"
    )

    if count >= ESCALATION_MAX_ROUNDS - 1:
        recipients = [{"id": aid} for aid in get_super_admin_ids()]
    else:
        recipients = get_all_admins()

    for admin in recipients:
        try:
            sent = await ctx.bot.send_message(
                admin["id"], msg, parse_mode="Markdown", reply_markup=kb,
            )
            record["recipients"].setdefault(admin["id"], []).append(sent.message_id)
        except Exception as e:
            logger.warning(f"Escalation DM failed for {admin['id']}: {e}")

    record["last_escalated_at"] = now.isoformat()
    record["escalation_count"]  = count + 1

    if count == 0:
        await async_mark_missed(alert_id)

    await alert_handler._persist()
    logger.info(f"Alert {alert_id} escalation #{count + 1} after {age_str}")


def register_jobs(app: Application) -> None:
    jq = app.job_queue

    report_time     = datetime.now(ET).replace(hour=6, minute=50, second=0, microsecond=0).timetz()
    maintenance_time = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0).timetz()
    jq.run_daily(job_daily_report,   time=report_time,     name="daily_report")
    jq.run_daily(job_db_maintenance, time=maintenance_time, name="db_maintenance")
    jq.run_repeating(job_escalation_check, interval=300, first=60, name="escalation_check")
    jq.run_repeating(job_heartbeat, interval=HEARTBEAT_MINUTES * 60, first=30, name="heartbeat")

    logger.info(
        f"Jobs registered: daily_report @ 06:50 ET, db_maintenance @ 04:00 ET, "
        f"escalation check every 5 min, heartbeat every {HEARTBEAT_MINUTES} min"
    )
