"""
Kanal boshqaruv boti (aiogram 3 + Gemini + SQLite + Flux AI + Web Photos)

Yangi imkoniyatlar:
- 100% kafolatlangan Post va So'rovnoma yuborish (xatolik bo'lsa zaxiraga o'tish)
- Har qanday formatdagi kanal havolalarini (@..., https://t.me/..., ID) qo'llab-quvvatlash
- Kengaytirilgan moderatsiya (so'kinish, haqorat, kamsitish, firibgarlik/masheniklikka qarshi)
- 20 soniyada qoidabuzarlik xabarlarini tozalash
- Guruh chatidagi har bir a'zoga xushmuomala javob / minnatdorchilik bildirish
- Rang-barang rasmlar (Flux AI har xil uslublar + Internetdan haqiqiy fotolar)
- Renderda kanallar o'chib ketmasligi uchun avtomatik JSON zaxira va tiklash
- Admin tomonidan karta raqami va egasini bot ichidan o'zgartirish
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

load_dotenv()

# ============================ SOZLAMALAR ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
DEFAULT_YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
YOUTUBE_API_KEY = DEFAULT_YOUTUBE_API_KEY
ADMIN_IDS_RAW = os.getenv("ADMIN_ID") or os.getenv("ADMIN_IDS", "6125160021")
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.replace(";", ",").split(",") if x.strip() and x.strip().isdigit()}

def is_admin(user_id: int) -> bool:
    return bool(user_id and (user_id in ADMIN_IDS))

AdminMsg = F.from_user.id.in_(ADMIN_IDS)
DEFAULT_CARD_NUMBER = os.getenv("CARD_NUMBER", "5614 6835 1125 2157")
DEFAULT_CARD_OWNER = os.getenv("CARD_OWNER", "TILLAYEVA NASIBA")

# TolovAPI.uz sozlamalari (To'liq asinxron aiohttp integratsiyasi)
TOLOV_API = os.getenv("TOLOV_API", "https://tolovapi.uz/api/").rstrip("/")
SHOP_ID = str(os.getenv("SHOP_ID", "339")).strip()
SHOP_KEY = str(os.getenv("SHOP_KEY", "")).strip()
TZ = ZoneInfo("Asia/Tashkent")
BOT_START_TIME = datetime.now(TZ)

def get_bot_uptime() -> str:
    diff = datetime.now(TZ) - BOT_START_TIME
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days} kun")
    if hours > 0:
        parts.append(f"{hours} soat")
    if minutes > 0:
        parts.append(f"{minutes} daqiqa")
    if not parts:
        parts.append(f"{seconds} soniya")
    return ", ".join(parts)

# 3 ta optimal Freemium tarif modeli
PLANS = {
    "start": {
        "name": "START (Bepul)",
        "price": 0,
        "channels": 1,
        "max_groups": 1,
        "daily_post_limit": 1,
        "hours": [12],
        "moderation": "standard",
        "ai_chat": False,
        "has_grounding": False,
        "has_youtube": False,
        "has_cliffhanger": False,
        "watermark": True,
        "polls": False,
        "desc": "1 ta kanal/guruh, kuniga 1 ta post, standart moderatsiya (0.3s), bazaviy shablonlar",
    },
    "pro": {
        "name": "PRO KANAL",
        "price": 59000,
        "channels": 2,
        "max_groups": 2,
        "daily_post_limit": 3,
        "hours": [9, 14, 21],
        "moderation": "ai",
        "ai_chat": True,
        "has_grounding": True,
        "has_youtube": False,
        "has_cliffhanger": True,
        "watermark": False,
        "polls": True,
        "desc": "2 ta kanal + 2 ta guruh, kuniga 3 ta post, Serial-Klifxenger, Google Search faktlari, reklamasiz",
    },
    "max": {
        "name": "MAX MEDIA / BIZNES",
        "price": 149000,
        "channels": 5,
        "max_groups": 999,
        "daily_post_limit": 8,
        "hours": [8, 10, 12, 14, 16, 18, 20, 22],
        "moderation": "vip",
        "ai_chat": True,
        "has_grounding": True,
        "has_youtube": True,
        "has_cliffhanger": True,
        "watermark": False,
        "polls": True,
        "desc": "5 ta kanal + cheksiz guruh, kuniga 8 ta post, Google + YouTube Data API trendlari, VIP moderatsiya, so'rovnomalar",
    },
}
# Qadimgi yozuvlar uchun moslashtirish (Backwards compatibility)
PLANS["basic"] = PLANS["start"]
PLANS["standard"] = PLANS["pro"]

PLAN_DAYS = 30

# ============================ KENGAYTIRILGAN MODERATSIYA ============================
ROOT_PROFANITIES = [
    r"(?:pashol\s*|di\s*|poshol\s*)?na[hx]u[yj]\w*",
    r"dalba(?:yo|ye|e|o)[bp]\w*",
    r"sik[a-z0-9']*\w*",
    r"[qk][o'u]?t[o']?[qk]\w*",
    r"gandon\w*",
    r"(?:z?h|j)ala[bp]\w*",
    r"ch[ou]rt\w*",
    r"[ye]iban\w*",
    r"eblan\w*",
    r"bl[ya]+[td]?\w*",
    r"suk[ai]\w*",
    r"haromi\w*",
    r"onang[nd]i\w*",
    r"foxish[aa]\w*",
    r"porno\w*",
    r"pedik\w*",
    r"pizd[ae]\w*",
    r"shilta\w*",
    r"qanjiq\w*",
    r"xuy\w*",
    r"zaeb\w*",
    r"poxuy\w*"
]

STANDALONE_PROFANITIES = [
    r"ahmoq\w*", r"itvachcha\w*", r"padariga\w*", r"chmo\w*", r"lox\w*",
    r"tupoy\w*", r"tentak\w*", r"mol\b", r"eshak\w*", r"maraz\w*", r"jinni\w*"
]

PROFANITY_RE = re.compile(
    r"(" + "|".join(ROOT_PROFANITIES) + r"|(?<!\w)(?:" + "|".join(STANDALONE_PROFANITIES) + r")(?!\w))",
    re.I
)

SCAM_PATTERNS = [
    r"kuniga\s*\d+\$?", r"kunlik\s*daromad", r"oyiga\s*\d+\$?",
    r"daromad\s*100%", r"investitsiya\s*(?:qiling|qilib)?", r"lichkaga\s*(?:yozing|o'ting)?",
    r"\blich\b", r"\blichka\b", r"pul\s*ko'paytirish", r"garantiyali\s*daromad",
    r"vznos", r"kripto(?:valyuta)?\s*ko'paytir", r"pul\s*tikib", r"tez\s*boyish",
    r"sxema\s*bor", r"tekinga\s*\d+\$?", r"tekin\s*pul", r"oson\s*pul",
    r"adminman\s*pul", r"parolingizni", r"1xbet", r"stavka", r"linebet",
    r"melbet", r"mostbet", r"kazinolar?", r"kripto\s*piramida",
    r"shuncha\s*pul\s*ishladim", r"sarmoyasiz\s*daromad",
    r"1win", r"aviator", r"lucky\s*jet", r"bukmeker", r"ruletka",
    r"t\.me/\+", r"t\.me/joinchat/", r"t\.me/[a-zA-Z0-9_]+bot\?start=",
    r"bit\.ly/", r"clck\.ru/", r"tinyurl\.com/", r"pul\s*ishlash", r"sarmoya",
    r"kripto\s*bot"
]
SCAM_RE = re.compile(r"(" + "|".join(SCAM_PATTERNS) + r")", re.I)


def normalize_text_for_moderation(text: str) -> str:
    """
    Keltirilgan matnni leetspeak, kirill-lotin transliteratsiyasi va
    harflar orasidagi bo'shliqlar hamda belgilardan tozalaydi.
    """
    if not text:
        return ""
    t = text.lower()
    trans_map = {
        '@': 'a', '$': 's', '0': 'o', '1': 'i', '!': 'i', '|': 'i', '3': 'e',
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'j', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'x', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sh',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'ў': 'o', 'қ': 'q', 'ғ': 'g', 'ҳ': 'h'
    }
    cleaned = [trans_map.get(ch, ch) for ch in t]
    norm = "".join(cleaned)
    compact = re.sub(r"[\s\.\,\-\_\*\#\/\\]+", "", norm)
    return f"{norm} {compact}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)

# Gemini AI (v1beta)
ai = genai.Client(api_key=GEMINI_API_KEY, http_options={"api_version": "v1beta"})

# ============================ BAZA VA MIGRATSIYA ============================
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        plan TEXT, expires_at TEXT, banned INTEGER DEFAULT 0, joined_at TEXT
    );
    CREATE TABLE IF NOT EXISTS channels (
        channel_id INTEGER PRIMARY KEY, owner_id INTEGER, title TEXT, niche TEXT, audience TEXT,
        group_id INTEGER, active INTEGER DEFAULT 1,
        posts_sent INTEGER DEFAULT 0, join_requests INTEGER DEFAULT 0,
        joined INTEGER DEFAULT 0, left_count INTEGER DEFAULT 0,
        group_messages INTEGER DEFAULT 0, warnings INTEGER DEFAULT 0, bans INTEGER DEFAULT 0,
        detailed_prompt TEXT DEFAULT '', custom_hours TEXT DEFAULT '',
        polls_sent INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT, amount INTEGER,
        file_id TEXT, status TEXT DEFAULT 'pending', created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS violations (
        chat_id INTEGER, user_id INTEGER, warnings INTEGER DEFAULT 0, cycle INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS required (chat TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS recent_posts (channel_id INTEGER, text TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS recent_images (channel_id INTEGER, image_key TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS chat_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, user_id INTEGER,
        user_name TEXT, text TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY,
        plan_name TEXT DEFAULT 'start',
        expires_at TEXT,
        daily_posts_count INTEGER DEFAULT 0,
        last_post_date TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS deals (
        deal_id TEXT PRIMARY KEY,
        buyer_id INTEGER,
        buyer_name TEXT,
        seller_id INTEGER,
        seller_name TEXT,
        title TEXT,
        amount INTEGER,
        status TEXT DEFAULT 'created',
        payment_id TEXT,
        seller_card TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id TEXT,
        user_id INTEGER,
        full_name TEXT,
        card_number TEXT,
        amount INTEGER,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    );
    """)
    # Baza barqarorligi: WAL rejimi, timeout va indekslar
    try:
        db.execute("PRAGMA journal_mode = WAL;")
        db.execute("PRAGMA busy_timeout = 10000;")
        db.execute("CREATE INDEX IF NOT EXISTS idx_recent_posts_channel ON recent_posts(channel_id, created_at DESC);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_recent_images ON recent_images(channel_id, created_at DESC);")
        db.commit()
    except Exception as e_pragma:
        logging.warning(f"PRAGMA sozlamalari xatosi: {e_pragma}")

    for col, col_type in [
        ("detailed_prompt", "TEXT DEFAULT ''"),
        ("custom_hours", "TEXT DEFAULT ''"),
        ("polls_sent", "INTEGER DEFAULT 0"),
        ("group_messages", "INTEGER DEFAULT 0"),
        ("warnings", "INTEGER DEFAULT 0"),
        ("bans", "INTEGER DEFAULT 0"),
        ("current_topic", "TEXT DEFAULT ''"),
        ("arc_day", "INTEGER DEFAULT 1"),
        ("arc_total_days", "INTEGER DEFAULT 3"),
        ("last_cliffhanger", "TEXT DEFAULT ''"),
    ]:
        try:
            db.execute(f"ALTER TABLE channels ADD COLUMN {col} {col_type}")
            db.commit()
        except Exception:
            pass

    # Baza boshlang'ich sozlamalari
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_number', ?)", (DEFAULT_CARD_NUMBER,))
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_owner', ?)", (DEFAULT_CARD_OWNER,))
    # Eski zaxira yoki placeholder kartani rasmiy rekvizitlarga yangilash
    db.execute("UPDATE settings SET value=? WHERE key='card_number' AND (value='8600 0000 0000 0000' OR value IS NULL OR value='')", (DEFAULT_CARD_NUMBER,))
    db.execute("UPDATE settings SET value=? WHERE key='card_owner' AND (value='Admin' OR value IS NULL OR value='')", (DEFAULT_CARD_OWNER,))
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (GEMINI_API_KEY,))
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('groq_api_key', ?)", (GROQ_API_KEY,))
    db.execute("INSERT INTO settings (key, value) VALUES ('youtube_api_key', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NULL OR value=''", (YOUTUBE_API_KEY,))
    
    # Mavjud foydalanuvchilarni obunalar jadvaliga sinxronlash
    try:
        today_s = datetime.now(TZ).strftime("%Y-%m-%d")
        db.execute("""
            INSERT OR IGNORE INTO subscriptions (user_id, plan_name, expires_at, daily_posts_count, last_post_date, created_at)
            SELECT user_id, 
                   CASE 
                     WHEN plan = 'max' THEN 'max'
                     WHEN plan IN ('standard', 'pro') THEN 'pro'
                     ELSE 'start'
                   END,
                   expires_at,
                   0,
                   ?,
                   COALESCE(joined_at, '')
            FROM users
        """, (today_s,))
    except Exception as e_sub_init:
        logging.warning(f"Obunalar sinxronizatsiya xatosi: {e_sub_init}")
    db.commit()

init_db()


def q(sql, args=(), one=False, commit=False):
    for attempt in range(4):
        try:
            cur = db.execute(sql, args)
            if commit:
                db.commit()
            return cur.fetchone() if one else cur.fetchall()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 3:
                time.sleep(0.08 * (attempt + 1))
                continue
            logging.error(f"Baza xatosi (q): {e} | SQL: {sql}")
            raise e
        except Exception as e:
            logging.error(f"Baza xatosi (q): {e} | SQL: {sql}")
            raise e


def now_utc():
    return datetime.now(timezone.utc)


def fmt(dt):
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def get_card_info():
    r_num = q("SELECT value FROM settings WHERE key='card_number'", one=True)
    r_owner = q("SELECT value FROM settings WHERE key='card_owner'", one=True)
    num = r_num["value"] if r_num and r_num["value"] else DEFAULT_CARD_NUMBER
    owner = r_owner["value"] if r_owner and r_owner["value"] else DEFAULT_CARD_OWNER
    return num, owner


def get_gemini_api_key():
    try:
        r = q("SELECT value FROM settings WHERE key='gemini_api_key'", one=True)
        if r and r["value"]:
            return r["value"].strip()
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY", "").strip()


def get_gemini_client():
    key = get_gemini_api_key()
    return genai.Client(api_key=key, http_options={"api_version": "v1beta"})


async def test_gemini_key(key: str = None):
    test_k = key.strip() if key else get_gemini_api_key()
    if not test_k:
        return False, "Kalit kiritilmagan (bo'sh)!"
    if test_k.startswith("AQ."):
        return False, (
            "Kiritilgan kalit formati noto'g'ri (AQ... bilan boshlanadi). "
            "Google AI Studio API kaliti har doim «AIzaSy...» bilan boshlanadi! "
            "Uni https://aistudio.google.com/app/apikey manzilidan «Create API key» bosib bepul olishingiz mumkin."
        )
    models_to_test = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]
    last_e = ""
    # 1. google-genai SDK orqali sinash
    try:
        test_client = genai.Client(api_key=test_k, http_options={"api_version": "v1beta"})
        for m in models_to_test:
            try:
                res = await test_client.aio.models.generate_content(model=m, contents="Test")
                if res and (res.text or "").strip():
                    return True, f"✅ Aloqa o'rnatildi! ({m} modeli javob berdi)"
            except Exception as e:
                last_e = str(e)
    except Exception as e:
        last_e = str(e)

    # 2. To'g'ridan-to'g'ri aiohttp REST orqali sinash (fallback)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            for m in models_to_test:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={test_k}"
                body = {"contents": [{"parts": [{"text": "Test"}]}]}
                async with session.post(url, json=body) as r:
                    if r.status == 200:
                        return True, f"✅ Aloqa o'rnatildi! (REST {m} modeli javob berdi)"
                    else:
                        resp_data = await r.json()
                        err_msg = resp_data.get("error", {}).get("message", "")
                        if err_msg:
                            last_e = err_msg
    except Exception as e_rest:
        if not last_e:
            last_e = str(e_rest)

    if "UNAUTHENTICATED" in last_e or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in last_e or "API_KEY_INVALID" in last_e or "API key not valid" in last_e:
        return False, (
            f"Google xatosi (401 Noto'g'ri kalit): Kalit qabul qilinmadi. "
            "Iltimos, https://aistudio.google.com/app/apikey manzilidan yangi «AIzaSy...» kalit oling."
        )
    return False, f"Google xatosi: {last_e or 'Javob olinmadi'}"


# ============================ ASINXRON TO'LOV TIZIMI (TolovAPI.uz) ============================
async def tolov_api_request(endpoint: str, method: str = "POST", data: dict = None) -> dict:
    """
    TolovAPI.uz bilan to'liq asinxron aloqa (aiohttp orqali).
    requests yoki time.sleep mutlaqo ishlatilmaydi.
    """
    url = f"{TOLOV_API}/{endpoint.lstrip('/')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Shop-Key": SHOP_KEY,
        "Content-Type": "application/json"
    }
    payload = dict(data or {})
    payload.setdefault("shop_id", SHOP_ID)
    payload.setdefault("shop_key", SHOP_KEY)
    
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            if method.upper() == "POST":
                async with session.post(url, json=payload) as resp:
                    text = await resp.text()
                    try:
                        return json.loads(text)
                    except Exception:
                        return {"ok": False, "status_code": resp.status, "text": text}
            else:
                async with session.get(url, params=payload) as resp:
                    text = await resp.text()
                    try:
                        return json.loads(text)
                    except Exception:
                        return {"ok": False, "status_code": resp.status, "text": text}
    except Exception as e:
        logging.error(f"TolovAPI aloqa xatosi ({endpoint}): {type(e).__name__} - {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def create_payment_async(amount: int, order_id: str = None, user_id: int = None) -> dict:
    """
    TolovAPI orqali asinxron to'lov yaratish.
    Agar summa band bo'lsa (amount_busy), res['suggested_amount'] bo'yicha qayta to'lov yaratiladi.
    """
    current_amount = int(amount)
    data = {"amount": current_amount}
    if order_id:
        data["order_id"] = str(order_id)
    if user_id:
        data["user_id"] = int(user_id)
        
    res = await tolov_api_request("payment/create", method="POST", data=data)
    
    # Agar summa band bo'lsa (error_code == "amount_busy"), res["suggested_amount"] orqali qayta urinish
    if not res.get("ok") and res.get("error_code") == "amount_busy":
        suggested = res.get("suggested_amount")
        if suggested:
            logging.info(f"TolovAPI: Summa {current_amount} band, suggested_amount={suggested} bo'yicha qayta yaratilmoqda...")
            data["amount"] = int(suggested)
            res = await tolov_api_request("payment/create", method="POST", data=data)
            
    return res


async def check_payment_status_async(payment_id: str) -> dict:
    """
    TolovAPI orqali to'lov holatini tekshirish.
    """
    data = {"payment_id": str(payment_id)}
    return await tolov_api_request("payment/status", method="POST", data=data)


async def cancel_payment_async(payment_id: str) -> dict:
    """
    TolovAPI orqali to'lovni bekor qilish.
    """
    data = {"payment_id": str(payment_id)}
    return await tolov_api_request("payment/cancel", method="POST", data=data)


async def watch_payment_task(user_id: int, payment_id: str, plan_key: str = None, amount: int = 0, deal_id: str = None):
    """
    asyncio.create_task() orqali ishlaydigan fon vazifasi (background worker).
    Har 3 soniyada to'lovni tekshiradi (maksimal 5 daqiqa = 100 marta).
    Foydalanuvchi to'laganda avtomatik unga xabar yuboriladi va xizmat yoqiladi.
    """
    max_checks = 100
    for _ in range(max_checks):
        await asyncio.sleep(3)
        try:
            status_res = await check_payment_status_async(payment_id)
            if status_res and status_res.get("status") == "paid":
                # TO'LOV MUVAFFAQIYATLI!
                if plan_key:
                    exp = apply_plan(user_id, plan_key)
                    plan_info = PLANS.get(plan_key, {})
                    p_name = plan_info.get("name", plan_key)
                    msg_text = (
                        f"🎉 <b>TO'LOV MUVAFFAQIYATLI QABUL QILINDI!</b>\n\n"
                        f"💎 <b>Faollashtirilgan tarif:</b> <b>{p_name}</b>\n"
                        f"💵 <b>To'langan summa:</b> <code>{amount:,} so'm</code>\n"
                        f"⏳ <b>Amal qilish muddati:</b> <code>{fmt(exp)}</code> gacha\n\n"
                        f"✨ Barcha imkoniyatlar avtomatik tarzda yoqildi! Xizmatdan unumli foydalaning."
                    ).replace(",", " ")
                    try:
                        await bot.send_message(user_id, msg_text, reply_markup=MENU)
                    except Exception as e_send:
                        logging.error(f"Foydalanuvchiga to'lov tasdig'i yuborilmadi ({user_id}): {e_send}")
                        
                    for aid in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                aid,
                                f"💰 <b>YANGI AVTOMATIK TO'LOV (TolovAPI)!</b>\n\n"
                                f"👤 <b>Foydalanuvchi ID:</b> <code>{user_id}</code>\n"
                                f"💎 <b>Tarif:</b> <b>{p_name}</b>\n"
                                f"💵 <b>Summa:</b> <code>{amount:,} so'm</code>\n"
                                f"🆔 <b>Payment ID:</b> <code>{payment_id}</code>".replace(",", " ")
                            )
                        except Exception:
                            pass
                elif deal_id:
                    q("UPDATE deals SET status='in_trade', updated_at=? WHERE deal_id=?", (now_utc().isoformat(), deal_id), commit=True)
                    d = q("SELECT * FROM deals WHERE deal_id=?", (deal_id,), one=True)
                    if d:
                        buyer_msg = (
                            f"✅ <b>Kafolat to'lovi muvaffaqiyatli qabul qilindi! (#{deal_id})</b>\n\n"
                            f"💵 Mablag' xavfsiz kafolat hisobiga olindi. Sotuvchi mahsulot yoki xizmatni topshirishi mumkin.\n"
                            f"Savdo holatini «🆘 Yordam / Nizolar» orqali kuzatib borishingiz mumkin."
                        )
                        try:
                            await bot.send_message(d["buyer_id"], buyer_msg, reply_markup=MENU)
                        except Exception:
                            pass
                        if d["seller_id"]:
                            seller_msg = (
                                f"🔔 <b>Xaridor to'lovni amalga oshirdi! (#{deal_id})</b>\n\n"
                                f"💵 Tovar summasi: <code>{d['amount']:,} so'm</code> kafolat hisobida saqlanmoqda.\n"
                                f"Siz mahsulot yoki xizmatni xaridorga taqdim etishingiz mumkin!".replace(",", " ")
                            )
                            try:
                                await bot.send_message(d["seller_id"], seller_msg, reply_markup=MENU)
                            except Exception:
                                pass
                return
            elif status_res and status_res.get("status") == "cancel":
                logging.info(f"TolovAPI: to'lov bekor qilingan: {payment_id}")
                return
        except Exception as e_watch:
            logging.error(f"watch_payment_task xatosi ({payment_id}): {e_watch}")


def get_groq_api_key():
    try:
        r = q("SELECT value FROM settings WHERE key='groq_api_key'", one=True)
        if r and r["value"]:
            return r["value"].strip()
    except Exception:
        pass
    return os.getenv("GROQ_API_KEY", "").strip()


async def ask_groq_ai(prompt: str, system_prompt: str = None, json_mode: bool = False):
    key = get_groq_api_key()
    if not key:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    models_to_try = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    for m in models_to_try:
        try:
            payload = {
                "model": m,
                "messages": messages,
                "max_tokens": 1200,
                "temperature": 0.95
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            msg_obj = choices[0].get("message", {})
                            txt = (msg_obj.get("content") or "").strip()
                            if txt:
                                return txt
        except Exception as e:
            logging.warning(f"Groq ({m}) xatosi: {e}")
            continue
    return None


async def test_groq_key(key: str = None):
    test_k = key.strip() if key else get_groq_api_key()
    if not test_k:
        return False, "Groq kaliti kiritilmagan!"
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {test_k}", "Content-Type": "application/json"}
    payload = {
        "model": "qwen/qwen3.8-27b",
        "messages": [{"role": "user", "content": "Salom"}],
        "max_tokens": 30
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    return True, "✅ Groq AI muvaffaqiyatli ulandi va chaqmoqdek tez javob berdi!"
                txt = await resp.text()
                return False, f"Groq xatosi ({resp.status}): {txt[:120]}"
    except Exception as e:
        return False, f"Groq ulanish xatosi: {e}"


def get_youtube_api_key():
    try:
        r = q("SELECT value FROM settings WHERE key='youtube_api_key'", one=True)
        if r and r["value"] and r["value"].strip():
            return r["value"].strip()
    except Exception:
        pass
    return os.getenv("YOUTUBE_API_KEY", DEFAULT_YOUTUBE_API_KEY).strip()


async def test_youtube_key(key: str = None):
    test_k = key.strip() if key else get_youtube_api_key()
    if not test_k:
        return False, "YouTube API kaliti kiritilmagan!"
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part": "snippet", "q": "test", "maxResults": 1, "key": test_k}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    return True, "✅ YouTube Data API v3 muvaffaqiyatli ulandi va so'rovga javob berdi!"
                txt = await resp.text()
                return False, f"YouTube API xatosi ({resp.status}): {txt[:120]}"
    except Exception as e:
        return False, f"YouTube ulanish xatosi: {e}"


async def fetch_trending_youtube_videos(query: str, api_key: str = None, max_results: int = 3):
    """
    YouTube Data API v3 ning 'search' endpointi orqali berilgan mavzu bo'yicha
    so'nggi 7 kundagi eng ommabop 3 ta trend videolarni tortib oladi.
    Parametrlar:
      - key: YOUTUBE_API_KEY yoki api_key
      - part: 'snippet'
      - q: kanal mavzusi (niche)
      - type: 'video'
      - order: 'viewCount'
      - maxResults: 3
      - publishedAfter: oxirgi 7 kunlik sana
    Natijadan faqat videolarning sarlavhasi (title) va qisqacha tavsifi (description) ajratib olinadi.
    Xatolik yuz berganda try-catch orqali bot to'xtab qolmaydi.
    """
    videos = []
    active_key = (api_key or get_youtube_api_key() or DEFAULT_YOUTUBE_API_KEY).strip()
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 1. Rasmiy YouTube Data API v3
    if active_key:
        try:
            url = "https://www.googleapis.com/youtube/v3/search"
            params = {
                "key": active_key,
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": "viewCount",
                "maxResults": max_results,
                "publishedAfter": seven_days_ago
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("items", []):
                            snippet = item.get("snippet", {})
                            t = (snippet.get("title") or "").strip()
                            d = (snippet.get("description") or "").strip()
                            vid_id = item.get("id", {}).get("videoId", "")
                            if t:
                                videos.append({
                                    "title": html.unescape(t),
                                    "description": html.unescape(d)[:300],
                                    "url": f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
                                })
                        if videos:
                            return videos[:max_results]
                    else:
                        txt = await resp.text()
                        logging.warning(f"YouTube Data API v3 javobi ({resp.status}): {txt[:120]}")
        except Exception as e:
            logging.warning(f"YouTube Data API v3 xatosi: {e}")

    # 2. YouTube Qidiruv tahlili (Fallback - kvota tugaganda yoki internet uzilganda bot to'xtab qolmasligi uchun)
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://www.youtube.com/results?search_query={encoded}&sp=EgQIAxAB"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "uz,en;q=0.9"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    matches = re.findall(r'"videoRenderer":\{"videoId":"([^"]+)".*?"title":\{"runs":\[\{"text":"([^"]+)"\}.*?"descriptionSnippet":\{"runs":\[\{"text":"([^"]*)"', text)
                    if not matches:
                        v_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', text)
                        v_titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}', text)
                        for i in range(min(len(v_ids), len(v_titles), max_results)):
                            videos.append({
                                "title": html.unescape(v_titles[i]),
                                "description": f"{query} bo'yicha eng dolzarb video darslik",
                                "url": f"https://www.youtube.com/watch?v={v_ids[i]}"
                            })
                    else:
                        for m in matches[:max_results]:
                            videos.append({
                                "title": html.unescape(m[1]),
                                "description": html.unescape(m[2])[:300],
                                "url": f"https://www.youtube.com/watch?v={m[0]}"
                            })
    except Exception as e:
        logging.warning(f"YouTube fallback scraper xatosi: {e}")

    if not videos:
        videos = [
            {
                "title": f"{query}: 2026-yilgi eng yangi amaliy qo'llanma",
                "description": "Eng so'nggi yangiliklar, tavsiyalar va amaliy keyslar",
                "url": f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
            }
        ]
    return videos[:max_results]


async def fetch_google_trending_facts(query: str, max_results: int = 3):
    """
    Google News / Grounding orqali so'nggi yangilik va trend faktlarni oladi.
    """
    facts = []
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        root = ET.fromstring(text)
                        for item in root.findall(".//item")[:max_results]:
                            title = item.findtext("title")
                            if title:
                                clean_t = re.sub(r" - [^-]+$", "", title)
                                facts.append(clean_t.strip())
                    except Exception:
                        pass
    except Exception as e:
        logging.warning(f"Google News RSS xatosi: {e}")

    if not facts:
        facts = [
            f"2026-yilda {query} sohasida AI vositalari va avtomatlashtirish eng yuqori o'sish ko'rsatmoqda.",
            f"Global bozorda {query} bo'yicha samaradorlikni oshirish va amaliy keyslar asosiy talabga aylandi."
        ]
    return facts[:max_results]


def get_user_subscription(uid: int):
    """
    Foydalanuvchi obuna ma'lumotlarini qaytaradi.
    Muddati tugagan obunalarni avtomatik aniqlaydi va kunlik postlar sonini har kuni yangilaydi.
    """
    today_s = datetime.now(TZ).strftime("%Y-%m-%d")
    sub = q("SELECT * FROM subscriptions WHERE user_id=?", (uid,), one=True)
    if not sub:
        u = q("SELECT plan, expires_at, joined_at FROM users WHERE user_id=?", (uid,), one=True)
        plan_name = "start"
        exp_iso = None
        if u and u["plan"]:
            raw_p = u["plan"]
            if raw_p == "max":
                plan_name = "max"
            elif raw_p in ("pro", "standard"):
                plan_name = "pro"
            else:
                plan_name = "start"
            exp_iso = u["expires_at"]
        q("""INSERT INTO subscriptions (user_id, plan_name, expires_at, daily_posts_count, last_post_date, created_at)
             VALUES (?,?,?,0,?,?)
             ON CONFLICT(user_id) DO NOTHING""",
          (uid, plan_name, exp_iso, today_s, now_utc().isoformat()), commit=True)
        sub = q("SELECT * FROM subscriptions WHERE user_id=?", (uid,), one=True)

    plan_name = (sub["plan_name"] if sub and sub["plan_name"] else "start").strip().lower()
    if plan_name not in PLANS:
        plan_name = "start"

    expires_at = sub["expires_at"] if sub else None
    daily_count = sub["daily_posts_count"] if sub else 0
    last_date = sub["last_post_date"] if sub else ""

    # Expire tekshiruvi: muddati tugagan pullik tariflarni startga tushirish
    exp_dt = None
    if expires_at and plan_name != "start":
        try:
            exp_dt = datetime.fromisoformat(expires_at)
            if exp_dt < now_utc():
                plan_name = "start"
                exp_dt = None
                q("UPDATE subscriptions SET plan_name='start', expires_at=NULL WHERE user_id=?", (uid,), commit=True)
                q("UPDATE users SET plan='start', expires_at=NULL WHERE user_id=?", (uid,), commit=True)
        except Exception:
            pass

    # Yangi kunda kunlik postlar hisoblagichini 0 ga tushirish
    if last_date != today_s:
        daily_count = 0
        q("UPDATE subscriptions SET daily_posts_count=0, last_post_date=? WHERE user_id=?", (today_s, uid), commit=True)

    if uid in ADMIN_IDS:
        plan_name = "max"
        exp_dt = datetime(2099, 1, 1, tzinfo=timezone.utc)

    plan_info = PLANS.get(plan_name, PLANS["start"])
    return {
        "user_id": uid,
        "plan_name": plan_name,
        "expires_at": exp_dt,
        "daily_posts_count": daily_count,
        "plan_info": plan_info
    }


def increment_daily_post_count(uid: int):
    """
    Foydalanuvchining bugungi chiqargan postlari sonini 1 taga oshiradi.
    """
    if not uid:
        return
    today_s = datetime.now(TZ).strftime("%Y-%m-%d")
    q("""UPDATE subscriptions 
         SET daily_posts_count = CASE WHEN last_post_date = ? THEN daily_posts_count + 1 ELSE 1 END,
             last_post_date = ?
         WHERE user_id=?""",
      (today_s, today_s, uid), commit=True)


def check_user_limits(uid: int, action_type: str) -> tuple[bool, str]:
    """
    Foydalanuvchi tarifi va limitlarini tekshiruvchi middleware.
    action_type: 'post', 'imgpost', 'poll', 'add_channel', 'add_group'
    """
    if uid in ADMIN_IDS:
        return True, ""

    sub = get_user_subscription(uid)
    p_info = sub["plan_info"]
    daily_used = sub["daily_posts_count"]
    daily_limit = p_info.get("daily_post_limit", 1)

    if action_type in ("post", "imgpost"):
        if daily_used >= daily_limit:
            msg = (
                f"⚠️ <b>Kunlik post limiti tugadi!</b>\n\n"
                f"Sizning <b>{p_info['name']}</b> tarifingiz bo'yicha kunlik limit: <b>{daily_limit} ta</b> post.\n"
                f"Bugun chiqarilgan: <b>{daily_used} ta</b> post.\n\n"
                f"💡 <i>Ko'proq post chiqarish va Google Search / YouTube trendlaridan foydalanish uchun tarifingizni yangilang:</i>\n\n"
                f"{plans_text()}"
            )
            return False, msg

    elif action_type == "poll":
        if not p_info.get("polls", False):
            msg = (
                f"⚠️ <b>So'rovnoma (Poll) funksiyasi cheklangan!</b>\n\n"
                f"Sizning <b>{p_info['name']}</b> tarifingizda so'rovnomalar mavjud emas.\n"
                f"Ushbu funksiya «PRO KANAL» va «MAX MEDIA» tariflarida faol.\n\n"
                f"💡 <i>Tarifingizni yangilash uchun tanlang:</i>\n\n"
                f"{plans_text()}"
            )
            return False, msg
        if daily_used >= daily_limit:
            msg = (
                f"⚠️ <b>Kunlik limit tugadi!</b>\n\n"
                f"Sizning <b>{p_info['name']}</b> tarifingiz bo'yicha kunlik limit: <b>{daily_limit} ta</b> xabar/so'rovnoma.\n"
                f"Bugun yuborilgan: <b>{daily_used} ta</b>.\n\n"
                f"💡 <i>Tarifingizni yangilang:</i>\n\n"
                f"{plans_text()}"
            )
            return False, msg

    elif action_type == "add_channel":
        ch_count = q("SELECT COUNT(*) c FROM channels WHERE owner_id=?", (uid,), one=True)["c"]
        max_ch = p_info.get("channels", 1)
        if ch_count >= max_ch:
            msg = (
                f"⚠️ <b>Kanal ulash limiti to'ldi!</b>\n\n"
                f"Sizning <b>{p_info['name']}</b> tarifingizda ko'pi bilan <b>{max_ch} ta</b> kanal ulash mumkin.\n"
                f"Sizda hozir ulangan: <b>{ch_count} ta</b> kanal.\n\n"
                f"💡 <i>Ko'proq kanal ulash uchun yuqoriroq tarifga o'ting:</i>\n\n"
                f"{plans_text()}"
            )
            return False, msg

    elif action_type == "add_group":
        grp_count = q("SELECT COUNT(DISTINCT group_id) c FROM channels WHERE owner_id=? AND group_id IS NOT NULL AND group_id != 0", (uid,), one=True)["c"]
        max_grp = p_info.get("max_groups", 1)
        if grp_count >= max_grp:
            msg = (
                f"⚠️ <b>Guruh nazorati limiti to'ldi!</b>\n\n"
                f"Sizning <b>{p_info['name']}</b> tarifingizda ko'pi bilan <b>{max_grp} ta</b> guruh ulash mumkin.\n"
                f"Sizda hozir ulangan: <b>{grp_count} ta</b> guruh.\n\n"
                f"💡 <i>Cheksiz guruhlarni nazorat qilish uchun «MAX MEDIA» tarifiga o'ting:</i>\n\n"
                f"{plans_text()}"
            )
            return False, msg

    return True, ""


def get_user_plan(uid):
    if uid in ADMIN_IDS:
        return "max", datetime(2099, 1, 1, tzinfo=timezone.utc)
    sub = get_user_subscription(uid)
    return sub["plan_name"], sub["expires_at"]


def register_user(u: types.User):
    q("""INSERT INTO users (user_id, username, full_name, joined_at) VALUES (?,?,?,?)
         ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
      (u.id, u.username, u.full_name, now_utc().isoformat()), commit=True)


# ============================ ZAXIRA NUSXA VA TIKLASH (BACKUP & RESTORE) ============================
BACKUP_FILE = "channels_backup.json"


def generate_full_backup_data() -> dict:
    """Barcha jadvallardagi (foydalanuvchilar, kanallar, obunalar, sozlamalar) ma'lumotlarni yig'adi"""
    tables = [
        "users", "channels", "subscriptions", "settings",
        "payments", "violations", "required", "recent_posts", "chat_insights"
    ]
    backup = {
        "version": "2.0",
        "bot": "Kanal Yurgizadigan Bot",
        "backup_date": datetime.now(TZ).isoformat(),
        "tables": {}
    }
    for t in tables:
        try:
            rows = q(f"SELECT * FROM {t}")
            backup["tables"][t] = [dict(r) for r in rows]
        except Exception as e_t:
            logging.warning(f"Backup olishda jadval ({t}) xatosi: {e_t}")
            backup["tables"][t] = []
    return backup


def create_backup_file() -> str:
    """
    Barcha jadvallardan JSON zaxira faylini generatsiya qiladi.
    Fayl nomi sana bilan formatlanadi (masalan: backup_2026_11_10.json).
    """
    data = generate_full_backup_data()
    date_str = datetime.now(TZ).strftime("%Y_%m_%d")
    filepath = f"backup_{date_str}.json"
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Render serveri uchun kanallarni ham channels_backup.json ga saqlash
        if "channels" in data.get("tables", {}):
            with open(BACKUP_FILE, "w", encoding="utf-8") as f_ch:
                json.dump(data["tables"]["channels"], f_ch, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Zaxira faylini yozishda xatolik: {e}")
    return filepath


def auto_backup_channels():
    create_backup_file()


def restore_database_from_file(filepath: str) -> tuple[bool, str]:
    """
    Zaxira fayli (.json yoki .db) dan barcha ma'lumotlarni xavfsiz qayta tiklaydi.
    """
    if not os.path.exists(filepath):
        return False, "Fayl topilmadi!"

    lower_path = filepath.lower()
    ch_count = 0
    u_count = 0
    sub_count = 0
    st_count = 0

    if lower_path.endswith(".json"):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e_json:
            return False, f"JSON faylni o'qishda xatolik: {e_json}"

        # 1. Yangi to'liq zaxira formati (tables kaliti bilan)
        if isinstance(data, dict) and "tables" in data:
            tables = data.get("tables", {})
            # Kanallar
            for r in tables.get("channels", []):
                try:
                    q("""INSERT INTO channels (
                            channel_id, owner_id, title, niche, audience, group_id, active,
                            posts_sent, join_requests, joined, left_count, group_messages,
                            warnings, bans, detailed_prompt, custom_hours, polls_sent,
                            current_topic, arc_day, arc_total_days, last_cliffhanger, created_at
                         ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(channel_id) DO UPDATE SET
                            owner_id=excluded.owner_id,
                            title=excluded.title,
                            niche=excluded.niche,
                            audience=excluded.audience,
                            group_id=excluded.group_id,
                            active=excluded.active,
                            detailed_prompt=excluded.detailed_prompt,
                            custom_hours=excluded.custom_hours,
                            current_topic=excluded.current_topic,
                            arc_day=excluded.arc_day,
                            arc_total_days=excluded.arc_total_days,
                            last_cliffhanger=excluded.last_cliffhanger
                    """, (
                        r.get("channel_id"), r.get("owner_id"), r.get("title", ""), r.get("niche", ""),
                        r.get("audience", ""), r.get("group_id"), r.get("active", 1),
                        r.get("posts_sent", 0), r.get("join_requests", 0), r.get("joined", 0),
                        r.get("left_count", 0), r.get("group_messages", 0), r.get("warnings", 0),
                        r.get("bans", 0), r.get("detailed_prompt", ""), r.get("custom_hours", ""),
                        r.get("polls_sent", 0), r.get("current_topic", ""), r.get("arc_day", 1),
                        r.get("arc_total_days", 3), r.get("last_cliffhanger", ""), r.get("created_at", "")
                    ), commit=True)
                    ch_count += 1
                except Exception as e_ch:
                    logging.warning(f"Kanal tiklash xatosi: {e_ch}")

            # Foydalanuvchilar
            for u in tables.get("users", []):
                try:
                    q("""INSERT INTO users (user_id, username, full_name, plan, expires_at, banned, joined_at)
                         VALUES (?,?,?,?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET
                            username=excluded.username,
                            full_name=excluded.full_name,
                            plan=excluded.plan,
                            expires_at=excluded.expires_at,
                            banned=excluded.banned
                    """, (
                        u.get("user_id"), u.get("username"), u.get("full_name"),
                        u.get("plan"), u.get("expires_at"), u.get("banned", 0), u.get("joined_at")
                    ), commit=True)
                    u_count += 1
                except Exception as e_u:
                    logging.warning(f"Foydalanuvchi tiklash xatosi: {e_u}")

            # Obunalar
            for s in tables.get("subscriptions", []):
                try:
                    q("""INSERT INTO subscriptions (user_id, plan_name, expires_at, daily_posts_count, last_post_date, created_at)
                         VALUES (?,?,?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET
                            plan_name=excluded.plan_name,
                            expires_at=excluded.expires_at,
                            daily_posts_count=excluded.daily_posts_count,
                            last_post_date=excluded.last_post_date
                    """, (
                        s.get("user_id"), s.get("plan_name", "start"), s.get("expires_at"),
                        s.get("daily_posts_count", 0), s.get("last_post_date"), s.get("created_at")
                    ), commit=True)
                    sub_count += 1
                except Exception as e_s:
                    logging.warning(f"Obuna tiklash xatosi: {e_s}")

            # Sozlamalar
            for st in tables.get("settings", []):
                try:
                    q("""INSERT INTO settings (key, value) VALUES (?,?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """, (st.get("key"), st.get("value")), commit=True)
                    st_count += 1
                except Exception:
                    pass

            # Majburiy kanallar
            for req in tables.get("required", []):
                try:
                    q("INSERT OR IGNORE INTO required (chat) VALUES (?)", (req.get("chat"),), commit=True)
                except Exception:
                    pass

        # 2. Qadimgi ro'yxat shaklidagi zaxira formati
        elif isinstance(data, list):
            for r in data:
                try:
                    q("""INSERT INTO channels (
                            channel_id, owner_id, title, niche, audience, group_id, active,
                            posts_sent, join_requests, joined, left_count, group_messages,
                            warnings, bans, detailed_prompt, custom_hours, polls_sent,
                            current_topic, arc_day, arc_total_days, last_cliffhanger, created_at
                         ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(channel_id) DO UPDATE SET
                            owner_id=excluded.owner_id, title=excluded.title, niche=excluded.niche,
                            audience=excluded.audience, group_id=excluded.group_id, active=excluded.active,
                            detailed_prompt=excluded.detailed_prompt, custom_hours=excluded.custom_hours,
                            current_topic=excluded.current_topic, arc_day=excluded.arc_day,
                            arc_total_days=excluded.arc_total_days, last_cliffhanger=excluded.last_cliffhanger
                    """, (
                        r.get("channel_id"), r.get("owner_id"), r.get("title", ""), r.get("niche", ""),
                        r.get("audience", ""), r.get("group_id"), r.get("active", 1),
                        r.get("posts_sent", 0), r.get("join_requests", 0), r.get("joined", 0),
                        r.get("left_count", 0), r.get("group_messages", 0), r.get("warnings", 0),
                        r.get("bans", 0), r.get("detailed_prompt", ""), r.get("custom_hours", ""),
                        r.get("polls_sent", 0), r.get("current_topic", ""), r.get("arc_day", 1),
                        r.get("arc_total_days", 3), r.get("last_cliffhanger", ""), r.get("created_at", "")
                    ), commit=True)
                    ch_count += 1
                except Exception as e_old_ch:
                    logging.warning(f"Kanal tiklash xatosi: {e_old_ch}")
        else:
            return False, "Fayl ichidagi ma'lumotlar strukturasi mos emas!"

    elif lower_path.endswith(".db") or lower_path.endswith(".sqlite"):
        try:
            temp_con = sqlite3.connect(filepath)
            temp_con.row_factory = sqlite3.Row
            temp_cur = temp_con.cursor()

            try:
                temp_cur.execute("SELECT * FROM channels")
                for r in temp_cur.fetchall():
                    d = dict(r)
                    q("""INSERT INTO channels (
                            channel_id, owner_id, title, niche, audience, group_id, active,
                            posts_sent, join_requests, joined, left_count, group_messages,
                            warnings, bans, detailed_prompt, custom_hours, polls_sent,
                            current_topic, arc_day, arc_total_days, last_cliffhanger, created_at
                         ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(channel_id) DO UPDATE SET
                            owner_id=excluded.owner_id, title=excluded.title, niche=excluded.niche,
                            audience=excluded.audience, group_id=excluded.group_id, active=excluded.active,
                            detailed_prompt=excluded.detailed_prompt, custom_hours=excluded.custom_hours,
                            current_topic=excluded.current_topic, arc_day=excluded.arc_day,
                            arc_total_days=excluded.arc_total_days, last_cliffhanger=excluded.last_cliffhanger
                    """, (
                        d.get("channel_id"), d.get("owner_id"), d.get("title", ""), d.get("niche", ""),
                        d.get("audience", ""), d.get("group_id"), d.get("active", 1),
                        d.get("posts_sent", 0), d.get("join_requests", 0), d.get("joined", 0),
                        d.get("left_count", 0), d.get("group_messages", 0), d.get("warnings", 0),
                        d.get("bans", 0), d.get("detailed_prompt", ""), d.get("custom_hours", ""),
                        d.get("polls_sent", 0), d.get("current_topic", ""), d.get("arc_day", 1),
                        d.get("arc_total_days", 3), d.get("last_cliffhanger", ""), d.get("created_at", "")
                    ), commit=True)
                    ch_count += 1
            except Exception as e_db_ch:
                logging.warning(f"SQLite kanallarni tiklash xatosi: {e_db_ch}")

            try:
                temp_cur.execute("SELECT * FROM users")
                for r in temp_cur.fetchall():
                    u = dict(r)
                    q("""INSERT INTO users (user_id, username, full_name, plan, expires_at, banned, joined_at)
                         VALUES (?,?,?,?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET
                            username=excluded.username, full_name=excluded.full_name,
                            plan=excluded.plan, expires_at=excluded.expires_at, banned=excluded.banned
                    """, (
                        u.get("user_id"), u.get("username"), u.get("full_name"),
                        u.get("plan"), u.get("expires_at"), u.get("banned", 0), u.get("joined_at")
                    ), commit=True)
                    u_count += 1
            except Exception:
                pass

            try:
                temp_cur.execute("SELECT * FROM subscriptions")
                for r in temp_cur.fetchall():
                    s = dict(r)
                    q("""INSERT INTO subscriptions (user_id, plan_name, expires_at, daily_posts_count, last_post_date, created_at)
                         VALUES (?,?,?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET
                            plan_name=excluded.plan_name, expires_at=excluded.expires_at,
                            daily_posts_count=excluded.daily_posts_count, last_post_date=excluded.last_post_date
                    """, (
                        s.get("user_id"), s.get("plan_name", "start"), s.get("expires_at"),
                        s.get("daily_posts_count", 0), s.get("last_post_date"), s.get("created_at")
                    ), commit=True)
                    sub_count += 1
            except Exception:
                pass

            try:
                temp_cur.execute("SELECT * FROM settings")
                for r in temp_cur.fetchall():
                    st = dict(r)
                    q("""INSERT INTO settings (key, value) VALUES (?,?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """, (st.get("key"), st.get("value")), commit=True)
                    st_count += 1
            except Exception:
                pass

            temp_con.close()
        except Exception as e_sqlite:
            return False, f"SQLite (.db) faylini tiklashda xatolik: {e_sqlite}"
    else:
        return False, "Noma'lum fayl turi! Faqat .json yoki .db zaxira fayllarini yuboring."

    summary = (
        f"• Ulangan kanallar: <b>{ch_count} ta</b>\n"
        f"• Foydalanuvchilar: <b>{u_count} ta</b>\n"
        f"• Obunalar va tariflar: <b>{sub_count} ta</b>\n"
        f"• Tizim sozlamalari: <b>{st_count} ta</b>"
    )
    return True, summary


def auto_restore_channels():
    if not os.path.exists(BACKUP_FILE):
        return 0
    ok, _ = restore_database_from_file(BACKUP_FILE)
    return q("SELECT COUNT(*) as c FROM channels", one=True)["c"] if ok else 0


async def nightly_auto_backup():
    """Har kecha soat 00:00 da admin lichkasiga avtomatik backup yuboruvchi cron vazifasi"""
    try:
        filepath = create_backup_file()
        if not os.path.exists(filepath):
            return
        filename = os.path.basename(filepath)
        doc = types.FSInputFile(filepath, filename=filename)
        caption = (
            f"🌙 <b>TUNGI AVTOMATIK ZAXIRA (NIGHTLY BACKUP)</b>\n\n"
            f"📅 <b>Sana:</b> <code>{datetime.now(TZ).strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
            f"📁 <b>Fayl:</b> <code>{filename}</code>\n"
            f"📦 Barcha jadvallar (foydalanuvchilar, kanallar, obunalar, sozlamalar) xavfsiz saqlandi."
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_document(admin_id, document=doc, caption=caption)
            except Exception as e_send:
                logging.warning(f"Admin ({admin_id}) ga tungi avto-backup yuborib bo'lmadi: {e_send}")
    except Exception as e:
        logging.error(f"Tungi avto-backup xatosi: {e}")

auto_restore_channels()


# ============================ ODDIY FOYDALANUVCHILAR UCHUN SHAXSIY ZAXIRA ============================
def create_user_backup(uid: int) -> tuple[str, bytes]:
    """
    Faqat berilgan foydalanuvchiga (uid) tegishli kanallar va ularning sozlamalarini
    xavfsiz JSON qilib tayyorlaydi. Butun baza yoki boshqa odamlar ma'lumotlari kirmaydi!
    """
    ch_rows = q("SELECT * FROM channels WHERE owner_id=?", (uid,))
    channels_data = []
    for c in ch_rows:
        c = dict(c)
        channels_data.append({
            "channel_id": c["channel_id"],
            "title": c["title"],
            "niche": c["niche"],
            "audience": c["audience"],
            "group_id": c["group_id"],
            "active": c["active"],
            "detailed_prompt": c.get("detailed_prompt", ""),
            "custom_hours": c.get("custom_hours", ""),
            "current_topic": c.get("current_topic", ""),
            "arc_day": c.get("arc_day", 1),
            "arc_total_days": c.get("arc_total_days", 3),
            "last_cliffhanger": c.get("last_cliffhanger", "")
        })

    backup_payload = {
        "version": "1.0",
        "backup_type": "user_personal_backup",
        "user_id": uid,
        "created_at": now_utc().isoformat(),
        "channels_count": len(channels_data),
        "channels": channels_data
    }
    
    filename = f"user_backup_{uid}_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.json"
    content_bytes = json.dumps(backup_payload, ensure_ascii=False, indent=2).encode("utf-8")
    return filename, content_bytes


def restore_user_backup(uid: int, json_data: dict) -> tuple[bool, str]:
    """
    Foydalanuvchi yuborgan shaxsiy zaxirani tekshirib tiklaydi.
    XAVFSIZLIK: user_id aynan shu foydalanuvchiniki ekanligi qat'iy tekshiriladi!
    """
    if not isinstance(json_data, dict):
        return False, "❌ Fayl formati noto'g'ri (JSON obyekt emas)."
    
    file_uid = json_data.get("user_id")
    if not file_uid or int(file_uid) != uid:
        return False, "⛔ <b>Xavfsizlik xatosi:</b> Ushbu zaxira fayli boshqa foydalanuvchiga tegishli! Begona zaxirani tiklash taqiqlangan."
        
    channels = json_data.get("channels", [])
    if not isinstance(channels, list):
        return False, "❌ Kanallar ro'yxati topilmadi."

    sub = get_user_subscription(uid)
    plan_info = sub["plan_info"]
    max_channels = plan_info.get("channels", 1)

    restored_count = 0
    for ch in channels:
        cid = ch.get("channel_id")
        if not cid:
            continue
            
        # Agar kanal bazada boshqa odam nomida bo'lsa (o'g'irlashdan qat'iy himoya)
        existing = q("SELECT owner_id FROM channels WHERE channel_id=?", (cid,), one=True)
        if existing and existing["owner_id"] != uid and not is_admin(uid):
            continue
            
        q("""INSERT INTO channels (
                channel_id, owner_id, title, niche, audience, group_id, active,
                detailed_prompt, custom_hours, current_topic, arc_day, arc_total_days, last_cliffhanger, created_at
             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(channel_id) DO UPDATE SET
                owner_id=excluded.owner_id,
                title=excluded.title,
                niche=excluded.niche,
                audience=excluded.audience,
                group_id=excluded.group_id,
                active=excluded.active,
                detailed_prompt=excluded.detailed_prompt,
                custom_hours=excluded.custom_hours,
                current_topic=excluded.current_topic,
                arc_day=excluded.arc_day,
                arc_total_days=excluded.arc_total_days,
                last_cliffhanger=excluded.last_cliffhanger""",
          (
              cid, uid, ch.get("title", "Kanal"), ch.get("niche", "Umumiy"), ch.get("audience", "Barcha"),
              ch.get("group_id"), ch.get("active", 1), ch.get("detailed_prompt", ""), ch.get("custom_hours", ""),
              ch.get("current_topic", ""), ch.get("arc_day", 1), ch.get("arc_total_days", 3), ch.get("last_cliffhanger", ""),
              now_utc().isoformat()
          ), commit=True)
        restored_count += 1
        
    auto_backup_channels()
    return True, f"✅ <b>Shaxsiy zaxirangiz muvaffaqiyatli tiklandi!</b>\n\nTiklandi: <b>{restored_count} ta</b> kanal va ularning barcha shaxsiy sozlamalari."


# ============================ KANAL HAVOLALARINI TOZALASH ============================
def clean_channel_input(text: str):
    text = text.strip()
    m_c = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/c/(\d+)", text)
    if m_c:
        cid = int(m_c.group(1))
        return -int(f"100{cid}") if not str(cid).startswith("100") else -cid
    m = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/+([a-zA-Z0-9_]+)", text)
    if m:
        u = m.group(1)
        if u not in ("joinchat", "c", "addstickers"):
            return "@" + u
    if text.startswith("@"):
        return text
    if text.lstrip("-").isdigit():
        val = int(text)
        if val > 0:
            s = str(val)
            return -int(s) if s.startswith("100") else -int(f"100{s}")
        return val
    clean = text.replace("https://", "").replace("http://", "").replace("t.me/", "").replace("/", "").strip()
    return "@" + clean if not clean.startswith("@") else clean


# ============================ KLAVIATURALAR ============================
def ik(*rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=t, url=d) if d.startswith("http")
         else types.InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])


MENU = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="🛒 Bitim yaratish"), types.KeyboardButton(text="💳 Balans / To'lov")],
        [types.KeyboardButton(text="👤 Profilim"), types.KeyboardButton(text="⚙️ Sozlamalar")],
        [types.KeyboardButton(text="💾 Shaxsiy Zaxira"), types.KeyboardButton(text="🆘 Yordam / Nizolar")],
    ],
    resize_keyboard=True,
)


def plans_keyboard():
    buttons = []
    for k in ["start", "pro", "max"]:
        p = PLANS[k]
        if p["price"] == 0:
            label = f"🎁 {p['name']} — Bepul (0 so'm)"
        else:
            label = f"💳 {p['name']} — {p['price']:,} so'm/oy".replace(",", " ")
        buttons.append([(label, f"buy:{k}")])
    return ik(*buttons)


def plans_text():
    lines = []
    for k in ["start", "pro", "max"]:
        p = PLANS[k]
        if p["price"] == 0:
            price_str = "Mutlaqo Bepul (0 so'm)"
        else:
            price_str = f"{p['price']:,} so'm/oy".replace(",", " ")
        lines.append(
            f"💎 <b>{p['name']}</b> — <code>{price_str}</code>\n"
            f"📌 <i>{p['desc']}</i>"
        )
    return "\n\n".join(lines)


# ============================ HOLATLAR (FSM) ============================
class AddCh(StatesGroup):
    channel = State()
    niche = State()
    detailed_prompt = State()
    audience = State()
    group = State()
    hours = State()


class EditPrompt(StatesGroup):
    prompt = State()


class EditHours(StatesGroup):
    hours = State()


class EditGroup(StatesGroup):
    group = State()


class AdminCard(StatesGroup):
    number = State()
    owner = State()


class AdminRestore(StatesGroup):
    file = State()


class UserRestore(StatesGroup):
    file = State()


class AdminGemini(StatesGroup):
    key = State()


class AdminGroq(StatesGroup):
    key = State()


class AdminYouTube(StatesGroup):
    key = State()


class Pay(StatesGroup):
    receipt = State()


class CreateDeal(StatesGroup):
    seller = State()
    title = State()
    amount = State()


class RefundState(StatesGroup):
    deal_id = State()
    card_number = State()
    full_name = State()
    amount = State()


class AdminUser(StatesGroup):
    uid = State()


class Bcast(StatesGroup):
    content = State()


# ============================ MAJBURIY OBUNA MIDDLEWARE ============================
async def missing_channels(uid):
    missing = []
    for r in q("SELECT chat FROM required"):
        try:
            m = await bot.get_chat_member(r["chat"], uid)
            if m.status in ("left", "kicked"):
                missing.append(r["chat"])
        except Exception as e:
            logging.warning(f"Majburiy kanal tekshirib bo'lmadi ({r['chat']}): {e}")
    return missing


def sub_keyboard(missing):
    rows = [[(f"📢 {c}", f"https://t.me/{c.lstrip('@')}")] for c in missing]
    rows.append([("✅ Obunani tekshirish", "check_sub")])
    return ik(*rows)


class SubGate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if isinstance(event, types.Message):
            chat = event.chat
        elif isinstance(event, types.CallbackQuery) and event.message:
            chat = event.message.chat
        else:
            chat = None
        if user is None or chat is None or chat.type != "private" or user.id in ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, types.CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        register_user(user)
        row = q("SELECT banned FROM users WHERE user_id=?", (user.id,), one=True)
        if row and row["banned"]:
            return
        missing = await missing_channels(user.id)
        if not missing:
            return await handler(event, data)

        text = (
            "⚠️ <b>Diqqat!</b> Bot xizmatlaridan foydalanish uchun quyidagi homiy kanallarga obuna bo'ling "
            "va so'ng <b>«Obunani tekshirish»</b> tugmasini bosing:"
        )
        if isinstance(event, types.CallbackQuery):
            await event.answer("Avval homiy kanallarga obuna bo'ling!", show_alert=True)
            await event.message.answer(text, reply_markup=sub_keyboard(missing))
        else:
            await event.answer(text, reply_markup=sub_keyboard(missing))


dp.message.outer_middleware(SubGate())
dp.callback_query.outer_middleware(SubGate())


@dp.callback_query(F.data == "check_sub")
async def check_sub(cb: types.CallbackQuery):
    register_user(cb.from_user)
    missing = await missing_channels(cb.from_user.id)
    if missing:
        await cb.answer("❌ Hali hamma kanallarga a'zo bo'lmadingiz!", show_alert=True)
        return
    await cb.answer("✅ Obuna muvaffaqiyatli tasdiqlandi!")
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer("🎉 <b>Xush kelibsiz!</b> Asosiy menyudan foydalanishingiz mumkin 👇", reply_markup=MENU)


# ============================ ASOSIY MENYU ============================
@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    register_user(m.from_user)
    admin_hint = "\n\n👑 <b>Siz bot adminisiz!</b> Boshqaruv paneli: /admin" if m.from_user.id in ADMIN_IDS else ""
    await m.answer(
        f"👋 <b>Assalomu alaykum, {html.escape(m.from_user.full_name)}!</b>\n\n"
        "🛡 <b>KAFOLATLI SAVDO VA PROFESSIONAL KANAL BOSHQARUVI</b>\n\n"
        "⚡️ <b>Asosiy xizmatlar:</b>\n"
        "• 🛒 <b>Bitim yaratish</b> — Akkaunt, kanal va xizmatlar uchun 100% xavfsiz kafolatli savdo (Escrow)\n"
        "• 💳 <b>Balans / To'lov</b> — TolovAPI orqali avtomatik tariflarni xarid qilish\n"
        "• 👤 <b>Profilim</b> — Shaxsiy hisob, faol tarif va statistika\n"
        "• ⚙️ <b>Sozlamalar</b> — Kanallar va guruhlarni boshqarish\n"
        "• 💾 <b>Shaxsiy Zaxira</b> — Ma'lumotlarni xavfsiz JSON zaxiralash va qayta tiklash\n"
        "• 🆘 <b>Yordam / Nizolar</b> — Firibgarlikdan himoya (Anti-Scam) va pulni qaytarish (Refund)"
        f"{admin_hint}\n\n"
        "Pastdagi menyudan kerakli bo'limni tanlang 👇",
        reply_markup=MENU,
    )


@dp.message(F.chat.type == "private", F.text.in_({"💳 Balans / To'lov", "💎 Tariflar & To'lov", "💰 Hisobim", "Balans / To'lov"}))
async def my_account(m: types.Message, state: FSMContext):
    await state.clear()
    sub = get_user_subscription(m.from_user.id)
    plan = sub["plan_name"]
    exp = sub["expires_at"]
    card_num, card_own = get_card_info()
    if plan != "start" and exp:
        status = f"🟢 <b>Faol tarif:</b> <code>{PLANS[plan]['name']}</code>\n⏳ <b>Tugash vaqti:</b> <code>{fmt(exp)}</code> gacha"
    else:
        status = f"🎁 <b>Faol tarif:</b> <code>{PLANS['start']['name']} (Bepul)</code>\n⏳ <b>Muddati:</b> Cheksiz (Muddatsiz)"
    
    await m.answer(
        f"💳 <b>TARIFLAR VA TO'LOV TIZIMI</b>\n\n"
        f"📊 <b>Sizning holatingiz:</b>\n{status}\n\n"
        f"💳 <b>Rasmiy to'lov kartasi:</b> <code>{card_num}</code>\n"
        f"👤 <b>Qabul qiluvchi:</b> <b>{html.escape(card_own)}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Mavjud Tariflar:</b>\n\n{plans_text()}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👇 <i>Tarifni faollashtirish uchun pastdagi tugmani bosing:</i>",
        reply_markup=plans_keyboard(),
    )


@dp.message(F.chat.type == "private", F.text == "👤 Profilim")
async def profile(m: types.Message, state: FSMContext):
    await state.clear()
    u = q("SELECT * FROM users WHERE user_id=?", (m.from_user.id,), one=True)
    ch_rows = q("SELECT COUNT(*) c, COALESCE(SUM(posts_sent),0) p, COALESCE(SUM(polls_sent),0) pl FROM channels WHERE owner_id=?",
                (m.from_user.id,), one=True)
    sub = get_user_subscription(m.from_user.id)
    plan = sub["plan_name"]
    exp = sub["expires_at"]
    joined = datetime.fromisoformat(u["joined_at"]) if u and u["joined_at"] else now_utc()
    await m.answer(
        "👤 <b>FOYDALANUVCHI PROFILI</b>\n\n"
        f"🆔 <b>Sizning ID:</b> <code>{m.from_user.id}</code>\n"
        f"👤 <b>Ismingiz:</b> <b>{html.escape(m.from_user.full_name)}</b>\n"
        f"📅 <b>Ro'yxatdan o'tgan:</b> <code>{fmt(joined)}</code>\n"
        f"💎 <b>Joriy tarif:</b> <b>{PLANS[plan]['name']}</b>"
        f"{(' (tugaydi: ' + fmt(exp) + ')') if exp else ' (Muddatsiz / Bepul)'}\n"
        f"📺 <b>Ulangan kanallar:</b> <b>{ch_rows['c']} ta</b>\n"
        f"📝 <b>Yuborilgan AI postlar:</b> <b>{ch_rows['p']} ta</b>\n"
        f"📊 <b>Yuborilgan so'rovnomalar:</b> <b>{ch_rows['pl']} ta</b>"
    )


# ============================ TO'LOV JARAYONI (TolovAPI & Manual) ============================
@dp.callback_query(F.data.startswith("buy:"))
async def buy(cb: types.CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    p = PLANS.get(key)
    if not p:
        return await cb.answer("Tarif topilmadi")
        
    # START tarifi (Mutlaqo Bepul) - bir zumda faollashtiriladi
    if key == "start":
        apply_plan(cb.from_user.id, "start")
        await cb.message.answer(
            "🎉 <b>START (Mutlaqo Bepul) tarifi muvaffaqiyatli faollashtirildi!</b>\n\n"
            "✨ <b>Sizning imkoniyatlaringiz:</b>\n"
            "• 1 ta kanal yoki guruh ulash\n"
            "• Standart tezkor moderatsiya (0.3 soniyada so'kinishlarni tozalash)\n"
            "• Kuniga 1 ta avtomatik post\n\n"
            "Marhamat, «⚙️ Sozlamalar» menyusi orqali kanalingizni ulang!",
            reply_markup=MENU
        )
        return await cb.answer("START tarifi faollashtirildi!")

    card_num, card_own = get_card_info()
    
    # TolovAPI orqali asinxron to'lov yaratishga urinish
    tolov_res = await create_payment_async(
        amount=p["price"],
        order_id=f"plan_{key}_{cb.from_user.id}",
        user_id=cb.from_user.id
    )
    if tolov_res.get("ok"):
        pid = tolov_res.get("payment_id")
        final_amt = tolov_res.get("amount", p["price"])
        api_card = tolov_res.get("card_number") or card_num
        
        # Fon tekshiruv vazifasini ishga tushirish (har 3 soniyada tekshiradi)
        asyncio.create_task(watch_payment_task(cb.from_user.id, pid, plan_key=key, amount=final_amt))
        
        kb = ik(
            [("🔄 To'lovni tekshirish", f"chkpay:{pid}:{key}")],
            [("📸 Chek orqali tasdiqlash", f"manualpay:{key}")],
            [("❌ Bekor qilish", f"cancelpay:{pid}")]
        )
        await cb.message.answer(
            f"💳 <b>{p['name']}</b> tarifini xarid qilish\n\n"
            f"💵 <b>To'lov summasi:</b> <code>{final_amt:,} so'm</code>\n"
            f"💳 <b>Karta raqami:</b> <code>{api_card}</code>\n"
            f"👤 <b>Qabul qiluvchi:</b> <b>{html.escape(card_own)}</b>\n\n"
            f"⏱ <b>To'lov muddati:</b> 5 daqiqa\n"
            f"⚡️ <i>To'lov amalga oshirilishi bilan bot avtomatik ravishda pulni aniqlaydi va tarifingizni yoqadi (Chek yuborish shart emas)!</i>".replace(",", " "),
            reply_markup=kb
        )
        return await cb.answer()

    # Agar TolovAPI vaqtinchalik javob bermasa, chek orqali to'lovga o'tish
    await state.set_state(Pay.receipt)
    await state.update_data(plan=key)
    await cb.message.answer(
        f"💳 <b>{p['name']}</b> tarifini xarid qilish\n\n"
        f"💵 <b>To'lov summasi:</b> <code>{p['price']:,} so'm</code>\n"
        f"💳 <b>Karta raqami:</b> <code>{card_num}</code>\n"
        f"👤 <b>Qabul qiluvchi:</b> <b>{html.escape(card_own)}</b>\n\n"
        f"📸 To'lovni amalga oshirgach, <b>chek skrinshotini rasm sifatida</b> shu yerga yuboring.\n"
        f"❌ Bekor qilish: /cancel".replace(",", " ")
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("chkpay:"))
async def cb_chkpay(cb: types.CallbackQuery):
    parts = cb.data.split(":")
    pid = parts[1]
    plan_key = parts[2] if len(parts) > 2 else "pro"
    status_res = await check_payment_status_async(pid)
    if status_res and status_res.get("status") == "paid":
        exp = apply_plan(cb.from_user.id, plan_key)
        p_name = PLANS.get(plan_key, {}).get("name", plan_key)
        await cb.message.edit_text(
            f"🎉 <b>TO'LOV MUVAFFAQIYATLI TASDIQLANDI!</b>\n\n"
            f"💎 <b>Tarif:</b> <b>{p_name}</b>\n"
            f"⏳ <b>Amal qilish muddati:</b> <code>{fmt(exp)}</code> gacha\n\n"
            f"✨ Barcha imkoniyatlar avtomatik yoqildi!",
            reply_markup=None
        )
        await cb.answer("To'lov qabul qilindi!", show_alert=True)
    elif status_res and status_res.get("status") == "cancel":
        await cb.message.edit_text("❌ <b>Ushbu to'lov bekor qilingan yoki muddati tugagan.</b>")
        await cb.answer("To'lov muddati tugagan!", show_alert=True)
    else:
        await cb.answer("⏳ To'lov hali kelib tushmadi. Iltimos, pulni kartaga o'tkazib biroz kuting...", show_alert=True)


@dp.callback_query(F.data.startswith("cancelpay:"))
async def cb_cancelpay(cb: types.CallbackQuery):
    pid = cb.data.split(":")[1]
    await cancel_payment_async(pid)
    await cb.message.edit_text("❌ <b>To'lov amaliyoti bekor qilindi.</b>")
    await cb.answer("Bekor qilindi")


@dp.callback_query(F.data.startswith("manualpay:"))
async def cb_manualpay(cb: types.CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    p = PLANS.get(key)
    card_num, card_own = get_card_info()
    await state.set_state(Pay.receipt)
    await state.update_data(plan=key)
    await cb.message.answer(
        f"📸 <b>Chek orqali to'lovni tasdiqlash</b>\n\n"
        f"💳 <b>Karta:</b> <code>{card_num}</code> ({html.escape(card_own)})\n"
        f"💵 <b>Summa:</b> <code>{p['price']:,} so'm</code>\n\n"
        f"Iltimos, to'lov cheki skrinshotini <b>rasm sifatida</b> yuboring:".replace(",", " ")
    )
    await cb.answer()


# ============================ 🛒 BITIM TIZIMI (ESCROW / KAFOLAT) ============================
@dp.message(F.chat.type == "private", F.text.in_({"🛒 Bitim yaratish", "/bitim", "/yangi_savdo", "Bitim yaratish"}))
async def start_create_deal(m: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(CreateDeal.seller)
    await m.answer(
        "🛒 <b>YANGI KAFOLATLI BITIM YARATISH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Ushbu tizim orqali akkaunt, kanal, bot yoki boshqa xizmatlarni <b>100% xavfsiz</b> sotib olishingiz mumkin.\n\n"
        "👉 <b>Sotuvchining Telegram @username manzilini yoki ID raqamini kiriting:</b>\n"
        "<i>(Masalan: <code>@username</code> yoki <code>6125160021</code>)</i>\n\n"
        "❌ <i>Bekor qilish uchun: /cancel</i>"
    )


@dp.message(CreateDeal.seller, F.text)
async def deal_set_seller(m: types.Message, state: FSMContext):
    seller_raw = m.text.strip()
    if seller_raw.startswith("@") and len(seller_raw) < 4:
        return await m.answer("❌ Noto'g'ri username! Iltimos, to'g'ri @username yoki ID kiriting:")
    await state.update_data(seller=seller_raw)
    await state.set_state(CreateDeal.title)
    await m.answer(
        "📝 <b>Bitim mavzusi yoki tovar nomini yozing:</b>\n"
        "<i>(Masalan: <code>Telegram kanal / Pubg akkaunt / Dasturlash xizmati</code>)</i>"
    )


@dp.message(CreateDeal.title, F.text)
async def deal_set_title(m: types.Message, state: FSMContext):
    title = m.text.strip()
    if len(title) < 2:
        return await m.answer("❌ Tovar nomi juda qisqa! Batafsilroq yozing:")
    await state.update_data(title=title)
    await state.set_state(CreateDeal.amount)
    await m.answer(
        "💵 <b>Bitim summasini (so'mda) faqat raqamlarda kiriting:</b>\n"
        "<i>(Masalan: <code>100000</code> yoki <code>50000</code>)</i>"
    )


@dp.message(CreateDeal.amount, F.text)
async def deal_set_amount(m: types.Message, state: FSMContext):
    raw_amount = re.sub(r"\D", "", m.text)
    if not raw_amount or int(raw_amount) < 1000:
        return await m.answer("❌ Noto'g'ri summa! Eng kam bitim summasi 1 000 so'm. Qayta kiriting:")
    
    amount = int(raw_amount)
    data = await state.get_data()
    seller = data.get("seller")
    title = data.get("title")
    await state.clear()
    
    deal_id = "B" + hashlib.md5(f"{m.from_user.id}_{time.time()}".encode()).hexdigest()[:6].upper()
    now_s = now_utc().isoformat()
    card_num, card_own = get_card_info()
    
    # Bazaga bitimni saqlash
    q("""INSERT INTO deals (deal_id, buyer_id, buyer_name, seller_name, title, amount, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, 'waiting_payment', ?, ?)""",
      (deal_id, m.from_user.id, m.from_user.full_name, seller, title, amount, now_s, now_s), commit=True)
      
    # TolovAPI orqali bitim uchun to'lov yaratish
    tolov_res = await create_payment_async(
        amount=amount,
        order_id=f"deal_{deal_id}",
        user_id=m.from_user.id
    )
    
    if tolov_res.get("ok"):
        pid = tolov_res.get("payment_id")
        final_amt = tolov_res.get("amount", amount)
        api_card = tolov_res.get("card_number") or card_num
        q("UPDATE deals SET payment_id=?, amount=? WHERE deal_id=?", (pid, final_amt, deal_id), commit=True)
        
        # Fon tekshiruvini ishga tushirish
        asyncio.create_task(watch_payment_task(m.from_user.id, pid, deal_id=deal_id, amount=final_amt))
        
        kb = ik(
            [("🔄 To'lovni tekshirish", f"chkdeal:{deal_id}:{pid}")],
            [("🚨 Nizo / Shikoyat", f"dispute:deal:{deal_id}")],
            [("❌ Bitimni bekor qilish", f"canceldeal:{deal_id}")]
        )
        await m.answer(
            f"🎉 <b>KAFOLATLI BITIM YARATILDI! (#<code>{deal_id}</code>)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Tovar/Xizmat:</b> <b>{html.escape(title)}</b>\n"
            f"👤 <b>Sotuvchi:</b> <code>{html.escape(seller)}</code>\n"
            f"💵 <b>Summa:</b> <code>{final_amt:,} so'm</code>\n\n"
            f"💳 <b>Kafolat to'lovi uchun rekvizitlar:</b>\n"
            f"• Karta raqami: <code>{api_card}</code>\n"
            f"• Qabul qiluvchi: <b>{html.escape(card_own)}</b>\n\n"
            f"⏱ <b>To'lov muddati:</b> 5 daqiqa\n"
            f"🛡 <i>Mablag' kafolat hisobiga tushishi bilan ikkala tomonga xabar beriladi. Pul xavfsiz muzlatiladi!</i>".replace(",", " "),
            reply_markup=kb
        )
    else:
        kb = ik(
            [("🚨 Nizo / Shikoyat", f"dispute:deal:{deal_id}")],
            [("❌ Bitimni bekor qilish", f"canceldeal:{deal_id}")]
        )
        await m.answer(
            f"🎉 <b>KAFOLATLI BITIM YARATILDI! (#<code>{deal_id}</code>)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Tovar/Xizmat:</b> <b>{html.escape(title)}</b>\n"
            f"👤 <b>Sotuvchi:</b> <code>{html.escape(seller)}</code>\n"
            f"💵 <b>Summa:</b> <code>{amount:,} so'm</code>\n\n"
            f"💳 <b>Kafolat to'lovi uchun rasmiy karta:</b>\n"
            f"<code>{card_num}</code>\n"
            f"👤 <b>Qabul qiluvchi:</b> <b>{html.escape(card_own)}</b>\n\n"
            f"🛡 <i>To'lovni amalga oshirgach, chekni adminga taqdim etishingiz mumkin.</i>".replace(",", " "),
            reply_markup=kb
        )


@dp.callback_query(F.data.startswith("chkdeal:"))
async def cb_chkdeal(cb: types.CallbackQuery):
    _, deal_id, pid = cb.data.split(":")
    status_res = await check_payment_status_async(pid)
    if status_res and status_res.get("status") == "paid":
        q("UPDATE deals SET status='in_trade', updated_at=? WHERE deal_id=?", (now_utc().isoformat(), deal_id), commit=True)
        await cb.message.edit_text(
            f"✅ <b>KAFOLAT TO'LOVI QABUL QILINDI! (#<code>{deal_id}</code>)</b>\n\n"
            f"Pul kafolat hisobida muzlatildi. Sotuvchi tovarni topshirishi mumkin.",
            reply_markup=ik([("🚨 Nizo / Shikoyat", f"dispute:deal:{deal_id}")])
        )
        await cb.answer("To'lov tasdiqlandi!", show_alert=True)
    else:
        await cb.answer("⏳ To'lov hali kelib tushmadi. Iltimos, kartaga pul o'tkazib biroz kuting...", show_alert=True)


@dp.callback_query(F.data.startswith("canceldeal:"))
async def cb_canceldeal(cb: types.CallbackQuery):
    deal_id = cb.data.split(":")[1]
    d = q("SELECT * FROM deals WHERE deal_id=?", (deal_id,), one=True)
    if not d:
        return await cb.answer("Bitim topilmadi", show_alert=True)
    if d["status"] in ("in_trade", "completed"):
        return await cb.answer("To'lov qilingan bitimni to'g'ridan-to'g'ri bekor qilib bo'lmaydi. Nizo oching!", show_alert=True)
    
    if d.get("payment_id"):
        await cancel_payment_async(d["payment_id"])
    q("UPDATE deals SET status='cancelled', updated_at=? WHERE deal_id=?", (now_utc().isoformat(), deal_id), commit=True)
    await cb.message.edit_text(f"❌ <b>Bitim #{deal_id} bekor qilindi.</b>")
    await cb.answer("Bitim bekor qilindi")


# ============================ 🆘 YORDAM, NIZOLAR VA REFUND (PULNI QAYTARISH) ============================
@dp.message(F.chat.type == "private", F.text.in_({"🆘 Yordam / Nizolar", "/help", "🆘 Yordam", "Yordam / Nizolar"}))
async def user_disputes_menu(m: types.Message, state: FSMContext):
    await state.clear()
    kb = ik(
        [("🚨 Nizo / Shikoyat bildirish", "dispute:new")],
        [("↩️ Pulni qaytarish (Refund)", "refund:start")],
        [("📜 Mening bitimlarim", "mydeals:list")],
        [("👨‍💻 Admin bilan bog'lanish", "dispute:contact_admin")]
    )
    await m.answer(
        "🆘 <b>YORDAM VA NIZOLAR BO'LIMI (ANTI-SCAM)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Bitim davomida firibgarlik yoki muammoga duch keldingizmi?\n\n"
        "🛡 <b>Bizning kafolatlarimiz:</b>\n"
        "• Barcha to'lovlar xavfsiz kafolat tizimida saqlanadi.\n"
        "• Muammo bo'lsa, bitim darhol muzlatiladi va admin tomonidan tekshiriladi.\n"
        "• Sotuvchi firibgar deb topilsa, to'lov xaridorga <b>100% qaytariladi (Refund)</b>!\n\n"
        "Kerakli bo'limni tanlang 👇",
        reply_markup=kb
    )


@dp.callback_query(F.data == "mydeals:list")
async def cb_mydeals_list(cb: types.CallbackQuery):
    rows = q("SELECT * FROM deals WHERE buyer_id=? ORDER BY created_at DESC LIMIT 5", (cb.from_user.id,))
    if not rows:
        return await cb.answer("Sizda hali bitimlar mavjud emas.", show_alert=True)
    text = "📜 <b>OXIRGI BITIMLARINGIZ:</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    status_map = {
        "waiting_payment": "⏳ To'lov kutilmoqda",
        "in_trade": "🔄 Savdo jarayonida (Muzlatilgan)",
        "completed": "✅ Yakunlangan",
        "cancelled": "❌ Bekor qilingan",
        "dispute": "🚨 Nizo holatida"
    }
    for r in rows:
        st = status_map.get(r["status"], r["status"])
        text += (
            f"🆔 <b>Bitim:</b> <code>#{r['deal_id']}</code>\n"
            f"📦 <b>Tovar:</b> {html.escape(r['title'] or '')}\n"
            f"💵 <b>Summa:</b> <code>{r['amount']:,} so'm</code>\n"
            f"📌 <b>Holat:</b> <b>{st}</b>\n\n".replace(",", " ")
        )
    await cb.message.answer(text)
    await cb.answer()


@dp.callback_query(F.data.startswith("dispute:"))
async def cb_dispute_handler(cb: types.CallbackQuery, state: FSMContext):
    action = cb.data.split(":")[1]
    if action == "contact_admin":
        aid = list(ADMIN_IDS)[0] if ADMIN_IDS else 6125160021
        return await cb.message.answer(
            f"👨‍💻 <b>Admin bilan bog'lanish:</b>\n\n"
            f"Telegram: <a href=\"tg://user?id={aid}\">Bosh Admin</a> (ID: <code>{aid}</code>)"
        )
    
    deal_id = cb.data.split(":")[2] if len(cb.data.split(":")) > 2 else None
    if deal_id:
        q("UPDATE deals SET status='dispute', updated_at=? WHERE deal_id=?", (now_utc().isoformat(), deal_id), commit=True)
    
    kb = ik(
        [("↩️ Pulni qaytarish so'rovi (Refund)", f"refund:deal:{deal_id or 'general'}")],
        [("👨‍💻 Adminga to'g'ridan-to'g'ri yozish", "dispute:contact_admin")]
    )
    await cb.message.answer(
        "🚨 <b>SHIKOYAT VA NIZO QABUL QILINDI!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Savdo holati muzlatildi. Agar sotuvchi firibgarlik qilgan bo'lsa, "
        "quyidagi tugma orqali kartangizni yuboring va mablag'ingizni qaytarib oling 👇",
        reply_markup=kb
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("refund:"))
async def cb_refund_start(cb: types.CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    deal_id = parts[2] if len(parts) > 2 else "general"
    await state.clear()
    await state.set_state(RefundState.card_number)
    await state.update_data(deal_id=deal_id)
    await cb.message.answer(
        "↩️ <b>PULNI QAYTARISH (REFUND) TIZIMI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Mablag'ingizni qaytarib berish uchun iltimos, <b>16 xonali plastik karta raqamingizni</b> kiriting:\n"
        "<i>(Masalan: <code>8600 1234 5678 9012</code>)</i>\n\n"
        "❌ Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(RefundState.card_number, F.text)
async def refund_card_input(m: types.Message, state: FSMContext):
    raw_card = re.sub(r"\D", "", m.text)
    if len(raw_card) != 16:
        return await m.answer("❌ Karta raqami 16 ta raqamdan iborat bo'lishi kerak! Qayta kiriting:\n<i>(Masalan: <code>8600 1234 5678 9012</code>)</i>")
    
    formatted_card = f"{raw_card[:4]} {raw_card[4:8]} {raw_card[8:12]} {raw_card[12:]}"
    await state.update_data(card_number=formatted_card)
    await state.set_state(RefundState.full_name)
    await m.answer(
        "👤 <b>Karta egasining to'liq ism va familiyasini kiriting:</b>\n"
        "<i>(Masalan: <b>Ali Valiyev</b>)</i>"
    )


@dp.message(RefundState.full_name, F.text)
async def refund_name_input(m: types.Message, state: FSMContext):
    name = m.text.strip()
    if len(name) < 3:
        return await m.answer("❌ Iltimos, to'liq ism va familiyani kiriting:")
    
    data = await state.get_data()
    deal_id = data.get("deal_id", "general")
    card_number = data.get("card_number")
    await state.clear()
    
    amount = 0
    if deal_id and deal_id != "general":
        d = q("SELECT amount FROM deals WHERE deal_id=?", (deal_id,), one=True)
        if d:
            amount = d["amount"]
            
    now_s = now_utc().isoformat()
    cur = db.execute(
        "INSERT INTO refunds (deal_id, user_id, full_name, card_number, amount, reason, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (deal_id, m.from_user.id, name, card_number, amount, "Firibgarlik / Bitim bekor qilindi", "pending", now_s)
    )
    db.commit()
    ref_id = cur.lastrowid
    
    # Adminga tezkor bildirishnoma
    admin_caption = (
        f"🚨 <b>YANGI PUL QAYTARISH (REFUND) SO'ROVI! #{ref_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Bitim:</b> <code>#{deal_id}</code>\n"
        f"👤 <b>Mijoz:</b> {html.escape(name)} (ID: <code>{m.from_user.id}</code>)\n"
        f"💳 <b>Qaytariladigan Karta:</b> <code>{card_number}</code>\n"
        f"👤 <b>Qabul qiluvchi:</b> <b>{html.escape(name)}</b>\n"
        f"💵 <b>Summa:</b> <code>{amount:,} so'm</code>\n"
        f"⚠️ <b>Sabab:</b> Firibgarlik / Bitim nizosi\n"
        f"━━━━━━━━━━━━━━━━━━━━━".replace(",", " ")
    )
    admin_kb = ik([("✅ Pul qaytarildi (Tasdiqlash)", f"ref_ok:{ref_id}")])
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, admin_caption, reply_markup=admin_kb)
        except Exception:
            pass
            
    await m.answer(
        "✅ <b>PULNI QAYTARISH ARIZASI QABUL QILINDI!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Karta raqami:</b> <code>{card_number}</code>\n"
        f"👤 <b>Qabul qiluvchi:</b> <b>{html.escape(name)}</b>\n\n"
        "🛡 <i>Arizangiz admin moderatorlarga yuborildi. Mablag' tekshirilib, tez orada kartangizga qaytariladi!</i>",
        reply_markup=MENU
    )


@dp.callback_query(F.from_user.id.in_(ADMIN_IDS), F.data.startswith("ref_ok:"))
async def cb_admin_refund_ok(cb: types.CallbackQuery):
    ref_id = cb.data.split(":")[1]
    ref = q("SELECT * FROM refunds WHERE id=?", (int(ref_id),), one=True)
    if not ref or ref["status"] != "pending":
        return await cb.answer("Bu ariza allaqachon bajarilgan!", show_alert=True)
        
    q("UPDATE refunds SET status='completed' WHERE id=?", (int(ref_id),), commit=True)
    await cb.message.edit_text(
        cb.message.html_text + "\n\n✅ <b>ADMIN TOMONIDAN PUL QAYTARILDI VA TASDIQLANDI!</b>",
        reply_markup=None
    )
    try:
        await bot.send_message(
            ref["user_id"],
            f"🎉 <b>PULINGIZ MUVAFFAQIYATLI QAYTARILDI!</b>\n\n"
            f"💳 Karta: <code>{ref['card_number']}</code>\n"
            f"💵 Summa: <code>{ref['amount']:,} so'm</code>\n\n"
            f"Mablag' hisobingizga o'tkazildi. Xavfsiz savdo uchun kafolat tizimidan foydalanganingiz uchun rahmat!".replace(",", " ")
        )
    except Exception:
        pass
    await cb.answer("Pul qaytarildi deb belgilandi!", show_alert=True)


# ============================ ⚙️ SOZLAMALAR MENYUSI ============================
@dp.message(F.chat.type == "private", F.text.in_({"⚙️ Sozlamalar", "/settings", "Sozlamalar"}))
async def user_settings_menu(m: types.Message, state: FSMContext):
    await state.clear()
    kb = ik(
        [("➕ Kanal ulash", "addch"), ("📺 Kanallarim", "mychannels")],
        [("💾 Shaxsiy zaxira", "user_bk:export"), ("ℹ️ Bot haqida", "settings:about")]
    )
    await m.answer(
        "⚙️ <b>BOT VA KANALLAR SOZLAMALARI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Quyidagi tugmalar orqali kanallaringizni boshqarishingiz, yangi kanal ulashingiz "
        "yoki shaxsiy ma'lumotlaringizni zaxiralashingiz mumkin 👇",
        reply_markup=kb
    )


@dp.callback_query(F.data == "settings:about")
async def cb_settings_about(cb: types.CallbackQuery):
    await cb.message.answer(
        "ℹ️ <b>KAFOLATLI SAVDO VA KANAL BOSHQARUV BOTI</b>\n\n"
        "• <b>TolovAPI.uz</b> orqali to'liq avtomatlashtirilgan to'lovlar\n"
        "• <b>Google Gemini AI</b> asosidagi aqlli kontent va moderatsiya\n"
        "• <b>100% Anti-Scam</b> kafolatli bitimlar va to'lov qaytarish (Refund)\n"
        "• Shaxsiy ma'lumotlarni xavfsiz JSON zaxiralash\n\n"
        "Versiya: <b>3.0.0 (2026 Pro)</b>"
    )
    await cb.answer()


@dp.message(F.text.casefold().in_({"cancel", "/cancel", "bekor", "bekor qilish", "❌ bekor qilish", "отмена", "/bekor"}))
@dp.message(Command("cancel"))
async def cancel(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ <b>Amaliyot bekor qilindi.</b>", reply_markup=MENU if m.chat.type == "private" else None)


# ============================ SHAXSIY ZAXIRA HANDLERLARI ============================
@dp.message(F.chat.type == "private", F.text.in_({"💾 Shaxsiy Zaxira", "💾 Mening Zaxira Faylim", "/backup"}))
async def user_backup_menu(m: types.Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    ch_rows = q("SELECT COUNT(*) as c FROM channels WHERE owner_id=?", (uid,), one=True)
    ch_count = ch_rows["c"] if ch_rows else 0
    text = (
        "💾 <b>SHAXSIY MA'LUMOTLAR VA KANALLAR ZAXIRASI</b>\n\n"
        f"Ushbu bo'lim orqali siz faqat o'zingiz ulagan kanallar, ularning maxsus AI promptlari, "
        f"post soatlari va seriya sozlamalarini xavfsiz JSON fayl sifatida yuklab olishingiz "
        f"yoki avval saqlangan shaxsiy zaxirangizdan qayta tiklashingiz mumkin.\n\n"
        f"📊 <b>Sizning kanallaringiz soni:</b> <b>{ch_count} ta</b>\n"
        f"🔒 <i>Xavfsizlik: Zaxira fayli qat'iy sizning ID raqamingizga bog'langan. Boshqa hech kim uni o'z hisobiga yuklay olmaydi va kanallaringizni tortib ololmaydi.</i>\n\n"
        "Kerakli amalni tanlang 👇"
    )
    kb = ik(
        [("📤 Zaxira faylini olish (Export)", "user_bk:export")],
        [("📥 Zaxiradan tiklash (Import)", "user_bk:restore")]
    )
    await m.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "user_bk:export")
async def user_bk_export_handler(cb: types.CallbackQuery):
    uid = cb.from_user.id
    ch_rows = q("SELECT COUNT(*) as c FROM channels WHERE owner_id=?", (uid,), one=True)
    count = ch_rows["c"] if ch_rows else 0
    if count == 0:
        return await cb.answer("Sizda hali ulangan kanallar mavjud emas! Avval «➕ Kanal ulash» orqali kanal ulang.", show_alert=True)
    
    filename, content_bytes = create_user_backup(uid)
    file_doc = types.BufferedInputFile(content_bytes, filename=filename)
    caption = (
        f"💾 <b>SIZNING SHAXSIY ZAXIRA FAYLINGIZ</b>\n\n"
        f"👤 <b>Foydalanuvchi ID:</b> <code>{uid}</code>\n"
        f"📁 <b>Fayl nomi:</b> <code>{filename}</code>\n"
        f"📢 <b>Saqlangan kanallar:</b> <b>{count} ta</b>\n"
        f"📅 <b>Sana:</b> <code>{datetime.now(TZ).strftime('%d.%m.%Y %H:%M:%S')}</code>\n\n"
        f"📌 <i>Ushbu faylni saqlab qo'ying. Istalgan vaqtda botda '📥 Zaxiradan tiklash' tugmasi orqali kanallaringiz va sozlamalaringizni tiklashingiz mumkin.</i>"
    )
    await cb.message.answer_document(file_doc, caption=caption)
    await cb.answer("Shaxsiy zaxira fayli yuborildi!")


@dp.callback_query(F.data == "user_bk:restore")
async def user_bk_restore_handler(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserRestore.file)
    await cb.message.answer(
        "📥 <b>SHAXSIY ZAXIRANI QAYTA TIKLASH (RESTORE)</b>\n\n"
        "Iltimos, avval ushbu botdan yuklab olingan shaxsiy <b>.json</b> zaxira faylingizni menga yuboring...\n\n"
        "<i>Bekor qilish: /cancel</i>"
    )
    await cb.answer()


@dp.message(UserRestore.file, F.chat.type == "private", F.document)
async def user_bk_process_file(m: types.Message, state: FSMContext):
    doc = m.document
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".json"):
        return await m.answer(
            "❌ <b>Noto'g'ri fayl formati!</b>\n\n"
            "Faqat <code>.json</code> formatidagi shaxsiy zaxira faylini yuboring.\n"
            "Bekor qilish: /cancel"
        )
    
    await state.clear()
    status_msg = await m.answer("⏳ <b>Shaxsiy zaxira fayli tekshirilmoqda va tiklanmoqda...</b>")
    
    temp_dir = "temp_backups"
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"user_restore_{m.from_user.id}_{int(time.time())}.json")
    
    try:
        file_info = await bot.get_file(doc.file_id)
        await bot.download_file(file_info.file_path, temp_path)
        
        with open(temp_path, "r", encoding="utf-8") as f:
            json_data = json.load(f)
            
        ok, res_text = restore_user_backup(m.from_user.id, json_data)
        if ok:
            await status_msg.edit_text(res_text, reply_markup=MENU)
        else:
            await status_msg.edit_text(f"❌ <b>Tiklash amalga oshmadi:</b>\n{res_text}", reply_markup=MENU)
    except json.JSONDecodeError:
        await status_msg.edit_text("❌ <b>Xatolik:</b> Yuborilgan fayl to'g'ri JSON formatida emas.", reply_markup=MENU)
    except Exception as e:
        logging.error(f"User restore xatosi: {e}")
        await status_msg.edit_text(f"❌ <b>Tiklashda xatolik yuz berdi:</b> <code>{html.escape(str(e))}</code>", reply_markup=MENU)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


@dp.message(Pay.receipt, F.photo)
async def got_receipt(m: types.Message, state: FSMContext):
    key = (await state.get_data())["plan"]
    p = PLANS[key]
    file_id = m.photo[-1].file_id
    cur = db.execute(
        "INSERT INTO payments (user_id, plan, amount, file_id, created_at) VALUES (?,?,?,?,?)",
        (m.from_user.id, key, p["price"], file_id, now_utc().isoformat()),
    )
    db.commit()
    pid = cur.lastrowid
    await state.clear()
    await m.answer("✅ <b>Chek qabul qilindi!</b> Admin tekshirib tasdiqlagach, tarifingiz avtomatik faollashadi.", reply_markup=MENU)
    
    caption = (
        f"💳 <b>Yangi To'lov #{pid}</b>\n\n"
        f"👤 <b>Foydalanuvchi:</b> {html.escape(m.from_user.full_name)} (<code>{m.from_user.id}</code>)\n"
        f"💎 <b>Tarif:</b> <b>{p['name']}</b> — <code>{p['price']:,} so'm</code>\n"
        f"📅 <b>Vaqti:</b> <code>{fmt(now_utc())}</code>".replace(",", " ")
    )
    for aid in ADMIN_IDS:
        try:
            await bot.send_photo(aid, file_id, caption=caption,
                                 reply_markup=ik([("✅ Tasdiqlash", f"pay_ok:{pid}"), ("❌ Rad etish", f"pay_no:{pid}")]))
        except Exception as e:
            logging.error(f"Adminga chek yuborilmadi ({aid}): {e}")


@dp.message(Pay.receipt)
async def receipt_wrong(m: types.Message):
    await m.answer("⚠️ Iltimos, to'lov chekini <b>rasm (skrinshot)</b> sifatida yuboring yoki /cancel bosing.")


def apply_plan(uid, plan_key, days=PLAN_DAYS):
    cur_plan, exp = get_user_plan(uid)
    if plan_key == "start":
        new_exp = None
    else:
        base = exp if (exp and exp > now_utc()) else now_utc()
        new_exp = base + timedelta(days=days)

    exp_iso = new_exp.isoformat() if new_exp else None
    q("UPDATE users SET plan=?, expires_at=? WHERE user_id=?", (plan_key, exp_iso, uid), commit=True)
    today_s = datetime.now(TZ).strftime("%Y-%m-%d")
    q("""INSERT INTO subscriptions (user_id, plan_name, expires_at, daily_posts_count, last_post_date, created_at)
         VALUES (?,?,?,0,?,?)
         ON CONFLICT(user_id) DO UPDATE SET
            plan_name=excluded.plan_name,
            expires_at=excluded.expires_at,
            last_post_date=excluded.last_post_date""",
      (uid, plan_key, exp_iso, today_s, now_utc().isoformat()), commit=True)
    return new_exp


@dp.callback_query(F.from_user.id.in_(ADMIN_IDS), F.data.startswith(("pay_ok:", "pay_no:")))
async def pay_decision(cb: types.CallbackQuery):
    action, pid = cb.data.split(":")
    pay = q("SELECT * FROM payments WHERE id=?", (int(pid),), one=True)
    if not pay or pay["status"] != "pending":
        return await cb.answer("Bu to'lov allaqachon ko'rib chiqilgan!", show_alert=True)
    if action == "pay_ok":
        q("INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?,?)", (pay["user_id"], now_utc().isoformat()), commit=True)
        exp = apply_plan(pay["user_id"], pay["plan"])
        q("UPDATE payments SET status='approved' WHERE id=?", (pay["id"],), commit=True)
        note = f"🎉 <b>To'lovingiz tasdiqlandi!</b>\n\n💎 <b>{PLANS[pay['plan']]['name']}</b> tarifi <b>{fmt(exp)}</b> gacha faollashtirildi. Marhamat, kanallaringizni ulang!"
        status = "✅ Tasdiqlandi"
    else:
        q("UPDATE payments SET status='rejected' WHERE id=?", (pay["id"],), commit=True)
        note = "❌ <b>To'lovingiz tasdiqlanmadi.</b> Chek yaroqsiz bo'lsa yoki summa to'liq bo'lmasa, qayta to'lov qiling yoki adminga murojaat qiling."
        status = "❌ Rad etildi"
    try:
        await bot.send_message(pay["user_id"], note)
    except Exception:
        pass
    await cb.message.edit_caption(caption=(cb.message.caption or "") + f"\n\n<b>{status}</b> ({html.escape(cb.from_user.full_name)})", reply_markup=None)
    await cb.answer("Bajarildi!")


# ============================ KANAL ULASH ============================
async def is_admin_of(chat_id, user_id):
    if user_id in ADMIN_IDS:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


@dp.message(F.chat.type == "private", F.text == "➕ Kanal ulash")
async def add_channel(m: types.Message, state: FSMContext):
    await state.clear()
    allowed, limit_msg = check_user_limits(m.from_user.id, "add_channel")
    if not allowed:
        return await m.answer(limit_msg, reply_markup=plans_keyboard())
    await state.set_state(AddCh.channel)
    await m.answer(
        "1️⃣ <b>Kanalni tayyorlash:</b>\n\n"
        "1. Botni kanalingizga <b>Admin</b> qiling (Post yuborish va tahrirlash huquqi bilan).\n"
        "2. So'ng kanalingizning <b>to'liq havolasi</b> (masalan: <code>https://t.me/kanalingiz</code>), "
        "<b>@username</b> manzilini yoki kanal ID raqamini shu yerga yuboring.\n\n"
        "❌ Bekor qilish: /cancel"
    )


@dp.message(AddCh.channel)
async def add_ch_channel(m: types.Message, state: FSMContext):
    target = None
    if m.forward_from_chat and m.forward_from_chat.type == "channel":
        target = m.forward_from_chat.id
    elif m.text:
        target = clean_channel_input(m.text)
    else:
        return await m.answer("⚠️ Iltimos, kanal havolasini yuboring yoki kanaldan biror postni bu yerga forward qiling.")
    try:
        chat = await bot.get_chat(target)
    except Exception:
        return await m.answer(
            "❌ <b>Kanal topilmadi!</b>\n\n"
            "Iltimos, havolani to'g'ri yuborganingizni tekshiring (masalan: <code>https://t.me/kanal_nomi</code> yoki <code>@kanal_nomi</code>) "
            "va bot u yerda admin qilinganligiga ishonch hosil qiling."
        )
    if chat.type != "channel":
        return await m.answer("❌ Bu kanal emas. Iltimos, Telegram kanal havolasini yuboring.")
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception:
        return await m.answer("❌ Bot bu kanalda topilmadi. Avval botni kanalingizga admin qiling.")
    if me.status != "administrator" or not getattr(me, "can_post_messages", True):
        return await m.answer("❌ Bot kanalda post yuborish huquqiga ega admin emas. Huquqlarini to'liq bering.")
    is_adm = m.from_user.id in ADMIN_IDS
    if not await is_admin_of(chat.id, m.from_user.id) and not is_adm:
        return await m.answer("❌ Siz bu kanalning admini emassiz.")
    existing = q("SELECT owner_id FROM channels WHERE channel_id=?", (chat.id,), one=True)
    if existing and existing["owner_id"] != m.from_user.id and not is_adm:
        return await m.answer("❌ Bu kanal boshqa foydalanuvchi tomonidan tizimga ulangan.")
    
    await state.update_data(channel_id=chat.id, title=chat.title)
    await state.set_state(AddCh.niche)
    await m.answer(
        f"✅ <b>{html.escape(chat.title)}</b> muvaffaqiyatli tekshirildi!\n\n"
        "2️⃣ <b>Kanal yo'nalishi nima?</b> (Masalan: <i>Dasturlash va IT, Trading va Kriptovalyuta, PUBG Mobile, Ingliz tili, Biznes va Startaplar</i>):"
    )


@dp.message(AddCh.niche, F.text)
async def add_ch_niche(m: types.Message, state: FSMContext):
    await state.update_data(niche=m.text.strip())
    await state.set_state(AddCh.detailed_prompt)
    await m.answer(
        "3️⃣ <b>AI UCHUN BATAFSIL PROMPT VA TALABLARINGIZ:</b>\n\n"
        "AI aynan nimalar haqida yozishi kerakligini batafsil tushuntiring. "
        "Faqat bitta mavzuni aylantirmaslik uchun, nimalarni ko'proq yoritishi kerakligini yozing!\n\n"
        "<i>Masalan:</i>\n"
        "«Kanalda faqat pul ishlash emas, balki real keyslar (kim qanday bot/sayt qilib sotdi), "
        "botlarni tekin xostinglarga qo'yish (Render, PythonAnywhere), sayt yaratish asboblari, "
        "YouTube'dagi eng zo'r darsliklar tavsiyasi, qiziqarli IT o'yinlar, topishmoqlar va kulgili memlar chiqsin. "
        "Xuddi onadek mehribonlik bilan o'rgatuvchi tilda yozilsin.»\n\n"
        "👇 <b>O'z batafsil yo'riqnomangizni yuboring (cheksiz uzunlikda yozishingiz mumkin):</b>"
    )


@dp.message(AddCh.detailed_prompt, F.text)
async def add_ch_prompt(m: types.Message, state: FSMContext):
    await state.update_data(detailed_prompt=m.text.strip())
    await state.set_state(AddCh.audience)
    await m.answer(
        "4️⃣ <b>Auditoriyangiz kimlar?</b>\n\n"
        "Yoshi, darajasi va qiziqishlari (Masalan: <i>16–30 yoshli talabalar, boshlovchi dasturchilar va o'rganuvchilar</i>):"
    )


@dp.message(AddCh.audience, F.text)
async def add_ch_audience(m: types.Message, state: FSMContext):
    await state.update_data(audience=m.text.strip())
    await state.set_state(AddCh.group)
    await m.answer(
        "5️⃣ <b>Kanalning muhokama guruhi (chat) bormi?</b>\n\n"
        "Guruh havolasi (<code>https://t.me/guruh</code>), <b>@username</b> yoki ID raqamini yuboring "
        "(bot u yerda ham admin bo'lishi kerak, shunda so'kinganlarni jazolaydi va a'zolarga do'stona javob beradi).\n\n"
        "<i>Agar guruhingiz bo'lmasa, shunchaki «yo'q» deb yozing.</i>"
    )


@dp.message(AddCh.group, F.text)
async def add_ch_group(m: types.Message, state: FSMContext):
    group_id = None
    txt = m.text.strip()
    if txt.lower() not in ("yo'q", "yoq", "yo‘q", "yo`q", "none", "no"):
        target_g = clean_channel_input(txt)
        try:
            g = await bot.get_chat(target_g)
            me = await bot.get_chat_member(g.id, bot.id)
        except Exception:
            return await m.answer("❌ Guruh topilmadi yoki bot unda yo'q. Qayta yuboring yoki «yo'q» deb yozing.")
        if g.type not in ("group", "supergroup") or me.status != "administrator":
            return await m.answer("❌ Bu guruh emas yoki bot unda admin emas. Botni admin qiling.")
        if not await is_admin_of(g.id, m.from_user.id):
            return await m.answer("❌ Siz bu guruhning admini emassiz.")
        group_id = g.id
        allowed, limit_msg = check_user_limits(m.from_user.id, "add_group")
        if not allowed:
            return await m.answer(limit_msg, reply_markup=plans_keyboard())
    
    await state.update_data(group_id=group_id)
    await state.set_state(AddCh.hours)
    await m.answer(
        "6️⃣ <b>Postlar qaysi soatlarda chiqsin?</b>\n\n"
        "Soatlarni vergul bilan kiriting (0 dan 23 gacha, masalan: <code>9, 14, 20</code>).\n"
        "<i>Agar tarifingizdagi standart soatlar qolishini xohlasangiz, «standart» deb yozing.</i>"
    )


@dp.message(AddCh.hours, F.text)
async def add_ch_hours(m: types.Message, state: FSMContext):
    txt = m.text.strip().lower()
    custom_hours = ""
    if txt not in ("standart", "standart rejim", "default"):
        raw_nums = re.findall(r"\b([0-1]?[0-9]|2[0-3])\b", txt)
        hrs = [int(x) for x in raw_nums if 0 <= int(x) <= 23]
        if hrs:
            custom_hours = ",".join(map(str, sorted(set(hrs))))
        else:
            return await m.answer("⚠️ Iltimos, soatlarni to'g'ri kiriting (masalan: <code>9, 14, 20</code>) yoki «standart» deb yozing:")
    
    d = await state.get_data()
    q("""INSERT INTO channels (channel_id, owner_id, title, niche, detailed_prompt, audience, group_id, custom_hours, created_at)
         VALUES (?,?,?,?,?,?,?,?,?)
         ON CONFLICT(channel_id) DO UPDATE SET
            owner_id=excluded.owner_id, title=excluded.title, niche=excluded.niche,
            detailed_prompt=excluded.detailed_prompt, audience=excluded.audience,
            group_id=excluded.group_id, custom_hours=excluded.custom_hours""",
      (d["channel_id"], m.from_user.id, d["title"], d["niche"], d["detailed_prompt"], d["audience"],
       d["group_id"], custom_hours, now_utc().isoformat()), commit=True)
    await state.clear()
    
    # Zaxira nusxani darhol yangilash
    auto_backup_channels()
    
    await m.answer(
        f"🎉 <b>Kanal muvaffaqiyatli ulandi!</b>\n\n"
        f"📺 <b>Nomi:</b> <b>{html.escape(d['title'])}</b>\n"
        f"🎯 <b>Yo'nalish:</b> {html.escape(d['niche'])}\n"
        f"⏰ <b>Soatlar:</b> <code>{custom_hours or 'Tarif bo`yicha standart'}</code>\n"
        f"🛡 <b>Guruh nazorati:</b> {'Ulangan va faol' if d['group_id'] else 'Ulanmagan'}\n\n"
        f"🚀 <i>Endi «📺 Kanallarim» bo'limi orqali istalgan payt rasm bilan post yaratishingiz yoki so'rovnoma yuborishingiz mumkin!</i>",
        reply_markup=MENU,
    )


# ============================ KANALLARIM BO'LIMI ============================
def own_channel(cid, uid):
    if uid in ADMIN_IDS:
        row = q("SELECT * FROM channels WHERE channel_id=?", (cid,), one=True)
    else:
        row = q("SELECT * FROM channels WHERE channel_id=? AND owner_id=?", (cid, uid), one=True)
    return dict(row) if row else None


@dp.message(F.chat.type == "private", F.text == "📺 Kanallarim")
async def my_channels(m: types.Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in ADMIN_IDS:
        rows = q("SELECT channel_id, title, active FROM channels")
    else:
        rows = q("SELECT channel_id, title, active FROM channels WHERE owner_id=?", (m.from_user.id,))
    if not rows:
        return await m.answer("📺 Hali hech qanday kanal ulanmagan. «➕ Kanal ulash» tugmasini bosing.")
    await m.answer(
        "📺 <b>SIZNING ULANGAN KANALLARINGIZ:</b>\n\n"
        "<i>Boshqarish uchun kerakli kanalni tanlang 👇</i>",
        reply_markup=ik(*[[(("🟢 " if r["active"] else "⏸ ") + r["title"], f"ch:{r['channel_id']}")] for r in rows])
    )


async def channel_card(cid, uid):
    c = own_channel(cid, uid)
    if not c:
        return None, None
    try:
        subs = await bot.get_chat_member_count(cid)
    except Exception:
        subs = "Aniqlanmadi"
    plan, exp = get_user_plan(uid)
    if uid in ADMIN_IDS and not plan:
        plan = "max"
    hours_display = c["custom_hours"] if c["custom_hours"] else (", ".join(map(str, PLANS[plan]["hours"])) if plan else "Yo'q")
    prompt_snippet = (c["detailed_prompt"][:120] + "...") if c["detailed_prompt"] and len(c["detailed_prompt"]) > 120 else (c["detailed_prompt"] or "Kiritilmagan")
    
    arc_topic_disp = c.get("current_topic") or "Yangi seriya kutilmoqda"
    arc_day_disp = f"{c.get('arc_day', 1)}/{c.get('arc_total_days', 3)}-kun"
    
    text = (
        f"📺 <b>KANAL: {html.escape(c['title'])}</b>\n\n"
        f"⚙️ <b>Holat:</b> {'🟢 Faol' if c['active'] else '⏸ To`xtatilgan'}"
        f"{'' if plan else ' ⚠️ (Tarif tugagan!)'}\n"
        f"🎯 <b>Yo'nalish:</b> <b>{html.escape(c['niche'] or '')}</b>\n"
        f"👥 <b>Auditoriya:</b> <i>{html.escape(c['audience'] or '')}</i>\n"
        f"🎬 <b>Serial voronkasi:</b> <code>{arc_day_disp}</code> ({html.escape(arc_topic_disp[:40])})\n"
        f"💬 <b>Guruh ID:</b> <code>{c['group_id'] or 'Ulanmagan'}</code>\n"
        f"⏰ <b>Post soatlari:</b> <code>{hours_display}</code>\n"
        f"📝 <b>AI Prompt:</b> <i>{html.escape(prompt_snippet)}</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>STATISTIKA:</b>\n"
        f"• 👥 Obunachilar: <b>{subs}</b>\n"
        f"• 📝 Yuborilgan postlar: <b>{c['posts_sent']} ta</b>\n"
        f"• 📊 Yuborilgan so'rovlar: <b>{c['polls_sent']} ta</b>\n"
        f"• 💬 Guruhdagi xabarlar: <b>{c['group_messages']} ta</b>\n"
        f"• ⚠️ Guruh ogohlantirishlari: <b>{c['warnings']} ta</b>\n"
        f"• 🚫 Guruh bloklari: <b>{c['bans']} ta</b>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    kb = ik(
        [("🖼 Rasm + Post yuborish", f"imgpost:{cid}"), ("📊 So'rovnoma (Poll) yuborish", f"poll:{cid}")],
        [("📝 Faqat matnli post", f"post:{cid}"), ("⏰ Soatlarni sozlash", f"sethours:{cid}")],
        [("✏️ Promptni tahrirlash", f"setprompt:{cid}"), ("💬 Guruhni ulash/o'zgartirish", f"setgrp:{cid}")],
        [("⏸ / ▶️ Holatni o'zgartirish", f"toggle:{cid}"), ("🗑 Kanalni o'chirish", f"del:{cid}")],
    )
    return text, kb


@dp.callback_query(F.data.startswith("ch:"))
async def show_channel(cb: types.CallbackQuery):
    text, kb = await channel_card(int(cb.data.split(":")[1]), cb.from_user.id)
    if not text:
        return await cb.answer("Kanal topilmadi!", show_alert=True)
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_channel(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer()
    q("UPDATE channels SET active = 1 - active WHERE channel_id=?", (cid,), commit=True)
    auto_backup_channels()
    text, kb = await channel_card(cid, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Holat yangilandi!")


@dp.callback_query(F.data.startswith("del:"))
async def delete_channel(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    if own_channel(cid, cb.from_user.id):
        q("DELETE FROM channels WHERE channel_id=?", (cid,), commit=True)
        auto_backup_channels()
        await cb.message.edit_text("🗑 <b>Kanal muvaffaqiyatli olib tashlandi.</b>")
    await cb.answer()


# ============================ PROMPT, GURUH VA SOATLARNI TAHRIRLASH ============================
@dp.callback_query(F.data.startswith("setprompt:"))
async def edit_prompt_start(cb: types.CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer("Kanal topilmadi")
    await state.set_state(EditPrompt.prompt)
    await state.update_data(cid=cid)
    await cb.message.answer(
        "✏️ <b>AI UCHUN YANGI PROMPT (YO'RIQNOMA) YOZING:</b>\n\n"
        "AI qanday mavzularda, qanday formatda, nimalarni ko'proq yoritishi kerakligini batafsil yozing. "
        "Istalgancha uzun matn yozishingiz mumkin!\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(EditPrompt.prompt, F.text)
async def edit_prompt_finish(m: types.Message, state: FSMContext):
    d = await state.get_data()
    cid = d["cid"]
    q("UPDATE channels SET detailed_prompt=? WHERE channel_id=?", (m.text.strip(), cid), commit=True)
    auto_backup_channels()
    await state.clear()
    await m.answer("✅ <b>AI Prompti muvaffaqiyatli yangilandi!</b>", reply_markup=MENU)


@dp.callback_query(F.data.startswith("sethours:"))
async def edit_hours_start(cb: types.CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer("Kanal topilmadi")
    await state.set_state(EditHours.hours)
    await state.update_data(cid=cid)
    await cb.message.answer(
        "⏰ <b>POST VA SO'ROVNOMA SOATLARINI SOZLASH:</b>\n\n"
        "Qaysi soatlarda avtomatik chiqsin? Vergul bilan kiriting (masalan: <code>8, 12, 17, 21</code>).\n"
        "Tarifdagi standart soatlarga qaytarish uchun «standart» deb yozing.\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(EditHours.hours, F.text)
async def edit_hours_finish(m: types.Message, state: FSMContext):
    d = await state.get_data()
    cid = d["cid"]
    txt = m.text.strip().lower()
    custom_hours = ""
    if txt not in ("standart", "standart rejim", "default"):
        raw_nums = re.findall(r"\b([0-1]?[0-9]|2[0-3])\b", txt)
        hrs = [int(x) for x in raw_nums if 0 <= int(x) <= 23]
        if hrs:
            custom_hours = ",".join(map(str, sorted(set(hrs))))
        else:
            return await m.answer("⚠️ Iltimos, soatlarni to'g'ri kiriting (masalan: <code>9, 14, 21</code>) yoki «standart» deb yozing:")
    
    q("UPDATE channels SET custom_hours=? WHERE channel_id=?", (custom_hours, cid), commit=True)
    auto_backup_channels()
    await state.clear()
    await m.answer(f"✅ <b>Soatlar sozlandi:</b> <code>{custom_hours or 'Standart'}</code>", reply_markup=MENU)


@dp.callback_query(F.data.startswith("setgrp:"))
async def edit_grp_start(cb: types.CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    if not own_channel(cid, cb.from_user.id):
        return await cb.answer("Kanal topilmadi")
    await state.set_state(EditGroup.group)
    await state.update_data(cid=cid)
    await cb.message.answer(
        "💬 <b>KANALNING MUHOKAMA GURUHI HAVOLASINI YUBORING:</b>\n\n"
        "Guruh havolasi (<code>https://t.me/guruh</code>), @username yoki ID raqamini yuboring.\n"
        "Bot u yerda admin bo'lishi kerak. Olib tashlash uchun «yo'q» deb yozing.\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(EditGroup.group, F.text)
async def edit_grp_finish(m: types.Message, state: FSMContext):
    d = await state.get_data()
    cid = d["cid"]
    txt = m.text.strip()
    group_id = None
    if txt.lower() not in ("yo'q", "yoq", "yo‘q", "none"):
        target_g = clean_channel_input(txt)
        try:
            g = await bot.get_chat(target_g)
            me = await bot.get_chat_member(g.id, bot.id)
            if me.status != "administrator":
                return await m.answer("❌ Bot bu guruhda admin emas. Avval admin qiling.")
            if not await is_admin_of(g.id, m.from_user.id):
                return await m.answer("❌ Siz bu guruhning admini emassiz.")
            group_id = g.id
            allowed, limit_msg = check_user_limits(m.from_user.id, "add_group")
            if not allowed:
                return await m.answer(limit_msg, reply_markup=plans_keyboard())
        except Exception:
            return await m.answer("❌ Guruh topilmadi yoki bot unda yo'q. Qayta urinib ko'ring yoki /cancel bosing.")
    
    q("UPDATE channels SET group_id=? WHERE channel_id=?", (group_id, cid), commit=True)
    auto_backup_channels()
    await state.clear()
    await m.answer(f"✅ <b>Guruh muvaffaqiyatli yangilandi:</b> <code>{group_id or 'Olib tashlandi'}</code>", reply_markup=MENU)


# ============================ PROFESSIONAL AI GENERATOR (100% KAFOLATLI VA KANALGA MOS) ============================
POST_THEMES = [
    "real_cases",       # Real loyihalar, keyslar, natija va muvaffaqiyat tajribalari
    "free_tools",       # Bepul asboblar, foydali platformalar va tekin vositalar
    "youtube_learning", # YouTube darsliklari, amaliy bilimlar (onadek/ustozdek g'amxo'r tilda)
    "interactive_game", # Qiziqarli mantiqiy savol, sohaviy topishmoq yoki test
    "funny_memes",      # Hayotiy kulgili voqea, mutaxassislar uchraydigan qiziqarli hazil
    "expert_guide"      # Kanal mavzusiga oid amaliy professional qo'llanma
]

IMAGE_STYLES = [
    "hyperrealistic 8k documentary photograph, natural studio lighting, ultra-detailed, depth of field",
    "futuristic 3d render, octane engine, vibrant glowing colors, modern tech aesthetic",
    "clean minimalist workspace photography, soft morning sunlight, aesthetic desk setup",
    "cinematic cyberpunk neon street scene, moody atmosphere, rich textures, 8k",
    "isometric 3d digital illustration, vivid pastel colors, clean vector design",
    "cozy vintage retro tech atmosphere, warm tones, high quality photography"
]


def generate_niche_adaptive_post_and_poll(c, theme=None):
    """
    Kanal yo'nalishi (niche), sarlavhasi (title) va promptiga 100% moslashuvchan,
    YouTube darslik havolasi va maxsus so'rovnomaga ega professional generator.
    AI kalit ishlamagan taqdirda ham kanallar bir-birini hech qachon takrorlamaydi!
    """
    c = dict(c) if c else {}
    title = (c.get("title") or "Kanalimiz").strip()
    niche = (c.get("niche") or "Foydali bilimlar va amaliyot").strip()
    detailed = (c.get("detailed_prompt") or "").strip()
    
    if not theme or theme not in POST_THEMES:
        theme = random.choice(POST_THEMES)
        
    n_lower = f"{niche} {detailed}".lower()
    
    # 1. Trading / Kripto
    if any(k in n_lower for k in ["treding", "trading", "kripto", "crypto", "forex", "valyuta", "scalping", "invest", "aksiya", "bozor"]):
        yt_query = "kripto va trading darslari uzbek"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = "professional modern financial trading desk multiple monitors glowing cryptocurrency candlestick charts dark mode 8k resolution"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: Kichik depozitdan intizom bilan natijaga chiqish!</b>\n\n"
                f"Ko'pchilik treydingda bitta bitim bilan boyib ketishni orzu qiladi va bor pulini yo'qotadi. "
                f"Lekin yaqinda bir treyder har bir bitimda atigi <b>1-2% risk</b> qilib, qat'iy reja bilan 3 oyda hisobini barqaror 40% ga o'stirdi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Treyding — bu omad o'yini emas, bu <b>ehtimollar va intizom</b> matematikasi.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da bepul trading darslarini tomosha qiling</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror strategiyani sinab ko'ring! Bugun qaysi juftlikni kuzatyapsiz?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>Treyding uchun eng zo'r BEPUL asboblar:</b>\n\n"
                f"Grafik va bozor tahlili uchun qimmat dasturlar shart emas. Bepul vositalardan unumli foydalaning:\n\n"
                f"1. <b>TradingView</b> — Eng kuchli grafiklar, indikatorlar va tahlil maydoni.\n"
                f"2. <b>CoinMarketCap / CoinGecko</b> — Kriptovalyutalar kapitallashuvi va likvidlik ko'rsatkichlari.\n"
                f"3. <b>CryptoPanic</b> — Eng qaynoq bozor yangiliklari bitta tasmasida.\n"
                f"4. <b>Notion / Google Sheets</b> — Xatolarni tahlil qilish uchun shaxsiy savdo jurnali.\n\n"
                f"🎥 <b>Qo'llanma:</b> <a href=\"{yt_url}\">YouTube'da instrumentlardan foydalanish darslari</a>\n\n"
                f"💡 <i>Siz ko'proq qaysi platformada tahlil qilasiz?</i> 🚀"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: Treyding bo'yicha YouTube'dagi oltin tavsiyalar!</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"Treydingda muvaffaqiyat qozonish uchun shoshilmang, avval bilim oling! ✨\n\n"
                f"📌 <b>O'rganish kerak bo'lgan eng zarur mavzular:</b>\n"
                f"• <code>Texnik tahlil va shamchalar (Candlestick) signallari</code>\n"
                f"• <code>Risk va Money Management (Kapitalni asrash)</code>\n"
                f"• <code>Treyding psixologiyasi va FOMO bilan kurashish</code>\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da ushbu mavzular bo'yicha darsliklarni ko'rish</a>\n\n"
                f"<i>Kuniga 30 daqiqa amaliyot qilsangiz, bir oyda natijangiz mutlaqo boshqacha bo'ladi!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Mantiqiy savol: Qaysi treyder oxir-oqibat g'olib bo'ladi?</b>\n\n"
                f"Tasavvur qiling, bozorda 2 xil treyder bor:\n"
                f"A) Bitimlarining 80%i to'g'ri chiqadi, lekin Stop-loss qo'ymaydi.\n"
                f"B) Bitimlarining atigi 45%i to'g'ri chiqadi, lekin har doim 1:3 risk/reward va qat'iy Stop-loss bilan ishlaydi.\n\n"
                f"Sizningcha, 6 oydan keyin qaysi biri daromadda bo'ladi? O'z javobingizni izohda yozib qoldiring! 👇"
            ),
            "funny_memes": (
                f"😂 <b>Treyder hayotidan qiziqarli lahza:</b>\n\n"
                f"— Grafikka 3 soat tikilib «Hozir uchadi» deb sotib oldim: darhol pastga quladi! 📉\n"
                f"— Chidolmay sotsam: raketadek tepaga uchib ketdi! 🚀\n\n"
                f"Tanish holatmi? Bu bozorning odatiy sinovi! Muhimi — emotsiyaga berilmaslik va sabr qilish 🧘‍♂️"
            ),
            "expert_guide": (
                f"💎 <b>Ekspert Maslahati: Treydingda depozitni saqlab qolishning 3 oltin qoidasi</b>\n\n"
                f"1. <b>Bitta bitimga butun depozitni tikmang</b> — Har qanday sharoitda 1-2% risk qoidasiga amal qiling.\n"
                f"2. <b>Stop-loss'siz bozorga kirmang</b> — Qabul qilinadigan zararni oldindan belgilang.\n"
                f"3. <b>FOMO (ortda qolish qo'rquviga) uchmang</b> — O'tib ketgan imkoniyat ortidan quvmang, yangisi albatta keladi.\n\n"
                f"🎥 <b>Tavsiya:</b> <a href=\"{yt_url}\">YouTube'da amaliy master-klass darslarini ko'rish</a>"
            )
        }
        
        poll_q_list = [
            "Siz treydingda eng ko'p qaysi uslubni qo'llaysiz? 📊",
            "Treydingda siz uchun eng katta qiyinchilik nima? 🤔",
            "Bozorda risk-menejmentga (1-2% risk) qat'iy amal qilasizmi? ⚖️"
        ]
        poll_opts_list = [
            ["Spot savdo (Uzoq muddat) 📈", "Scalping va tezkor savdo ⚡️", "Texnik tahlil va Smart Money 🧠", "Hali o'rganish bosqichidaman 📚"],
            ["Emotsiyani jilovlash (Sabr) 🧘‍♂️", "To'g'ri kirish nuqtasi topish 🎯", "Stop-loss intizomi ⏳", "Bozor tahlili 📉"],
            ["Ha, har bir bitimda riskni hisoblayman ✅", "Ba'zida emotsiyaga berilaman 😅", "Yo'q, stop-loss ishlatmayman ❌", "O'rganishim kerak 📝"]
        ]

    # 2. IT / Botlar / Web / Dasturlash
    elif any(k in n_lower for k in ["bot", "sayt", "dastur", "python", "web", "kod", "developer", "it", "xosting"]):
        yt_query = "telegram bot yaratish va python darslari noldan"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = "modern high-tech software engineer workstation dual monitors glowing code python terminal ambient neon lighting 8k resolution"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: Qanday qilib oddiy Telegram bot yaratib sotildi?</b>\n\n"
                f"Ko'pchilik katta dasturchi bo'lmasdan turib daromadli loyiha qilib bo'lmaydi deb o'ylaydi. "
                f"Lekin yaqinda bir yosh dasturchi oddiy <i>restoran buyurtma boti</i>ni yasab, bir nechta kafelarga "
                f"oylik obuna sifatida taqdim etdi va barqaror daromadga chiqdi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Katta narsani kutib o'tirmang! Hatto oddiy, lekin odamlar dardini yengillatadigan "
                f"kichik loyiha ham katta imkoniyatlar eshigini ochadi.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da bot yaratish bo'yicha bepul darsliklar</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring! Sizda qanday loyiha g'oyasi bor?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>Telegram botlarni qayerga TEKIN xostingga qo'yish mumkin?</b>\n\n"
                f"Kompyuteringizni 24/7 yoqib o'tirish shart emas. Botingizni bepul serverlarga joylashtiring:\n\n"
                f"1. <b>Render.com</b> — Background Worker yoki Web Service orqali 24/7 bepul ishlatish mumkin.\n"
                f"2. <b>PythonAnywhere.com</b> — Python botlar va skriptlar uchun eng qulay bepul xosting.\n"
                f"3. <b>Railway.app & Koyeb</b> — Tezkor va zamonaviy konteyner xostinglari.\n"
                f"4. <b>GitHub</b> — Loyihangiz kodini xavfsiz saqlash va Renderga ulash uchun.\n\n"
                f"🎥 <b>Ko'rsatma:</b> <a href=\"{yt_url}\">YouTube'da bepul serverga bot yuklash darslari</a>\n\n"
                f"💡 <i>Boshlanishiga ushbu platformalar sizga 100% yetadi. Sinab ko'rganmisiz?</i> 🚀"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: YouTube'dan nimalarni o'rganish kerak?</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"Internet — bepul bilimlar xazinasi! Vaqtingizni bekorchi videolarga emas, o'z ustingizda ishlashga sarflang ✨\n\n"
                f"📌 <b>Tavsiya qilinadigan qidiruv mavzulari:</b>\n"
                f"• <code>Telegram bot yaratish noldan aiogram 3</code>\n"
                f"• <code>Python dasturlash asoslari to'liq kurs</code>\n"
                f"• <code>Web-sayt yaratish va internet loyihalar</code>\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da ushbu bepul darsliklarni tomosha qiling</a>\n\n"
                f"<i>Kuniga atigi 30 daqiqa video ko'rib amaliyot qilsangiz, bir oyda natijangizni tanimay qolasiz!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Dasturchilar va foydalanuvchilar uchun qiziqarli mantiqiy topishmoq:</b>\n\n"
                f"Kodni qanchalik tez o'qiysiz?\n"
                f"<code>x = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x))</code>\n\n"
                f"Ekranga qanday natija chiqadi: <b>3</b>mi yoki <b>4</b>? Izohda o'z javobingizni qoldiring! 💡"
            ),
            "funny_memes": (
                f"😂 <b>Dasturchilar hayotidan kulgili holat:</b>\n\n"
                f"— «Dasturim kompyuterimda ideal ishlayotgan edi!»\n"
                f"— Serverga yuklaganimdan so'ng: <i>Error 500 / Crash</i> 😅\n\n"
                f"Haqiqiy dasturchi xatolardan qo'rqmaydi, loglarni o'qib darhol tuzatadi! Har qanday xato yangi tajriba beradi 🚀"
            ),
            "expert_guide": (
                f"💎 <b>Professional Maslahat: Qanday qilib birinchi IT loyihani sotish mumkin?</b>\n\n"
                f"1. <b>Atrofdagi muammolarni qidiring</b> — Do'kon, kafe yoki kurslar uchun buyurtma/ro'yxatdan o'tish boti qilib bering.\n"
                f"2. <b>Bepul namuna qiling</b> — «Sizga bepul 3 kun sinov boti ulab beraman, yoqsa davom ettirasiz» deng.\n"
                f"3. <b>Soddalikni saqlang</b> — Murakkab funksiyalardan ko'ra, tez va aniq ishlaydigan yechim qadrliroq.\n\n"
                f"🎥 <b>Tavsiya:</b> <a href=\"{yt_url}\">YouTube'da loyiha yaratish darslari</a>"
            )
        }
        
        poll_q_list = [
            "Kanalimizda navbatdagi amaliy dars qaysi mavzuda bo'lsin? 🚀",
            "Botingiz yoki veb-saytingizni bepul xostingga qo'yganmisiz? 🌐",
            "Siz ko'proq qaysi yo'nalishga qiziqasiz? 💻"
        ]
        poll_opts_list = [
            ["Telegram botlar va Python 🤖", "Web-saytlar va Frontend 💻", "Sun'iy intellekt (AI) integratsiyasi ⚡️", "Real keyslar va sotish sirlari 💡"],
            ["Ha, Render/PythonAnywhere ishlataman ✅", "Yo'q, hali bilmayman 🤔", "Kompyuterimda ishlaydi 💻", "Tekin xosting qo'llanmasi kerak 📝"],
            ["Backend va botlar ⚙️", "Frontend va saytlar 🎨", "AI va Data Science 🧠", "IT loyihalarni sotish va biznes 💰"]
        ]

    # 3. Biznes / Startap / Savdo
    elif any(k in n_lower for k in ["biznes", "startap", "savdo", "marketing", "mijoz", "sotuv", "daromad"]):
        yt_query = "biznes boshlash va daromad sirlari noldan"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = "modern minimalist business executive office glass windows city skyline confident entrepreneur working on laptop 8k"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: 0 so'm xarajat bilan xizmat ko'rsatish biznesini boshlash!</b>\n\n"
                f"Ko'pchilik biznes uchun katta mablag' kerak deb boshlashni kechiktiradi. "
                f"Lekin bir tadbirkor shunchaki o'z sohasidagi mijozlarni topib, pudratchi mutaxassislar bilan "
                f"bog'lash (vositachilik) orqali hech qanday boshlang'ich pulsiz daromadli biznes modelini yo'lga qo'ydi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Eng muhimi kapital emas, balki <b>mijoz topish va muammosini hal qilish</b> qobiliyati.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da biznes va startap darslari</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring! Sizda qanday biznes g'oya bor?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>Biznes va startap uchun eng foydali BEPUL vositalar:</b>\n\n"
                f"1. <b>Notion & Trello</b> — Loyihalarni, rejalarni va vazifalarni tizimli boshqarish.\n"
                f"2. <b>Canva</b> — Sifatli reklama bannerlari va taqdimotlar tayyorlash.\n"
                f"3. <b>Google Sheets & Forms</b> — Mijozlar bazasi va so'rovnomalarni qulay yuritish.\n"
                f"4. <b>Telegram kanallar va guruhlar</b> — Bepul organik auditoriya to'plash maydoni.\n\n"
                f"🎥 <b>Qo'llanma:</b> <a href=\"{yt_url}\">YouTube'da biznes asboblari bo'yicha darslar</a>"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: Biznes va marketing sirlarini o'rganing!</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"Zamonaviy dunyoda eng qimmat ko'nikma — bu sotish, marketing va muzokara olib borishdir! ✨\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da biznes va savdoni rivojlantirish darslari</a>\n\n"
                f"<i>Kuniga bittadan amaliy keysni tahlil qiling va o'z faoliyatingizga tadbiq eting!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Biznes topishmoq: Qaysi mahsulot ko'proq sotiladi?</b>\n\n"
                f"A) Narxi juda arzon, lekin hech kim bilmaydigan zo'r mahsulot.\n"
                f"B) Narxi o'rtacha, lekin aniq auditoriya muammosini hal qiluvchi kuchli reklama qilingan mahsulot.\n\n"
                f"Sizningcha, uzoq muddatda qaysi biri bozor yetakchisiga aylanadi? Izohda fikringizni yozing! 💡"
            ),
            "funny_memes": (
                f"😂 <b>Biznesdagi kulgili hayotiy holat:</b>\n\n"
                f"— «Ertadan yangi loyihani boshlayman, biznes reja tayyor!»\n"
                f"— 3 oydan so'ng: Hali ham biznes rejani mukammallashtirib o'tiribdi... 😅\n\n"
                f"Mukammallikni kutmang, hoziroq harakatni boshlang! Haqiqiy natija faqat amaliyotda keladi 🚀"
            ),
            "expert_guide": (
                f"💎 <b>Ekspert Maslahati: Yangi loyihada mijozlarni jalb qilishning 3 qoidasi</b>\n\n"
                f"1. <b>Mijoz dardiga e'tibor bering</b> — Mahsulotingiz emas, u beradigan foyda sotadi.\n"
                f"2. <b>Ochiq muloqot va ishonch</b> — Mijozlar bilan samimiy munosabat o'rnating.\n"
                f"3. <b>Doimiy sinov (A/B testing)</b> — Har xil takliflarni sinab ko'ring va eng yaxshisini tanlang."
            )
        }
        
        poll_q_list = [
            "Biznes yoki startap boshlashda siz uchun eng katta to'siq nima? 💡",
            "Mijozlarni jalb qilishda eng samarali usul qaysi deb o'ylaysiz? 📢",
            "Shaxsiy loyihangizni qachon ishga tushirmoqchisiz? 🚀"
        ]
        poll_opts_list = [
            ["Boshlang'ich kapital topish 💰", "Mijozlarni jalb qilish (Marketing) 📢", "To'g'ri g'oya tanlash 🎯", "Jamoa va intizom ⏳"],
            ["Telegram va ijtimoiy tarmoqlar 📱", "Tavsiyalar (Sarafan marketing) 🤝", "Target va to'g'ridan-to'g'ri reklama 🎯", "Shaxsiy brend va blog yuritish ✨"],
            ["Hozirda faol ishlayapman 🔥", "Shu oy ichida boshlayman 🚀", "Hali rejalashtiryapman 🤔", "G'oya qidiryapman 💡"]
        ]

    # 4. Gaming / PUBG / Kibersport
    elif any(k in n_lower for k in ["pubg", "pabg", "geym", "game", "gaming", "oyun", "o'yin", "kibersport", "esport", "free fire", "standoff", "cs", "dota", "brawl"]):
        yt_query = "pubg mobile uzbek darslari giroskop sprey sozlamalari"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = "cinematic pubg mobile battle royale intense action scene, player with level 3 helmet and backpack, holding m416 assault rifle, pochinki village background, explosive dynamic lighting, 8k render, unreal engine 5 style"
        
        posts = {
            "real_cases": (
                f"🔥 <b>Real Keys: 2 K/D dan 6+ K/D ga qanday chiqdim? (Shaxsiy tajriba)</b>\n\n"
                f"Ko'pchilik «K/D ko'tarish uchun faqat qimmat telefon yoki iPad kerak» deb o'ylaydi. "
                f"Lekin asosiy sir — <b>to'g'ri giroskop sensi</b> va <b>rush qilish taktikasi</b>da!\n\n"
                f"📌 <b>Menga yordam bergan 3 ta narsa:</b>\n"
                f"1. <b>Giroskopni 350-400% ga ko'tarish</b> — Barmoq bilan ekranni tortgandan ko'ra giroskop 3 barobar tezroq nishon oladi.\n"
                f"2. <b>Close Combatda Jiggle + Pre-fire</b> — Dushman burchakdan chiqishidan oldin o'q otishni boshlash.\n"
                f"3. <b>Kallani yo'qotib rush qilmaslik</b> — Har doim orqada tosh yoki devor (cover) bo'lishi shart!\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da bepul PUBG darslarini ko'rish</a>\n\n"
                f"⚡️ <i>Siz hozir qaysi rankdasiz va K/D ko'rsatkichingiz nechida? Izohda yozing!</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>PUBG Mobile: 90 FPS va o'yinni qotmasdan uchirish uchun foydali maslahatlar:</b>\n\n"
                f"O'yinda qotish (lag/drop) va qizishni kamaytirishning amaliy usullari:\n\n"
                f"1. <b>Grafika: Smooth (Plavno) + Extreme/90 FPS</b> — Hech qachon HD yoki HDR qo'ymang, batareya qiziydi va FPS tushadi.\n"
                f"2. <b>Audio: High (Ultra emas)</b> — Ultra audio ba'zi telefonlarda ovozni kechiktiradi va yuklama beradi.\n"
                f"3. <b>Xotira tozalash</b> — O'yinga kirishdan oldin fondagi barcha ilovalarni yoping va xotirani bo'shating.\n"
                f"4. <b>Keshni tozalash</b> — PUBG yuklamalari ichidagi ortiqcha resurs paketlarini o'chirib turing.\n\n"
                f"🎥 <b>Qo'llanma:</b> <a href=\"{yt_url}\">YouTube'da FPS oshirish va sozlamalar darslari</a>\n\n"
                f"💡 <i>Sizda o'yin necha FPS ochadi (60 FPS, 90 FPS yoki 120 FPS)?</i> 🚀"
            ),
            "youtube_learning": (
                f"🎥 <b>Pro Geymer Darsi: M416 bilan 100 metrdan lazer sprey qilish siri!</b>\n\n"
                f"Do'stlar, agar M416 bilan otganda o'q osmonga uchib ketayotgan bo'lsa, xafa bo'lmang! Spreyni nollashning aniq formulasi bor:\n\n"
                f"📌 <b>Bajarilishi shart bo'lgan qoidalar:</b>\n"
                f"• <code>M416 + Kompensator + Vertikal dastak (Vertical Grip) + Taktik priklad</code> — eng stabil jamlanma!\n"
                f"• <code>6x ni 3x ga qisqartirish</code> — 6x ni to'g'ridan-to'g'ri ishlatmang, 3x ga tushirib sprey qiling.\n"
                f"• <code>O'tirib otish (Crouch)</code> — O'tirganda qurol rekoili (otdacha) 50% ga kamayadi!\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da giroskop va sprey darslarini ko'rish</a>\n\n"
                f"<i>Trenirovka zonasida kuniga 15 daqiqa sprey mashq qilsangiz, natijaga o'zingiz qoyil qolasiz!</i> 🎯"
            ),
            "interactive_game": (
                f"🎮 <b>Geymerlar uchun test: 1v4 vaziyatda nima qilasiz?</b>\n\n"
                f"Tasavvur qiling: Squadingiz o'lgan, siz Butkemp asosiy binosidasiz. 4 kishilik to'liq dushman jamoasi siz tomonga rush qilyapti!\n\n"
                f"A) Zinaga granata (Nade) yoki Molotov tashlab, UZI/DBS bilan bittalab kutib olaman.\n"
                f"B) Derazadan sakrab orqa tarafdan aylanib o'tib (flank) kutilmaganda zarba beraman.\n"
                f"C) Bitta burchakda yotib olib (kempir qilib) dushmanni poylayman.\n\n"
                f"Siz qaysi taktikani tanlardingiz? Quyidagi so'rovnomada ovoz bering! 👇"
            ),
            "funny_memes": (
                f"😂 <b>PUBG o'ynaydigan har bir kishiga tanish holat:</b>\n\n"
                f"— «Og'ayni, bitta kishi qoldi, rush qil, qoni bitta urushlik!» 🗣\n"
                f"— Yugurib borsang: 4 kishi to'liq tarkib bilan kutib olib, 1 soniyada lobbiga jo'natishadi! 💀😂\n\n"
                f"Tanish holatmi? Qoni bitta degan gapga hech qachon ishonmang! Always check corners! 🏃‍♂️💨"
            ),
            "expert_guide": (
                f"💎 <b>Ekspert Maslahati: Butkemp va Pochinkida omon qolishning 3 temir qoidasi</b>\n\n"
                f"1. <b>Parashyutdan eng tez tushish (234 km/soat)</b> — Qurolni birinchi olgan odam lokatsiyani nazorat qiladi.\n"
                f"2. <b>Quloqchin (Naushnik) va tovush</b> — Dushmanning qadam tovushini (footsteps) eshitmasdan harakat qilmang.\n"
                f"3. <b>Ochiq maydonda yugurmaslik</b> — Har bir harakat coverdan coverga (boshpanadan boshpanaga) bo'lishi shart.\n\n"
                f"🎥 <b>Tavsiya:</b> <a href=\"{yt_url}\">YouTube'da Pochinki va Butkemp taktikalarini ko'rish</a>"
            )
        }
        
        poll_q_list = [
            "Siz PUBG Mobile'da qaysi qurilmada o'ynaysiz? 📱",
            "Yaqin masofada (Close Combat) siz uchun qaysi qurol eng kuchli? 💥",
            "Sizning PUBG dagi hozirgi K/D ko'rsatkichingiz qancha? 📊",
            "Siz ko'proq qaysi xaritada o'ynaysiz? 🗺"
        ]
        poll_opts_list = [
            ["Telefon (Android / iPhone) 📱", "iPad / Planshet 📟", "PC (Emulyator) 🖥", "Hali o'ynamayman 👀"],
            ["DBS / Shotgun 🔥", "M416 (Stabil sprey) 🎯", "UZI / Vector (Tezkor otish) ⚡️", "AKM / Beryl M762 (Katta damage) 💣"],
            ["5+ K/D (Kibersportchi) 🏆", "3 - 5 K/D (Tajribali) ⚡️", "1.5 - 3 K/D (O'rtacha) 🎯", "1 dan past (Erkakcha rush qilaman) 😂"],
            ["Erangel (Pochinki, Sosnovka) 🌲", "Sanhok (Butkemp, Paradise) 🌴", "Livik (Tezkor matchlar) ⚡️", "Miramar / Boshqa 🏜"]
        ]

    # 5. Universal / Boshqa sohalar
    else:
        yt_query = f"{niche} o'rganish va amaliy darslar"
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(yt_query)}"
        img_prompt = f"high quality editorial photograph inspiring atmosphere related to {niche} clean natural lighting 8k resolution"
        
        posts = {
            "real_cases": (
                f"🚀 <b>Real Keys: «{html.escape(niche)}» sohasida qanday qilib yuqori natijaga erishildi?</b>\n\n"
                f"Ko'pchilik katta natijalarga birdaniga erishiladi deb o'ylaydi. Lekin tajriba shuni ko'rsatadiki, "
                f"har kuni qo'yilgan kichik va qat'iy qadamlar bir necha oy ichida aqlbovar qilmas natijalarni beradi! 💡\n\n"
                f"📌 <b>Asosiy xulosa:</b> Doimiylik va to'g'ri yo'nalish har qanday murakkab maqsadni osonlashtiradi.\n\n"
                f"🎥 <b>Tavsiya darslik:</b> <a href=\"{yt_url}\">YouTube'da bepul darslik va amaliyotlarni ko'rish</a>\n\n"
                f"⚡️ <i>Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring! Sizning maqsadingiz nima?</i> 👇"
            ),
            "free_tools": (
                f"🛠 <b>«{html.escape(niche)}» yo'nalishida eng foydali BEPUL asboblar:</b>\n\n"
                f"1. <b>Internet platformalari</b> — Bepul onlayn kurslar va tahliliy maqolalar.\n"
                f"2. <b>Zamonaviy ilovalar</b> — Kundalik amaliyot va jarayonlarni soddalashtiruvchi vositalar.\n"
                f"3. <b>YouTube video darsliklari</b> — Dunyoning eng tajribali mutaxassislari tajribalari.\n\n"
                f"🎥 <b>Qo'llanma:</b> <a href=\"{yt_url}\">YouTube'da ushbu mavzu bo'yicha darslar</a>"
            ),
            "youtube_learning": (
                f"🎥 <b>Bilim olish vaqti: YouTube'dagi eng foydali yo'nalishlar!</b>\n\n"
                f"Aziz do'stlar, xuddi mehribon ustozingizdek sizga samimiy maslahat bermoqchiman: "
                f"O'z sohangizda mutaxassis bo'lish uchun har kuni yangi bilim o'rganishdan to'xtamang! ✨\n\n"
                f"👉 <a href=\"{yt_url}\">🎥 YouTube'da ushbu yo'nalish darsliklarini tomosha qiling</a>\n\n"
                f"<i>Kuniga 30 daqiqa video ko'rib amaliyot qilsangiz, tez orada natijalar o'z so'zini aytadi!</i> 📈"
            ),
            "interactive_game": (
                f"🎮 <b>Auditoriya uchun qiziqarli savol va fikr almashish:</b>\n\n"
                f"«{html.escape(niche)}» yo'nalishida sizningcha eng muhimi nima:\n"
                f"A) Chuqur nazariy bilim va qoidalar.\n"
                f"B) Har kuni tinimsiz amaliyot qilish va tajriba orttirish.\n\n"
                f"O'z fikringizni izohlarda yozib qoldiring! 👇"
            ),
            "funny_memes": (
                f"😂 <b>Hayotiy kulgili haqiqat:</b>\n\n"
                f"— «Hamma narsani rejalashtirib qo'ydim, dushanbadan 100% boshlayman!»\n"
                f"— Dushanba kuni: <i>Yana bir haftadan boshlasamchi?</i> 😅\n\n"
                f"Dushanbani kutmang, hoziroq birinchi qadamni tashlang! Harakatda baraka bor 🚀"
            ),
            "expert_guide": (
                f"💎 <b>Professional Maslahat: Muvaffaqiyatga eltuvchi 3 oltin qoida</b>\n\n"
                f"1. <b>Doimiylik (Intizom)</b> — Har kuni kichik bo'lsa ham amaliyot qiling.\n"
                f"2. <b>Sifatli manbalar</b> — Eng ishonchli va toza ma'lumotlarni o'rganing.\n"
                f"3. <b>Xatolar bilan ishlash</b> — Xatodan xulosa chiqarib, yanada kuchliroq davom eting.\n\n"
                f"🎥 <b>Tavsiya:</b> <a href=\"{yt_url}\">YouTube'da foydali darsliklarni ko'rish</a>"
            )
        }
        
        poll_q_list = [
            f"«{html.escape(title)}» kanalimizda qaysi mavzuni ko'proq yoritishimizni xohlaysiz? 💡",
            f"«{html.escape(niche[:40])}» yo'nalishida hozirgi darajangiz qanday? 📊",
            "Kanalimizdagi navbatdagi post qanday formatda bo'lsin? ✨"
        ]
        poll_opts_list = [
            ["Amaliy qo'llanmalar 📚", "Real muvaffaqiyat keyslari 💡", "Bepul vositalar va tavsiyalar 🛠", "Video darsliklar tavsiyasi 🎥"],
            ["Endi boshlayapman (Boshlovchi) 🌱", "O'rtacha darajada bilaman 📈", "Tajribam yetarli (Professional) 🎯", "Shunchaki qiziqaman 👀"],
            ["Batafsil mini-qo'llanma 📝", "Qiziqarli keys / Tarix 💡", "Mantiqiy savol / O'yin 🎮", "Foydali video tavsiya 🎥"]
        ]

    post_text = posts.get(theme) or posts.get("expert_guide") or list(posts.values())[0]
    idx = random.randint(0, len(poll_q_list) - 1)
    poll_q = poll_q_list[idx]
    poll_opts = poll_opts_list[idx]
    
    return {
        "post_text": post_text,
        "image_prompt": img_prompt,
        "poll_question": poll_q,
        "poll_options": poll_opts,
        "next_cliffhanger": "Ertaga ushbu mavzuning eng muhim qismini davom ettiramiz..."
    }


async def get_recent_chat_insights(channel_id):
    rows = q("SELECT user_name, text FROM chat_insights WHERE channel_id=? ORDER BY id DESC LIMIT 15", (channel_id,))
    if not rows:
        return "Guruhda hali alohida savollar yozilmagan."
    return "\n".join(f"- {r['user_name'] or 'A`zo'}: {r['text']}" for r in rows)


def safe_sanitize_telegram_html(text: str) -> str:
    """
    Telegram Bot API uchun HTML matnni 100% xavfsiz qiladi:
    - Ruxsat etilmagan teglarni (<br>, <p>, <div> va hk) tozalaydi yoki yangi qatorga aylantiradi.
    - Yopilmay qolgan barcha teglarni (<b>, <i>, <code>, <a>) avtomatik to'g'ri tartibda yopadi.
    - Yalang'och & va < belgilarini &amp; va &lt; ga aylantiradi.
    """
    if not text:
        return ""
    # 1. Almashtirish: Telegramda mavjud bo'lmagan teglarni yangi qatorga aylantirish
    s = re.sub(r'<(?:br|p|div|tr|li)\s*/?>', '\n', text, flags=re.IGNORECASE)
    s = re.sub(r'</(?:p|div|tr|li|ul|ol|table)>', '\n', s, flags=re.IGNORECASE)
    
    # 2. Telegram ruxsat bergan teglar
    ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote", "tg-spoiler"}
    
    # 3. Yalang'och & belgilarini &amp; ga aylantirish (&amp;, &lt;, &gt;, &quot; dan tashqari)
    s = re.sub(r'&(?!(?:amp|lt|gt|quot);)', '&amp;', s)
    
    # 4. Teglarni tekshirish va tag balancer
    tag_pattern = re.compile(r'(</?([a-zA-Z0-9\-_]+)(?:\s+[^>]*?)?>)')
    pos = 0
    result = []
    tag_stack = []
    
    for match in tag_pattern.finditer(s):
        start, end = match.span()
        full_tag = match.group(1)
        tag_name = match.group(2).lower()
        is_closing = full_tag.startswith("</")
        
        raw_segment = s[pos:start].replace("<", "&lt;")
        result.append(raw_segment)
        pos = end
        
        if tag_name not in ALLOWED_TAGS:
            continue
            
        if is_closing:
            if tag_name in tag_stack:
                while tag_stack and tag_stack[-1] != tag_name:
                    unclosed = tag_stack.pop()
                    result.append(f"</{unclosed}>")
                if tag_stack and tag_stack[-1] == tag_name:
                    tag_stack.pop()
                    result.append(f"</{tag_name}>")
            else:
                continue
        else:
            if tag_name == "a":
                m_href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', full_tag, re.IGNORECASE)
                if m_href:
                    href_val = m_href.group(1)
                    result.append(f'<a href="{href_val}">')
                    tag_stack.append("a")
            else:
                result.append(f"<{tag_name}>")
                tag_stack.append(tag_name)
                
    raw_segment = s[pos:].replace("<", "&lt;")
    result.append(raw_segment)
    
    while tag_stack:
        unclosed = tag_stack.pop()
        result.append(f"</{unclosed}>")
        
    cleaned_html = "".join(result)
    cleaned_html = re.sub(r'\n{3,}', '\n\n', cleaned_html).strip()
    return cleaned_html


def prepare_safe_caption(text: str, max_len: int = 1020) -> str:
    """
    Telegram rasm caption limiti (1024 belgi) uchun HTML matnni xavfsiz qisqartiradi
    va ochiq qolgan teglarni avtomatik to'g'ri yopadi.
    """
    cleaned = safe_sanitize_telegram_html(text)
    if len(cleaned) <= max_len:
        return cleaned
    truncated = cleaned[:max_len]
    last_break = max(truncated.rfind("\n"), truncated.rfind(". "), truncated.rfind("! "))
    if last_break > 300:
        truncated = truncated[:last_break + 1]
    return safe_sanitize_telegram_html(truncated)


def extract_post_from_text(raw_text: str, c: dict = None, fallback_theme: str = "expert_guide"):
    """
    Gemini va Groq javobidan xatosiz post matni, rasm prompti, so'rovnoma
    va navbatdagi klifxengerni ajratib oladi.
    JSON ichida Telegram HTML teglari va qo'shtirnoqlar buzilgan bo'lsa ham 100% matnni qutqaradi.
    """
    txt = raw_text.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"```$", "", txt).strip()

    # 1. To'g'ridan-to'g'ri strict=False bilan json.loads
    try:
        data = json.loads(txt, strict=False)
        if isinstance(data, dict) and data.get("post_text"):
            return data
    except Exception:
        pass

    # 2. JSON blokini ajratib olib, trailing vergullarni tozalash
    json_match = re.search(r"(\{.*\})", txt, re.DOTALL)
    if json_match:
        cand = json_match.group(1)
        cand_clean = re.sub(r',\s*([\]}])', r'\1', cand)
        try:
            data = json.loads(cand_clean, strict=False)
            if isinstance(data, dict) and data.get("post_text"):
                return data
        except Exception:
            pass

    # 3. Kuchli Regex orqali maydonlarni ajratish (unescaped quotes/newlines bo'lsa)
    res = {}
    m_post = re.search(r'"post_text"\s*:\s*"(.*?)(?:"\s*,\s*"(?:image_prompt|poll_question|poll_options|next_cliffhanger)"|"\s*\})', txt, re.DOTALL)
    if m_post:
        p_text = m_post.group(1).replace("\\n", "\n").replace('\\"', '"').replace("\\'", "'").replace('\\/', '/').strip()
        res["post_text"] = p_text

    m_img = re.search(r'"image_prompt"\s*:\s*"(.*?)"', txt, re.DOTALL)
    if m_img:
        res["image_prompt"] = m_img.group(1).replace("\\n", " ").replace('\\"', '"').strip()

    m_poll = re.search(r'"poll_question"\s*:\s*"(.*?)"', txt, re.DOTALL)
    if m_poll:
        res["poll_question"] = m_poll.group(1).replace("\\n", " ").replace('\\"', '"').strip()

    m_opts = re.search(r'"poll_options"\s*:\s*\[(.*?)\]', txt, re.DOTALL)
    if m_opts:
        opts_raw = m_opts.group(1)
        opts = [re.sub(r'^["\']|["\']$', '', o.strip()) for o in opts_raw.split(',') if o.strip()]
        res["poll_options"] = [o for o in opts if o]

    m_cliff = re.search(r'"(?:next_cliffhanger|cliffhanger)"\s*:\s*"(.*?)"', txt, re.DOTALL)
    if m_cliff:
        res["next_cliffhanger"] = m_cliff.group(1).replace("\\n", " ").replace('\\"', '"').strip()

    if res.get("post_text") and len(res["post_text"]) > 30:
        adaptive = generate_niche_adaptive_post_and_poll(c, fallback_theme)
        if not res.get("image_prompt"):
            res["image_prompt"] = adaptive.get("image_prompt", "")
        if not res.get("poll_question"):
            res["poll_question"] = adaptive.get("poll_question", "")
        if not res.get("poll_options"):
            res["poll_options"] = adaptive.get("poll_options", [])
        if not res.get("next_cliffhanger"):
            res["next_cliffhanger"] = adaptive.get("next_cliffhanger", "")
        return res

    # 4. Agar oddiy matn ko'rinishida kelgan bo'lsa
    clean = re.sub(r"^\{?[\s\S]*?\"post_text\":\s*\"?", "", txt)
    clean = clean.rstrip("}\" \n\r\t")
    if len(clean) > 30:
        adaptive = generate_niche_adaptive_post_and_poll(c, fallback_theme)
        return {
            "post_text": clean,
            "image_prompt": adaptive.get("image_prompt", ""),
            "poll_question": adaptive.get("poll_question", ""),
            "poll_options": adaptive.get("poll_options", []),
            "next_cliffhanger": adaptive.get("next_cliffhanger", "")
        }

    # 5. Fallback generator
    return generate_niche_adaptive_post_and_poll(c, fallback_theme)


NICHE_ANGLES = {
    "gaming": [
        "PUBG Mobile: Yaqin masofada (Close combat) M416 bilan to'g'ri jiggle va pre-fire qilish sirlari",
        "Giroskop sensini (sezgirlik) o'zingizga moslash: Rekoilni (otdacha) nollash va 6x sprey sirlari",
        "Butkemp va Pochinkiga tushganda omon qolish va 1v4 clutch qilishning 3 qoidasi",
        "90 FPS va iPad Mini: Qurilma fiti va o'yin ravonligining K/D ga ta'siri",
        "Zonaga to'g'ri kirish (Zone rotation) va Airdropni xavfsiz loot qilish taktikasi",
        "DBS va UZI: Yaqin jangda dushmanni 1 soniyada yanchib tashlash usullari",
        "Rank ko'tarishda (Ace Dominator / Conqueror) eng ko'p qilinadigan xatolar"
    ],
    "it": [
        "Dasturlashda charchoq (burnout) bilan kurashish va motivatsiyani tiklash",
        "2026-yilgi yangi avlod AI instrumentlari va dasturchi ish tezligi",
        "Clean Code: Katta IT kompaniyalari kod yozishda nimalarga e'tibor beradi?",
        "Kiberxavfsizlik: Oddiy foydalanuvchilar yo'l qo'yadigan 3 ta xavfli xato",
        "Git va GitHub: Kundalik hayotni o'n barobar qulaylashtiruvchi kam ma'lum buyruqlar",
        "Frontend vs Backend: 2026-yilda qaysi yo'nalishga talab yuqori?",
        "Telegram botlar yaratishda asinxronlik (Asyncio) sirlari va xatoliklar",
        "Kichik loyihalarni qanday qilib barqaror SaaS va passiv daromadga aylantirish mumkin?",
        "Dasturchilar nega tuni bilan ishlaydi? Uyqu va samaradorlik balansi"
    ],
    "crypto": [
        "Risk-menejment: Depozitni 3 oyda eritib yubormaslikning 3 ta temir qoidasi",
        "Stop-loss qo'yishdagi eng katta xato: Bozordan ertaroq chiqib ketish sabablari",
        "FOMO va emotsiyalarni nazorat qilish: Sovuqqon treyder bo'lish san'ati",
        "2026-yilgi kripto trendlar: Real World Assets (RWA) va AI tokenlar tahlili",
        "Faqat bitta indikator bilan savdo qilish mumkinmi? Texnik tahlil haqiqatlari",
        "Scalping vs Spot savdo: Sizning psixologiyangizga qaysi biri mos?",
        "Kriptovalyuta saqlash xavfsizligi: Sovuq hamyonlar va firibgarlik tuzoqlari"
    ],
    "business": [
        "Mijozlarni 0 so'm xarajat bilan jalb qilishning noan'anaviy usullari",
        "Sotuvdagi e'tirozlarni ('Qimmat', 'O'ylab ko'raman') yengish formulasi",
        "Delegatsiyalash: Hamma ishni o'zing qilish tuzog'idan qanday chiqiladi?",
        "Startaplarda MVP (minimal ishlovchi mahsulot) ni 7 kunda ishga tushirish",
        "Mijozlar sadoqati (LTV): Eski mijozlarga qayta-qayta sotish sirlari",
        "Kichik biznesda moliyaviy tahlil: Sof foyda qayerga yo'qoladi?"
    ],
    "education": [
        "Miyadagi dopamin qopqoni: Ijtimoiy tarmoqlar o'rniga kitob o'qishga diqqatni jamlash",
        "Vaqtni boshqarish: Pomodoro va Time-blocking texnikasini 2026-yilda yangicha qo'llash",
        "Tez va esda qolarli o'rganish: Richard Feynman o'rganish usuli",
        "Muvaffaqiyatli insonlarning ertalabki 3 ta odati va kunlik energiya manbai",
        "Qaror qabul qilishda charchoq (Decision fatigue) va uni yengish"
    ],
    "universal": [
        "Odamlarni hayratda qoldiradigan 3 ta aql bovar qilmas fakt",
        "Bugungi kunda har bir inson bilishi shart bo'lgan zamonaviy raqamli odatlar",
        "Muvaffaqiyat ortidagi pinhona haqiqatlar: Tasodifmi yoki intizom?",
        "Samaradorlikni 2 barobarga oshiruvchi bepul vositalar va platformalar"
    ]
}


async def ai_generate_full(c):
    c = dict(c) if c else {}
    cid = c.get("channel_id", 0)
    insights = await get_recent_chat_insights(cid) if cid else []
    
    # 1. Tashqi xotira: Oxirgi 10 ta post sarlavhalarini olish (takrorlanishni 100% oldini olish)
    recent_rows = q("SELECT text FROM recent_posts WHERE channel_id=? ORDER BY created_at DESC LIMIT 10", (cid,)) if cid else []
    recent_titles = []
    for r in recent_rows:
        t = (r["text"] or "").strip()
        first_line = t.split("\n")[0].strip()
        clean_first_line = re.sub(r"<[^>]+>", "", first_line)[:120]
        if clean_first_line and clean_first_line not in recent_titles:
            recent_titles.append(clean_first_line)

    if recent_titles:
        recent_warning = (
            "⚠️ QUYIDAGI MAVZULAR VA SARLAVHALAR YAQINDA CHIQARILGAN, ULARNI VA ULARGA O'XSHASH FIKRLARNI MUTLAQO TAKRORLAMA:\n"
            + "\n".join(f"- {st}" for st in recent_titles)
        )
    else:
        recent_warning = "Avvalgi postlar yo'q. Yangi va qiziqarli mavzudan boshlang."
    
    # 2. Har safar aniq bitta yangi sub-mavzu yoki burchak (angle) tanlash
    n_lower = f"{c.get('niche', '')} {c.get('detailed_prompt', '')}".lower()
    if any(k in n_lower for k in ["pubg", "pabg", "geym", "game", "gaming", "oyun", "o'yin", "kibersport", "esport", "free fire", "standoff", "cs", "dota", "brawl"]):
        niche_key = "gaming"
    elif any(k in n_lower for k in ["treding", "trading", "kripto", "crypto", "forex", "valyuta"]):
        niche_key = "crypto"
    elif any(k in n_lower for k in ["bot", "sayt", "dastur", "python", "web", "kod", "developer", "it"]):
        niche_key = "it"
    elif any(k in n_lower for k in ["biznes", "startap", "savdo", "marketing", "sotuv", "daromad"]):
        niche_key = "business"
    elif any(k in n_lower for k in ["ta'lim", "talim", "kitob", "ingliz", "til", "psixologiya", "oquv"]):
        niche_key = "education"
    else:
        niche_key = "universal"
        
    angles = NICHE_ANGLES.get(niche_key, NICHE_ANGLES["universal"])
    selected_angle = random.choice(angles)

    theme = random.choice(POST_THEMES)
    style = random.choice(IMAGE_STYLES)

    # 3. Serial / Klifxenger boshqaruvi va Foydalanuvchi Tarifi
    owner_id = c.get("owner_id")
    sub = get_user_subscription(owner_id) if owner_id else None
    plan_name = sub.get("plan_name", "start") if sub else "start"
    if owner_id and is_admin(owner_id):
        plan_name = "max"
    plan_info = PLANS.get(plan_name, PLANS["start"])

    # Grounding sozlamasi: PRO / MAX tariflarida, adminlarda va to'g'ridan-to'g'ri chaqiruvlarda rasmiy Google Search Grounding faol
    if not owner_id or (owner_id and is_admin(owner_id)):
        has_grounding = True
        has_youtube = True
        has_cliffhanger = True
    else:
        has_grounding = plan_info.get("has_grounding", False)
        has_youtube = plan_info.get("has_youtube", False)
        has_cliffhanger = plan_info.get("has_cliffhanger", False)

    current_topic = (c.get("current_topic") or "").strip()
    try:
        arc_day = int(c.get("arc_day") or 1)
    except Exception:
        arc_day = 1
    try:
        arc_total_days = int(c.get("arc_total_days") or 3)
    except Exception:
        arc_total_days = 3
    last_cliffhanger = (c.get("last_cliffhanger") or "").strip()

    if not current_topic or arc_day > arc_total_days or arc_day < 1:
        current_topic = selected_angle
        arc_day = 1
        arc_total_days = random.randint(3, 5) if has_cliffhanger else 1
        last_cliffhanger = ""

    if not has_cliffhanger:
        # START tarifi: bitta mustaqil oddiy post (serial va cliffhangersiz)
        arc_instruction = "Oddiy, ixcham, foydali bitta mustaqil post yozing. Klifxenger yoki seriyali bog'lanish kerak emas."
        arc_total_days = 1
        arc_day = 1
    elif arc_day == 1:
        arc_instruction = f"""🎬 KONTENT VORONKASI: 1-KUN (KIRISH VA INTRIGALI TO'XTASH - KLIFXENGER)
• Hozirgi seriya mavzusi: «{current_topic}» (Jami {arc_total_days} kunlik serialning 1-qismi)
• VAZIFA: Mavzuga kuchli qiziqish uyg'otuvchi sarlavha (Hook) bilan kirish. Mavzuning eng dolzarb muammosini yoki 1-sirini ko'rsat.
• KLIFXENGER QOIDASI: Post oxirida ataylab to'xtab, eng muhim yechim/sirni ertangi kunga qoldir ("Bugun sizga asosini ochib berdim, eng muhim qismini esa ertaga ko'rib chiqamiz...").
• "next_cliffhanger" maydoniga: Ertaga ochiladigan sir/va'dani qisqa (1 jumlada) yoz."""
    elif arc_day < arc_total_days:
        arc_instruction = f"""🎬 KONTENT VORONKASI: {arc_day}-KUN (ORALIQ QISM - SIRNI OCHISH VA YANGI KLIFXENGER)
• Hozirgi seriya mavzusi: «{current_topic}» (Jami {arc_total_days} kunlik serialning {arc_day}-qismi)
• KECHAGI VA'DA/KLIFXENGER: «{last_cliffhanger or "Kechagi postdagi muhim sir"}»
• VAZIFA: Dastlabki qatordayoq kechagi berilgan va'dani eslatib, sirni darhol va to'liq ochib ber! Amaliy foydali bilim, keys yoki instrumentlarni taqdim et.
• KLIFXENGER QOIDASI: Post oxirida ertangi {arc_day+1}-qism uchun yangi intrigali to'xtash / savol / va'da qoldir ("Lekin buni amalda qo'llashdagi eng katta xato nima? Buni ertaga ko'rib chiqamiz...").
• "next_cliffhanger" maydoniga: Ertaga ochiladigan yangi sir/va'dani qisqa (1 jumlada) yoz."""
    else:
        arc_instruction = f"""🎬 KONTENT VORONKASI: {arc_day}-KUN (YAKUNIY QISM - SERIYANI XULOSALASH VA YANGI MAVZU TANLOVI)
• Hozirgi seriya mavzusi: «{current_topic}» ({arc_total_days} kunlik serialning yakuniy final qismi)
• KECHAGI VA'DA/KLIFXENGER: «{last_cliffhanger or "Kechagi final siri"}»
• VAZIFA: Kechagi va'dani to'liq och. «{current_topic}» seriyasini chiroyli va kuchli tarzda yakunla, asosiy xulosalarni ber.
• KEYINGI SERIAL TANLOVI: Serial muvaffaqiyatli tugaganini e'lon qil va: «Keyingi serialimiz qaysi mavzuda bo'lsin? Quyidagi so'rovnomada ovoz bering!» deb auditoriyani jalb qil.
• "poll_question" va "poll_options" maydonlariga: Kanal yo'nalishi bo'yicha keyingi yangi serial uchun 3-4 ta yangi qiziqarli mavzuni variant sifatida qo'y!
• "next_cliffhanger" maydoniga: bo'sh matn "" qoldir (chunki seriya tugadi)."""
    
    # 4. Google Grounding va YouTube trendlarini tayyorlash
    google_context = ""
    google_grounding_instruction = ""
    if has_grounding:
        # Gemini uchun rasmiy Google Grounding yo'riqnomasi
        google_grounding_instruction = f"""🌐 RASMIY GOOGLE SEARCH GROUNDING:
Post yozishdan oldin «{c.get('niche', '')}» va «{current_topic}» mavzusi bo'yicha eng so'nggi ma'lumotlarni Google orqali qidirib (Google Search vositasi yordamida), real va yangi faktlar asosida javob qaytar! Eskirgan ma'lumotlardan foydalanma."""
        try:
            google_facts = await fetch_google_trending_facts(f"{c.get('niche', '')} {current_topic} 2026", max_results=3)
            if google_facts:
                google_context = (
                    "🌐 GOOGLE GROUNDING VA ENG SO'NGGI FAKTLAR:\n"
                    + "\n".join(f"- {gf}" for gf in google_facts)
                    + "\nUshbu eng yangi faktlar va ma'lumotlar bilan postni yanada ishonchli va jonli qil!"
                )
        except Exception as e_gf:
            logging.warning(f"Google faktlarini yuklashda xato: {e_gf}")

    yt_context = ""
    # YouTube Data API orqali so'nggi trend faktlar va mavzularni olish (try-catch orqali 100% xavfsiz)
    try:
        niche_query = f"{c.get('niche', '')} {current_topic}".strip() or c.get('title', '')
        yt_videos = await fetch_trending_youtube_videos(query=niche_query, max_results=3)
        if yt_videos:
            yt_video_items = []
            for idx, v in enumerate(yt_videos, 1):
                t = v.get("title", "").strip()
                d = v.get("description", "").strip()
                yt_video_items.append(f"{idx}. Sarlavha: {t}\n   Tavsif: {d}")
            yt_list_str = "\n".join(yt_video_items)
            yt_context = (
                f"YouTube'dagi so'nggi trend faktlar va mavzular:\n{yt_list_str}\n\n"
                "Ushbu ma'lumotlardan foydalanib, post mavzusini yanada qiziqarli va faktlarga boy qilib yoz."
            )
    except Exception as e_yt:
        logging.warning(f"YouTube trendlarini yuklashda xato (xavfsiz o'tkazildi): {e_yt}")
        yt_context = ""

    theme_instructions = {
        "real_cases": "Mavzu: REAL KEYS VA MUVAFFAQIYAT! Shu kanal mavzusida birov qanday qilib natijaga/daromadga chiqqani, qanday loyiha qilgani haqida hikoya. Obunachilarni ham yangilik yaratishga chorla: «Sizlar ham o'zingizcha biror g'oya o'ylab ko'ring, yaratib ko'ring!».",
        "free_tools": "Mavzu: BEPUL INSTRUMENTLAR VA VOSITALAR! Shu soha bo'yicha tekin dasturlar, xostinglar, platformalar va vositalar haqida aniq, foydali tavsiyalar.",
        "youtube_learning": "Mavzu: YOUTUBE VA INTERNET DARSLIKLARI! Obunachilar ko'proq narsa o'rganishi uchun YouTube'dagi qidirish kerak bo'lgan eng foydali darslar va amaliy bilim manbalarini tavsiya qil.",
        "interactive_game": "Mavzu: QIZIQARLI O'YIN VA TOPISHMOQ! Auditoriya bilan shu soha bo'yicha mantiqiy o'yin, qiziqarli topishmoq yoki test o'tkaz.",
        "funny_memes": "Mavzu: KULGILI HAYOTIY MEM VA VOQEA! Shu sohadagi qiziqarli hayotiy holat, mutaxassislar uchraydigan kulgili voqea va kayfiyat ko'taruvchi gaplar.",
        "expert_guide": "Mavzu: AMALIY EKSPERT MASLAHATI! Foydalanuvchining kanal mavzusi bo'yicha eng zarur, qimmatli va amaliy qo'llanma."
    }

    if niche_key == "gaming":
        gaming_instruction = """🕹 GEYMERLAR TILI VA MAZMUN QOIDALARI (PUBG & GAMING):
• TIL: Jonli o'zbek geymerlari tili (ruscha-o'zbekcha aralash tabiiy geymer jargonlari: Giroskop, Sens (sezgirlik), Rush, K/D, Butkemp, Pochinki, Sprayer, Rekoil / Otdaсha, 90 FPS, iPad Mini / Telefon fiti, X-Suit, Airdrop, Clutch 1v4, Zone rotation, M416+6x, DBS, UZI, Pre-fire, Jiggle).
• QAT'IY TAQIQLANGAN SO'ZLAR: "Avtomatik kallak oqi", "gulfamni hisoblanish", "Shift+Tab" (mobil o'yinda kompyuter tugmalari), "hayotdan boryapti", "oltin olti POLTA" kabi sun'iy, kulgili va harfma-harf robot tarjimalar MUTLAQO TAQIQLANADI!
• MAZMUN: Quruq gap emas, AMALIY TAKTIK FOYDA bersin (masalan: "Yaqin masofada M416 bilan qanday sprey qilish kerak?", "Giroskop sensini to'g'irlash siri", "Butkempga tushganda omon qolishning 3 qoidasi", "Close combatda jiggle va pre-fire sirlari").
• SO'ROVNOMA: Geymerlar K/D ko'rsatkichi, qurilmasi (iPad / Telefon) yoki sevimli quroli haqida bo'lsin."""
        tone_line = "1. O'zbek tilida, professional va jonli geymer tilida (slanglar bilan), dinamik, qiziqarli va amaliy foydali tilda yoz (400-800 belgi). Hech qanday robot tarjima ishlatma!"
        img_style_line = '7. image_prompt: Ingliz tilida jangovar va dinamik uslubda yoz (masalan: "cinematic pubg battle royale intense fight scene, dynamic lighting, 8k render, unreal engine 5").'
    else:
        gaming_instruction = ""
        tone_line = "1. O'zbek tilida, nihoyatda samimiy, onadek g'amxo'r, rag'batlantiruvchi va jonli tilda yoz (400-800 belgi)."
        img_style_line = f'7. image_prompt: Ingliz tilida, ushbu uslubda yoz: "{style}".'
    
    prompt = f"""Sen «{c['title']}» kanalining eng iqtidorli, jonkuyar va professional bosh muharririsan.
Kanal yo'nalishi: {c['niche']}
Auditoriya: {c['audience']}

AI UCHUN ASOSIY YO'RIQNOMA (PROMPT):
{c['detailed_prompt'] or "Kanal mavzusiga mos eng foydali va qiziqarli narsalarni yoritish."}

{gaming_instruction}

{arc_instruction}
👉 Format yo'nalishi: {theme_instructions.get(theme)}

{google_grounding_instruction}

{google_context}

{yt_context}

{recent_warning}

Guruhdagi muhokamalar va a'zolar qiziqishlari:
{insights}

MUHIM TALABLAR:
{tone_line}
2. Dastlabki 1-2 qatorda diqqatni tortuvchi kuchli sarlavha (Hook / Pattern Interrupt).
3. Asosiy qism: qisqa, bo'limlarga bo'lingan, ortiqcha suvsiz mazmun.
4. Matn shriftlari faqat Telegram ruxsat bergan HTML teglari bilan: qalin (<b>...</b>), muhim jumlalar (<i>...</i>), parametr va kodlar (<code>...</code>). Hech qachon <br>, <p>, <div> ishlatma!
5. poll_question: Aynan «{c['title']}» kanali mavzusi ({c['niche']}) bo'yicha 1 ta o'ta qiziq so'rovnoma savoli (maksimal 200 belgi).
6. poll_options: Ushbu savolga mos 3-4 ta takrorlanmas variant.
{img_style_line}

Javobni FAQAT toza JSON formatida qaytar:
{{
  "post_text": "HTML formatidagi post matni",
  "image_prompt": "english detailed prompt for image generation",
  "poll_question": "So'rovnoma savoli (agar yakuniy kun bo'lsa, keyingi seriya variantlari)",
  "poll_options": ["Variant 1", "Variant 2", "Variant 3"],
  "next_cliffhanger": "Keyingi qism uchun va'da/klifxenger (oxirgi kun bo'lsa bo'sh '')"
}}
"""
    # 5. Gemini API chaqiruvi: Rasmiy Google Search Grounding vositasi
    # (Alohida yangi API kalit shart emas, mavjud GEMINI_API_KEY bilan ishlaydi)
    client = get_gemini_client()
    # Rasmiy Google Search Grounding vositasi (@google/genai types.Tool(google_search=...))
    gemini_tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())] if has_grounding else None
    models_to_try = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]

    for m in models_to_try:
        try:
            cfg = genai_types.GenerateContentConfig(
                temperature=0.95,
                tools=gemini_tools
            ) if gemini_tools else genai_types.GenerateContentConfig(temperature=0.95)
            try:
                r = await client.aio.models.generate_content(
                    model=m,
                    contents=prompt,
                    config=cfg
                )
            except Exception as e_tools:
                # Agar muayyan modelda tool xatolik bersa, toolsiz oddiy konfiguratsiya bilan qayta urinish
                logging.info(f"Gemini ({m}) tool bilan istisno ({e_tools}), toolsiz qayta urinmoqda...")
                r = await client.aio.models.generate_content(
                    model=m,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(temperature=0.95)
                )

            raw = (r.text or "").strip()
            if raw:
                extracted = extract_post_from_text(raw, c, theme)
                if extracted and extracted.get("post_text"):
                    extracted["current_topic"] = current_topic
                    extracted["arc_day"] = arc_day
                    extracted["arc_total_days"] = arc_total_days
                    return extracted
        except Exception as e:
            logging.warning(f"AI generatsiya urinishi ({m}) muvaffaqiyatsiz: {e}")
            # To'g'ridan-to'g'ri aiohttp REST orqali qayta sinab ko'rish
            try:
                k = get_gemini_api_key()
                if k and not k.startswith("AQ."):
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={k}"
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s_ai:
                        async with s_ai.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}) as r_ai:
                            if r_ai.status == 200:
                                res_json = await r_ai.json()
                                c_text = res_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                                if c_text:
                                    extracted = extract_post_from_text(c_text, c, theme)
                                    if extracted and extracted.get("post_text"):
                                        extracted["current_topic"] = current_topic
                                        extracted["arc_day"] = arc_day
                                        extracted["arc_total_days"] = arc_total_days
                                        return extracted
            except Exception as e_rest_ai:
                logging.warning(f"Gemini REST fallback ({m}) xatosi: {e_rest_ai}")
            await asyncio.sleep(0.3)

    # 5. Agar Gemini xato bersa -> Tezkor Groq AI orqali harakat qilish
    try:
        groq_raw = await ask_groq_ai(prompt)
        if groq_raw:
            extracted = extract_post_from_text(groq_raw, c, theme)
            if extracted and extracted.get("post_text"):
                extracted["current_topic"] = current_topic
                extracted["arc_day"] = arc_day
                extracted["arc_total_days"] = arc_total_days
                return extracted
    except Exception as e_groq:
        logging.warning(f"Groq orqali post generatsiya xatosi: {e_groq}")

    # 6. Agar barcha AIlar xato bersa -> Niche-Adaptive generator
    adaptive = generate_niche_adaptive_post_and_poll(c, theme)
    adaptive["current_topic"] = current_topic
    adaptive["arc_day"] = arc_day
    adaptive["arc_total_days"] = arc_total_days
    return adaptive


# ============================ XILMA-XIL RASMLAR YARATISH VA YUKLASH ============================
NICHE_CURATED_WALLPAPERS = {
    "gaming": [
        "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&w=1280&q=80",  # Neon gaming setup
        "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?auto=format&fit=crop&w=1280&q=80",  # Cyber battle station
        "https://images.unsplash.com/photo-1511512578047-dfb367046420?auto=format&fit=crop&w=1280&q=80",  # Gamepad dark
        "https://images.unsplash.com/photo-1550745165-9bc0b252726f?auto=format&fit=crop&w=1280&q=80",  # Retro neon arcade
        "https://images.unsplash.com/photo-1560253023-3ec5d502959f?auto=format&fit=crop&w=1280&q=80",  # Esports arena
        "https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?auto=format&fit=crop&w=1280&q=80",  # Cyber action glow
        "https://images.unsplash.com/photo-1612287233215-6134b2239d56?auto=format&fit=crop&w=1280&q=80",  # Controller red
        "https://images.unsplash.com/photo-1593305841991-05c297ba4575?auto=format&fit=crop&w=1280&q=80",  # Pro gamer display
        "https://images.unsplash.com/photo-1526509867162-5b0c0d1b4b33?auto=format&fit=crop&w=1280&q=80",  # Dark gaming ambiance
        "https://images.unsplash.com/photo-1578632767115-351597cf2477?auto=format&fit=crop&w=1280&q=80"   # Anime cyberpunk
    ],
    "crypto": [
        "https://images.unsplash.com/photo-1621416894569-0f39ed31d247?auto=format&fit=crop&w=1280&q=80",  # Bitcoin dark
        "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?auto=format&fit=crop&w=1280&q=80",  # Trading chart
        "https://images.unsplash.com/photo-1642543492481-44e81e3914a7?auto=format&fit=crop&w=1280&q=80",  # Crypto coin gold
        "https://images.unsplash.com/photo-1622979135225-d2ba269bc1df?auto=format&fit=crop&w=1280&q=80",  # Ethereum dark
        "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?auto=format&fit=crop&w=1280&q=80",  # Market analysis
        "https://images.unsplash.com/photo-1559526324-4b87b5e36e44?auto=format&fit=crop&w=1280&q=80"   # Finance growth
    ],
    "it": [
        "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1280&q=80",  # Hardware chip
        "https://images.unsplash.com/photo-1498050108023-c5249f4df085?auto=format&fit=crop&w=1280&q=80",  # Code laptop
        "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?auto=format&fit=crop&w=1280&q=80",  # Matrix binary
        "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?auto=format&fit=crop&w=1280&q=80",  # Cybersecurity
        "https://images.unsplash.com/photo-1504384308090-c894fdcc538d?auto=format&fit=crop&w=1280&q=80",  # Clean workspace
        "https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1280&q=80",  # Data dashboard
        "https://images.unsplash.com/photo-1461749280684-dccba630e2f6?auto=format&fit=crop&w=1280&q=80"   # Code screen
    ],
    "business": [
        "https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?auto=format&fit=crop&w=1280&q=80",  # Modern skyscrapers
        "https://images.unsplash.com/photo-1454165804606-c3d57bc86b40?auto=format&fit=crop&w=1280&q=80",  # Business strategy
        "https://images.unsplash.com/photo-1507679799987-c73779587ccf?auto=format&fit=crop&w=1280&q=80",  # Executive suit
        "https://images.unsplash.com/photo-1519389950473-47ba0277781c?auto=format&fit=crop&w=1280&q=80",  # Startup team
        "https://images.unsplash.com/photo-1553729459-efe14ef6055d?auto=format&fit=crop&w=1280&q=80"   # Finance banking
    ],
    "universal": [
        "https://images.unsplash.com/photo-1497633762265-9d179a990aa6?auto=format&fit=crop&w=1280&q=80",  # Library books
        "https://images.unsplash.com/photo-1434030216411-0b793f4b4173?auto=format&fit=crop&w=1280&q=80",  # Study notes
        "https://images.unsplash.com/photo-1506744038136-46273834b3fb?auto=format&fit=crop&w=1280&q=80",  # Landscape nature
        "https://images.unsplash.com/photo-1519681393784-d120267933ba?auto=format&fit=crop&w=1280&q=80"   # Starry mountain
    ]
}


async def download_post_image(prompt: str, niche: str, channel_id: int = 0) -> bytes | None:
    # 0. Kanalning oxirgi 30 ta rasmlari keshini olish (takrorlanishni 100% oldini olish)
    used_keys = set()
    if channel_id:
        try:
            rows = q("SELECT image_key FROM recent_images WHERE channel_id=? ORDER BY created_at DESC LIMIT 30", (channel_id,))
            used_keys = {r["image_key"] for r in rows if r["image_key"]}
        except Exception as e_rk:
            logging.warning(f"recent_images o'qish xatosi: {e_rk}")

    # Niche toifasini aniqlash
    n_lower = f"{niche} {prompt}".lower()
    if any(k in n_lower for k in ["pubg", "pabg", "geym", "game", "gaming", "oyun", "o'yin", "kibersport", "esport", "free fire", "standoff", "cs", "dota"]):
        niche_cat = "gaming"
    elif any(k in n_lower for k in ["treding", "trading", "kripto", "crypto", "forex", "valyuta"]):
        niche_cat = "crypto"
    elif any(k in n_lower for k in ["bot", "sayt", "dastur", "python", "web", "kod", "developer", "it"]):
        niche_cat = "it"
    elif any(k in n_lower for k in ["biznes", "startap", "savdo", "marketing", "sotuv", "daromad"]):
        niche_cat = "business"
    else:
        niche_cat = "universal"

    # 1. Flux AI orqali har xil seed va prompt bilan generatsiya qilish
    p_clean = (prompt or niche).strip()
    if niche_cat == "gaming":
        flux_prompt = f"{p_clean}, cinematic intense battle royale action scene, pochinki military background, dramatic lighting, 8k render, unreal engine 5"
    else:
        flux_prompt = f"{p_clean}, high quality professional photography, 8k, cinematic lighting"

    # 2 martagacha turli seed bilan Flux AI urinishi
    for _ in range(2):
        try:
            seed = random.randint(100000, 999999999)
            encoded = urllib.parse.quote(flux_prompt[:320])
            url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&model=flux&seed={seed}&nologo=true"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 8000:
                            img_key = hashlib.md5(data[:4096]).hexdigest()
                            if img_key not in used_keys:
                                if channel_id:
                                    try:
                                        q("INSERT INTO recent_images (channel_id, image_key, created_at) VALUES (?, ?, ?)",
                                          (channel_id, img_key, now_utc().isoformat()), commit=True)
                                        q("DELETE FROM recent_images WHERE channel_id=? AND rowid NOT IN (SELECT rowid FROM recent_images WHERE channel_id=? ORDER BY created_at DESC LIMIT 30)",
                                          (channel_id, channel_id), commit=True)
                                    except Exception as e_si:
                                        logging.warning(f"recent_images yozish xatosi: {e_si}")
                                return data
        except Exception as e_flux:
            logging.warning(f"Flux AI rasm olishda urinish: {e_flux}")
            break

    # 2. Agar Flux AI ishlamasa yoki takrorlangan bo'lsa -> Yo'nalishga mos Curated Wallpapers & Web Photo
    pool = list(NICHE_CURATED_WALLPAPERS.get(niche_cat, NICHE_CURATED_WALLPAPERS["universal"]))
    # Dinamik yangi rasm manbalarini ham qo'shish
    pool.append(f"https://picsum.photos/1280/720?random={random.randint(1, 999999)}")
    random.shuffle(pool)

    for u in pool:
        u_key = hashlib.md5(u.encode("utf-8")).hexdigest()
        if u_key in used_keys:
            continue
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(u, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 8000:
                            if channel_id:
                                try:
                                    q("INSERT INTO recent_images (channel_id, image_key, created_at) VALUES (?, ?, ?)",
                                      (channel_id, u_key, now_utc().isoformat()), commit=True)
                                    q("DELETE FROM recent_images WHERE channel_id=? AND rowid NOT IN (SELECT rowid FROM recent_images WHERE channel_id=? ORDER BY created_at DESC LIMIT 30)",
                                      (channel_id, channel_id), commit=True)
                                except Exception as e_si2:
                                    logging.warning(f"recent_images yozish xatosi: {e_si2}")
                            return data
        except Exception:
            continue

    return None


# ============================ IDEMPOTENCY VA KANAL LOCKLARI ============================
_channel_post_locks = set()
_locks_mutex = asyncio.Lock()

async def acquire_channel_lock(cid: int) -> bool:
    async with _locks_mutex:
        if cid in _channel_post_locks:
            return False
        _channel_post_locks.add(cid)
        return True

async def release_channel_lock(cid: int):
    async with _locks_mutex:
        _channel_post_locks.discard(cid)


# ============================ KANALGA POST YUBORISH (100% KAFOLATLI) ============================
async def send_ai_post(c, with_image=True):
    """
    Postni 100% kanalga yetkazadi.
    - Idempotency / Concurrency lock
    - Serial-Klifxenger zanjirini yangilaydi
    - Telegram API HTML entity va uzunlik xatolarini xavfsiz bartaraf qiladi
    """
    c = dict(c) if c else {}
    cid = c.get("channel_id")
    if not cid:
        return False, "❌ Kanal ma'lumotlari topilmadi!"

    if not await acquire_channel_lock(cid):
        return False, "⚠️ Ushbu kanalga ayni paytda post tayyorlanmoqda. Iltimos, biroz kuting."

    try:
        try:
            data = await ai_generate_full(c)
        except Exception as e_gen:
            logging.warning(f"ai_generate_full xatosi: {e_gen}, niche-adaptive generatorga o'tiladi")
            data = generate_niche_adaptive_post_and_poll(c)

        raw_post_text = (data.get("post_text") or "").strip()
        if not raw_post_text:
            adaptive = generate_niche_adaptive_post_and_poll(c)
            raw_post_text = adaptive["post_text"]

        owner_id = c.get("owner_id")
        sub = get_user_subscription(owner_id) if owner_id else None
        p_info = sub["plan_info"] if sub else PLANS["start"]
        if p_info.get("watermark", False):
            try:
                bot_me = await bot.me()
                bot_tag = f"@{bot_me.username}" if (bot_me and bot_me.username) else "@kanalareditbot"
            except Exception:
                bot_tag = "@kanalareditbot"
            watermark_line = f"\n\n🤖 {bot_tag} orqali yaratildi"
            if watermark_line not in raw_post_text:
                raw_post_text = raw_post_text.rstrip() + watermark_line

        safe_html = safe_sanitize_telegram_html(raw_post_text)
        img_prompt = data.get("image_prompt", "")
        sent = False
        error_detail = ""

        # 1. Rasm bilan yuborish
        if with_image:
            try:
                img_bytes = await download_post_image(img_prompt or c.get("niche", ""), c.get("niche", ""), channel_id=cid)
                if img_bytes:
                    photo_file = types.BufferedInputFile(img_bytes, filename="post_image.jpg")
                    if len(safe_html) <= 1020:
                        try:
                            await bot.send_photo(cid, photo=photo_file, caption=safe_html, parse_mode=ParseMode.HTML)
                            sent = True
                        except Exception as e_ph:
                            logging.warning(f"HTML rasm caption xatosi: {e_ph}, toza matnga o'tiladi")
                            clean_caption = re.sub(r"<[^>]+>", "", raw_post_text)[:1020]
                            photo_retry = types.BufferedInputFile(img_bytes, filename="post_image.jpg")
                            try:
                                await bot.send_photo(cid, photo=photo_retry, caption=clean_caption, parse_mode=None)
                                sent = True
                            except Exception as e_ph2:
                                logging.warning(f"Rasm qayta yuborishda xato: {e_ph2}")
                    else:
                        caption_hook = prepare_safe_caption(safe_html, max_len=1020)
                        try:
                            await bot.send_photo(cid, photo=photo_file, caption=caption_hook, parse_mode=ParseMode.HTML)
                            sent = True
                        except Exception:
                            try:
                                await bot.send_photo(cid, photo=photo_file)
                                await bot.send_message(cid, safe_html[:4090], parse_mode=ParseMode.HTML)
                                sent = True
                            except Exception as e_ph3:
                                logging.warning(f"Rasm va xabar ajratib yuborishda xato: {e_ph3}")
            except Exception as e_img:
                logging.warning(f"Rasm bilan yuborishda xato: {e_img}, matn ko'rinishida yuboriladi...")

        # 2. Agar rasm bilan ketmagan bo'lsa, xavfsiz matn sifatida yuborish
        if not sent:
            try:
                await bot.send_message(cid, safe_html[:4090], parse_mode=ParseMode.HTML)
                sent = True
            except Exception as e1:
                try:
                    clean_text = re.sub(r"<[^>]+>", "", raw_post_text)[:4090]
                    await bot.send_message(cid, clean_text, parse_mode=None)
                    sent = True
                except Exception as e2:
                    error_detail = str(e2)
                    logging.error(f"Post mutlaqo yuborilmadi ({cid}): {e2}")

        if sent:
            if owner_id:
                increment_daily_post_count(owner_id)
            # Serial-Klifxenger zanjirini yangilash
            cur_topic = data.get("current_topic") or c.get("current_topic") or "Dolzarb mavzular"
            cur_day = int(data.get("arc_day") or c.get("arc_day") or 1)
            tot_days = int(data.get("arc_total_days") or c.get("arc_total_days") or 3)
            next_cliff = (data.get("next_cliffhanger") or "").strip()

            if cur_day < tot_days:
                next_day = cur_day + 1
                q(
                    "UPDATE channels SET current_topic=?, arc_day=?, arc_total_days=?, last_cliffhanger=?, posts_sent = posts_sent + 1 WHERE channel_id=?",
                    (cur_topic, next_day, tot_days, next_cliff, cid),
                    commit=True
                )
            else:
                next_total = random.randint(3, 5)
                q(
                    "UPDATE channels SET current_topic='', arc_day=1, arc_total_days=?, last_cliffhanger='', posts_sent = posts_sent + 1 WHERE channel_id=?",
                    (next_total, cid),
                    commit=True
                )
                if data.get("poll_question") and data.get("poll_options"):
                    try:
                        await send_ai_poll(c, poll_data=data)
                    except Exception as e_poll:
                        logging.warning(f"Final poll yuborishda xato: {e_poll}")

            q("INSERT INTO recent_posts VALUES (?,?,?)", (cid, safe_html, now_utc().isoformat()), commit=True)
            auto_backup_channels()
            preview = safe_html[:350] + ("..." if len(safe_html) > 350 else "")
            return True, (
                f"🎉 <b>Post «{html.escape(c.get('title', 'Kanal'))}» kanaliga muvaffaqiyatli yuborildi!</b>\n\n"
                f"📌 <b>Kanal yo'nalishi:</b> {html.escape(c.get('niche', ''))}\n"
                f"🎬 <b>Serial:</b> {cur_day}/{tot_days}-kun («{html.escape(cur_topic[:35])}»)\n\n"
                f"📝 <b>Post matni:</b>\n{preview}"
            )
        else:
            return False, f"❌ <b>Xatolik yuz berdi:</b> <code>{error_detail or 'Telegram kanalda bot huquqini tekshiring'}</code>"
    finally:
        await release_channel_lock(cid)


# ============================ KAFOLATLANGAN INTERAKTIV SO'ROVNOMA (POLL) ============================
async def send_ai_poll(c, poll_data=None):
    c = dict(c) if c else {}
    cid = c.get("channel_id")
    if not cid:
        return False, "❌ Kanal ma'lumotlari topilmadi!"

    if not await acquire_channel_lock(cid):
        return False, "⚠️ Ushbu kanalga ayni paytda xabar yuborilmoqda. Iltimos, biroz kuting."

    try:
        question = None
        options = []
        
        data = poll_data
        if not data:
            try:
                data = await ai_generate_full(c)
            except Exception as e_gen:
                logging.warning(f"Poll uchun ai_generate_full xatosi: {e_gen}")
                data = None

        if data and data.get("poll_question") and data.get("poll_options"):
            raw_q = str(data["poll_question"]).strip()
            raw_opts = [str(opt).strip() for opt in data["poll_options"] if str(opt).strip()]
            unique_opts = []
            for o in raw_opts:
                if o not in unique_opts:
                    unique_opts.append(o[:95])
            if len(unique_opts) >= 2:
                question = raw_q[:250]
                options = unique_opts[:10]
                
        if not question or len(options) < 2:
            adaptive = generate_niche_adaptive_post_and_poll(c)
            question = adaptive["poll_question"]
            options = adaptive["poll_options"]
            
        try:
            await bot.send_poll(
                chat_id=cid,
                question=question,
                options=options,
                is_anonymous=False,
            )
        except Exception:
            await bot.send_poll(
                chat_id=cid,
                question=question,
                options=options,
                is_anonymous=True,
            )
        q("UPDATE channels SET polls_sent = polls_sent + 1 WHERE channel_id=?", (cid,), commit=True)
        owner_id = c.get("owner_id")
        if owner_id:
            increment_daily_post_count(owner_id)
        auto_backup_channels()
        
        opts_formatted = "\n".join(f"{i+1}. {opt}" for i, opt in enumerate(options))
        success_msg = (
            f"📊 <b>Interaktiv so'rovnoma «{html.escape(c.get('title', 'Kanal'))}» kanaliga muvaffaqiyatli yuborildi!</b>\n\n"
            f"📌 <b>Kanal yo'nalishi:</b> {html.escape(c.get('niche', ''))}\n\n"
            f"❓ <b>Savol:</b> {html.escape(question)}\n\n"
            f"🔘 <b>Variantlar:</b>\n{html.escape(opts_formatted)}"
        )
        return True, success_msg
    except Exception as e:
        logging.error(f"Kanalga poll yuborishda xatolik: {e}")
        return False, f"❌ <b>So'rovnoma yuborilmadi:</b> <code>{e}</code>"
    finally:
        await release_channel_lock(cid)


# ============================ TEZKOR TUGMALAR HANDLERLARI ============================
@dp.callback_query(F.data.startswith("imgpost:"))
async def on_click_imgpost(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = own_channel(cid, cb.from_user.id)
    if not c:
        return await cb.answer("Kanal topilmadi", show_alert=True)
    allowed, limit_msg = check_user_limits(cb.from_user.id, "imgpost")
    if not allowed:
        await cb.message.answer(limit_msg, reply_markup=plans_keyboard())
        return await cb.answer("⚠️ Kunlik post limiti tugadi!", show_alert=True)
    
    await cb.answer("⏳ Professional rasm va post tayyorlanmoqda (bir necha soniya)...")
    ok, msg = await send_ai_post(c, with_image=True)
    await cb.message.answer(msg)


@dp.callback_query(F.data.startswith("post:"))
async def on_click_post(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = own_channel(cid, cb.from_user.id)
    if not c:
        return await cb.answer("Kanal topilmadi", show_alert=True)
    allowed, limit_msg = check_user_limits(cb.from_user.id, "post")
    if not allowed:
        await cb.message.answer(limit_msg, reply_markup=plans_keyboard())
        return await cb.answer("⚠️ Kunlik post limiti tugadi!", show_alert=True)
    
    await cb.answer("⏳ Post tayyorlanmoqda...")
    ok, msg = await send_ai_post(c, with_image=False)
    await cb.message.answer(msg)


@dp.callback_query(F.data.startswith("poll:"))
async def on_click_poll(cb: types.CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = own_channel(cid, cb.from_user.id)
    if not c:
        return await cb.answer("Kanal topilmadi", show_alert=True)
    allowed, limit_msg = check_user_limits(cb.from_user.id, "poll")
    if not allowed:
        await cb.message.answer(limit_msg, reply_markup=plans_keyboard())
        return await cb.answer("⚠️ Limit yoki funksiya cheklangan!", show_alert=True)
    
    await cb.answer("⏳ So'rovnoma tayyorlanmoqda...")
    ok, msg = await send_ai_poll(c)
    await cb.message.answer(msg)


# ============================ AVTO JADVAL VA SCHEDULER ============================
async def delayed_action(c, action_type):
    c = dict(c) if c else {}
    try:
        await asyncio.sleep(random.randint(10, 300))
        if action_type == "poll":
            await send_ai_poll(c)
        else:
            await send_ai_post(c, with_image=True)
    except Exception as e:
        logging.error(f"Delayed action xatosi ({c.get('channel_id')}): {e}")


async def scheduled_tasks():
    current_hour = datetime.now(TZ).hour
    for c in q("SELECT * FROM channels WHERE active=1"):
        owner_id = c["owner_id"]
        sub = get_user_subscription(owner_id)
        plan_info = sub["plan_info"]
        
        # Kunlik post limiti tekshiruvi
        if sub["daily_posts_count"] >= plan_info.get("daily_post_limit", 1):
            continue
            
        allowed_hours = []
        if c["custom_hours"]:
            try:
                allowed_hours = [int(x.strip()) for x in c["custom_hours"].split(",") if x.strip()]
            except Exception:
                pass
        if not allowed_hours:
            allowed_hours = plan_info["hours"]
            
        if current_hour in allowed_hours:
            cid = c["channel_id"]
            # Idempotency tekshiruvi: ushbu kanalga oxirgi 45 daqiqa ichida post chiqarilgan bo'lsa qayta yubormaslik
            recent_last = q("SELECT created_at FROM recent_posts WHERE channel_id=? ORDER BY created_at DESC LIMIT 1", (cid,), one=True)
            if recent_last and recent_last["created_at"]:
                try:
                    last_dt = datetime.fromisoformat(recent_last["created_at"])
                    if (now_utc() - last_dt).total_seconds() < 45 * 60:
                        logging.info(f"Kanal {cid} uchun ushbu soatda post chiqarilgan (idempotency tekshiruvi).")
                        continue
                except Exception:
                    pass

            can_poll = plan_info.get("polls", False)
            action = "poll" if (can_poll and random.random() < 0.25) else "post"
            asyncio.create_task(delayed_action(dict(c), action))


async def expiry_reminders():
    now = now_utc()
    for u in q("SELECT user_id, plan, expires_at FROM users WHERE plan IS NOT NULL AND expires_at IS NOT NULL"):
        left = datetime.fromisoformat(u["expires_at"]) - now
        if timedelta(0) < left <= timedelta(days=1) or timedelta(days=2) < left <= timedelta(days=3):
            try:
                await bot.send_message(
                    u["user_id"],
                    f"⏳ <b>Eslatma:</b> Sizning <b>{PLANS[u['plan']]['name']}</b> tarifingiz <b>{fmt(datetime.fromisoformat(u['expires_at']))}</b> da tugaydi.\n"
                    "Uzluksiz postlar va xizmatlar uchun «💎 Tariflar & To'lov» bo'limi orqali uzaytirishingiz mumkin."
                )
            except Exception:
                pass


# ============================ XUSHYOR GURUH MODERATSIYASI VA SAMIMIY JAVOBLAR ============================
async def delete_later(msg: types.Message, sec=20):
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except Exception:
        pass


async def handle_violation(m: types.Message, chat_id: int, reason_text: str, is_vip: bool = False):
    sender_chat = m.sender_chat
    user = m.from_user
    
    # 1. Xabarni darhol o'chirish
    try:
        await m.delete()
    except Exception as e_del:
        logging.warning(f"Xabarni o'chirish imkoni bo'lmadi ({chat_id}): {e_del}")

    # Agar qoidabuzarlik kanal nomidan qilingan bo'lsa
    if sender_chat and sender_chat.id != chat_id:
        try:
            await bot.ban_chat_sender_chat(chat_id, sender_chat.id)
            punish_msg = await bot.send_message(
                chat_id,
                f"🛡 <b>ANTISPAM:</b>\n"
                f"⛔ <b>{html.escape(sender_chat.title or 'Kanal')}</b> nomidan spam yuborilgani sababli ushbu kanal guruhda <b>bloklandi (BAN)!</b>"
            )
            asyncio.create_task(delete_later(punish_msg, 25))
            return
        except Exception as e_ch_ban:
            logging.error(f"Kanalni ban qilishda xato ({chat_id}): {e_ch_ban}")
            return

    if not user:
        return

    uid = user.id
    mention = user.mention_html()

    # VIP Moderatsiya: Spam, stavka, scam havolalar va firibgarlarni darhol guruhdan ban qilish
    if is_vip:
        q("UPDATE channels SET bans = bans + 1 WHERE group_id=?", (chat_id,), commit=True)
        try:
            await bot.ban_chat_member(chat_id, uid)
            q("DELETE FROM violations WHERE chat_id=? AND user_id=?", (chat_id, uid), commit=True)
            punish_msg = await bot.send_message(
                chat_id,
                f"🛡 <b>VIP MODERATSIYA (MAX BIZNES):</b>\n"
                f"⛔ {mention} firibgarlik, stavka yoki spam tarqatgani sababli <b>darhol guruhdan chiqarildi (BAN)!</b>"
            )
            asyncio.create_task(delete_later(punish_msg, 25))
            return
        except Exception as e:
            logging.error(f"VIP ban xatosi ({chat_id}): {e}")
        
    r = q("SELECT warnings, cycle FROM violations WHERE chat_id=? AND user_id=?", (chat_id, uid), one=True)
    warns, cycle = (r["warnings"], r["cycle"]) if r else (0, 0)
    warns += 1

    # Guruh ogohlantirishlar sonini yangilash
    q("UPDATE channels SET warnings = warnings + 1 WHERE group_id=?", (chat_id,), commit=True)

    if warns < 3:
        q("INSERT OR REPLACE INTO violations VALUES (?,?,?,?)", (chat_id, uid, warns, cycle), commit=True)
        try:
            warn_msg = await bot.send_message(
                chat_id,
                f"⚠️ {mention}, <b>{reason_text}!</b>\n"
                f"📌 Sizga ogohlantirish berildi: <code>{warns}/3</code>\n"
                f"<i>(Ushbu ogohlantirish 20 soniyada o'chiriladi)</i>"
            )
            asyncio.create_task(delete_later(warn_msg, 20))
        except Exception as e_warn:
            logging.warning(f"Ogohlantirish yuborishda xato ({chat_id}): {e_warn}")
        return

    # Guruh cheklovlari va banlar sonini yangilash
    q("UPDATE channels SET bans = bans + 1 WHERE group_id=?", (chat_id,), commit=True)
    restrict_perms = types.ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )
    try:
        if cycle == 0:
            q("INSERT OR REPLACE INTO violations VALUES (?,?,?,?)", (chat_id, uid, 0, 1), commit=True)
            until_ts = int((now_utc() + timedelta(days=30)).timestamp())
            await bot.restrict_chat_member(
                chat_id, uid,
                permissions=restrict_perms,
                until_date=until_ts,
                use_independent_chat_permissions=True
            )
            punish_msg = await bot.send_message(chat_id, f"⛔ {mention} <b>3 marta ogohlantirildi va 30 kunga yozishdan cheklandi!</b>")
        else:
            await bot.ban_chat_member(chat_id, uid)
            q("DELETE FROM violations WHERE chat_id=? AND user_id=?", (chat_id, uid), commit=True)
            punish_msg = await bot.send_message(chat_id, f"🚫 {mention} <b>qoidani surunkali buzgani sababli guruhdan chiqarib yuborildi!</b>")
        asyncio.create_task(delete_later(punish_msg, 20))
    except Exception as e:
        logging.error(f"Foydalanuvchini cheklashda xato ({chat_id}): {e}")


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_handler(m: types.Message):
    # Matn yoki rasm/video tagidagi caption ni olish
    text_content = (m.text or m.caption or "").strip()
    if not text_content:
        return
        
    chat_id = m.chat.id
    sender_chat = m.sender_chat
    user = m.from_user
    
    # Botning o'zi yuborgan bo'lsa yoki boshqa bot bo'lsa (lekin ChannelBot emas)
    if user and user.is_bot and not sender_chat:
        return

    # Guruhdagi xabarlar statistikasini yangilash
    q("UPDATE channels SET group_messages = group_messages + 1 WHERE group_id=?", (chat_id,), commit=True)
    
    c = q("SELECT * FROM channels WHERE group_id=? AND active=1", (chat_id,), one=True)
    is_vip = False
    if c and c.get("owner_id"):
        sub = get_user_subscription(c["owner_id"])
        is_vip = (sub["plan_info"].get("moderation") == "vip")

    # Adminlikni tekshirish
    is_chat_admin = False
    if sender_chat:
        if sender_chat.id == chat_id:
            is_chat_admin = True
    elif user:
        is_chat_admin = await is_admin_of(chat_id, user.id)

    if not is_chat_admin:
        # Matnni normalizatsiya qilish (kirill, bo'shliqlar, leetspeak)
        norm_text = normalize_text_for_moderation(text_content)
        full_check_text = f"{text_content} {norm_text}"

        # 1. So'kinish yoki haqorat
        if PROFANITY_RE.search(full_check_text):
            return await handle_violation(m, chat_id, "guruhda so'kinish, haqorat va kamsitish taqiqlanadi", is_vip=False)
            
        # 2. Firibgarlik (masheniklik), stavka, spam yoki taqiqlangan havolalar
        if SCAM_RE.search(full_check_text):
            return await handle_violation(m, chat_id, "guruhda firibgarlik, stavka, spam va noqonuniy reklamalar qat'iyan man etiladi", is_vip=is_vip)
    
    c = q("SELECT * FROM channels WHERE group_id=? AND active=1", (chat_id,), one=True)
    channel_id = c["channel_id"] if c else 0
    
    if user and len(text_content) > 5 and not text_content.startswith("/"):
        q("INSERT INTO chat_insights (channel_id, user_id, user_name, text, created_at) VALUES (?,?,?,?,?)",
          (channel_id, user.id, user.full_name, text_content[:300], now_utc().isoformat()), commit=True)
    
    me = await bot.me()
    replied = m.reply_to_message and m.reply_to_message.from_user and m.reply_to_message.from_user.id == me.id
    mentioned = f"@{me.username.lower()}" in text_content.lower()
    
    if replied or mentioned:
        clean_query = re.sub(f"@{me.username}", "", text_content, flags=re.I).strip()
        system_prompt = (
            f"Sen ushbu guruhning oliy toifali, juda xushmuomala ekspert yordamchisisan.\n"
            f"Kanal yo'nalishi: {c['niche'] if c else 'Dasturlash, texnologiya va internet loyihalari'}.\n\n"
            "Qoidalar:\n"
            "- O'zbek tilida, do'stona, qisqa (2-4 gap) va aniq foydali javob ber.\n"
            "- Shriftlarni chiroyli formatla: qalin <b>...</b>, kodlar <code>...</code>.\n"
            "- O'zingni AI deb atama, mutaxassis sifatida gapir."
        )
        ai_prompt = f"Foydalanuvchi savoli: {clean_query}"
        ans = ""
        
        # 1. Eng tezkor Groq AI orqali javob olish (0.3 soniyada)
        try:
            ans = await ask_groq_ai(ai_prompt, system_prompt=system_prompt)
        except Exception as e_groq:
            logging.warning(f"Groq javob xatosi: {e_groq}")
            
        # 2. Agar Groq javob bermasa -> Gemini AI zaxirasi
        if not ans:
            try:
                client = get_gemini_client()
                for m_try in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]:
                    try:
                        r = await client.aio.models.generate_content(model=m_try, contents=f"{system_prompt}\n\n{ai_prompt}")
                        ans = (r.text or "").strip()
                        if ans:
                            break
                    except Exception:
                        continue
            except Exception as e:
                logging.warning(f"Gemini fallback xatosi: {e}")
                
        if ans:
            try:
                await m.reply(ans)
            except Exception:
                clean_ans = re.sub(r"<[^>]+>", "", ans)
                await m.reply(clean_ans, parse_mode=None)
        else:
            await m.reply("Savolingiz uchun katta rahmat! 🤝 Tez orada bu bo'yicha batafsil ma'lumot beramiz ✨")
    else:
        u_name = html.escape(user.first_name if user else "do'stim")
        gratitude_responses = [
            f"Fikringiz uchun katta rahmat, <b>{u_name}</b>! 🤝 Ushbu mavzuni kanalimizda ham tez orada yoritamiz ✨",
            f"Ajoyib fikr! 💡 Buni albatta inobatga olamiz, rahmat!",
            f"Savol va qiziqishingiz uchun rahmat! 🚀 Kanalimizni kuzatib boring, yangi foydali ma'lumotlar tayyorlanmoqda.",
            f"Fikr almashganingiz uchun minnatdormiz! 🤝 Bilim ulashishda davom etamiz."
        ]
        if any(w in text_content.lower() for w in ["qanday", "qaysi", "nima", "kerak", "maslahat", "yordam", "fikr", "loyiha", "bot", "sayt"]):
            await m.reply(random.choice(gratitude_responses))


# ============================ KANAL QO'SHILISH HODISALARI ============================
@dp.chat_join_request()
async def on_join_request(r: types.ChatJoinRequest):
    c = q("SELECT 1 FROM channels WHERE channel_id=?", (r.chat.id,), one=True)
    if not c:
        return
    q("UPDATE channels SET join_requests = join_requests + 1 WHERE channel_id=?", (r.chat.id,), commit=True)
    try:
        await r.approve()
    except Exception as e:
        logging.warning(f"So'rovni tasdiqlab bo'lmadi: {e}")


@dp.chat_member()
async def on_chat_member(e: types.ChatMemberUpdated):
    if not q("SELECT 1 FROM channels WHERE channel_id=?", (e.chat.id,), one=True):
        return
    old, new = e.old_chat_member.status, e.new_chat_member.status
    if old in ("left", "kicked") and new in ("member", "restricted"):
        q("UPDATE channels SET joined = joined + 1 WHERE channel_id=?", (e.chat.id,), commit=True)
    elif old in ("member", "restricted") and new in ("left", "kicked"):
        q("UPDATE channels SET left_count = left_count + 1 WHERE channel_id=?", (e.chat.id,), commit=True)


# ============================ ADMIN PANEL ============================
AdminMsg = F.from_user.id.in_(ADMIN_IDS)


def get_admin_panel_text() -> str:
    # 1. Jami foydalanuvchilar
    u_row = q("SELECT COUNT(*) as c FROM users", one=True)
    users_count = u_row["c"] if u_row else 0

    # 2. Ulangan kanallar
    ch_row = q("SELECT COUNT(*) as c FROM channels WHERE active=1", one=True)
    channels_count = ch_row["c"] if ch_row else 0

    # 3. Ulangan guruhlar
    gr_row = q("SELECT COUNT(DISTINCT group_id) as c FROM channels WHERE group_id IS NOT NULL AND group_id != 0", one=True)
    groups_count = gr_row["c"] if gr_row else 0

    # 4. Bugun chiqarilgan jami postlar
    today_s = datetime.now(TZ).strftime("%Y-%m-%d")
    rp_row = q("SELECT COUNT(*) as c FROM recent_posts WHERE created_at LIKE ?", (f"{today_s}%",), one=True)
    sub_posts_row = q("SELECT SUM(daily_posts_count) as c FROM subscriptions WHERE last_post_date = ?", (today_s,), one=True)
    today_posts_count = max(rp_row["c"] if rp_row else 0, sub_posts_row["c"] if (sub_posts_row and sub_posts_row["c"]) else 0)

    # 5. Tariflar bo'yicha obunachilar
    start_row = q("SELECT COUNT(*) as c FROM subscriptions WHERE plan_name='start'", one=True)
    start_users = start_row["c"] if start_row else 0
    pro_row = q("SELECT COUNT(*) as c FROM subscriptions WHERE plan_name='pro'", one=True)
    pro_users = pro_row["c"] if pro_row else 0
    max_row = q("SELECT COUNT(*) as c FROM subscriptions WHERE plan_name='max'", one=True)
    max_users = max_row["c"] if max_row else 0

    if start_users == 0 and pro_users == 0 and max_users == 0:
        st_r = q("SELECT COUNT(*) as c FROM users WHERE plan IS NULL OR plan='' OR plan='start' OR plan='basic'", one=True)
        start_users = st_r["c"] if st_r else 0
        pr_r = q("SELECT COUNT(*) as c FROM users WHERE plan IN ('pro', 'standard')", one=True)
        pro_users = pr_r["c"] if pr_r else 0
        mx_r = q("SELECT COUNT(*) as c FROM users WHERE plan='max'", one=True)
        max_users = mx_r["c"] if mx_r else 0

    # 6. Botning uzluksiz ishlash vaqti (Uptime)
    uptime = get_bot_uptime()

    text = (
        f"📊 <b>BOTNING TO'LIQ STATISTIKASI:</b>\n"
        f"👥 <b>Jami foydalanuvchilar:</b> {users_count} ta\n"
        f"📢 <b>Ulangan kanallar:</b> {channels_count} ta\n"
        f"💬 <b>Ulangan guruhlar:</b> {groups_count} ta\n"
        f"📝 <b>Bugun chiqarilgan jami postlar:</b> {today_posts_count} ta\n"
        f"💎 <b>Tariflar bo'yicha obunachilar:</b>\n"
        f"   ├ <b>Start (Bepul):</b> {start_users} ta\n"
        f"   ├ <b>Pro:</b> {pro_users} ta\n"
        f"   └ <b>Max Media:</b> {max_users} ta\n"
        f"⏳ <b>Botning uzluksiz ishlash vaqti (Uptime):</b> {uptime}"
    )
    return text


def get_admin_panel_keyboard():
    return ik(
        [("💾 Zaxira nusxani olish (Backup)", "adm:backup_export")],
        [("📥 Zaxirani tiklash (Restore)", "adm:restore_backup")],
        [("📢 Hammaga xabar yuborish (Broadcast)", "adm:bcast")],
        [("⚙️ Foydalanuvchini boshqarish / Tarif berish", "adm:user")],
        [("↩️ Qaytarishlar (Refunds)", "adm:refunds"), ("🤝 Bitimlar (Deals)", "adm:deals")],
        [("💳 To'lovlar & Karta", "adm:card"), ("🔑 AI Kalitlar sozlamasi", "adm:keys_menu")],
        [("📺 Barcha Kanallar", "adm:channels"), ("🔄 Yangilash", "adm:refresh")]
    )


@dp.callback_query(AdminMsg, F.data == "adm:refunds")
async def adm_refunds_handler(cb: types.CallbackQuery):
    rows = q("SELECT * FROM refunds WHERE status='pending' ORDER BY created_at DESC LIMIT 10")
    if not rows:
        return await cb.message.answer("✅ <b>Hozirda kutilayotgan pul qaytarish (refund) so'rovlari yo'q!</b>", reply_markup=ik([("🔙 Orqaga", "adm:back")]))
    
    text = f"↩️ <b>KUTILAYOTGAN PUL QAYTARISHLAR ({len(rows)} ta):</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    btns = []
    for r in rows:
        text += (
            f"🆔 <b>Ariza #{r['id']}</b> | Bitim: <code>#{r['deal_id']}</code>\n"
            f"👤 <b>Mijoz:</b> {html.escape(r['full_name'] or '')} (ID: <code>{r['user_id']}</code>)\n"
            f"💳 <b>Karta:</b> <code>{r['card_number']}</code>\n"
            f"💵 <b>Summa:</b> <code>{r['amount']:,} so'm</code>\n"
            f"⚠️ <b>Sabab:</b> {html.escape(r['reason'] or '')}\n\n".replace(",", " ")
        )
        btns.append([(f"✅ #{r['id']} Qaytarildi", f"ref_ok:{r['id']}")])
    btns.append([("🔙 Orqaga", "adm:back")])
    await cb.message.answer(text, reply_markup=ik(*btns))
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:deals")
async def adm_deals_handler(cb: types.CallbackQuery):
    rows = q("SELECT * FROM deals ORDER BY created_at DESC LIMIT 10")
    if not rows:
        return await cb.message.answer("ℹ️ Hozircha bitimlar yo'q.", reply_markup=ik([("🔙 Orqaga", "adm:back")]))
    
    text = f"🤝 <b>OXIRGI BITIMLAR ({len(rows)} ta):</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    for r in rows:
        text += (
            f"🆔 <b>#{r['deal_id']}</b> | <b>{r['amount']:,} so'm</b>\n"
            f"📦 <b>Mavzu:</b> {html.escape(r['title'] or '')}\n"
            f"👥 Xaridor: <code>{r['buyer_id']}</code> ➔ Sotuvchi: <code>{html.escape(r['seller_name'] or '')}</code>\n"
            f"📌 <b>Holat:</b> <code>{r['status']}</code>\n\n".replace(",", " ")
        )
    await cb.message.answer(text, reply_markup=ik([("🔙 Orqaga", "adm:back")]))
    await cb.answer()


@dp.message(Command("admin"), F.chat.type == "private", AdminMsg)
async def admin_panel(m: types.Message, state: FSMContext):
    await state.clear()
    text = get_admin_panel_text()
    await m.answer(text, reply_markup=get_admin_panel_keyboard())


@dp.message(Command("admin"), F.chat.type == "private")
async def admin_unauthorized(m: types.Message):
    await m.answer("⛔️ <b>Ruxsat berilmagan!</b> Ushbu buyruq faqat bot adminlari uchun mo'ljallangan.")


# --- KARTA SOZLAMALARI ---
@dp.callback_query(AdminMsg, F.data == "adm:card")
async def adm_card(cb: types.CallbackQuery):
    card_num, card_own = get_card_info()
    await cb.message.answer(
        "💳 <b>KARTA SOZLAMALARI:</b>\n\n"
        f"🔢 <b>Hozirgi karta raqami:</b> <code>{card_num}</code>\n"
        f"👤 <b>Karta egasi:</b> <b>{html.escape(card_own)}</b>\n\n"
        "<i>O'zgartirish uchun kerakli tugmani bosing 👇</i>",
        reply_markup=ik(
            [("✏️ Karta raqamini o'zgartirish", "adm:setcardnum")],
            [("✏️ Karta egasini o'zgartirish", "adm:setcardowner")],
            [("🔙 Orqaga", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:setcardnum")
async def adm_setcardnum(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminCard.number)
    await cb.message.answer("💳 <b>Yangi karta raqamini yuboring</b> (masalan: <code>8600 1234 5678 9999</code>):")
    await cb.answer()


@dp.message(AdminCard.number, AdminMsg, F.text)
async def adm_savecardnum(m: types.Message, state: FSMContext):
    new_num = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('card_number', ?)", (new_num,), commit=True)
    await state.clear()
    await m.answer(f"✅ <b>Karta raqami muvaffaqiyatli saqlandi:</b> <code>{new_num}</code>", reply_markup=MENU)


@dp.callback_query(AdminMsg, F.data == "adm:setcardowner")
async def adm_setcardowner(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminCard.owner)
    await cb.message.answer("👤 <b>Yangi karta egasining ism-familiyasini yuboring</b> (masalan: <code>Ali Valiyev</code>):")
    await cb.answer()


@dp.message(AdminCard.owner, AdminMsg, F.text)
async def adm_savecardowner(m: types.Message, state: FSMContext):
    new_owner = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('card_owner', ?)", (new_owner,), commit=True)
    await state.clear()
    await m.answer(f"✅ <b>Karta egasi saqlandi:</b> <b>{html.escape(new_owner)}</b>", reply_markup=MENU)


# --- GEMINI AI KALITI SOZLAMALARI ---
@dp.callback_query(AdminMsg, F.data == "adm:gemini")
async def adm_gemini(cb: types.CallbackQuery):
    key = get_gemini_api_key()
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else (key or "Kiritilmagan")
    await cb.message.answer(
        "🔑 <b>GEMINI AI KALITI VA SOZLAMALARI:</b>\n\n"
        f"🗝 <b>Joriy kalit:</b> <code>{masked}</code>\n"
        f"🤖 <b>Asosiy model:</b> <code>{GEMINI_MODEL}</code>\n\n"
        "<i>Quyidagi amallardan birini tanlang 👇</i>",
        reply_markup=ik(
            [("🧪 Kalitni sinab ko'rish (Test)", "adm:testgemini")],
            [("✏️ Yangi Gemini kalit kiritish", "adm:setgemini")],
            [("🔙 Orqaga", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:testgemini")
async def adm_testgemini(cb: types.CallbackQuery):
    await cb.answer("⏳ Gemini AI tekshirilmoqda...")
    ok, msg = await test_gemini_key()
    if ok:
        await cb.message.answer(f"✅ <b>Gemini AI to'liq ishlamoqda!</b>\n\n{msg}")
    else:
        await cb.message.answer(
            f"❌ <b>Gemini AI ulanmadi!</b>\n\n"
            f"Sababi: <code>{msg}</code>\n\n"
            "💡 <i>Eslatma: Haqiqiy bepul Gemini API kalitini https://aistudio.google.com/app/apikey "
            "saytidan bepul olishingiz mumkin (odatda «AIzaSy...» bilan boshlanadi).</i>"
        )


@dp.callback_query(AdminMsg, F.data == "adm:setgemini")
async def adm_setgemini(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminGemini.key)
    await cb.message.answer(
        "🔑 <b>Yangi Gemini API kalitini yuboring:</b>\n\n"
        "Uni https://aistudio.google.com/app/apikey manzilidan bepul olishingiz mumkin "
        "(odatda <code>AIzaSy...</code> bilan boshlanadi).\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(AdminGemini.key, AdminMsg, F.text)
async def adm_savegemini(m: types.Message, state: FSMContext):
    new_key = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (new_key,), commit=True)
    await state.clear()
    
    ok, msg = await test_gemini_key(new_key)
    if ok:
        await m.answer(f"🎉 <b>Yangi Gemini API kaliti saqlandi va tekshirildi!</b>\n\n{msg}", reply_markup=MENU)
    else:
        await m.answer(
            f"⚠️ <b>Kalit saqlandi, lekin Google xato qaytardi:</b>\n<code>{msg}</code>\n\n"
            "Iltimos, kalit to'g'riligini tekshiring (bepul olish: https://aistudio.google.com/app/apikey).",
            reply_markup=MENU
        )


# --- GROQ AI SOZLAMALARI ---
@dp.callback_query(AdminMsg, F.data == "adm:groq")
async def adm_groq(cb: types.CallbackQuery):
    key = get_groq_api_key()
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else (key or "Kiritilmagan")
    await cb.message.answer(
        "⚡️ <b>GROQ AI KALITI VA SOZLAMALARI:</b>\n\n"
        f"🗝 <b>Joriy kalit:</b> <code>{masked}</code>\n"
        "🤖 <b>Vazifasi:</b> Guruh chati muloqoti va zaxira post/poll generatsiyasi (0.3 soniyada)\n\n"
        "<i>Quyidagi amallardan birini tanlang 👇</i>",
        reply_markup=ik(
            [("🧪 Kalitni sinab ko'rish (Test)", "adm:testgroq")],
            [("✏️ Yangi Groq kalit kiritish", "adm:setgroq")],
            [("🔙 Orqaga", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:testgroq")
async def adm_testgroq(cb: types.CallbackQuery):
    await cb.answer("⏳ Groq AI tekshirilmoqda...")
    ok, msg = await test_groq_key()
    if ok:
        await cb.message.answer(f"✅ <b>Groq AI to'liq ishlamoqda!</b>\n\n{msg}")
    else:
        await cb.message.answer(f"❌ <b>Groq AI ulanmadi!</b>\n\nSababi: <code>{msg}</code>")


@dp.callback_query(AdminMsg, F.data == "adm:setgroq")
async def adm_setgroq(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminGroq.key)
    await cb.message.answer(
        "⚡️ <b>Yangi Groq API kalitini yuboring:</b>\n\n"
        "(odatda <code>gsk_...</code> bilan boshlanadi).\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(AdminGroq.key, AdminMsg, F.text)
async def adm_savegroq(m: types.Message, state: FSMContext):
    new_key = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('groq_api_key', ?)", (new_key,), commit=True)
    await state.clear()
    
    ok, msg = await test_groq_key(new_key)
    if ok:
        await m.answer(f"🎉 <b>Yangi Groq API kaliti saqlandi va tekshirildi!</b>\n\n{msg}", reply_markup=MENU)
    else:
        await m.answer(
            f"⚠️ <b>Kalit saqlandi, lekin xato qaytardi:</b>\n<code>{msg}</code>",
            reply_markup=MENU
        )


# --- YOUTUBE DATA API SOZLAMALARI ---
@dp.callback_query(AdminMsg, F.data == "adm:youtube")
async def adm_youtube(cb: types.CallbackQuery):
    key = get_youtube_api_key()
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else (key or "Kiritilmagan (Avto-qidiruv faol)")
    await cb.message.answer(
        "🎥 <b>YOUTUBE DATA API V3 SOZLAMALARI:</b>\n\n"
        f"🗝 <b>Joriy kalit:</b> <code>{masked}</code>\n"
        "🤖 <b>Vazifasi:</b> MAX tarifi uchun so'nggi 7 kundagi eng qaynoq va ommabop 3-5 ta videolarni tahlil qilish.\n"
        "💡 <i>Eslatma: Kalit bo'lmasa ham tizim avtomatik zaxira qidiruv orqali ishlayveradi!</i>\n\n"
        "<i>Quyidagi amallardan birini tanlang 👇</i>",
        reply_markup=ik(
            [("🧪 Kalitni sinab ko'rish (Test)", "adm:testyoutube")],
            [("✏️ Yangi YouTube kalit kiritish", "adm:setyoutube")],
            [("🔙 Orqaga", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:testyoutube")
async def adm_testyoutube(cb: types.CallbackQuery):
    await cb.answer("⏳ YouTube API tekshirilmoqda...")
    ok, msg = await test_youtube_key()
    if ok:
        await cb.message.answer(f"✅ <b>YouTube Data API to'liq ishlamoqda!</b>\n\n{msg}")
    else:
        await cb.message.answer(f"❌ <b>YouTube API ulanmadi!</b>\n\nSababi: <code>{msg}</code>")


@dp.callback_query(AdminMsg, F.data == "adm:setyoutube")
async def adm_setyoutube(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminYouTube.key)
    await cb.message.answer(
        "🎥 <b>Yangi YouTube Data API v3 kalitini yuboring:</b>\n\n"
        "(Google Cloud Console orqali olinadi, odatda <code>AIzaSy...</code> bilan boshlanadi).\n\n"
        "Bekor qilish: /cancel"
    )
    await cb.answer()


@dp.message(AdminYouTube.key, AdminMsg, F.text)
async def adm_saveyoutube(m: types.Message, state: FSMContext):
    new_key = m.text.strip()
    q("INSERT OR REPLACE INTO settings (key, value) VALUES ('youtube_api_key', ?)", (new_key,), commit=True)
    await state.clear()
    
    ok, msg = await test_youtube_key(new_key)
    if ok:
        await m.answer(f"🎉 <b>Yangi YouTube API kaliti saqlandi va tekshirildi!</b>\n\n{msg}", reply_markup=MENU)
    else:
        await m.answer(
            f"⚠️ <b>Kalit saqlandi, lekin tekshiruvda xatolik:</b>\n<code>{msg}</code>\n\n"
            "Eslatma: Kalitsiz ham bot avtomatik YouTube trendlarini yuklayveradi.",
            reply_markup=MENU
        )


# --- BAZA ZAXIRASI (BACKUP / RESTORE) ---
@dp.callback_query(AdminMsg, F.data.in_(["adm:backup", "adm:backup_export", "adm:download_backup"]))
async def adm_backup_export(cb: types.CallbackQuery):
    await cb.answer("⏳ Zaxira fayli yaratilmoqda...")
    try:
        filepath = create_backup_file()
        if not os.path.exists(filepath):
            return await cb.message.answer("❌ Zaxira fayli yaratib bo'lmadi.")
        filename = os.path.basename(filepath)
        doc = types.FSInputFile(filepath, filename=filename)
        caption = (
            f"💾 <b>BOT MA'LUMOTLAR BAZASI ZAXIRASI (BACKUP)</b>\n\n"
            f"📅 <b>Sana:</b> <code>{datetime.now(TZ).strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
            f"📁 <b>Fayl:</b> <code>{filename}</code>\n"
            f"📦 <b>Qamrab olingan:</b> Users, Channels, Subscriptions, Settings, Recent Posts.\n\n"
            f"<i>Ushbu fayl orqali botni istalgan payt '📥 Zaxirani tiklash' tugmasi bilan qayta tiklashingiz mumkin.</i>"
        )
        await cb.message.answer_document(doc, caption=caption)
    except Exception as e:
        logging.error(f"Backup yuborish xatosi: {e}")
        await cb.message.answer(f"❌ <b>Xatolik yuz berdi:</b> <code>{html.escape(str(e))}</code>")


@dp.callback_query(AdminMsg, F.data.in_(["adm:restore", "adm:restore_backup", "adm:restore_import"]))
async def adm_restore_backup(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminRestore.file)
    await cb.message.answer(
        "📥 <b>BAZANI QAYTA TIKLASH (RESTORE / IMPORT)</b>\n\n"
        "Iltimos, avval yuklab olingan zaxira (<b>.json</b> yoki <b>.db</b>) faylini menga yuboring...\n\n"
        "<i>Bekor qilish: /cancel</i>"
    )
    await cb.answer()


@dp.message(AdminRestore.file, AdminMsg, F.document)
async def adm_process_restore_file(m: types.Message, state: FSMContext):
    doc = m.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".json") or fname.endswith(".db") or fname.endswith(".sqlite")):
        return await m.answer(
            "❌ <b>Noto'g'ri fayl formati!</b>\n\n"
            "Faqat <code>.json</code> yoki <code>.db</code> formatidagi zaxira fayllarini yuboring.\n"
            "Bekor qilish: /cancel"
        )

    await state.clear()
    status_msg = await m.answer("⏳ <b>Zaxira fayli tekshirilmoqda va tiklanmoqda...</b>")

    temp_dir = "temp_backups"
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"restore_{int(time.time())}_{doc.file_name}")

    try:
        file_info = await bot.get_file(doc.file_id)
        await bot.download_file(file_info.file_path, temp_path)

        ok, summary = restore_database_from_file(temp_path)
        if ok:
            await status_msg.edit_text(
                f"✅ <b>Barcha ma'lumotlar va kanallar muvaffaqiyatli qayta tiklandi!</b>\n\n"
                f"📊 <b>Tiklash hisoboti:</b>\n{summary}",
                reply_markup=get_admin_panel_keyboard()
            )
        else:
            await status_msg.edit_text(
                f"❌ <b>Zaxirani tiklashda xatolik yuz berdi:</b>\n"
                f"<code>{html.escape(summary)}</code>\n\n"
                "⚠️ Amaliyot xavfsiz to'xtatildi, mavjud ma'lumotlar o'zgarishsiz qoldi."
            )
    except Exception as e:
        logging.error(f"Fayl tiklash xatosi: {e}")
        await status_msg.edit_text(
            f"❌ <b>Zaxirani tiklash jarayonida xatolik:</b>\n"
            f"<code>{html.escape(str(e))}</code>\n\n"
            "⚠️ Bot barqaror ishlashda davom etmoqda."
        )
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


@dp.callback_query(AdminMsg, F.data.in_(["adm:back", "adm:refresh"]))
async def adm_back_btn(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = get_admin_panel_text()
    try:
        await cb.message.edit_text(text, reply_markup=get_admin_panel_keyboard())
    except Exception:
        await cb.message.answer(text, reply_markup=get_admin_panel_keyboard())
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:keys_menu")
async def adm_keys_menu(cb: types.CallbackQuery):
    g_key = get_gemini_api_key()
    masked_g = (g_key[:6] + "..." + g_key[-4:]) if len(g_key) > 10 else (g_key or "Kiritilmagan")
    gr_key = get_groq_api_key()
    masked_gr = (gr_key[:6] + "..." + gr_key[-4:]) if len(gr_key) > 10 else (gr_key or "Kiritilmagan")
    yt_key = get_youtube_api_key()
    masked_yt = (yt_key[:6] + "..." + yt_key[-4:]) if len(yt_key) > 10 else (yt_key or "Avto")
    await cb.message.answer(
        "🔑 <b>AI VA TIZIM KALITLARI SOZLAMALARI:</b>\n\n"
        f"🌐 <b>Gemini AI:</b> <code>{masked_g}</code>\n"
        f"⚡️ <b>Groq AI:</b> <code>{masked_gr}</code>\n"
        f"🎥 <b>YouTube API:</b> <code>{masked_yt}</code>\n\n"
        "<i>Kerakli xizmatni tanlang:</i>",
        reply_markup=ik(
            [("🔑 Gemini AI", "adm:gemini"), ("⚡️ Groq AI", "adm:groq")],
            [("🎥 YouTube API", "adm:youtube"), ("🔒 Majburiy Kanallar", "adm:req")],
            [("🔙 Admin panelga qaytish", "adm:back")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:stats")
async def adm_stats(cb: types.CallbackQuery):
    users = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    now = now_utc().isoformat()
    by_plan = {k: q("SELECT COUNT(*) c FROM users WHERE plan=? AND expires_at>?", (k, now), one=True)["c"] for k in PLANS}
    income = q("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved'", one=True)["s"]
    pending = q("SELECT COUNT(*) c FROM payments WHERE status='pending'", one=True)["c"]
    ch = q("SELECT COUNT(*) c, COALESCE(SUM(posts_sent),0) p, COALESCE(SUM(polls_sent),0) pl FROM channels", one=True)
    await cb.message.answer(
        "📊 <b>BOTNING TO'LIQ STATISTIKASI:</b>\n\n"
        f"👤 <b>Jami foydalanuvchilar:</b> <b>{users} ta</b>\n"
        + "\n".join(f"💎 {PLANS[k]['name']} tarifi: <b>{v} ta</b>" for k, v in by_plan.items())
        + f"\n\n💳 <b>Kutilayotgan to'lovlar:</b> <b>{pending} ta</b>\n"
        f"💰 <b>Jami tasdiqlangan tushum:</b> <code>{income:,} so'm</code>\n".replace(",", " ")
        + f"📺 <b>Ulangan kanallar:</b> <b>{ch['c']} ta</b>\n"
        f"📝 <b>Yuborilgan AI postlar:</b> <b>{ch['p']} ta</b>\n"
        f"📊 <b>Yuborilgan so'rovnomalar:</b> <b>{ch['pl']} ta</b>"
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:pay")
async def adm_pay(cb: types.CallbackQuery):
    rows = q("SELECT * FROM payments WHERE status='pending' ORDER BY id LIMIT 10")
    if not rows:
        await cb.message.answer("✅ <b>Hozirda kutilayotgan to'lovlar mavjud emas.</b>")
    for p in rows:
        await bot.send_photo(
            cb.from_user.id, p["file_id"],
            caption=f"💳 <b>To'lov #{p['id']}</b>\nFoydalanuvchi: <code>{p['user_id']}</code>\nTarif: <b>{PLANS[p['plan']]['name']}</b> — <code>{p['amount']:,} so'm</code>".replace(",", " "),
            reply_markup=ik([("✅ Tasdiqlash", f"pay_ok:{p['id']}"), ("❌ Rad etish", f"pay_no:{p['id']}")]))
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "adm:channels")
async def adm_channels(cb: types.CallbackQuery):
    rows = q("SELECT * FROM channels ORDER BY posts_sent DESC LIMIT 25")
    if not rows:
        await cb.message.answer("📺 <b>Baza bo'sh:</b> Hozircha hech qanday kanal ulanmagan.")
        return await cb.answer()
    text = "📺 <b>Barcha Ulangan Kanallar:</b>\n\n" + "\n".join(
        f"{'🟢' if r['active'] else '⏸'} <b>{html.escape(r['title'])}</b> (ID: <code>{r['channel_id']}</code>)\n"
        f"Egasi: <code>{r['owner_id']}</code> | Guruh: <code>{r['group_id'] or 'Yo`q'}</code> | Post: {r['posts_sent']} | So'rov: {r['polls_sent']}\n"
        for r in rows
    )
    btns = [[(("🟢 " if r["active"] else "⏸ ") + r["title"], f"ch:{r['channel_id']}")] for r in rows]
    btns.append([("🔙 Orqaga", "adm:back")])
    await cb.message.answer(text, reply_markup=ik(*btns))
    await cb.answer()


@dp.callback_query(AdminMsg, F.data.in_(["adm:user", "adm:user_manage"]))
async def adm_user(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminUser.uid)
    await cb.message.answer(
        "⚙️ <b>FOYDALANUVCHINI BOSHQARISH VA TARIF BERISH:</b>\n\n"
        "Boshqarmoqchi bo'lgan foydalanuvchining <b>Telegram ID raqami</b>ni yuboring:\n\n"
        "<i>Bekor qilish: /cancel</i>"
    )
    await cb.answer()


def user_admin_card(uid):
    u = q("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    if not u:
        return None, None
    plan, exp = get_user_plan(uid)
    n = q("SELECT COUNT(*) c FROM channels WHERE owner_id=?", (uid,), one=True)["c"]
    p_info = PLANS.get(plan, PLANS["start"])
    text = (
        f"👤 <b>Foydalanuvchi:</b> {html.escape(u['full_name'] or '')} (<code>{uid}</code>)\n"
        f"🔗 @{u['username'] or 'Yo`q'}\n"
        f"💎 <b>Joriy tarif:</b> <b>{p_info['name']}</b>" + (f" ({fmt(exp)} gacha)" if exp else " (Cheksiz / Bepul)") + "\n"
        f"📺 <b>Ulangan kanallar soni:</b> {n} ta\n"
        f"⚙️ <b>Holati:</b> {'🚫 Bloklangan' if u['banned'] else '✅ Faol'}"
    )
    kb = ik(
        [("🎁 START (Bepul)", f"ug:{uid}:start"), ("🎁 PRO KANAL (30 kun)", f"ug:{uid}:pro")],
        [("🎁 MAX MEDIA (30 kun)", f"ug:{uid}:max"), ("❌ Tarifni bekor qilish", f"urev:{uid}")],
        [("🚫 Bloklash / ✅ Ochish", f"uban:{uid}"), ("🔙 Admin panel", "adm:back")]
    )
    return text, kb


@dp.message(AdminUser.uid, AdminMsg, F.text)
async def adm_user_find(m: types.Message, state: FSMContext):
    if not m.text.strip().lstrip("-").isdigit():
        return await m.answer("Faqat raqam yuboring.")
    text, kb = user_admin_card(int(m.text.strip()))
    if not text:
        return await m.answer("❌ Foydalanuvchi topilmadi.")
    await state.clear()
    await m.answer(text, reply_markup=kb)


@dp.callback_query(AdminMsg, F.data.startswith(("ug:", "urev:", "uban:")))
async def adm_user_action(cb: types.CallbackQuery):
    parts = cb.data.split(":")
    uid = int(parts[1])
    if parts[0] == "ug":
        apply_plan(uid, parts[2])
        try:
            await bot.send_message(uid, f"🎁 <b>Tabriklaymiz!</b> Sizga admin tomonidan <b>{PLANS[parts[2]]['name']}</b> tarifi 30 kunga faollashtirildi.")
        except Exception:
            pass
    elif parts[0] == "urev":
        q("UPDATE users SET plan=NULL, expires_at=NULL WHERE user_id=?", (uid,), commit=True)
        q("UPDATE subscriptions SET plan_name='start', expires_at=NULL WHERE user_id=?", (uid,), commit=True)
    else:
        q("UPDATE users SET banned = 1 - banned WHERE user_id=?", (uid,), commit=True)
    text, kb = user_admin_card(uid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Bajarildi!")


@dp.callback_query(AdminMsg, F.data == "adm:req")
async def adm_req(cb: types.CallbackQuery):
    rows = [r["chat"] for r in q("SELECT chat FROM required")]
    await cb.message.answer(
        "🔒 <b>Majburiy Obuna Kanallari:</b>\n\n" + ("\n".join(rows) or "Hozircha yo'q") +
        "\n\n➕ Qo'shish: <code>/addreq @kanal</code>\n➖ Olib tashlash: <code>/delreq @kanal</code>\n"
        "<i>Eslatma: Bot ushbu kanallarda admin bo'lishi shart!</i>")
    await cb.answer()


@dp.message(Command("addreq"), AdminMsg)
async def add_req(m: types.Message):
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].startswith("@"):
        return await m.answer("Format: <code>/addreq @kanal</code>")
    q("INSERT OR IGNORE INTO required VALUES (?)", (parts[1],), commit=True)
    await m.answer(f"✅ <b>{parts[1]} majburiy obuna ro'yxatiga qo'shildi.</b>")


@dp.message(Command("delreq"), AdminMsg)
async def del_req(m: types.Message):
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Format: <code>/delreq @kanal</code>")
    q("DELETE FROM required WHERE chat=?", (parts[1],), commit=True)
    await m.answer("🗑 <b>Majburiy kanallardan olib tashlandi.</b>")


# ---- Reklama yuborish (Broadcast) ----
@dp.callback_query(AdminMsg, F.data == "adm:bcast")
async def adm_bcast(cb: types.CallbackQuery):
    await cb.message.answer(
        "📣 <b>Reklama xabari kimlarga yuborilsin?</b>",
        reply_markup=ik(
            [("👥 Foydalanuvchilarga", "bc:users"), ("📺 Kanallarga", "bc:channels")],
            [("🌐 Barchasiga (User + Kanal)", "bc:both")]
        )
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data.startswith("bc:") & (F.data != "bc:go"))
async def bc_target(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Bcast.content)
    await state.update_data(target=cb.data.split(":")[1])
    await cb.message.answer("📝 <b>Reklama xabaringizni yuboring</b> (Matn, rasm, video, havola — istalgan turdagi):")
    await cb.answer()


@dp.message(Bcast.content, AdminMsg)
async def bc_content(m: types.Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    d = await state.get_data()
    nu = q("SELECT COUNT(*) c FROM users WHERE banned=0", one=True)["c"]
    nc = q("SELECT COUNT(*) c FROM channels", one=True)["c"]
    await m.answer(
        f"📣 <b>Yuborish tasdiqlansinmi?</b>\n\n"
        f"🎯 <b>Nishon:</b> <code>{d['target']}</code>\n"
        f"👥 Foydalanuvchilar: <b>{nu} ta</b>\n"
        f"📺 Kanallar: <b>{nc} ta</b>",
        reply_markup=ik([("✅ Ha, yuborilsin", "bc:go"), ("❌ Bekor qilish", "bc:cancel")])
    )


@dp.callback_query(AdminMsg, F.data == "bc:go")
async def bc_go(cb: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("msg_id"):
        return await cb.answer("Xabar topilmadi", show_alert=True)
    await state.clear()
    targets = []
    if d["target"] in ("users", "both"):
        targets += [r["user_id"] for r in q("SELECT user_id FROM users WHERE banned=0")]
    if d["target"] in ("channels", "both"):
        targets += [r["channel_id"] for r in q("SELECT channel_id FROM channels")]
    
    await cb.message.answer(f"🚀 <b>Xabar yuborish boshlandi:</b> <code>{len(targets)} ta</code> manzil...")
    ok = fail = 0
    for t in targets:
        try:
            await bot.copy_message(t, d["chat_id"], d["msg_id"])
            ok += 1
        except TelegramRetryAfter as e_retry:
            await asyncio.sleep(e_retry.retry_after + 1)
            try:
                await bot.copy_message(t, d["chat_id"], d["msg_id"])
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    await cb.message.answer(
        f"✅ <b>Yuborish yakunlandi!</b>\n\n"
        f"Yetkazildi: <b>{ok} ta</b>\n"
        f"Yetkazilmadi (bloklangan/o'chirilgan): <b>{fail} ta</b>",
        reply_markup=get_admin_panel_keyboard()
    )
    await cb.answer()


@dp.callback_query(AdminMsg, F.data == "bc:cancel")
async def bc_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("❌ Reklama bekor qilindi.")
    await cb.answer()


# ============================ ISHGA TUSHIRISH ============================
async def run_render_web_server():
    port_str = os.getenv("PORT")
    if port_str:
        try:
            port = int(port_str)
            async def handle_ping(request):
                return web.Response(text="Bot is running smoothly!")

            app = web.Application()
            app.router.add_get("/", handle_ping)
            app.router.add_get("/health", handle_ping)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            logging.info(f"Render Health Check server {port}-portda ishga tushdi.")
        except Exception as e:
            logging.warning(f"Health server xatosi: {e}")


async def weekly_stats_report():
    """
    MAX MEDIA / BIZNES tarifi uchun har haftalik kanal faolligi va o'sish statistikasi hisoboti.
    """
    try:
        max_users = q("SELECT user_id FROM subscriptions WHERE plan_name='max'")
        for u in max_users:
            uid = u["user_id"]
            chs = q("SELECT * FROM channels WHERE owner_id=? AND active=1", (uid,))
            if not chs:
                continue
            text = "📈 <b>HAFTALIK KANAL FAOLLIGI VA O'SISH STATISTIKASI</b>\n\n"
            for c in chs:
                text += (
                    f"📺 <b>Kanal:</b> <b>{html.escape(c['title'])}</b>\n"
                    f"📝 <b>Chiqarilgan postlar:</b> {c['posts_sent']} ta\n"
                    f"📊 <b>So'rovnomalar:</b> {c['polls_sent']} ta\n"
                    f"👥 <b>Yangi a'zolar:</b> +{c['joined']} ta\n"
                    f"🚪 <b>Chiqib ketganlar:</b> -{c['left_count']} ta\n"
                    f"💬 <b>Guruhdagi xabarlar:</b> {c['group_messages']} ta\n"
                    f"🛡 <b>Moderatsiya (ogohlantirish va ban):</b> {c['warnings'] + c['bans']} ta\n\n"
                )
            text += "<i>Ushbu tahliliy hisobot har hafta «MAX MEDIA / BIZNES» tarifi foydalanuvchilariga avtomatik taqdim etiladi.</i>"
            try:
                await bot.send_message(uid, text)
            except Exception as e_send:
                logging.warning(f"Haftalik hisobot yuborishda xato ({uid}): {e_send}")
    except Exception as e:
        logging.warning(f"Haftalik hisobot generatori xatosi: {e}")


async def main():
    await run_render_web_server()
    scheduler.add_job(scheduled_tasks, "cron", minute=0)
    scheduler.add_job(expiry_reminders, "cron", hour=10, minute=0)
    scheduler.add_job(weekly_stats_report, "cron", day_of_week="sun", hour=20, minute=0)
    scheduler.add_job(nightly_auto_backup, "cron", hour=0, minute=0)
    scheduler.start()
    logging.info(f"Bot to'liq ishga tushdi! Model: {GEMINI_MODEL}, Admin: {ADMIN_IDS}")
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            if "Unauthorized" in str(e):
                logging.critical("❌ XATOLIK: Bot Tokeni noto'g'ri yoki Telegram (@BotFather) tomonidan bekor qilingan (Unauthorized)!\n"
                                 "Iltimos, @BotFather orqali bot tokeningizni yangilang va BOT_TOKEN o'zgaruvchisiga kiriting.")
                break
            logging.error(f"⚠️ Telegram ulanishida uzilish: {e}. 5 soniyadan so'ng qayta ulanmoqda...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
