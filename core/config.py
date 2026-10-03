import os
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv('DB_PATH', str(BASE_DIR / 'data' / 'bot.db')))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
TZ = ZoneInfo(os.getenv('TIMEZONE', 'Europe/Moscow'))
TIMEZONE = TZ
VERSION = os.getenv('BOT_VERSION', '10.0.0')
HELP_USERNAME = os.getenv('HELP_USERNAME', '@miravynvoida')
MINI_APP_URL = os.getenv('MINI_APP_URL', '').rstrip('/')
WEB_HOST = os.getenv('WEB_HOST', '0.0.0.0')
WEB_PORT = int(os.getenv('PORT', os.getenv('WEB_PORT', '3000')))
TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
INIT_DATA_MAX_AGE = int(os.getenv('TELEGRAM_INIT_DATA_MAX_AGE', '86400'))
