"""
handlers/webapp_utils.py

Shared helper for the "Report" button. If PUBLIC_URL is configured, it opens
the Report Mini App (a popup window inside Telegram — see dashboard.py's
/report-app route) so filling out the report doesn't get interleaved with
incoming alert DMs in the same chat. If PUBLIC_URL isn't set, it falls back
to the original chat-based /report ConversationHandler flow so nothing
breaks on deployments that haven't configured a public domain yet.
"""

from telegram import InlineKeyboardButton, WebAppInfo

from config import config


def report_button(case_id: str) -> InlineKeyboardButton:
    if config.PUBLIC_URL:
        return InlineKeyboardButton(
            "📋 Report",
            web_app=WebAppInfo(url=f"{config.PUBLIC_URL}/report-app?case_id={case_id}"),
        )
    return InlineKeyboardButton("📋 Report", callback_data=f"solve|{case_id}")
