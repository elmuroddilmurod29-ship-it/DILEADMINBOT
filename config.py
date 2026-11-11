"""
CONFIG.PY - Kafil / Garant Telegram Boti Konfiguratsiyasi
"""
import os
import re
from datetime import timezone, timedelta
from typing import Any, List
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN", "8479391754:AAFavvM86ZNJsn9VAv_I2krOcGTmBkY2zzY").strip()

# Admin Telegram ID raqamlari
ADMIN_ID_RAW = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "6125160021"))
ADMIN_IDS: List[int] = [int(x.strip()) for x in ADMIN_ID_RAW.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = [6125160021]

# Google Gemini AI Sozlamalari
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6JMo868rFZogyeUVQKd-GUplYFwYlZ74YZcjia26W_ObA").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

# TolovAPI.uz sozlamalari
TOLOV_API_URL = os.getenv("TOLOV_API", "https://tolovapi.uz/api/").rstrip("/") + "/"
SHOP_ID = os.getenv("SHOP_ID", "339").strip()
SHOP_KEY = os.getenv("SHOP_KEY", "cFpP7PdwqrDsVAdX").strip()

# Kafil rasmiy karta rekvizitlari
DEFAULT_CARD_NUMBER = os.getenv("CARD_NUMBER", "5614 6835 1125 2157").strip()
DEFAULT_CARD_OWNER = os.getenv("CARD_OWNER", "TILLAYEVA NASIBA").strip()

# Render Web Service Port sozlamasi
PORT = int(os.getenv("PORT", "10000"))

# SQLite ma'lumotlar bazasi fayli
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Toshkent vaqt mintaqasi (UTC+5)
TZ = timezone(timedelta(hours=5))

# Yordamchi formatlash funksiyalari
def esc_html(text: Any) -> str:
    """HTML maxsus belgilarini xavfsiz formatlaydi."""
    if text is None:
        return ""
    s = str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def fmt(num: Any) -> str:
    """Raqamlarni chiroyli formatda ajratadi (masalan: 50 000)."""
    try:
        val = int(num)
        return f"{val:,}".replace(",", " ")
    except Exception:
        return str(num)

def format_card(card: Any) -> str:
    """16 xonali karta raqamini chiroyli ajratib beradi (5614 6835 1125 2157)."""
    if not card:
        return ""
    digits = re.sub(r"\D", "", str(card))
    if len(digits) == 16:
        return f"{digits[0:4]} {digits[4:8]} {digits[8:12]} {digits[12:16]}"
    return str(card)

def is_admin(user_id: int) -> bool:
    """Foydalanuvchi admin ekanligini tekshiradi."""
    if user_id in ADMIN_IDS:
        return True
    try:
        import database as db
        custom = db.get_setting("extra_admins", "")
        if custom and str(user_id) in [x.strip() for x in custom.split(",")]:
            return True
    except Exception:
        pass
    return False
