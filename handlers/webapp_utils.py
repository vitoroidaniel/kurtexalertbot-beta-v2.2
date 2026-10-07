"""Kurtex Report Mini App button helper."""
from urllib.parse import urlencode
from telegram import InlineKeyboardButton, WebAppInfo
from config import config

def report_url(case_id: str) -> str:
    base = (config.PUBLIC_URL or "").rstrip("/")
    return f"{base}/report-app?{urlencode({'case_id': str(case_id), 'source': 'report_button', 'v': '2.5'})}"

def report_button(case_id: str) -> InlineKeyboardButton:
    # Chat workflow is intentionally the source of truth. The Mini App can be
    # opened separately once core Telegram callbacks are healthy.
    return InlineKeyboardButton("📋 Report", callback_data=f"solve|{case_id}")
