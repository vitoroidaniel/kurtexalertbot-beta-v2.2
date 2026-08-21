"""
config.py — All secrets from environment variables on Railway.
Locally falls back to encrypted files + config.ini.
"""

import os
import sys
import getpass
import configparser
from pathlib import Path

BASE_DIR = Path(__file__).parent
INI_FILE = BASE_DIR / "config.ini"


def _load_token():
    token = os.getenv("BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        from cryptography.fernet import Fernet
        KEY_FILE   = BASE_DIR / ".secret.key"
        TOKEN_FILE = BASE_DIR / ".bot_token"
        if KEY_FILE.exists() and TOKEN_FILE.exists():
            return Fernet(KEY_FILE.read_bytes()).decrypt(TOKEN_FILE.read_bytes()).decode()
    except Exception:
        pass
    try:
        token = getpass.getpass("Paste your bot token: ").strip()
        if not token:
            sys.exit("No token provided.")
        try:
            from cryptography.fernet import Fernet
            KEY_FILE   = BASE_DIR / ".secret.key"
            TOKEN_FILE = BASE_DIR / ".bot_token"
            key = Fernet.generate_key()
            KEY_FILE.write_bytes(key)
            KEY_FILE.chmod(0o600)
            TOKEN_FILE.write_bytes(Fernet(key).encrypt(token.encode()))
            TOKEN_FILE.chmod(0o600)
        except ImportError:
            pass
        return token
    except (EOFError, OSError):
        sys.exit("BOT_TOKEN environment variable is required on Railway.")


def _load_ini():
    if os.getenv("DRIVER_GROUP_ID") and os.getenv("REPORTS_GROUP_ID"):
        return None
    ini = configparser.ConfigParser()
    if INI_FILE.exists():
        ini.read(INI_FILE)
        return ini
    try:
        driver  = input("Driver group ID: ").strip()
        reports = input("Reports group ID: ").strip()
        ini["channels"] = {"driver_group_id": driver, "reports_group_id": reports}
        with open(INI_FILE, "w") as fh:
            ini.write(fh)
        return ini
    except (EOFError, OSError):
        sys.exit("DRIVER_GROUP_ID and REPORTS_GROUP_ID required on Railway.")


class Config:
    TELEGRAM_TOKEN       = _load_token()
    _ini                 = _load_ini()
    DRIVER_GROUP_ID      = int(os.getenv("DRIVER_GROUP_ID")  or (_ini.get("channels", "driver_group_id",  fallback="0") if _ini else "0"))
    REPORTS_GROUP_ID     = int(os.getenv("REPORTS_GROUP_ID") or (_ini.get("channels", "reports_group_id", fallback="0") if _ini else "0"))
    AI_ALERTS_CHANNEL_ID = int(os.getenv("AI_ALERTS_CHANNEL_ID") or (_ini.get("channels", "ai_alerts_channel_id", fallback="0") if _ini else "0"))
    WEBHOOK_URL          = os.getenv("WEBHOOK_URL", "").strip()
    WEBHOOK_SECRET       = os.getenv("WEBHOOK_SECRET", "").strip()
    USE_WEBHOOK          = bool(WEBHOOK_URL)
    DATA_DIR             = os.getenv("DATA_DIR", "/app/data")


config = Config()
