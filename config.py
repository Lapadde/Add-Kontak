import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# ─── Masa aktif layanan (tanggal TERAKHIR bot masih boleh dipakai, kalender server) ───
# Setelah tanggal ini: semua fitur diblokir sampai tanggal diperbarui & bot di-restart.
SERVICE_LAST_VALID_YEAR = 2026
SERVICE_LAST_VALID_MONTH = 5
SERVICE_LAST_VALID_DAY = 30

# Kontak perpanjang layanan (tombol URL di bot)
SUPPORT_WHATSAPP_NUMBER = os.getenv("SUPPORT_WHATSAPP_NUMBER", "6288704220953")
SUPPORT_TELEGRAM_USERNAME = os.getenv("SUPPORT_TELEGRAM_USERNAME", "mulaikosi").lstrip("@")

# Bot Telegram Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")

# API Credentials (dari https://my.telegram.org/apps)
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")

# Session Directory
SESSION_DIR = "sessions/users"

# Admin User IDs (pisahkan dengan koma jika lebih dari satu)
# Contoh: ADMIN_USER_IDS = [123456789, 987654321]
# Atau dari environment variable: ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
ADMIN_USER_IDS = []
# admin_ids_str = os.getenv("ADMIN_USER_IDS", "7103599889, 6987171667, 5461528568, 1457643716")
admin_ids_str = os.getenv("ADMIN_USER_IDS", "6987171667, 1839496427")
# admin_ids_str = os.getenv("ADMIN_USER_IDS", "6987171667")
if admin_ids_str:
    try:
        ADMIN_USER_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]
    except ValueError:
        ADMIN_USER_IDS = []
        print("⚠️ Warning: ADMIN_USER_IDS format tidak valid, menggunakan list kosong")

# Ensure session directory exists
os.makedirs(SESSION_DIR, exist_ok=True)


def get_service_last_valid_date() -> date:
    return date(SERVICE_LAST_VALID_YEAR, SERVICE_LAST_VALID_MONTH, SERVICE_LAST_VALID_DAY)


def is_service_expired() -> bool:
    return date.today() > get_service_last_valid_date()


def support_whatsapp_url() -> str:
    n = SUPPORT_WHATSAPP_NUMBER.strip().replace("+", "").replace(" ", "")
    return f"https://wa.me/{n}"


def support_telegram_url() -> str:
    u = SUPPORT_TELEGRAM_USERNAME.strip().lstrip("@")
    return f"https://t.me/{u}"

