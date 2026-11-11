"""Configuration loaded from the project's .env file."""
from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
DB_PATH = Path(os.getenv('DB_PATH', str(ROOT / 'data' / 'prices.sqlite3'))).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH
OZON_HEADLESS = os.getenv('OZON_HEADLESS', 'false').lower() in ('1', 'true', 'yes')
OZON_TIMEOUT_MS = max(5000, int(os.getenv('OZON_TIMEOUT_MS', '30000')))
CHECK_INTERVAL_MIN = max(15, int(os.getenv('CHECK_INTERVAL_MIN', '60')))
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
SCHEDULER_ENABLED = os.getenv('SCHEDULER_ENABLED', 'true').lower() == 'true'
APP_PASSWORD = os.getenv('APP_PASSWORD', '').strip()
APP_TIMEZONE = os.getenv('APP_TIMEZONE', 'Europe/Moscow').strip()

# Telegram responds to user commands and buttons. There are NO scheduled price messages.
TELEGRAM_POLLING_ENABLED = os.getenv('TELEGRAM_POLLING_ENABLED', 'true').lower() in ('1', 'true', 'yes')

BACKUP_INTERVAL_HOURS = max(1, int(os.getenv('BACKUP_INTERVAL_HOURS', '24')))
