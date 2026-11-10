"""
Professional KAFIL / GARANT (Escrow Middleman) Telegram Boti
============================================================
Xususiyatlari:
1. TolovAPI.uz to'liq asinxron to'lov tizimi (amount_busy avtomatik suggested_amount qayta urinish, fon kuzatuvchisi).
2. Birinchi savdo 0% (MUTLAQO TEKIN), keyingi savdolar 10% komissiya va 10% Keshbek tizimi.
3. Keshbek balansi va keyingi savdolarda keshbekni chegirma sifatida ishlatish.
4. Yuqori darajadagi xavfsizlik: Escrow muzlatish, savdo chati, xaridor tasdiqlashi bilan sotuvchiga pul o'tkazish.
5. Nizolar (Dispute) va to'lovni qaytarish (Refund): 16 xonali karta olish, yozishmalarni adminga yuborish.
6. Render talablariga mos '0.0.0.0' da aiohttp HTTP web-server (No open ports muammosiz).
"""

import os
import sys
import json
import random
import string
import logging
import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape as esc_html

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.filters import Command, CommandStart, StateFilter

load_dotenv()

# ============================ SOZLAMALAR ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8479391754:AAFavvM86ZNJsn9VAv_I2krOcGTmBkY2zzY").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "6125160021").strip()
ADMIN_IDS = {int(x.strip()) for x in ADMIN_ID_RAW.replace(";", ",").split(",") if x.strip().isdigit()}

def is_admin(user_id: int) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)

# Rasmiy karta rekvizitlari
DEFAULT_CARD_NUMBER = os.getenv("CARD_NUMBER", "5614 6835 1125 2157").strip()
DEFAULT_CARD_OWNER = os.getenv("CARD_OWNER", "TILLAYEVA NASIBA").strip()

# TolovAPI.uz sozlamalari
TOLOV_API = os.getenv("TOLOV_API", "https://tolovapi.uz/api/").rstrip("/")
SHOP_ID = str(os.getenv("SHOP_ID", "339")).strip()
SHOP_KEY = str(os.getenv("SHOP_KEY", "cFpP7PdwqrDsVAdX")).strip()

TZ = ZoneInfo("Asia/Tashkent")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ============================ MA'LUMOTLAR BAZASI ============================
DB_PATH = "escrow.db"
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

def init_db():
    db.executescript("""
    PRAGMA journal_mode = WAL;
    PRAGMA busy_timeout = 10000;

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        completed_deals INTEGER DEFAULT 0,
        cashback_balance INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS deals (
        deal_id TEXT PRIMARY KEY,
        buyer_id INTEGER,
        buyer_username TEXT,
        seller_id INTEGER,
        seller_username TEXT,
        title TEXT,
        amount INTEGER,
        fee INTEGER DEFAULT 0,
        cashback_used INTEGER DEFAULT 0,
        total_to_pay INTEGER,
        cashback_earned INTEGER DEFAULT 0,
        status TEXT DEFAULT 'created',
        payment_id TEXT,
        seller_card TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id TEXT,
        buyer_id INTEGER,
        buyer_name TEXT,
        card_number TEXT,
        card_owner TEXT,
        amount INTEGER,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS deal_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id TEXT,
        sender_id INTEGER,
        sender_tag TEXT,
        role TEXT,
        message_text TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_number', ?)", (DEFAULT_CARD_NUMBER,))
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('card_owner', ?)", (DEFAULT_CARD_OWNER,))
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('commission_percent', '10')")
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cashback_percent', '10')")
    db.commit()

init_db()

def q(sql: str, args=(), one: bool = False, commit: bool = False):
    try:
        cur = db.cursor()
        cur.execute(sql, args)
        if commit:
            db.commit()
            return cur.lastrowid
        rows = cur.fetchone() if one else cur.fetchall()
        return rows
    except Exception as e:
        logging.error(f"SQL Xatosi: {e}\nQuery: {sql}")
        return None

def get_setting(key: str, default: str = "") -> str:
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return str(r["value"]).strip() if r and r["value"] is not None else default

def set_setting(key: str, val: str):
    q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(val)), commit=True)

def fmt(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)

def ensure_user(user_id: int, username: str = "", full_name: str = ""):
    u = q("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)
    now_s = datetime.now(TZ).isoformat()
    clean_un = username.lstrip("@") if username else ""
    if not u:
        q("INSERT INTO users (user_id, username, full_name, completed_deals, cashback_balance, is_banned, created_at) VALUES (?, ?, ?, 0, 0, 0, ?)",
          (user_id, clean_un, full_name or "Foydalanuvchi", now_s), commit=True)
    else:
        q("UPDATE users SET username=?, full_name=? WHERE user_id=?", (clean_un, full_name or u["full_name"], user_id), commit=True)

def get_user(user_id: int):
    return q("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)

def generate_deal_id() -> str:
    while True:
        digits = "".join(random.choices(string.digits, k=5))
        d_id = f"K{digits}"
        exists = q("SELECT deal_id FROM deals WHERE deal_id=?", (d_id,), one=True)
        if not exists:
            return d_id

# ============================ TOLOVAPI.UZ INTEGRATSIYASI ============================
async def tolov_api_request(endpoint: str, method: str = "POST", data: dict = None) -> dict:
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
    current_amount = int(amount)
    data = {"amount": current_amount}
    if order_id:
        data["order_id"] = str(order_id)
    if user_id:
        data["user_id"] = int(user_id)
        
    res = await tolov_api_request("payment/create", method="POST", data=data)
    if not res.get("ok") and res.get("error_code") == "amount_busy":
        suggested = res.get("suggested_amount")
        if suggested:
            logging.info(f"TolovAPI: Summa {current_amount} band, suggested_amount={suggested} bo'yicha qayta yaratilmoqda...")
            data["amount"] = int(suggested)
            res = await tolov_api_request("payment/create", method="POST", data=data)
    return res

async def check_payment_status_async(payment_id: str) -> dict:
    return await tolov_api_request("payment/status", method="POST", data={"payment_id": str(payment_id)})

async def cancel_payment_async(payment_id: str) -> dict:
    return await tolov_api_request("payment/cancel", method="POST", data={"payment_id": str(payment_id)})

# ============================ TELEGRAM BOT VA DISPATCHER ============================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ============================ FSM HOLATLARI ============================
class CreateDeal(StatesGroup):
    seller = State()
    title = State()
    amount = State()
    use_cashback = State()

class RefundProcess(StatesGroup):
    card_number = State()
    card_owner = State()
    reason = State()

class AdminChangeCard(StatesGroup):
    card = State()
    owner = State()

class AdminChangePercent(StatesGroup):
    percent = State()

# ============================ TUGMALAR VA MENYU ============================
def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="🛒 Yangi bitim yaratish"), KeyboardButton(text="📂 Mening bitimlarim")],
        [KeyboardButton(text="💰 Keshbek balansim"), KeyboardButton(text="👤 Profilim")],
        [KeyboardButton(text="🆘 Nizo / Yordam")]
    ]
    if is_admin(user_id):
        kb.append([KeyboardButton(text="👑 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# ============================ TO'LOV FON NAZORATI ============================
async def watch_payment_task(deal_id: str, payment_id: str, buyer_id: int, seller_id: int, deal_amount: int):
    max_checks = 100 # 5 daqiqa (har 3 soniyada)
    for _ in range(max_checks):
        await asyncio.sleep(3)
        deal = q("SELECT * FROM deals WHERE deal_id=?", (deal_id,), one=True)
        if not deal or deal["status"] != "waiting_payment":
            break
        try:
            status_res = await check_payment_status_async(payment_id)
            if status_res and status_res.get("ok"):
                st = str(status_res.get("status", "")).lower()
                if st in ("success", "paid", "completed"):
                    now_s = datetime.now(TZ).isoformat()
                    q("UPDATE deals SET status='in_trade', updated_at=? WHERE deal_id=?", (now_s, deal_id), commit=True)
                    
                    # Xaridorga xabar
                    await bot.send_message(
                        buyer_id,
                        f"🎉 <b>TO'LOV MUVAFFAQIYATLI QABUL QILINDI!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 <b>Bitim:</b> <code>#{deal_id}</code>\n"
                        f"🔒 <b>Holat:</b> <b>Pul Kafil hisobida xavfsiz muzlatildi!</b>\n\n"
                        f"Sotuvchiga to'lov qabul qilingani haqida xabar berildi. Sotuvchi tovarni (akkaunt, kod yoki xizmatni) sizga topshirishi lozim.\n\n"
                        f"💬 <i>Ushbu bot orqali sotuvchi bilan xavfsiz muloqot qilishingiz mumkin.</i>",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Tovarni to'liq tekshirdim va qabul qildim", callback_data=f"confirm_deal:{deal_id}")],
                            [InlineKeyboardButton(text="🚨 Nizo ochish (Shikoyat)", callback_data=f"dispute_deal:{deal_id}")]
                        ])
                    )
                    
                    # Sotuvchiga xabar
                    if seller_id:
                        await bot.send_message(
                            seller_id,
                            f"💰 <b>XARIDOR TO'LOVNI AMALGA OSHIRDI!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🆔 <b>Bitim:</b> <code>#{deal_id}</code>\n"
                            f"💵 <b>Tovar narxi:</b> <b>{fmt(deal_amount)} so'm</b>\n"
                            f"🔒 <b>Holat:</b> Pul Kafil hisobida xavfsiz saqlanmoqda!\n\n"
                            f"Endi xaridorga tovarni (ma'lumotlarni) to'liq topshiring. Xaridor tasdiqlagan zahoti pul sizning kartangizga o'tkaziladi.\n\n"
                            f"💬 <i>Bot orqali xaridorga xabaringizni yozishingiz mumkin.</i>",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🚨 Nizo ochish (Shikoyat)", callback_data=f"dispute_deal:{deal_id}")]
                            ])
                        )
                    
                    # Adminga bildirishnoma
                    for a_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                a_id,
                                f"💳 <b>Yangi to'lov avtomatik tasdiqlandi!</b>\n"
                                f"Bitim: <code>#{deal_id}</code> | TolovAPI ID: <code>{payment_id}</code>\n"
                                f"Summa: <b>{fmt(deal['total_to_pay'])} so'm</b>"
                            )
                        except Exception:
                            pass
                    break
        except Exception as e_watch:
            logging.warning(f"watch_payment_task xatosi ({deal_id}): {e_watch}")

# ============================ ASOSIY BUYRUQLAR ============================
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    un = msg.from_user.username or ""
    fn = msg.from_user.full_name or "Foydalanuvchi"
    ensure_user(uid, un, fn)
    
    text_args = msg.text.split()[1] if len(msg.text.split()) > 1 else ""
    
    # Sotuvchi bitim havolasi orqali kirgan holat: /start deal_K12345
    if text_args.startswith("deal_"):
        d_id = text_args.replace("deal_", "").strip()
        deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
        if not deal:
            await msg.answer("❌ Bitim topilmadi yoki muddati o'tgan.", reply_markup=main_menu_keyboard(uid))
            return
        
        if deal["buyer_id"] == uid:
            await msg.answer("⚠️ Siz ushbu bitimning xaridorisiz. Sotuvchi ushbu havola orqali savdoga qo'shilishi kerak.", reply_markup=main_menu_keyboard(uid))
            return
            
        if deal["status"] not in ("created", "waiting_seller"):
            await msg.answer(f"⚠️ <b>#{d_id}</b> bitimiga allaqachon sotuvchi biriktirilgan (Holat: {deal['status']}).", reply_markup=main_menu_keyboard(uid))
            return
            
        # Sotuvchini biriktirish
        now_s = datetime.now(TZ).isoformat()
        q("UPDATE deals SET seller_id=?, seller_username=?, status='waiting_payment', updated_at=? WHERE deal_id=?",
          (uid, un.lstrip("@"), now_s, d_id), commit=True)
          
        await msg.answer(
            f"🤝 <b>SIZ SAVDOGA SOTUVCHI SIFATIDA QO'SHILDINGIZ!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Bitim:</b> <code>#{d_id}</code>\n"
            f"📦 <b>Tovar:</b> <b>{esc_html(deal['title'])}</b>\n"
            f"💵 <b>Siz oladigan summa:</b> <b>{fmt(deal['amount'])} so'm</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ <b>Xaridor kafil hisobiga to'lov qilishi kutilmoqda.</b>\n"
            f"⚠️ <i>Diqqat: Hozircha xaridorga hech qanday akkaunt yoki ma'lumot bermang! Pul tushgach bot xabar beradi.</i>",
            reply_markup=main_menu_keyboard(uid)
        )
        
        # Xaridorga to'lov tafsilotlarini yuborish
        card_num = get_setting("card_number", DEFAULT_CARD_NUMBER)
        card_own = get_setting("card_owner", DEFAULT_CARD_OWNER)
        
        # TolovAPI to'lov yaratish
        total_pay = deal["total_to_pay"]
        pay_res = await create_payment_async(total_pay, order_id=d_id, user_id=deal["buyer_id"])
        
        exact_amount = total_pay
        payment_id = None
        if pay_res and pay_res.get("ok"):
            payment_id = pay_res.get("payment_id")
            exact_amount = int(pay_res.get("amount", total_pay))
            q("UPDATE deals SET payment_id=?, total_to_pay=? WHERE deal_id=?", (payment_id, exact_amount, d_id), commit=True)
            asyncio.create_task(watch_payment_task(d_id, payment_id, deal["buyer_id"], uid, deal["amount"]))
            
        fee_note = "🎁 <b>Xizmat haqi:</b> 0 so'm (1-savdo bepul!)" if deal["fee"] == 0 else f"💼 <b>Xizmat haqi:</b> {fmt(deal['fee'])} so'm"
        cashback_note = f"\n💰 <b>Keshbek chegirmasi:</b> -{fmt(deal['cashback_used'])} so'm" if deal["cashback_used"] > 0 else ""
        
        payment_msg = (
            f"🎉 <b>SOTUVCHI SAVDOGA QO'SHILDI!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Bitim:</b> <code>#{d_id}</code>\n"
            f"👤 <b>Sotuvchi:</b> @{esc_html(un.lstrip('@'))} (ID: <code>{uid}</code>)\n"
            f"📦 <b>Tovar:</b> <b>{esc_html(deal['title'])}</b>\n"
            f"💵 <b>Tovar narxi:</b> {fmt(deal['amount'])} so'm\n"
            f"{fee_note}{cashback_note}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 <b>TO'LOV REKVIZITLARI:</b>\n"
            f"Karta raqami: <code>{card_num}</code>\n"
            f"Qabul qiluvchi: <b>{esc_html(card_own)}</b>\n\n"
            f"💰 <b>O'TKAZILADIGAN ANIQ SUMMA:</b>\n"
            f"👉 <code>{fmt(exact_amount)}</code> <b>so'm</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>DIQQAT: Ko'rsatilgan summadan 1 so'm ham kam yoki ortiq to'lamang! Aynan ko'rsatilgan summani aniq o'tkazing, aks holda to'lov tizim tomonidan avtomatik tasdiqlanmay qolishi mumkin.</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏱ <i>To'lovni amalga oshirishingiz bilan tizim avtomatik tasdiqlaydi (3-5 soniya).</i>"
        )
        
        inline_kb = []
        if pay_res and pay_res.get("payment_url"):
            inline_kb.append([InlineKeyboardButton(text="💳 Havola orqali to'lash", url=pay_res["payment_url"])])
        inline_kb.append([InlineKeyboardButton(text="❌ Savdoni bekor qilish", callback_data=f"cancel_deal:{d_id}")])
        
        await bot.send_message(deal["buyer_id"], payment_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_kb))
        return

    welcome_text = (
        f"👋 <b>Assalomu alaykum, {esc_html(fn)}!</b>\n\n"
        f"🛡 <b>KAFIL / GARANT XAVFSIZ SAVDO BOTIGA XUSH KELIBSIZ!</b>\n\n"
        f"Ushbu bot orqali internetdagi har qanday oldi-sotdi (Telegram kanallar, akkauntlar, dasturlar, xizmatlar)ni "
        f"<b>100% xavfsiz va firibgarlarsiz</b> amalga oshirishingiz mumkin.\n\n"
        f"🎁 <b>Aksiya:</b> Siz uchun 1-savdo <b>MUTLAQO BEPUL (0% komissiya)</b>!\n"
        f"💎 Har bir yakunlangan savdodan keyin <b>10% KESHBEK</b> balansingizga qaytariladi!\n\n"
        f"Quyidagi menyudan kerakli bo'limni tanlang:"
    )
    await msg.answer(welcome_text, reply_markup=main_menu_keyboard(uid))

# ============================ 1. YANGI BITIM YARATISH (FSM) ============================
@router.message(F.text == "🛒 Yangi bitim yaratish")
async def start_create_deal(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    ensure_user(uid, msg.from_user.username, msg.from_user.full_name)
    
    # Avval yakunlanmagan faol savdosi bormi tekshirish
    active = q("SELECT deal_id FROM deals WHERE buyer_id=? AND status IN ('created', 'waiting_seller', 'waiting_payment', 'in_trade', 'dispute')", (uid,), one=True)
    if active:
        await msg.answer(f"⚠️ Sizda faol bitim mavjud: <code>#{active['deal_id']}</code>.\nAvval ushbu bitimni yakunlang yoki bekor qiling.", reply_markup=main_menu_keyboard(uid))
        return

    await state.set_state(CreateDeal.seller)
    await msg.answer(
        "🤝 <b>Yangi xavfsiz bitim yaratish (1-qadam / 3):</b>\n\n"
        "Sotuvchining <b>Telegram username</b> (@username) yoki uning <b>Telegram ID</b> raqamini kiriting:\n\n"
        "<i>(Agar sotuvchining ID raqami bo'lmasa, shunchaki uning @username ini yozing)</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(F.text == "❌ Bekor qilish")
async def cancel_fsm(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Jarayon bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))

@router.message(CreateDeal.seller)
async def process_seller(msg: Message, state: FSMContext):
    seller_input = msg.text.strip().lstrip("@")
    if not seller_input:
        await msg.answer("Iltimos, to'g'ri username yoki ID kiriting:")
        return
    await state.update_data(seller=seller_input)
    await state.set_state(CreateDeal.title)
    await msg.answer(
        "📦 <b>Bitim mavzusi (2-qadam / 3):</b>\n\n"
        "Sotib olinayotgan tovar yoki xizmat nomini batafsil yozing:\n"
        "<i>(Masalan: @kanal_nomi kanali adminlik huquqi, yoki PUBG 75-daraja akkaunt)</i>"
    )

@router.message(CreateDeal.title)
async def process_title(msg: Message, state: FSMContext):
    title = msg.text.strip()
    if len(title) < 3:
        await msg.answer("Iltimos, tovar nomini aniqroq yozing:")
        return
    await state.update_data(title=title)
    await state.set_state(CreateDeal.amount)
    await msg.answer(
        "💵 <b>Savdo summasi (3-qadam / 3):</b>\n\n"
        "Tovar narxini so'mda, faqat raqamlarda kiriting (masalan: <code>100000</code>):"
    )

@router.message(CreateDeal.amount)
async def process_amount(msg: Message, state: FSMContext):
    txt = re.sub(r"\s+", "", msg.text.strip())
    if not txt.isdigit() or int(txt) < 1000:
        await msg.answer("⚠️ Iltimos, minimal summa 1 000 so'm. Summani to'g'ri raqam bilan kiriting:")
        return
    amount = int(txt)
    await state.update_data(amount=amount)
    
    uid = msg.from_user.id
    user = get_user(uid)
    cashback_bal = user["cashback_balance"] if user else 0
    
    # Komissiya hisobi: 1-savdo 0% (bepul), keyingi savdolar commission_percent (10%)
    completed_count = user["completed_deals"] if user else 0
    comm_pct = int(get_setting("commission_percent", "10"))
    fee = 0 if completed_count == 0 else round(amount * comm_pct / 100)
    await state.update_data(fee=fee)
    
    # Agar keshbek bo'lsa, undan foydalanishni taklif qilish
    if cashback_bal > 0:
        await state.set_state(CreateDeal.use_cashback)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Ha, {fmt(min(cashback_bal, fee or round(amount*0.1)))} so'm chegirma olish", callback_data="cb_use:yes")],
            [InlineKeyboardButton(text="❌ Yo'q, to'liq to'layman", callback_data="cb_use:no")]
        ])
        await msg.answer(
            f"💰 <b>SIZDA {fmt(cashback_bal)} SO'M KESHBEK MAVJUD!</b>\n\n"
            f"Ushbu savdoda keshbekingizni chegirma sifatida ishlatishni xohlaysizmi?",
            reply_markup=kb
        )
        return

    # Keshbek yo'q bo'lsa, to'g'ridan-to'g'ri yaratish
    await finalize_deal_creation(msg, state, uid, cashback_to_use=0)

@router.callback_query(F.data.startswith("cb_use:"))
async def cb_use_handler(call: CallbackQuery, state: FSMContext):
    await call.answer()
    use_it = call.data.split(":")[1] == "yes"
    data = await state.get_data()
    amount = data.get("amount", 0)
    fee = data.get("fee", 0)
    
    uid = call.from_user.id
    user = get_user(uid)
    cashback_bal = user["cashback_balance"] if user else 0
    
    discount = min(cashback_bal, fee or round(amount * 0.1)) if use_it else 0
    await finalize_deal_creation(call.message, state, uid, cashback_to_use=discount)

async def finalize_deal_creation(msg_target: Message, state: FSMContext, uid: int, cashback_to_use: int = 0):
    data = await state.get_data()
    await state.clear()
    
    amount = data["amount"]
    title = data["title"]
    seller_tag = data["seller"]
    fee = data.get("fee", 0)
    
    total_to_pay = max(1000, (amount + fee) - cashback_to_use)
    cashback_earned = round(amount * 0.10) # 10% keshbek
    
    deal_id = generate_deal_id()
    now_s = datetime.now(TZ).isoformat()
    
    bot_info = await bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start=deal_{deal_id}"
    
    q("""
    INSERT INTO deals (deal_id, buyer_id, buyer_username, seller_username, title, amount, fee, cashback_used, total_to_pay, cashback_earned, status, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?)
    """, (deal_id, uid, msg_target.chat.username or "", seller_tag, title, amount, fee, cashback_to_use, total_to_pay, cashback_earned, now_s, now_s), commit=True)
    
    fee_text = "🎁 0% (1-savdo MUTLAQO BEPUL!)" if fee == 0 else f"{fmt(fee)} so'm"
    discount_text = f"\n💰 <b>Keshbek chegirmasi:</b> -{fmt(cashback_to_use)} so'm" if cashback_to_use > 0 else ""
    
    deal_card = (
        f"✅ <b>XAVFSIZ BITIM MUVAFFAQIYATLI YARATILDI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Bitim raqami:</b> <code>#{deal_id}</code>\n"
        f"📦 <b>Tovar/Xizmat:</b> <b>{esc_html(title)}</b>\n"
        f"💵 <b>Tovar narxi:</b> {fmt(amount)} so'm\n"
        f"💼 <b>Kafil xizmat haqi:</b> {fee_text}{discount_text}\n"
        f"💰 <b>Jami to'lov:</b> <b>{fmt(total_to_pay)} so'm</b>\n"
        f"🎁 <b>Keshbek (savdo yakunida beriladi):</b> +{fmt(cashback_earned)} so'm\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔗 <b>SOTUVCHINI SAVDOGA TAKLIF QILISH HAVOLASI:</b>\n"
        f"👉 <code>{invite_link}</code>\n\n"
        f"Ushbu havolani sotuvchiga yuboring. Sotuvchi savdoga qo'shilishi bilanoq sizga to'lov rekvizitlari yuboriladi."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Havolani ulashish", url=f"https://t.me/share/url?url={invite_link}&text=Xavfsiz%20savdoga%20qo'shiling")],
        [InlineKeyboardButton(text="❌ Savdoni bekor qilish", callback_data=f"cancel_deal:{deal_id}")]
    ])
    await msg_target.answer(deal_card, reply_markup=kb)

# ============================ 2. KESHBEK BALANSI VA PROFIL ============================
@router.message(F.text == "💰 Keshbek balansim")
async def view_cashback(msg: Message):
    uid = msg.from_user.id
    ensure_user(uid, msg.from_user.username, msg.from_user.full_name)
    u = get_user(uid)
    
    bal = u["cashback_balance"] if u else 0
    completed = u["completed_deals"] if u else 0
    
    text = (
        f"💎 <b>SIZNING KESHBEK BALANSINGIZ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Mavjud keshbek:</b> <b>{fmt(bal)} so'm</b>\n"
        f"🏆 <b>Muvaffaqiyatli savdolaringiz:</b> {completed} ta\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 <b>Keshbek tizimi qanday ishlaydi?</b>\n"
        f"• Har bir muvaffaqiyatli yakunlangan savdodan so'ng savdo summasining <b>10% miqdori</b> keshbek sifatida hisobingizga qo'shiladi.\n"
        f"• Keshbekni keyingi savdolaringizda <b>chegirma</b> sifatida 100% ishlatishingiz mumkin!\n"
        f"• 1-savdo esa kafil komissiyasisiz — <b>MUTLAQO BEPUL</b>."
    )
    await msg.answer(text, reply_markup=main_menu_keyboard(uid))

@router.message(F.text == "👤 Profilim")
async def view_profile(msg: Message):
    uid = msg.from_user.id
    ensure_user(uid, msg.from_user.username, msg.from_user.full_name)
    u = get_user(uid)
    
    status_badge = "🌟 Faol foydalanuvchi" if not u["is_banned"] else "🚫 Bloklangan"
    text = (
        f"👤 <b>SHAXSIY PROFILINGIZ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Telegram ID:</b> <code>{uid}</code>\n"
        f"👤 <b>F.I.O:</b> {esc_html(u['full_name'])}\n"
        f"🏷 <b>Username:</b> @{esc_html(u['username'] or 'mavjud emas')}\n"
        f"🛡 <b>Holat:</b> {status_badge}\n"
        f"📦 <b>Yakunlangan savdolar:</b> {u['completed_deals']} ta\n"
        f"💰 <b>Keshbek balansi:</b> <b>{fmt(u['cashback_balance'])} so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<i>Kafil boti orqali savdolaringiz doimo xavfsiz himoyalangan!</i>"
    )
    await msg.answer(text, reply_markup=main_menu_keyboard(uid))

@router.message(F.text == "🆘 Nizo / Yordam")
async def support_help_handler(msg: Message):
    uid = msg.from_user.id
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else 6125160021
    text = (
        "🆘 <b>YORDAM VA XAVFSIZLIK MARKAZI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛡 <b>Kafil / Garant boti qanday ishlaydi?</b>\n"
        "1. <b>Bitim yaratish:</b> Xaridor «🛒 Yangi bitim yaratish» tugmasi orqali tovar nomi va narxini kiritadi.\n"
        "2. <b>Sotuvchini ulash:</b> Havola sotuvchiga yuboriladi. Sotuvchi start bosib savdoga ulanadi.\n"
        "3. <b>Xavfsiz to'lov:</b> Xaridor kafil rekvizitlariga to'lov qiladi. TolovAPI pulni bir necha soniyada aniqlaydi va mablag' <b>in_trade (muzlatilgan)</b> holatiga o'tadi.\n"
        "4. <b>Tovarni topshirish:</b> Sotuvchi tovarni topshiradi. Tomonlar bot ichidagi xavfsiz chat orqali gaplashadi.\n"
        "5. <b>Yakunlash:</b> Xaridor tovarni tekshirib olgach «✅ Tovarni to'liq tekshirdim va qabul qildim» tugmasini bosadi va mablag' sotuvchiga beriladi.\n\n"
        "🎁 <b>Imtiyoz va Keshbek:</b>\n"
        "• <b>1-savdo:</b> 0% xizmat haqi — MUTLAQO TEKIN!\n"
        "• <b>Keyingi savdolar:</b> Har bir yakunlangan savdodan so'ng xaridorga 10% keshbek beriladi va keyingi savdolarda chegirma sifatida ishlatiladi.\n\n"
        "🚨 <b>Nizo (Dispute) va Kafolat:</b>\n"
        "Agar sotuvchi tovarni bermasa, noto'g'ri bersa yoki aldamoqchi bo'lsa, xaridor darhol «🚨 Nizo ochish» tugmasini bosadi. Savdo to'xtatilib, xaridorning kartasiga pul qaytarish (Refund) so'rovi adminga tushadi.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📞 <b>Garant admin:</b> <a href=\"tg://user?id={admin_id}\">Admin bilan bog'lanish</a>"
    )
    await msg.answer(text, reply_markup=main_menu_keyboard(uid), disable_web_page_preview=True)

# ============================ 3. MENING BITIMLARIM ============================
@router.message(F.text == "📂 Mening bitimlarim")
async def my_deals_list(msg: Message):
    uid = msg.from_user.id
    deals_rows = q("SELECT * FROM deals WHERE buyer_id=? OR seller_id=? ORDER BY updated_at DESC LIMIT 10", (uid, uid))
    if not deals_rows:
        await msg.answer("Sizda hali hech qanday bitimlar mavjud emas. Yangi bitim yaratish uchun '🛒 Yangi bitim yaratish' tugmasini bosing.", reply_markup=main_menu_keyboard(uid))
        return

    text = "📂 <b>OXIRGI SAVDOLARINGIZ:</b>\n\n"
    kb = []
    status_map = {
        "created": "🆕 Yaratildi (Sotuvchi kutilmoqda)",
        "waiting_payment": "💳 To'lov kutilmoqda",
        "in_trade": "🔒 Muzlatilgan (Tovar topshirilmoqda)",
        "completed": "✅ Yakunlandi",
        "dispute": "🚨 Nizo holatida",
        "cancelled": "❌ Bekor qilindi",
        "refunded": "↩️ Qaytarildi"
    }
    for d in deals_rows:
        role = "Xaridor" if d["buyer_id"] == uid else "Sotuvchi"
        st = status_map.get(d["status"], d["status"])
        text += (
            f"🆔 <b>#{d['deal_id']}</b> | {st}\n"
            f"📦 {esc_html(d['title'])} — <b>{fmt(d['amount'])} so'm</b> ({role})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        if d["status"] in ("in_trade", "waiting_payment", "dispute"):
            kb.append([InlineKeyboardButton(text=f"Ko'rish: #{d['deal_id']}", callback_data=f"view_deal:{d['deal_id']}")])

    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)

@router.callback_query(F.data.startswith("view_deal:"))
async def view_deal_callback(call: CallbackQuery):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
    if not deal:
        await call.message.answer("Bitim topilmadi.")
        return
        
    uid = call.from_user.id
    role = "Xaridor" if deal["buyer_id"] == uid else "Sotuvchi"
    
    text = (
        f"📄 <b>BITIM TAFSILOTLARI:</b> <code>#{deal['deal_id']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Sizning rolingiz:</b> {role}\n"
        f"📦 <b>Tovar:</b> {esc_html(deal['title'])}\n"
        f"💵 <b>Tovar summasi:</b> {fmt(deal['amount'])} so'm\n"
        f"💰 <b>Jami to'lov:</b> {fmt(deal['total_to_pay'])} so'm\n"
        f"🔒 <b>Holat:</b> <b>{deal['status'].upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    
    btns = []
    if deal["status"] == "in_trade":
        if deal["buyer_id"] == uid:
            btns.append([InlineKeyboardButton(text="✅ Tovarni to'liq tekshirdim va qabul qildim", callback_data=f"confirm_deal:{d_id}")])
        btns.append([InlineKeyboardButton(text="🚨 Nizo ochish (Shikoyat)", callback_data=f"dispute_deal:{d_id}")])
    elif deal["status"] == "created" and deal["buyer_id"] == uid:
        btns.append([InlineKeyboardButton(text="❌ Savdoni bekor qilish", callback_data=f"cancel_deal:{d_id}")])
        
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns) if btns else None)

# ============================ 4. BITIMNI YAKUNLASH (XARIDOR TASDIG'I) ============================
@router.callback_query(F.data.startswith("confirm_deal:"))
async def confirm_deal_handler(call: CallbackQuery):
    d_id = call.data.split(":")[1]
    deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
    if not deal or deal["status"] != "in_trade":
        await call.answer("Bitim allaqachon yakunlangan yoki nizo holatida.", show_alert=True)
        return
        
    if deal["buyer_id"] != call.from_user.id:
        await call.answer("Faqat xaridor tovarni qabul qilganini tasdiqlashi mumkin!", show_alert=True)
        return

    # Tasdiqlash dialogi
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ HA, to'liq tekshirdim (Pul sotuvchiga o'tsin)", callback_data=f"final_complete:{d_id}")],
        [InlineKeyboardButton(text="❌ Orqaga", callback_data=f"view_deal:{d_id}")]
    ])
    await call.message.edit_text(
        f"⚠️ <b>DIQQAT: DIQQAT BILAN TEKSHIRING!</b>\n\n"
        f"Siz haqiqatan ham <b>«{esc_html(deal['title'])}»</b> tovarini to'liq tekshirib oldingizmi?\n"
        f"Tasdiqlaganingizdan so'ng muzlatilgan <b>{fmt(deal['amount'])} so'm</b> pul sotuvchiga o'tkaziladi va orqaga qaytarib bo'lmaydi!",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("final_complete:"))
async def execute_complete_deal(call: CallbackQuery):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
    if not deal or deal["status"] != "in_trade":
        await call.message.answer("Bitim holati o'zgargan.")
        return

    now_s = datetime.now(TZ).isoformat()
    q("UPDATE deals SET status='completed', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)
    
    # 1. Xaridorga 10% keshbek hisoblash va Completed Deals ni oshirish
    buyer = get_user(deal["buyer_id"])
    earned = deal["cashback_earned"]
    used = deal["cashback_used"]
    new_cb = max(0, (buyer["cashback_balance"] - used) + earned)
    new_completed = buyer["completed_deals"] + 1
    
    q("UPDATE users SET completed_deals=?, cashback_balance=? WHERE user_id=?", (new_completed, new_cb, deal["buyer_id"]), commit=True)
    
    # 2. Sotuvchining Completed Deals ini ham oshirish
    if deal["seller_id"]:
        q("UPDATE users SET completed_deals=completed_deals+1 WHERE user_id=?", (deal["seller_id"],), commit=True)

    # Xaridorga xabar
    await call.message.edit_text(
        f"🎉 <b>SAVDO MUVAFFAQIYATLI YAKUNLANDI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Bitim:</b> <code>#{d_id}</code>\n"
        f"📦 <b>Tovar:</b> {esc_html(deal['title'])}\n"
        f"💵 <b>To'langan summa:</b> {fmt(deal['total_to_pay'])} so'm\n"
        f"🎁 <b>Sizga berilgan 10% keshbek:</b> <b>+{fmt(earned)} so'm</b>\n"
        f"💰 <b>Joriy keshbek balansingiz:</b> <b>{fmt(new_cb)} so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<i>Xavfsiz savdo uchun Kafil botidan foydalanganingizdan minnatdormiz!</i>"
    )
    
    # Sotuvchiga xabar
    if deal["seller_id"]:
        await bot.send_message(
            deal["seller_id"],
            f"🎉 <b>XARIDOR TOVARNI QABUL QILDI! SAVDO YAKUNLANDI.</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Bitim:</b> <code>#{d_id}</code>\n"
            f"💵 <b>Sizga o'tkaziladigan toza summa:</b> <b>{fmt(deal['amount'])} so'm</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💳 <i>Pul tez orada kafil tomonidan kartangizga o'tkazib beriladi.</i>"
        )
        
    # Adminga xabar (Sotuvchiga pul tashlab berish buyrug'i)
    for a_id in ADMIN_IDS:
        try:
            await bot.send_message(
                a_id,
                f"✅ <b>SAVDO MUVAFFAQIYATLI YAKUNLANDI! (#{d_id})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🛒 <b>Xaridor:</b> ID <code>{deal['buyer_id']}</code>\n"
                f"💼 <b>Sotuvchi:</b> ID <code>{deal['seller_id']}</code> (@{deal['seller_username']})\n"
                f"💵 <b>Sotuvchiga to'lanadigan summa:</b> <b>{fmt(deal['amount'])} so'm</b>\n"
                f"💼 <b>Admin komissiyasi:</b> {fmt(deal['fee'])} so'm\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>Iltimos, sotuvchiga uning {fmt(deal['amount'])} so'm pulini o'tkazib bering.</i>"
            )
        except Exception:
            pass

# ============================ 5. NIZO VA REFUND TIZIMI ============================
@router.callback_query(F.data.startswith("dispute_deal:"))
async def open_dispute_handler(call: CallbackQuery, state: FSMContext):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
    if not deal or deal["status"] not in ("in_trade", "waiting_payment"):
        await call.message.answer("Ushbu bitimda hozir nizo ochib bo'lmaydi.")
        return

    now_s = datetime.now(TZ).isoformat()
    q("UPDATE deals SET status='dispute', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)
    
    await state.set_state(RefundProcess.card_number)
    await state.update_data(dispute_deal_id=d_id)
    
    await call.message.answer(
        f"🚨 <b>NIZO OCHILDI! SAVDO TO'XTATILDI. (#{d_id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Muzlatilgan mablag'ni xaridorga qaytarish (Refund) arizasini shakllantirish uchun:\n\n"
        f"💳 <b>16 xonali plastik karta raqamingizni kiriting:</b>\n"
        f"<i>(Masalan: 8600 1234 5678 9012)</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(RefundProcess.card_number)
async def process_refund_card(msg: Message, state: FSMContext):
    card_clean = re.sub(r"\s+", "", msg.text.strip())
    if not card_clean.isdigit() or len(card_clean) != 16:
        await msg.answer("⚠️ Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qaytadan kiriting:")
        return
        
    await state.update_data(card_number=card_clean)
    await state.set_state(RefundProcess.card_owner)
    await msg.answer("👤 Karta egasining <b>Ism va Familiyasini</b> kiriting (masalan: ALISHER VALIYEV):")

@router.message(RefundProcess.card_owner)
async def process_refund_owner(msg: Message, state: FSMContext):
    owner = msg.text.strip().upper()
    if len(owner) < 3:
        await msg.answer("Iltimos, ism-familiyani to'liq kiriting:")
        return
        
    await state.update_data(card_owner=owner)
    await state.set_state(RefundProcess.reason)
    await msg.answer("📝 Nizo sababini yozing (Sotuvchi qanday qoidani buzdi yoki tovar nega berilmadi?):")

@router.message(RefundProcess.reason)
async def process_refund_reason(msg: Message, state: FSMContext):
    reason = msg.text.strip()
    data = await state.get_data()
    await state.clear()
    
    d_id = data["dispute_deal_id"]
    card_num = data["card_number"]
    card_own = data["card_owner"]
    
    deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
    now_s = datetime.now(TZ).isoformat()
    refund_amount = deal["total_to_pay"] if deal else 0
    
    ref_id = q("""
    INSERT INTO refunds (deal_id, buyer_id, buyer_name, card_number, card_owner, amount, reason, status, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (d_id, msg.from_user.id, msg.from_user.full_name, card_num, card_own, refund_amount, reason, now_s), commit=True)
    
    # Savdo yozishmalarini yig'ish
    logs = q("SELECT * FROM deal_messages WHERE deal_id=? ORDER BY id ASC LIMIT 50", (d_id,))
    chat_history = ""
    if logs:
        for l in logs:
            chat_history += f"<b>{esc_html(l['sender_tag'])}:</b> {esc_html(l['message_text'])}\n"
    else:
        chat_history = "<i>(Chatda yozishmalar mavjud emas)</i>\n"

    await msg.answer(
        f"✅ <b>NIZO VA TO'LOVNI QAYTARISH ARIZASI QABUL QILINDI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Ariza ID: <code>#{ref_id}</code>\n"
        f"💳 Qaytariladigan karta: <code>{card_num}</code> ({card_own})\n"
        f"💰 Qaytariladigan summa: <b>{fmt(refund_amount)} so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Admin arizani va tomonlarning yozishmalarini tekshirib chiqib, pulni kartangizga qaytaradi.",
        reply_markup=main_menu_keyboard(msg.from_user.id)
    )
    
    # Adminga batafsil bildirishnoma
    admin_alert = (
        f"🚨 <b>YANGI NIZO VA REFUND SO'ROVI! (Bitim #{d_id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Ariza raqami: <code>#{ref_id}</code>\n"
        f"🛒 Xaridor: ID <code>{msg.from_user.id}</code> ({esc_html(msg.from_user.full_name)})\n"
        f"💼 Sotuvchi: ID <code>{deal['seller_id']}</code> (@{esc_html(deal['seller_username'])})\n"
        f"💵 Tovar summasi: {fmt(deal['amount'])} so'm\n"
        f"💰 Qaytarish talab qilingan: <b>{fmt(refund_amount)} so'm</b>\n"
        f"💳 Xaridor kartasi: <code>{card_num}</code> ({card_own})\n"
        f"📝 Sabab: <i>{esc_html(reason)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💬 <b>TOMONLARNING CHAT YOZISHMALARI:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{chat_history}"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↩️ Xaridorga qaytarish ({fmt(refund_amount)} so'm)", callback_data=f"adm_refund:{ref_id}")],
        [InlineKeyboardButton(text=f"➡️ Sotuvchiga o'tkazish ({fmt(deal['amount'])} so'm)", callback_data=f"adm_release:{d_id}")]
    ])
    
    for a_id in ADMIN_IDS:
        try:
            await bot.send_message(a_id, admin_alert, reply_markup=admin_kb)
        except Exception:
            pass

# ============================ 6. SAVDO CHATI (XAVFSIZ YOZISHMA) ============================
@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_([
    "🛒 Yangi bitim yaratish", "📂 Mening bitimlarim", "💰 Keshbek balansim",
    "👤 Profilim", "🆘 Nizo / Yordam", "👑 Admin panel", "❌ Bekor qilish"
]))
async def trade_chat_router(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    uid = msg.from_user.id
    # Foydalanuvchining ayni damdagi faol 'in_trade' yoki 'dispute' savdosi bormi?
    deal = q("SELECT * FROM deals WHERE (buyer_id=? OR seller_id=?) AND status IN ('in_trade', 'dispute') ORDER BY updated_at DESC LIMIT 1", (uid, uid), one=True)
    if not deal:
        return

    is_buyer = (deal["buyer_id"] == uid)
    receiver_id = deal["seller_id"] if is_buyer else deal["buyer_id"]
    role = "Xaridor" if is_buyer else "Sotuvchi"
    sender_tag = f"@{msg.from_user.username}" if msg.from_user.username else f"{role} ({uid})"
    
    # Yozishmani bazada saqlash
    now_s = datetime.now(TZ).isoformat()
    q("INSERT INTO deal_messages (deal_id, sender_id, sender_tag, role, message_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      (deal["deal_id"], uid, sender_tag, role, msg.text, now_s), commit=True)
      
    if receiver_id:
        try:
            await bot.send_message(
                receiver_id,
                f"💬 <b>{role} ({esc_html(sender_tag)}):</b>\n{esc_html(msg.text)}"
            )
        except Exception as e_send:
            logging.warning(f"Savdo xabarini yetkazishda xato: {e_send}")

# ============================ 7. ADMIN PANEL ============================
@router.message(F.text == "👑 Admin panel")
@router.message(Command("admin"))
async def admin_panel_handler(msg: Message):
    if not is_admin(msg.from_user.id):
        return
        
    total_users = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    active_deals = q("SELECT COUNT(*) c FROM deals WHERE status IN ('in_trade', 'waiting_payment')", one=True)["c"]
    disputes = q("SELECT COUNT(*) c FROM deals WHERE status='dispute'", one=True)["c"]
    pending_refunds = q("SELECT COUNT(*) c FROM refunds WHERE status='pending'", one=True)["c"]
    
    current_card = get_setting("card_number", DEFAULT_CARD_NUMBER)
    current_owner = get_setting("card_owner", DEFAULT_CARD_OWNER)
    comm_pct = get_setting("commission_percent", "10")
    
    text = (
        f"👑 <b>KAFIL BOTI ADMIN BOSHQARUV PANELI</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Foydalanuvchilar:</b> {total_users} ta\n"
        f"🔄 <b>Faol bitimlar:</b> {active_deals} ta\n"
        f"🚨 <b>Ochiq nizolar:</b> {disputes} ta\n"
        f"↩️ <b>Kutilayotgan refundlar:</b> {pending_refunds} ta\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Karta:</b> <code>{current_card}</code> ({current_owner})\n"
        f"💼 <b>Komissiya foizi:</b> {comm_pct}%\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Karta ma'lumotlarini o'zgartirish", callback_data="adm_change_card")],
        [InlineKeyboardButton(text="💼 Komissiya foizini o'zgartirish", callback_data="adm_change_percent")],
        [InlineKeyboardButton(text="🚨 Ochiq nizolarni ko'rish", callback_data="adm_view_disputes")]
    ])
    await msg.answer(text, reply_markup=kb)

@router.callback_query(F.data == "adm_change_card")
async def adm_change_card_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    await state.set_state(AdminChangeCard.card)
    await call.message.answer("Yangi 16 xonali karta raqamini kiriting:")

@router.message(AdminChangeCard.card)
async def adm_process_card(msg: Message, state: FSMContext):
    card = re.sub(r"\s+", "", msg.text.strip())
    await state.update_data(new_card=card)
    await state.set_state(AdminChangeCard.owner)
    await msg.answer("Karta egasining Ism-Familiyasini kiriting:")

@router.message(AdminChangeCard.owner)
async def adm_process_owner(msg: Message, state: FSMContext):
    owner = msg.text.strip().upper()
    data = await state.get_data()
    await state.clear()
    
    set_setting("card_number", data["new_card"])
    set_setting("card_owner", owner)
    await msg.answer(f"✅ Karta muvaffaqiyatli yangilandi:\n<code>{data['new_card']}</code> ({owner})")

@router.callback_query(F.data == "adm_change_percent")
async def adm_change_pct_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    await state.set_state(AdminChangePercent.percent)
    await call.message.answer("Yangi komissiya foizini kiriting (masalan: 10):")

@router.message(AdminChangePercent.percent)
async def adm_process_pct(msg: Message, state: FSMContext):
    txt = msg.text.strip()
    if not txt.isdigit():
        await msg.answer("Faqat butun son kiriting:")
        return
    set_setting("commission_percent", txt)
    await state.clear()
    await msg.answer(f"✅ Komissiya foizi {txt}% ga o'zgartirildi.")

@router.callback_query(F.data.startswith("adm_refund:"))
async def adm_resolve_refund(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    ref_id = int(call.data.split(":")[1])
    ref = q("SELECT * FROM refunds WHERE id=?", (ref_id,), one=True)
    if not ref:
        await call.message.answer("Ariza topilmadi.")
        return
        
    q("UPDATE refunds SET status='approved' WHERE id=?", (ref_id,), commit=True)
    q("UPDATE deals SET status='refunded' WHERE deal_id=?", (ref["deal_id"],), commit=True)
    
    await call.message.edit_text(
        f"✅ <b>REFUND TASDIQLANDI! (#Ariza {ref_id})</b>\n"
        f"Bitim: <code>#{ref['deal_id']}</code>\n"
        f"Karta: <code>{ref['card_number']}</code> ({ref['card_owner']})\n"
        f"Summa: <b>{fmt(ref['amount'])} so'm</b> xaridorga qaytarildi."
    )
    
    try:
        await bot.send_message(
            ref["buyer_id"],
            f"✅ <b>PULINGIZ QAYTARILDI!</b>\n"
            f"Bitim: <code>#{ref['deal_id']}</code>\n"
            f"Summa: <b>{fmt(ref['amount'])} so'm</b> <code>{ref['card_number']}</code> kartangizga qaytarildi."
        )
    except Exception:
        pass

    deal_info = q("SELECT * FROM deals WHERE deal_id=?", (ref["deal_id"],), one=True)
    if deal_info and deal_info.get("seller_id"):
        try:
            await bot.send_message(
                deal_info["seller_id"],
                f"ℹ️ <b>NIZO YAKUNI: #{ref['deal_id']}</b>\n"
                f"Nizo xaridor foydasiga hal qilindi va mablag' xaridorga qaytarildi."
            )
        except Exception:
            pass

@router.callback_query(F.data == "adm_view_disputes")
async def adm_view_disputes_handler(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    disputes = q("SELECT * FROM deals WHERE status='dispute' ORDER BY updated_at DESC LIMIT 10")
    if not disputes:
        await call.message.answer("✅ Hozirda hech qanday ochiq nizolar mavjud emas.")
        return
    
    text = "🚨 <b>OCHIQ NIZOLAR RO'YXATI:</b>\n\n"
    kb = []
    for d in disputes:
        text += (
            f"🆔 <b>#{d['deal_id']}</b> | <b>{fmt(d['amount'])} so'm</b>\n"
            f"📦 {esc_html(d['title'])}\n"
            f"🛒 Xaridor: <code>{d['buyer_id']}</code> (@{esc_html(d['buyer_username'] or 'yo`q')})\n"
            f"💼 Sotuvchi: <code>{d['seller_id']}</code> (@{esc_html(d['seller_username'] or 'yo`q')})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        kb.append([InlineKeyboardButton(text=f"Ko'rish: #{d['deal_id']}", callback_data=f"view_deal:{d['deal_id']}")])
    
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("adm_release:"))
async def adm_release_handler(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
    if not deal:
        await call.message.answer("Bitim topilmadi.")
        return
    if deal["status"] != "dispute":
        await call.message.answer(f"Bu bitim nizo holatida emas (Holat: {deal['status']}).")
        return
        
    now_s = datetime.now(TZ).isoformat()
    q("UPDATE deals SET status='completed', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)
    q("UPDATE refunds SET status='rejected' WHERE deal_id=?", (d_id,), commit=True)
    
    buyer = get_user(deal["buyer_id"])
    earned = deal["cashback_earned"]
    used = deal["cashback_used"]
    new_cb = max(0, (buyer["cashback_balance"] - used) + earned)
    q("UPDATE users SET completed_deals=completed_deals+1, cashback_balance=? WHERE user_id=?", (new_cb, deal["buyer_id"]), commit=True)
    if deal["seller_id"]:
        q("UPDATE users SET completed_deals=completed_deals+1 WHERE user_id=?", (deal["seller_id"],), commit=True)
        
    await call.message.edit_text(
        f"✅ <b>NIZO SOTUVCHI FOYDASIGA HAL QILINDI!</b>\n"
        f"🆔 <b>Bitim:</b> <code>#{d_id}</code>\n"
        f"💼 <b>Sotuvchi:</b> ID <code>{deal['seller_id']}</code> (@{esc_html(deal['seller_username'] or '')})\n"
        f"💵 <b>O'tkaziladigan summa:</b> <b>{fmt(deal['amount'])} so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Iltimos, sotuvchining kartasiga {fmt(deal['amount'])} so'm mablag'ni o'tkazib bering.</i>"
    )
    
    if deal["seller_id"]:
        try:
            await bot.send_message(
                deal["seller_id"],
                f"🎉 <b>XUSHXABAR: Nizo sizning foydangizga hal qilindi!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>Bitim:</b> <code>#{d_id}</code>\n"
                f"💵 <b>To'lanadigan summa:</b> <b>{fmt(deal['amount'])} so'm</b>\n\n"
                f"Kafil tez orada ushbu mablag'ni kartangizga o'tkazib beradi."
            )
        except Exception:
            pass
            
    try:
        await bot.send_message(
            deal["buyer_id"],
            f"ℹ️ <b>NIZO YAKUNI: #{d_id}</b>\n"
            f"Admin tomonidan taqdim etilgan dalillar va yozishmalar ko'rib chiqildi hamda nizo sotuvchi foydasiga hal qilindi."
        )
    except Exception:
        pass

@router.callback_query(F.data.startswith("cancel_deal:"))
async def cancel_deal_handler(call: CallbackQuery):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = q("SELECT * FROM deals WHERE deal_id=?", (d_id,), one=True)
    if not deal:
        await call.message.answer("Bitim topilmadi.")
        return
    if deal["status"] not in ("created", "waiting_seller", "waiting_payment"):
        await call.message.answer("Bu savdoni bekor qilib bo'lmaydi (Pul kafil hisobida).")
        return
        
    now_s = datetime.now(TZ).isoformat()
    q("UPDATE deals SET status='cancelled', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)
    if deal.get("payment_id"):
        asyncio.create_task(cancel_payment_async(deal["payment_id"]))
        
    await call.message.edit_text(f"❌ <b>#{d_id}</b> raqamli bitim bekor qilindi.")
    
    other_id = deal["seller_id"] if call.from_user.id == deal["buyer_id"] else deal["buyer_id"]
    if other_id:
        try:
            await bot.send_message(other_id, f"❌ <b>#{d_id}</b> raqamli bitim ikkinchi tomon tomonidan bekor qilindi.")
        except Exception:
            pass

# ============================ RENDER DUMMY WEB SERVER ============================
async def run_render_web_server():
    try:
        async def handle_ping(request):
            return web.Response(text="OK: Kafil Escrow Bot is running smoothly on Render!\n")

        app = web.Application()
        app.router.add_get("/", handle_ping)
        app.router.add_get("/health", handle_ping)
        app.router.add_get("/ping", handle_ping)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logging.info(f"Render HTTP server '0.0.0.0':{PORT} manzilida muvaffaqiyatli ishga tushdi.")
    except Exception as e:
        logging.warning(f"Render HTTP server ishga tushirishda xato: {e}")

# ============================ ISHGA TUSHIRISH ============================
async def main():
    await run_render_web_server()
    logging.info(f"Kafil / Garant boti ishga tushdi! Adminlar: {ADMIN_IDS}")
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            if "Unauthorized" in str(e):
                logging.critical("Bot tokeni yaroqsiz! Telegram @BotFather orqali tekshiring.")
                break
            logging.error(f"Ulanishda uzilish: {e}. 5 soniyadan so'ng qayta ulanadi...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
