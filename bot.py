"""
BOT.PY - Kafil / Garant (Escrow Middleman) Telegram Boti
Aiogram 3.x asosida to'liq, xatosiz, modulli arxitektura.
"""
import os
import sys
import re
import json
import random
import string
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    BufferedInputFile,
    ChatMemberUpdated
)

from config import (
    BOT_TOKEN, ADMIN_IDS, DEFAULT_CARD_NUMBER, DEFAULT_CARD_OWNER,
    TOLOV_API_URL, SHOP_ID, SHOP_KEY, PORT, TZ, esc_html, fmt, is_admin
)
import database as db
from ai_helper import ask_gemini_ai, check_chat_safety, analyze_deals_for_admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("kafil_bot")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ============================ 1. UNIKAL KOD GENERATORLARI ============================
def generate_deal_id() -> str:
    chars = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = "K" + "".join(random.choice(chars) for _ in range(6))
        exists = db.get_deal(code)
        if not exists:
            return code
    return "K" + str(random.randint(100000, 999999))

def generate_group_code() -> str:
    return "KAFIL-" + "".join(random.choice("0123456789ABCDEF") for _ in range(5))

# ============================ 2. ASOSIY MENYU TUGMALARI ============================
def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="📜 Tarix"), KeyboardButton(text="🆘 SOS (Yordam)")],
        [KeyboardButton(text="👥 Referal Tizimi"), KeyboardButton(text="🤝 Yangi Savdo Ochish")],
        [KeyboardButton(text="🚫 Firibgarlar Ro'yxati"), KeyboardButton(text="💾 Zaxira Nusxa (Backup)")],
        [KeyboardButton(text="📑 Arizalarim")]
    ]
    if is_admin(user_id):
        kb.append([KeyboardButton(text="👑 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# ============================ 3. FSM HOLATLARI ============================
class CreateDealFSM(StatesGroup):
    mode = State()
    title = State()
    seller = State()
    amount = State()
    group_link = State()

class SOSFSM(StatesGroup):
    ai_query = State()
    complaint_seller = State()
    complaint_reason = State()

class WithdrawFSM(StatesGroup):
    card_number = State()
    card_owner = State()

class BackupFSM(StatesGroup):
    upload_file = State()

class AdminFSM(StatesGroup):
    broadcast = State()
    add_black_user = State()
    add_black_reason = State()
    add_channel_id = State()
    add_channel_title = State()
    add_channel_link = State()
    change_card = State()
    change_owner = State()
    change_commission = State()
    ai_chat = State()
    reply_ticket_id = State()
    reply_ticket_text = State()

# ============================ 4. TOLOVAPI.UZ INTEGRATSIYASI ============================
async def create_payment_async(amount: int, order_id: Optional[str] = None, user_id: Optional[int] = None) -> Optional[dict]:
    url = f"{TOLOV_API_URL}payment/create"
    headers = {"Content-Type": "application/json"}
    payload = {
        "shop_id": SHOP_ID,
        "shop_key": SHOP_KEY,
        "amount": amount
    }
    if order_id:
        payload["order_id"] = str(order_id)
    if user_id:
        payload["user_id"] = str(user_id)

    try:
        timeout = ClientTimeout(total=15)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("status") == "success":
                    return {
                        "ok": True,
                        "payment_id": str(data.get("payment_id", "")),
                        "amount": int(data.get("amount", amount)),
                        "payment_url": data.get("payment_url", ""),
                        "raw": data
                    }
                elif data.get("error") == "amount_busy":
                    suggested = data.get("suggested_amount")
                    if suggested and int(suggested) != amount:
                        return await create_payment_async(int(suggested), order_id, user_id)
                logger.warning(f"TolovAPI create xatosi: {data}")
                return None
    except Exception as e:
        logger.error(f"TolovAPI request xatosi: {e}")
        return None

async def check_payment_status_async(payment_id: str) -> Optional[dict]:
    url = f"{TOLOV_API_URL}payment/status"
    headers = {"Content-Type": "application/json"}
    payload = {
        "shop_id": SHOP_ID,
        "shop_key": SHOP_KEY,
        "payment_id": str(payment_id)
    }
    try:
        timeout = ClientTimeout(total=10)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    st = data.get("status", "").lower()
                    return {
                        "ok": True,
                        "paid": st in ("success", "paid", "completed"),
                        "status": st,
                        "raw": data
                    }
                return None
    except Exception as e:
        logger.warning(f"TolovAPI status xatosi: {e}")
        return None

async def cancel_payment_async(payment_id: str) -> bool:
    url = f"{TOLOV_API_URL}payment/cancel"
    headers = {"Content-Type": "application/json"}
    payload = {
        "shop_id": SHOP_ID,
        "shop_key": SHOP_KEY,
        "payment_id": str(payment_id)
    }
    try:
        timeout = ClientTimeout(total=10)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                return resp.status == 200
    except Exception:
        return False

# ============================ 5. MAJBURIY OBUNA VA REFERALLAR ============================
async def check_user_subscribed(user_id: int) -> Tuple[bool, List[dict]]:
    channels = db.q("SELECT * FROM mandatory_channels")
    if not channels:
        return True, []
    unsubscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["channel_id"], user_id=user_id)
            if member.status not in ("creator", "administrator", "member", "restricted"):
                unsubscribed.append(dict(ch))
        except Exception:
            pass
    return (len(unsubscribed) == 0), unsubscribed

async def reward_referral_if_eligible(referred_id: int):
    u = db.get_user(referred_id)
    if not u or not u["referred_by"]:
        return
    ref_row = db.q("SELECT * FROM referrals WHERE referred_id=? AND status='pending'", (referred_id,), one=True)
    if not ref_row:
        return
        
    referrer_id = u["referred_by"]
    today_prefix = datetime.now(TZ).strftime("%Y-%m-%d")
    today_count = db.q("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='rewarded' AND created_at LIKE ?",
                       (referrer_id, f"{today_prefix}%"), one=True)["c"]
                       
    if today_count >= 20:
        logger.info(f"Referrer {referrer_id} kunlik 20 ta limitga yetgan.")
        return
        
    db.q("UPDATE users SET referral_balance=referral_balance+100 WHERE user_id=?", (referrer_id,), commit=True)
    db.q("UPDATE referrals SET status='rewarded' WHERE id=?", (ref_row["id"],), commit=True)
    
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 <b>YANGI REFERAL MUKOFOTI!</b>\n"
            f"Siz taklif qilgan foydalanuvchi (@{esc_html(u['username'] or str(referred_id))}) botdan to'liq ro'yxatdan o'tdi.\n"
            f"💰 Hisobingizga <b>+100 so'm</b> qo'shildi!\n"
            f"Bugun qo'shgan referallaringiz: {today_count + 1}/20 ta"
        )
    except Exception:
        pass

async def penalize_referral_if_left(user_id: int):
    ref = db.q("SELECT * FROM referrals WHERE referred_id=? AND status='rewarded'", (user_id,), one=True)
    if not ref:
        return
    referrer_id = ref["referrer_id"]
    referrer = db.get_user(referrer_id)
    if not referrer:
        return
        
    # Foydalanuvchining balansi manfiyga kirishiga ham ruxsat beriladi
    new_bal = referrer["referral_balance"] - 200
    db.q("UPDATE users SET referral_balance=? WHERE user_id=?", (new_bal, referrer_id), commit=True)
    db.q("UPDATE referrals SET status='penalized' WHERE id=?", (ref["id"],), commit=True)
    
    try:
        left_u = db.get_user(user_id)
        tag = f"@{left_u['username']}" if left_u and left_u['username'] else f"ID {user_id}"
        await bot.send_message(
            referrer_id,
            f"⚠️ <b>REFERAL JARIMASI: -200 so'm!</b>\n"
            f"Siz taklif qilgan {tag} foydalanuvchi kanaldan chiqib ketdi.\n"
            f"Qoidalarga ko'ra referal balansingizdan 200 so'm jarima ayirildi.\n"
            f"Joriy balansingiz: <b>{fmt(new_bal)} so'm</b>"
        )
    except Exception:
        pass

# ============================ 6. ASINXRON FON TAYMERLARI ============================
async def deal_5min_timer(deal_id: str):
    """5 daqiqalik asinxron bekor qilish taymeri."""
    await asyncio.sleep(300)
    deal = db.get_deal(deal_id)
    if deal and deal["status"] == "waiting_payment":
        now_s = datetime.now(TZ).isoformat()
        db.q("UPDATE deals SET status='cancelled', updated_at=? WHERE deal_id=?", (now_s, deal_id), commit=True)
        if deal.get("payment_id"):
            await cancel_payment_async(deal["payment_id"])
        try:
            await bot.send_message(
                deal["buyer_id"],
                f"⏱ <b>5 DAQIQA VAQT TUGADI! (#{deal_id})</b>\n\n"
                f"Belgilangan 5 daqiqa ichida to'lov amalga oshirilmaganligi sababli to'lov avtomatik <b>bekor qilindi</b>."
            )
        except Exception:
            pass

async def complaints_auto_expire_loop():
    """5 kun o'tgach, arizasi hal qilinmagan sotuvchini avtomatik qora ro'yxatga kirituvchi loop."""
    while True:
        try:
            await asyncio.sleep(600)
            now_s = datetime.now(TZ).isoformat()
            expired = db.q("SELECT * FROM complaints WHERE status='pending' AND deadline_at <= ?", (now_s,))
            for c in expired:
                db.q("UPDATE complaints SET status='blacklisted' WHERE id=?", (c["id"],), commit=True)
                db.add_to_blacklist(
                    user_id=c["accused_id"],
                    username=c["accused_username"],
                    reason=f"Ariza #{c['id']} bo'yicha 5 kunda javob bermagan: {c['reason']}",
                    deal_id=c["deal_id"] or "",
                    added_by="complaint_deadline"
                )
                try:
                    await bot.send_message(
                        c["complainant_id"],
                        f"ℹ️ <b>ARIZA YAKUNI: #{c['id']}</b>\n"
                        f"Siz yuborgan ariza bo'yicha 5 kun ichida sotuvchi muammoni hal qilmadi va ariza qaytarib olinmadi.\n\n"
                        f"Biz ushbu foydalanuvchini (@{esc_html(c['accused_username'])}) <b>FIRIBGARLAR (Qora ro'yxat)</b> qatoriga qo'shdik. "
                        f"Endi u boshqa hech kimni alday olmaydi!"
                    )
                except Exception:
                    pass
                if c["accused_id"]:
                    try:
                        await bot.send_message(
                            c["accused_id"],
                            f"🚫 <b>SIZ QORA RO'YXATGA KIRITILDINGIZ!</b>\n"
                            f"Ariza #{c['id']} bo'yicha 5 kunlik muddat tugadi va siz muammoni hal qilmadingiz. "
                            f"Profilingiz butunlay bloklandi."
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Complaints expiry loop xatosi: {e}")

async def referral_leave_checker_loop():
    """Majburiy kanallardan chiqib ketgan referallarni tekshiruvchi fon taymeri."""
    while True:
        try:
            await asyncio.sleep(1800) # Har 30 minutda tekshirish
            rewarded_refs = db.q("SELECT * FROM referrals WHERE status='rewarded'")
            for r in rewarded_refs:
                is_sub, _ = await check_user_subscribed(r["referred_id"])
                if not is_sub:
                    await penalize_referral_if_left(r["referred_id"])
        except Exception as e:
            logger.warning(f"Referral checker loop xatosi: {e}")

# ============================ 7. /START VA OBUNA HANDLERLARI ============================
@router.message(CommandStart())
async def cmd_start_handler(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    username = msg.from_user.username
    full_name = msg.from_user.full_name

    args = msg.text.split()[1] if len(msg.text.split()) > 1 else ""
    referred_by = None
    if args.startswith("ref_"):
        ref_id_str = args.replace("ref_", "").strip()
        if ref_id_str.isdigit() and int(ref_id_str) != uid:
            referred_by = int(ref_id_str)
            exists = db.get_user(uid)
            if not exists:
                db.q("INSERT INTO referrals (referrer_id, referred_id, status, created_at) VALUES (?, ?, 'pending', ?)",
                     (referred_by, uid, datetime.now(TZ).isoformat()), commit=True)

    db.ensure_user(uid, username, full_name, referred_by)

    # Sotuvchi bitim havolasi orqali kirgan holat: /start deal_K12345
    if args.startswith("deal_"):
        d_id = args.replace("deal_", "").strip()
        deal = db.get_deal(d_id)
        if deal:
            if deal["buyer_id"] == uid:
                await msg.answer("⚠️ Siz ushbu bitimning xaridorisiz. Sotuvchi ushbu havola orqali ulanishi kerak.", reply_markup=main_menu_keyboard(uid))
                return
            now_s = datetime.now(TZ).isoformat()
            clean_un = (username or "").lstrip("@")
            db.q("UPDATE deals SET seller_id=?, seller_username=?, updated_at=? WHERE deal_id=?", (uid, clean_un, now_s, d_id), commit=True)
            await msg.answer(
                f"🤝 <b>SIZ SAVDOGA SOTUVCHI SIFATIDA QO'SHILDINGIZ! (#{d_id})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Tovar: <b>{esc_html(deal['title'])}</b>\n"
                f"💵 Siz oladigan summa: <b>{fmt(deal['amount'])} so'm</b>\n"
                f"🛒 Xaridor: @{esc_html(deal['buyer_username'])}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔒 <b>Pul kafil hisobida xavfsiz muzlatilgan!</b>\n"
                f"Siz bemalol tovaringizni (akkaunt/kanal/kod) xaridorga taqdim etishingiz mumkin.\n"
                f"Xaridor tovarni tekshirib olgach 'Oldim' deb tasdiqlashi bilan pul sizga o'tkaziladi.",
                reply_markup=main_menu_keyboard(uid)
            )
            try:
                await bot.send_message(
                    deal["buyer_id"],
                    f"🎉 <b>Sotuvchi @{esc_html(clean_un)} savdoga qo'shildi! (#{d_id})</b>\n"
                    f"Sotuvchi bilan bot ichidagi xavfsiz chat orqali muloqot qilishingiz mumkin."
                )
            except Exception:
                pass
            return

    # Majburiy obuna tekshiruvi
    is_sub, missing = await check_user_subscribed(uid)
    if not is_sub:
        kb = []
        for ch in missing:
            kb.append([InlineKeyboardButton(text=f"📢 {ch['channel_title']}", url=ch["invite_link"])])
        kb.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")])
        await msg.answer(
            f"👋 <b>Assalomu alaykum, {esc_html(full_name)}!</b>\n\n"
            f"Botdan to'liq foydalanish va kafolatli savdolarni amalga oshirish uchun quyidagi rasmiy kanallarga a'zo bo'ling:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
        )
        return

    await reward_referral_if_eligible(uid)

    # Qora ro'yxat tekshiruvi
    banned, b_reason = db.is_blacklisted(user_id=uid, username=username)
    if banned:
        await msg.answer(
            f"🚫 <b>DIQQAT: Sizning hisobingiz QORA RO'YXATGA tushgan!</b>\n"
            f"Sabab: <i>{esc_html(b_reason)}</i>\n"
            f"Sizga ushbu botdan foydalanish taqiqlanadi."
        )
        return

    text = (
        f"🛡 <b>KAFOLATLI SAVDO (ESCROW / GARANT) BOTIGA XUSH KELIBSIZ!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Bu yerda siz har qanday akkaunt, kanal, script yoki xizmatlarni 100% kafolat ostida xavfsiz sotib olishingiz yoki sotishingiz mumkin.\n\n"
        f"🎁 <b>Birinchi savdongiz — MUTLAQO TEKIN (0% komissiya)!</b>\n"
        f"👥 Do'stlaringizni taklif qiling va har bir do'stingiz uchun <b>+100 so'm</b> oling!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Quyidagi menyulardan keraklisini tanlang:"
    )
    await msg.answer(text, reply_markup=main_menu_keyboard(uid))

@router.callback_query(F.data == "check_sub")
async def callback_check_sub(call: CallbackQuery):
    uid = call.from_user.id
    is_sub, missing = await check_user_subscribed(uid)
    if not is_sub:
        await call.answer("⚠️ Hali barcha kanallarga a'zo bo'lmadingiz!", show_alert=True)
        return
    await call.answer("✅ Rahmat! Obuna tasdiqlandi.", show_alert=False)
    await reward_referral_if_eligible(uid)
    await call.message.delete()
    await call.message.answer(
        "🎉 <b>Obuna muvaffaqiyatli tasdiqlandi!</b>\nKafil botining barcha imkoniyatlaridan to'liq foydalanishingiz mumkin.",
        reply_markup=main_menu_keyboard(uid)
    )

# ============================ 8. 1-TUGMA: 📜 TARIX ============================
@router.message(F.text == "📜 Tarix")
async def history_handler(msg: Message):
    uid = msg.from_user.id
    deals = db.q("SELECT * FROM deals WHERE buyer_id=? OR seller_id=? ORDER BY updated_at DESC LIMIT 10", (uid, uid))
    if not deals:
        await msg.answer(
            "📂 <b>Sizda hali hech qanday savdolar tarixi mavjud emas.</b>\n"
            "Yangi savdo boshlash uchun <b>«🤝 Yangi Savdo Ochish»</b> tugmasini bosing.",
            reply_markup=main_menu_keyboard(uid)
        )
        return

    text = "📜 <b>SIZNING SAVDOLARINGIZ TARIXI:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    kb = []
    status_uz = {
        "created": "🆕 Yaratildi",
        "waiting_payment": "💳 To'lov kutilmoqda",
        "in_trade": "🔒 Muzlatilgan (Topshirilmoqda)",
        "completed": "✅ Yakunlandi",
        "dispute": "🚨 Nizo / Shikoyat",
        "cancelled": "❌ Bekor qilindi",
        "refunded": "↩️ Pul qaytarildi"
    }
    for d in deals:
        role = "Xaridor" if d["buyer_id"] == uid else "Sotuvchi"
        st = status_uz.get(d["status"], d["status"])
        created_date = (d["created_at"] or "")[:16].replace("T", " ")
        text += (
            f"🆔 <b>#{d['deal_id']}</b> | {st}\n"
            f"📦 <b>Tovar:</b> {esc_html(d['title'])}\n"
            f"💵 <b>Summa:</b> <b>{fmt(d['amount'])} so'm</b> ({role})\n"
            f"📅 <b>Sana:</b> {created_date}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        row = [
            InlineKeyboardButton(text=f"Batafsil Tarixi: #{d['deal_id']}", callback_data=f"deal_detail:{d['deal_id']}")
        ]
        if d["status"] in ("in_trade", "completed", "dispute"):
            row.append(InlineKeyboardButton(text="Ariza berish", callback_data=f"file_comp:{d['deal_id']}"))
        kb.append(row)

    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("deal_detail:"))
async def deal_detail_callback(call: CallbackQuery):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = db.get_deal(d_id)
    if not deal:
        await call.message.answer("Bitim topilmadi.")
        return

    messages = db.get_deal_messages(d_id)
    chat_log = ""
    if messages:
        for m in messages:
            time_part = (m["created_at"] or "")[11:16]
            chat_log += f"[{time_part}] <b>{esc_html(m['sender_tag'])}:</b> {esc_html(m['message_text'])}\n"
    else:
        chat_log = "<i>(Chat yozishmalari mavjud emas)</i>\n"

    created_date = (deal["created_at"] or "")[:19].replace("T", " ")
    updated_date = (deal["updated_at"] or "")[:19].replace("T", " ")

    text = (
        f"📄 <b>SAVDO TO'LIQ TAFSILOTLARI VA YOZISHMALAR LOGI:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Bitim ID:</b> <code>#{deal['deal_id']}</code>\n"
        f"📦 <b>Tovar/Xizmat:</b> <b>{esc_html(deal['title'])}</b>\n"
        f"💵 <b>Kelishilgan summa:</b> {fmt(deal['amount'])} so'm\n"
        f"💼 <b>Komissiya:</b> {fmt(deal['fee'])} so'm\n"
        f"💰 <b>To'langan summa:</b> {fmt(deal['total_to_pay'])} so'm\n"
        f"🔒 <b>Holati:</b> {deal['status'].upper()}\n"
        f"📅 <b>Boshlangan sana:</b> {created_date}\n"
        f"🔄 <b>Oxirgi yangilanish:</b> {updated_date}\n"
        f"🛒 <b>Xaridor:</b> ID <code>{deal['buyer_id']}</code> (@{esc_html(deal['buyer_username'] or 'yo`q')})\n"
        f"💼 <b>Sotuvchi:</b> ID <code>{deal['seller_id']}</code> (@{esc_html(deal['seller_username'] or 'yo`q')})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 <b>CHAT YOZISHMALARI:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{chat_log}"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = [
        [InlineKeyboardButton(text="⚖️ Ushbu savdo bo'yicha ariza berish", callback_data=f"file_comp:{d_id}")]
    ]
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# ============================ 9. 2-TUGMA: 🆘 SOS (YORDAM) ============================
@router.message(F.text == "🆘 SOS (Yordam)")
async def sos_menu_handler(msg: Message, state: FSMContext):
    await state.clear()
    text = (
        "🆘 <b>YORDAM VA XAVFSIZLIK MARKAZI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Assalomu alaykum! Men Kafil Botining sun'iy intellekt (Gemini AI) assistentiman 🤖.\n\n"
        "<b>Qanday yordam bera olaman?</b>\n"
        "Quyidagi xizmatlardan birini tanlang:"
    )
    kb = [
        [InlineKeyboardButton(text="💬 Gemini AI bilan savol-javob", callback_data="sos_ask_ai")],
        [InlineKeyboardButton(text="🚨 Meni sotuvchi aldadi (Ariza)", callback_data="sos_file_complaint")],
        [InlineKeyboardButton(text="📞 Admin bilan to'g'ridan-to'g'ri bog'lanish", callback_data="sos_contact_admin")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "sos_ask_ai")
async def sos_ask_ai_click(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SOSFSM.ai_query)
    await call.message.answer(
        "🤖 <b>GEMINI AI ASSISTENTI:</b>\n\n"
        "Qanday yordam bera olaman? Muammoingizni yoki savolingizni batafsil yozib qoldiring:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(SOSFSM.ai_query)
async def process_ai_query(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    user_query = msg.text.strip()
    wait_msg = await msg.answer("🤖 <i>Gemini AI vaziyatni tahlil qilmoqda...</i>")

    ai_resp = await ask_gemini_ai(user_query)
    await wait_msg.delete()

    ticket_id = db.q(
        "INSERT INTO ai_tickets (user_id, user_name, question, status, created_at) VALUES (?, ?, ?, 'open', ?)",
        (msg.from_user.id, msg.from_user.full_name, user_query, datetime.now(TZ).isoformat()), commit=True
    )

    # Agar AI muammoni mustaqil hal qila olmasa yoki ADMIN_GA_UZATISH qaytarsa
    if "ADMIN_GA_UZATISH" in ai_resp:
        clean_resp = ai_resp.replace("ADMIN_GA_UZATISH", "").strip()
        await msg.answer(
            f"🤖 <b>AI JAVOBI:</b>\n\n{clean_resp}\n\n"
            f"⚠️ <i>Ushbu masala jiddiy bo'lganligi sababli murojaatingiz avtomatik ravishda adminga yuborildi. "
            f"Admin javob bergach, bot javobni sizga yetkazadi.</i>",
            reply_markup=main_menu_keyboard(msg.from_user.id)
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Javob berish", callback_data=f"adm_reply_ticket:{ticket_id}")]
        ])
        for a_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    a_id,
                    f"🆘 <b>YANGI MUROJAAT (AI orqali adminga uzatildi): #{ticket_id}</b>\n"
                    f"Foydalanuvchi: {esc_html(msg.from_user.full_name)} (ID: <code>{msg.from_user.id}</code>)\n"
                    f"Murojaat:\n<i>{esc_html(user_query)}</i>",
                    reply_markup=admin_kb
                )
            except Exception:
                pass
        await state.clear()
        return

    kb = [
        [InlineKeyboardButton(text="👨‍💼 Adminga yuborish", callback_data=f"fwd_admin:{ticket_id}")],
        [InlineKeyboardButton(text="✅ Tushunarli, rahmat", callback_data="ai_resolved")]
    ]
    await msg.answer(
        f"🤖 <b>GEMINI AI JAVOBI:</b>\n\n"
        f"{ai_resp}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Agar javob sizni qoniqtirmasa, quyidagi tugma orqali adminga yuborishingiz mumkin.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

@router.callback_query(F.data == "ai_resolved")
async def ai_resolved_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Yordam berganimizdan xursandmiz!")
    await call.message.edit_text("✅ Murojaatingiz yakunlandi. Yana savollaringiz bo'lsa marhamat!")

@router.callback_query(F.data.startswith("fwd_admin:"))
async def fwd_admin_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    t_id = int(call.data.split(":")[1])
    ticket = db.q("SELECT * FROM ai_tickets WHERE id=?", (t_id,), one=True)
    if not ticket:
        await call.message.answer("Murojaat topilmadi.")
        return

    await call.message.edit_text(
        "✅ <b>Murojaatingiz adminga yetkazildi!</b>\n"
        "Admin javob yozishi bilan bot sizga xabarni darhol yetkazadi. Iltimos, kuting."
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Javob yozish", callback_data=f"adm_reply_ticket:{t_id}")]
    ])
    for a_id in ADMIN_IDS:
        try:
            await bot.send_message(
                a_id,
                f"🆘 <b>YORDAM SO'ROVI: #{t_id}</b>\n"
                f"Foydalanuvchi: <b>{esc_html(ticket['user_name'])}</b> (ID: <code>{ticket['user_id']}</code>)\n"
                f"Savol / Muammo:\n<i>{esc_html(ticket['question'])}</i>",
                reply_markup=admin_kb
            )
        except Exception:
            pass

@router.callback_query(F.data == "sos_contact_admin")
async def sos_contact_admin_cb(call: CallbackQuery):
    await call.answer()
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else 6125160021
    await call.message.answer(
        f"📞 <b>ADMIN BILAN TO'G'RIDAN-TO'G'RI BOG'LANISH:</b>\n\n"
        f"Garant admin bilan to'g'ridan-to'g'ri bog'lanish uchun havola:\n"
        f"👉 <a href=\"tg://user?id={admin_id}\">Rasmiy Garant Admin</a>",
        disable_web_page_preview=True
    )

# SOS orqali "Meni sotuvchi aldadi" arizasi
@router.callback_query(F.data == "sos_file_complaint")
async def sos_complaint_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SOSFSM.complaint_seller)
    await call.message.answer(
        "🚨 <b>FIRIBGARLIK BO'YICHA ARIZA QOLDIRISH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Sizni qaysi sotuvchi aldadi?\n\n"
        "👉 <b>O'sha sotuvchining @username'ini kiriting:</b>\n"
        "<i>(Masalan: @sotuvchi_username)</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(SOSFSM.complaint_seller)
async def process_comp_seller(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    seller_tag = msg.text.strip().lstrip("@")
    if not seller_tag:
        await msg.answer("Iltimos, to'g'ri username kiriting:")
        return

    await state.update_data(target_seller=seller_tag)
    await state.set_state(SOSFSM.complaint_reason)
    await msg.answer(
        f"Sotuvchi: <b>@{esc_html(seller_tag)}</b>\n\n"
        f"📝 <b>Endi qanday aldov sodir bo'lganini batafsil yozib bering:</b>"
    )

@router.message(SOSFSM.complaint_reason)
async def process_comp_reason(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    reason = msg.text.strip()
    data = await state.get_data()
    seller_tag = data.get("target_seller", "")
    deal_id = data.get("deal_id", "")
    await state.clear()

    now_dt = datetime.now(TZ)
    deadline_dt = now_dt + timedelta(days=5)
    deadline_s = deadline_dt.isoformat()

    accused_user = db.q("SELECT user_id, username FROM users WHERE LOWER(username)=?", (seller_tag.lower(),), one=True)
    accused_id = accused_user["user_id"] if accused_user else None

    comp_id = db.create_complaint(
        deal_id=deal_id,
        complainant_id=msg.from_user.id,
        complainant_username=msg.from_user.username or "",
        accused_id=accused_id,
        accused_username=seller_tag,
        reason=reason,
        deadline_s=deadline_s
    )

    await msg.answer(
        f"✅ <b>ARIZANGIZ QABUL QILINDI! (#{comp_id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Sotuvchi: @{esc_html(seller_tag)}\n"
        f"Muddat: <b>5 kun</b> ({deadline_dt.strftime('%d.%m.%Y %H:%M')} gacha)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Sotuvchiga darhol ariza xabarnomasi yuborildi. Agar u 5 kun ichida siz bilan bog'lanib masalani hal qilmasa, "
        f"tizim uni avtomatik ravishda <b>FIRIBGAR</b> qilib qora ro'yxatga kiritadi!",
        reply_markup=main_menu_keyboard(msg.from_user.id)
    )

    if accused_id:
        try:
            await bot.send_message(
                accused_id,
                f"⚠️ <b>DIQQAT: SIZNING USTINGIZDAN SHIKOYAT ARIZASI KELIB TUSHDI! (#{comp_id})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Ariza beruvchi: @{esc_html(msg.from_user.username or 'Xaridor')} (ID: <code>{msg.from_user.id}</code>)\n"
                f"Sabab: <i>{esc_html(reason)}</i>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱ <b>Foydalanuvchi bilan bog'lanib masalani hal qiling, aks holda qora ro'yxatga tushasiz!</b>\n"
                f"Sizga muammoni hal qilish uchun 5 kun muddat berildi."
            )
        except Exception:
            pass

    for a_id in ADMIN_IDS:
        try:
            await bot.send_message(
                a_id,
                f"🚨 <b>YANGI ARIZA TUSHDI: #{comp_id}</b>\n"
                f"Kimdan: @{esc_html(msg.from_user.username or '')} (ID: <code>{msg.from_user.id}</code>)\n"
                f"Kimga: @{esc_html(seller_tag)} (ID: <code>{accused_id or 'nomalum'}</code>)\n"
                f"Sabab: <i>{esc_html(reason)}</i>\n"
                f"Muddat: 5 kun ({deadline_dt.strftime('%d.%m.%Y')})"
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("file_comp:"))
async def file_comp_deal_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = db.get_deal(d_id)
    if not deal:
        await call.message.answer("Bitim topilmadi.")
        return

    target_un = deal["seller_username"] if call.from_user.id == deal["buyer_id"] else deal["buyer_username"]
    await state.update_data(target_seller=target_un, deal_id=d_id)
    await state.set_state(SOSFSM.complaint_reason)
    await call.message.answer(
        f"⚖️ <b>#{d_id} BITIMI BO'YICHA ARIZA YUBORISH:</b>\n"
        f"Qarshi tomon: @{esc_html(target_un)}\n\n"
        f"📝 <b>Muammoni va sababni batafsil yozing:</b>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

# ============================ 10. 3-TUGMA: 👥 REFERAL TIZIMI ============================
@router.message(F.text == "👥 Referal Tizimi")
async def referral_menu_handler(msg: Message):
    uid = msg.from_user.id
    u = db.get_user(uid)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"

    total_refs = db.q("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='rewarded'", (uid,), one=True)["c"]
    today_prefix = datetime.now(TZ).strftime("%Y-%m-%d")
    today_refs = db.q("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='rewarded' AND created_at LIKE ?",
                      (uid, f"{today_prefix}%"), one=True)["c"]

    bal = u["referral_balance"] if u else 0

    text = (
        f"👥 <b>DO'STLARNI TAKLIF QILISH VA REFERAL TIZIMI</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Sizning shaxsiy referal havolangiz:\n"
        f"👉 <code>{ref_link}</code>\n"
        f"<i>(Havolani bosib nusxalab oling)</i>\n\n"
        f"📊 <b>STATISTIKANGIZ:</b>\n"
        f"• Jami taklif qilingan faol do'stlar: <b>{total_refs} ta</b>\n"
        f"• Bugun qo'shilganlar: <b>{today_refs} / 20 ta</b>\n"
        f"• Referal balansingiz: <b>{fmt(bal)} so'm</b>\n\n"
        f"📌 <b>Qat'iy Qoidalar:</b>\n"
        f"• Har bir chaqirilgan do'st uchun: <b>+100 so'm</b>\n"
        f"• SHART: Do'stingiz kanallarga a'zo bo'lib, botni ishlatgandagina 100 so'm qo'shiladi.\n"
        f"• Kunlik limit: Maksimal <b>20 tagacha</b> odam qo'sha olasiz.\n"
        f"• Jarima: Agar chaqirilgan odam kanaldan chiqib ketsa, balansingizdan <b>-200 so'm</b> jarima yechiladi.\n"
        f"• Minimal yechib olish summasi: qat'iy <b>10 000 so'm</b>.\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = [
        [InlineKeyboardButton(text="💸 Pulni yechib olish", callback_data="withdraw_referral")],
        [InlineKeyboardButton(text="🔗 Do'stlarga ulashish", url=f"https://t.me/share/url?url={ref_link}&text=Garant%20botida%201-savdo%20bepul!")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "withdraw_referral")
async def withdraw_referral_click(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    u = db.get_user(uid)
    bal = u["referral_balance"] if u else 0
    if bal < 10000:
        await call.answer(f"⚠️ Minimal yechish summasi qat'iy 10 000 so'm! Sizda: {fmt(bal)} so'm.", show_alert=True)
        return

    await call.answer()
    await state.set_state(WithdrawFSM.card_number)
    await state.update_data(withdraw_amount=bal)
    await call.message.answer(
        f"💸 <b>PULNI YECHIB OLISH ({fmt(bal)} so'm)</b>\n\n"
        f"💳 <b>16 xonali plastik karta raqamingizni kiriting:</b>\n"
        f"<i>(Masalan: 8600 1234 5678 9012)</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(WithdrawFSM.card_number)
async def process_withdraw_card(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return
    card = re.sub(r"\s+", "", msg.text.strip())
    if not card.isdigit() or len(card) != 16:
        await msg.answer("⚠️ Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qaytadan kiriting:")
        return
    await state.update_data(card_number=card)
    await state.set_state(WithdrawFSM.card_owner)
    await msg.answer("Karta egasining Ism-Familiyasini kiriting:")

@router.message(WithdrawFSM.card_owner)
async def process_withdraw_owner(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return
    owner = msg.text.strip().upper()
    data = await state.get_data()
    amount = data["withdraw_amount"]
    card = data["card_number"]
    await state.clear()

    uid = msg.from_user.id
    now_s = datetime.now(TZ).isoformat()
    db.q("UPDATE users SET referral_balance=referral_balance-? WHERE user_id=?", (amount, uid), commit=True)
    w_id = db.q("INSERT INTO withdrawals (user_id, amount, card_number, card_owner, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (uid, amount, card, owner, now_s), commit=True)

    await msg.answer(
        f"✅ <b>Adminga yuborildi, admin tez orada pulingizni tashlab beradi.</b>\n\n"
        f"So'rov raqami: <code>#{w_id}</code>\n"
        f"Summa: <b>{fmt(amount)} so'm</b>\n"
        f"Karta: <code>{card}</code> ({owner})",
        reply_markup=main_menu_keyboard(uid)
    )

    adm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ To'landi", callback_data=f"adm_pay_w:{w_id}")],
        [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"adm_rej_w:{w_id}")]
    ])
    for a_id in ADMIN_IDS:
        try:
            await bot.send_message(
                a_id,
                f"💸 <b>REFERAL PUL YECHISH SO'ROVI: #{w_id}</b>\n"
                f"Foydalanuvchi: ID <code>{uid}</code> (@{esc_html(msg.from_user.username or '')})\n"
                f"Summa: <b>{fmt(amount)} so'm</b>\n"
                f"Karta: <code>{card}</code> ({owner})",
                reply_markup=adm_kb
            )
        except Exception:
            pass

# ============================ 11. 4-TUGMA: 🤝 YANGI SAVDO OCHISH ============================
@router.message(F.text == "🤝 Yangi Savdo Ochish")
async def start_new_deal_handler(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id

    active = db.get_active_deal_for_user(uid)
    if active:
        await msg.answer(
            f"⚠️ Sizda allaqachon faol bitim mavjud: <code>#{active['deal_id']}</code>.\n"
            f"Avval ushbu bitimni yakunlang yoki bekor qiling.",
            reply_markup=main_menu_keyboard(uid)
        )
        return

    text = (
        "🤝 <b>YANGI KAFOLATLI SAVDO OCHISH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Savdoni qaysi usulda amalga oshirmoqchisiz?\n\n"
        "1. <b>🤖 AI yordamida guruhda:</b>\n"
        "Botni guruhga qo'shasiz, Gemini AI savdoni nazorat qilib turadi.\n\n"
        "2. <b>📱 Botning o'zida:</b>\n"
        "Savdo bot ichidagi xavfsiz chat orqali kechadi."
    )
    kb = [
        [InlineKeyboardButton(text="🤖 AI yordamida guruhda", callback_data="trade_mode:group")],
        [InlineKeyboardButton(text="📱 Botning o'zida", callback_data="trade_mode:bot")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("trade_mode:"))
async def process_deal_mode_choice(call: CallbackQuery, state: FSMContext):
    await call.answer()
    mode = call.data.split(":")[1]
    await state.update_data(trade_mode=mode)
    await state.set_state(CreateDealFSM.title)

    mode_name = "AI yordamida guruhda" if mode == "group" else "Botning o'zida"
    await call.message.answer(
        f"Tanlandi: <b>{mode_name}</b>\n\n"
        f"📦 <b>Mahsulot yoki xizmat nomini kiriting:</b>\n"
        f"<i>(Masalan: PUBG akkaunt, Telegram kanal, Script yoki Dizayn xizmati)</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(CreateDealFSM.title)
async def process_deal_title_step(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    title = msg.text.strip()
    await state.update_data(deal_title=title)
    await state.set_state(CreateDealFSM.seller)
    await msg.answer(
        "Siz xaridorsiz, sotuvchining @username'ini bering:\n"
        "<i>(Masalan: @sotuvchi_username)</i>"
    )

@router.message(CreateDealFSM.seller)
async def process_deal_seller_step(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    seller_tag = msg.text.strip().lstrip("@")
    if not seller_tag:
        await msg.answer("Iltimos, to'g'ri username kiriting:")
        return

    # Qora ro'yxat tekshiruvi
    banned, reason = db.is_blacklisted(username=seller_tag)
    if banned:
        await state.clear()
        await msg.answer(
            f"🚫 <b>DIQQAT! USHBU FOYDALANUVCHI BILAN SAVDO QILISH TAQIQLANADI!</b>\n"
            f"Sotuvchi @{esc_html(seller_tag)} <b>QORA RO'YXATDA</b> mavjud!\n"
            f"Sabab: <i>{esc_html(reason)}</i>\n\n"
            f"Ushbu shaxs bilan savdo qilish xavfli bo'lganligi sababli savdo to'xtatildi.",
            reply_markup=main_menu_keyboard(msg.from_user.id)
        )
        return

    await state.update_data(seller_username=seller_tag)
    await state.set_state(CreateDealFSM.amount)

    buyer = db.get_user(msg.from_user.id)
    is_first = (buyer and buyer["completed_deals"] == 0)
    promo_text = "\n🎁 <b>Siz birinchi marta foydalanayotganingiz uchun sizdan % olmaymiz (0%)!</b>" if is_first else "\n💼 Keyingi savdolar uchun qat'iy 10% komissiya qo'shiladi."

    await msg.answer(
        f"💵 <b>To'lov summasini kiriting:</b>\n"
        f"<i>(Min: 1 000 so'm, Max: 100 000 so'm)</i>:{promo_text}\n\n"
        f"Faqat raqamda kiriting (Masalan: 50000):"
    )

@router.message(CreateDealFSM.amount)
async def process_deal_amount_step(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    txt = re.sub(r"\s+", "", msg.text.strip())
    if not txt.isdigit():
        await msg.answer("Iltimos, to'lov summasini faqat butun raqamlarda kiriting:")
        return
    amount = int(txt)
    if amount < 1000 or amount > 100000:
        await msg.answer("⚠️ To'lov summasi kamida 1 000 so'm va ko'pi bilan 100 000 so'm bo'lishi kerak:")
        return

    data = await state.get_data()
    title = data["deal_title"]
    seller_tag = data["seller_username"]
    mode = data["trade_mode"]
    uid = msg.from_user.id

    buyer = db.get_user(uid)
    is_first = (buyer and buyer["completed_deals"] == 0)
    fee = 0 if is_first else int(amount * 0.10)
    total_to_pay = amount + fee

    deal_id = generate_deal_id()
    pay_res = await create_payment_async(total_to_pay, order_id=deal_id, user_id=uid)
    exact_amount = total_to_pay
    payment_id = None
    if pay_res and pay_res.get("ok"):
        payment_id = pay_res.get("payment_id")
        exact_amount = int(pay_res.get("amount", total_to_pay))

    db.create_deal(
        deal_id=deal_id,
        buyer_id=uid,
        buyer_username=msg.from_user.username or "",
        seller_username=seller_tag,
        title=title,
        amount=amount,
        fee=fee,
        total_to_pay=exact_amount,
        mode=mode,
        payment_id=payment_id
    )

    await state.clear()
    asyncio.create_task(deal_5min_timer(deal_id))

    card_num = db.get_setting("card_number", DEFAULT_CARD_NUMBER)
    card_own = db.get_setting("card_owner", DEFAULT_CARD_OWNER)
    fee_label = "0 so'm (1-savdo bepul!)" if fee == 0 else f"{fmt(fee)} so'm (10%)"

    payment_card = (
        f"💳 <b>KAFOLATLI TO'LOV HISOBI OCHILDI! (#{deal_id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Mahsulot:</b> <b>{esc_html(title)}</b>\n"
        f"👤 <b>Sotuvchi:</b> @{esc_html(seller_tag)}\n"
        f"💵 <b>Kelishilgan summa:</b> {fmt(amount)} so'm\n"
        f"💼 <b>Komissiya:</b> {fee_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>TO'LOV KARTASI:</b>\n"
        f"👉 <code>{card_num}</code>\n"
        f"👤 Qabul qiluvchi: <b>{esc_html(card_own)}</b>\n\n"
        f"💰 <b>O'TKAZILADIGAN ANIQ SUMMA:</b>\n"
        f"👉 <code>{fmt(exact_amount)}</code> <b>so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>Manashu summadan bir so'm ham ko'p yoki kam tashlamang, aks holda to'lov amalga oshmaydi!</b>\n"
        f"⏱ <b>Siz 5 minut ichida to'lov qilmasangiz to'lov avtomatik bekor qilinadi.</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = []
    if pay_res and pay_res.get("payment_url"):
        kb.append([InlineKeyboardButton(text="💳 Havola orqali to'lash", url=pay_res["payment_url"])])
    kb.append([InlineKeyboardButton(text="✅ To'lov qildim", callback_data=f"i_paid:{deal_id}")])
    kb.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel_deal:{deal_id}")])

    await msg.answer(payment_card, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("i_paid:"))
async def i_paid_callback_step(call: CallbackQuery, state: FSMContext):
    d_id = call.data.split(":")[1]
    deal = db.get_deal(d_id)
    if not deal or deal["status"] != "waiting_payment":
        await call.answer("Bitim holati o'zgargan yoki muddati tugagan.", show_alert=True)
        return

    payment_id = deal["payment_id"]
    is_paid = False
    if payment_id:
        chk = await check_payment_status_async(payment_id)
        if chk and chk.get("paid"):
            is_paid = True

    if not is_paid:
        await call.answer("⏳ To'lov hali kelib tushmadi. Iltimos, pulni aniq summasini o'tkazib 10-15 soniyadan so'ng qayta bosing.", show_alert=True)
        return

    await call.answer("✅ To'lov muvaffaqiyatli qabul qilindi!", show_alert=True)
    now_s = datetime.now(TZ).isoformat()
    db.q("UPDATE deals SET status='in_trade', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)

    if deal["mode"] == "group":
        await state.set_state(CreateDealFSM.group_link)
        await state.update_data(active_deal_id=d_id)
        await call.message.edit_text(
            f"🎉 <b>TO'LOV TASDIQLANDI VA MUZLATILDI! (#{d_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Endi meni guruhga qo'shishingiz va admin qilishingiz kerak, so'ng guruh havolasini bering:"
        )
    else:
        bot_info = await bot.get_me()
        invite_link = f"https://t.me/{bot_info.username}?start=deal_{d_id}"
        await call.message.edit_text(
            f"🎉 <b>TO'LOV TASDIQLANDI VA MUZLATILDI! (#{d_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"To'lov kafil hisobida xavfsiz muzlatildi.\n\n"
            f"Sotuvchi @{esc_html(deal['seller_username'])} ga quyidagi havolani yuboring:\n"
            f"👉 <code>{invite_link}</code>\n\n"
            f"Sotuvchi ulangach, bot ichidagi xavfsiz chat ochiladi!"
        )

@router.message(CreateDealFSM.group_link)
async def process_group_link_step(msg: Message, state: FSMContext):
    data = await state.get_data()
    d_id = data.get("active_deal_id")
    deal = db.get_deal(d_id)
    if not deal:
        await state.clear()
        await msg.answer("Bitim topilmadi.")
        return

    chat_input = msg.text.strip()
    target_chat_id = None
    if chat_input.startswith("-100") or chat_input.lstrip("-").isdigit():
        target_chat_id = int(chat_input)
    elif "@" in chat_input:
        target_chat_id = chat_input.split("/")[-1]
        if not target_chat_id.startswith("@"):
            target_chat_id = "@" + target_chat_id
    elif "t.me/" in chat_input:
        target_chat_id = "@" + chat_input.split("t.me/")[-1].replace("+", "").split("/")[0]

    bot_user = await bot.get_me()
    is_admin_in_group = False
    actual_chat_id = None

    if target_chat_id:
        try:
            member = await bot.get_chat_member(target_chat_id, bot_user.id)
            if member.status in ("administrator", "creator"):
                is_admin_in_group = True
                chat_obj = await bot.get_chat(target_chat_id)
                actual_chat_id = chat_obj.id
        except Exception:
            pass

    if not is_admin_in_group:
        await msg.answer("Siz meni admin qilmadingiz! Iltimos, botni guruhga admin qilib havolasini qayta yuboring:")
        return

    group_code = generate_group_code()
    db.q("UPDATE deals SET group_chat_id=?, group_code=? WHERE deal_id=?", (actual_chat_id, group_code, d_id), commit=True)
    await state.clear()

    await msg.answer(
        f"✅ <b>MEN GURUHDA ADMIN BO'LDIM!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Endi manashu kodni guruhga yozing va sotuvchini ham guruhga qo'shing:\n\n"
        f"👉 <code>{group_code}</code>\n"
        f"<i>(Ustiga bossangiz nusxalanadi)</i>",
        reply_markup=main_menu_keyboard(msg.from_user.id)
    )

# ============================ 12. GURUHDA GEMINI AI BILAN SAVDO BOSHQARUVI ============================
@router.message(F.chat.type.in_(["group", "supergroup"]))
async def group_chat_ai_monitor(msg: Message):
    chat_id = msg.chat.id
    text = (msg.text or "").strip()
    sender_id = msg.from_user.id
    sender_un = (msg.from_user.username or "").lstrip("@").lower()

    if text.startswith("KAFIL-"):
        deal = db.q("SELECT * FROM deals WHERE group_code=? AND status='in_trade'", (text,), one=True)
        if deal:
            await msg.answer(
                f"🤖 <b>Savdo boshlaymizmi? Ikkalangiz ham 'ha' deb yozingchi, ikkalangiz ham shu yerdamisiz?</b>\n\n"
                f"Xaridor: @{esc_html(deal['buyer_username'])}\n"
                f"Sotuvchi: @{esc_html(deal['seller_username'])}\n"
                f"Tovar: <b>{esc_html(deal['title'])}</b>"
            )
            return

    deal = db.q("SELECT * FROM deals WHERE group_chat_id=? AND status IN ('in_trade', 'goods_delivered', 'buyer_confirmed', 'buyer_statement', 'seller_statement')", (chat_id,), one=True)
    if not deal:
        return

    is_buyer = (sender_id == deal["buyer_id"] or sender_un == (deal["buyer_username"] or "").lower())
    is_seller = (sender_id == deal["seller_id"] or sender_un == (deal["seller_username"] or "").lower())

    if not is_buyer and not is_seller:
        return

    if is_seller and not deal["seller_id"]:
        db.q("UPDATE deals SET seller_id=? WHERE deal_id=?", (sender_id, deal["deal_id"]), commit=True)
        deal = db.get_deal(deal["deal_id"])

    role = "Xaridor" if is_buyer else "Sotuvchi"
    sender_tag = f"@{msg.from_user.username}" if msg.from_user.username else f"{role} ({sender_id})"
    db.log_deal_message(deal["deal_id"], sender_id, sender_tag, role, text)

    # Gemini AI Moderatsiya (check_chat_safety)
    is_safe, warn_reason = await check_chat_safety(text)
    if not is_safe:
        await msg.reply(
            f"⚠️ <b>GEMINI AI OGOHLANTIRISHI:</b>\n"
            f"Qoidabuzarlik: <i>{warn_reason}</i>\n"
            f"Iltimos, qoidalarga rioya qiling, aks holda bitim to'xtatiladi!"
        )

    # Ikkala tomon "ha" deb yozishi
    if deal["status"] == "in_trade" and (deal["group_buyer_ready"] == 0 or deal["group_seller_ready"] == 0):
        if text.lower() in ("ha", "xa", "ha tayyorman", "tayyorman"):
            if is_buyer:
                db.q("UPDATE deals SET group_buyer_ready=1 WHERE deal_id=?", (deal["deal_id"],), commit=True)
            if is_seller:
                db.q("UPDATE deals SET group_seller_ready=1 WHERE deal_id=?", (deal["deal_id"],), commit=True)

            deal = db.get_deal(deal["deal_id"])
            if deal["group_buyer_ready"] == 1 and deal["group_seller_ready"] == 1:
                await msg.answer(
                    f"🔒 <b>To'lov kafolatlangan va ushlab turilibdi.</b>\n"
                    f"Oluvchi @{esc_html(deal['buyer_username'])} {fmt(deal['amount'])} so'm to'lagan.\n"
                    f"Sotuvchi @{esc_html(deal['seller_username'])}, siz bemalol mahsulotni bera olasiz. "
                    f"Men xavfsizligingizni tekshirib turaman."
                )
            else:
                missing = "sotuvchi" if deal["group_buyer_ready"] == 1 else "oluvchi"
                await msg.answer(f"Sizda {missing} yo'q, u yozgandagina savdo boshlanadi.")
            return

    # Mahsulot topshirilgach xaridor tasdig'i ("oldim")
    if is_buyer and any(w in text.lower() for w in ["men oldim", "oldim", "tekshirdim", "qabul qildim"]):
        db.q("UPDATE deals SET status='buyer_statement' WHERE deal_id=?", (deal["deal_id"],), commit=True)
        await msg.answer(
            f"✅ Xaridor mahsulot o'tganini tasdiqladi!\n\n"
            f"1️⃣ <b>OLUVCHIDAN RASMIY TILXAT-ARIZA:</b>\n"
            f"Xaridor @{esc_html(deal['buyer_username'])}, quyidagi matnni nusxalab yuboring:\n\n"
            f"<code>Men {esc_html(deal['title'])}ni to'liq qabul qilib oldim, agar keyinchalik sotuvchi qaytarib olsa yoki aldasa, bot javob bermaydi</code>"
        )
        return

    # Oluvchi arizasi qabul qilinishi
    if deal["status"] == "buyer_statement" and is_buyer and "bot javob bermaydi" in text.lower():
        db.q("UPDATE deals SET buyer_statement=?, status='seller_statement' WHERE deal_id=?", (text, deal["deal_id"]), commit=True)
        await msg.answer(
            f"✅ Oluvchi arizasi qabul qilindi!\n\n"
            f"2️⃣ <b>SOTUVCHIDAN RASMIY TILXAT-ARIZA:</b>\n"
            f"Sotuvchi @{esc_html(deal['seller_username'])}, quyidagi matnni nusxalab yuboring:\n\n"
            f"<code>Men {esc_html(msg.from_user.full_name)} agar sotilgan narsani qaytarib olsam yoki aldov qilsam, qora ro'yxatga solishingizni so'rayman</code>"
        )
        return

    # Sotuvchi arizasi qabul qilinishi
    if deal["status"] == "seller_statement" and is_seller and "qora ro'yxat" in text.lower():
        db.q("UPDATE deals SET seller_statement=?, status='buyer_confirmed' WHERE deal_id=?", (text, deal["deal_id"]), commit=True)
        await msg.answer(
            f"✅ Sotuvchi arizasi ham qabul qilindi!\n\n"
            f"Xaridor @{esc_html(deal['buyer_username'])}, savdo to'liq yakunlandimi? "
            f"Pulni sotuvchiga o'tkazishga rozimisiz? Rozilik uchun <b>«ha roziman»</b> deb yozing:"
        )
        return

    # Xaridor yakuniy roziligi
    if deal["status"] == "buyer_confirmed" and is_buyer and ("roziman" in text.lower() or "ha" in text.lower()):
        db.q("UPDATE deals SET status='completed' WHERE deal_id=?", (deal["deal_id"],), commit=True)
        db.q("UPDATE users SET completed_deals=completed_deals+1 WHERE user_id=?", (deal["buyer_id"],), commit=True)
        if deal["seller_id"]:
            db.q("UPDATE users SET completed_deals=completed_deals+1 WHERE user_id=?", (deal["seller_id"],), commit=True)

        await msg.answer(
            f"🎉 <b>XARIDOR ROZILIK BILDIRDI! SAVDO MUVAFFAQIYATLI YAKUNLANDI.</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Sotuvchi @{esc_html(deal['seller_username'])}, kelishilgan <b>{fmt(deal['amount'])} so'm</b> pulni olish uchun "
            f"<b>16 xonali plastik karta raqamingiz va Ism-Familiyangizni</b> shu yerga yozing:"
        )
        return

    # Sotuvchi karta raqamini berganda
    if deal["status"] == "completed" and is_seller and re.search(r"\d{16}", text.replace(" ", "")):
        card_match = re.search(r"\d{16}", text.replace(" ", ""))
        card_num = card_match.group(0) if card_match else ""
        owner_name = text.replace(card_num, "").strip()
        db.q("UPDATE deals SET seller_card=?, seller_card_owner=? WHERE deal_id=?", (card_num, owner_name, deal["deal_id"]), commit=True)

        await msg.answer(
            f"💳 <b>KARTANGIZ QABUL QILINDI:</b> <code>{card_num}</code> ({owner_name})\n"
            f"Mablag' kafil tomonidan kartangizga o'tkazib beriladi. Kafil botidan foydalanganingiz uchun rahmat!"
        )
        for a_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    a_id,
                    f"✅ <b>GURUH SAVDOSI YAKUNLANDI! SOTUVCHIGA PUL O'TKAZING:</b>\n"
                    f"Bitim: <code>#{deal['deal_id']}</code>\n"
                    f"Sotuvchi: ID <code>{deal['seller_id']}</code> (@{esc_html(deal['seller_username'])})\n"
                    f"Karta: <code>{card_num}</code> ({owner_name})\n"
                    f"Summa: <b>{fmt(deal['amount'])} so'm</b>\n"
                    f"Admin komissiyasi: {fmt(deal['fee'])} so'm"
                )
            except Exception:
                pass

@router.chat_member()
async def group_member_left_monitor(event: ChatMemberUpdated):
    chat_id = event.chat.id
    deal = db.q("SELECT * FROM deals WHERE group_chat_id=? AND status IN ('in_trade', 'goods_delivered', 'buyer_statement', 'seller_statement')", (chat_id,), one=True)
    if not deal:
        return

    left_user = event.old_chat_member.user
    if event.new_chat_member.status in ("left", "kicked"):
        is_seller = (left_user.id == deal["seller_id"] or (left_user.username or "").lower() == (deal["seller_username"] or "").lower())
        if is_seller:
            kb = [
                [InlineKeyboardButton(text="🚨 Ha, aldamoqchi bo'ldi (Firibgar)", callback_data=f"scam_confirm:{deal['deal_id']}:{left_user.id}")],
                [InlineKeyboardButton(text="Yo'q, adashib chiqdi", callback_data=f"scam_cancel:{deal['deal_id']}")]
            ]
            await bot.send_message(
                chat_id,
                f"⚠️ <b>DIQQAT: Sotuvchi @{esc_html(deal['seller_username'])} chatdan chiqib ketdi!</b>\n\n"
                f"Xaridor @{esc_html(deal['buyer_username'])}, bu sizni aldamoqchi bo'ldimi?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
            )

@router.callback_query(F.data.startswith("scam_confirm:"))
async def scam_confirm_cb(call: CallbackQuery):
    await call.answer()
    parts = call.data.split(":")
    d_id = parts[1]
    seller_uid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    deal = db.get_deal(d_id)

    db.add_to_blacklist(
        user_id=seller_uid,
        username=deal["seller_username"],
        reason="Guruh savdosida chatdan qochib ketgan (Firibgarlik)",
        deal_id=d_id,
        added_by="ai_chat_guard"
    )
    now_s = datetime.now(TZ).isoformat()
    db.q("UPDATE deals SET status='dispute', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)

    await call.message.edit_text(
        f"🚫 <b>SOTUVCHI QORA RO'YXATGA KIRITILDI VA BOT BUTUNLAY YOPILDI!</b>\n"
        f"Foydalanuvchi: @{esc_html(deal['seller_username'])} (ID: <code>{seller_uid}</code>)\n\n"
        f"Xaridorning puli muzlatilgan va refund qilib qaytarib beriladi."
    )

@router.callback_query(F.data.startswith("scam_cancel:"))
async def scam_cancel_cb(call: CallbackQuery):
    await call.answer("Ogohlantirish bekor qilindi.")
    await call.message.edit_text("ℹ️ Sotuvchi qayta qo'shilishi kutilmoqda.")

# ============================ 13. BOT ICHIDAGI SHIFRLANGAN CHAT ============================
@router.message(F.chat.type == "private" & F.text & ~F.text.startswith("/") & ~F.text.in_([
    "📜 Tarix", "🆘 SOS (Yordam)", "👥 Referal Tizimi", "🤝 Yangi Savdo Ochish",
    "🚫 Firibgarlar Ro'yxati", "💾 Zaxira Nusxa (Backup)", "📑 Arizalarim", "👑 Admin Panel", "❌ Bekor qilish"
]))
async def private_trade_chat_router(msg: Message, state: FSMContext):
    cur_st = await state.get_state()
    if cur_st is not None:
        return

    uid = msg.from_user.id
    deal = db.q("SELECT * FROM deals WHERE (buyer_id=? OR seller_id=?) AND mode='bot' AND status IN ('in_trade', 'goods_delivered', 'buyer_statement', 'seller_statement', 'buyer_confirmed') ORDER BY updated_at DESC LIMIT 1", (uid, uid), one=True)
    if not deal:
        return

    is_buyer = (uid == deal["buyer_id"])
    target_id = deal["seller_id"] if is_buyer else deal["buyer_id"]
    role = "Xaridor" if is_buyer else "Sotuvchi"
    sender_tag = f"@{msg.from_user.username}" if msg.from_user.username else f"{role} ({uid})"
    text_content = msg.text.strip()
    now_s = datetime.now(TZ).isoformat()

    db.log_deal_message(deal["deal_id"], uid, sender_tag, role, text_content)

    if target_id:
        try:
            await bot.send_message(
                target_id,
                f"💬 <b>{role} ({esc_html(sender_tag)}):</b>\n{esc_html(text_content)}"
            )
        except Exception:
            pass

    if is_buyer and any(w in text_content.lower() for w in ["men oldim", "oldim", "tekshirdim", "qabul qildim"]):
        db.q("UPDATE deals SET status='completed', updated_at=? WHERE deal_id=?", (now_s, deal["deal_id"]), commit=True)
        db.q("UPDATE users SET completed_deals=completed_deals+1 WHERE user_id=?", (deal["buyer_id"],), commit=True)
        if deal["seller_id"]:
            db.q("UPDATE users SET completed_deals=completed_deals+1 WHERE user_id=?", (deal["seller_id"],), commit=True)

        finish_text = (
            f"🎉 <b>SAVDO MUVAFFAQIYATLI YAKUNLANDI! (#{deal['deal_id']})</b>\n"
            f"Tovar: <b>{esc_html(deal['title'])}</b>\n"
            f"Summa: <b>{fmt(deal['amount'])} so'm</b>\n\n"
            f"Kafolatli Garant xizmatidan foydalanganingiz uchun rahmat!"
        )
        await msg.answer(finish_text, reply_markup=main_menu_keyboard(uid))
        if deal["seller_id"]:
            try:
                seller_finish = (
                    f"🎉 <b>XARIDOR TOVARNI QABUL QILDI! SAVDO YAKUNLANDI. (#{deal['deal_id']})</b>\n"
                    f"Sizga o'tkaziladigan summa: <b>{fmt(deal['amount'])} so'm</b>\n\n"
                    f"💳 To'lovni olish uchun 16 xonali plastik karta raqamingiz va Ism-Familiyangizni shu yerga yozing:"
                )
                await bot.send_message(deal["seller_id"], seller_finish)
            except Exception:
                pass

# ============================ 14. 5-TUGMA: 🚫 FIRIBGARLAR RO'YXATI ============================
@router.message(F.text == "🚫 Firibgarlar Ro'yxati")
async def blacklist_menu_handler(msg: Message):
    rows = db.get_all_blacklisted(25)
    if not rows:
        await msg.answer(
            "🛡 <b>Hozircha firibgarlar ro'yxati bo'sh!</b>\n"
            "Tizim barcha nohaqliklarni oldini oladi.",
            reply_markup=main_menu_keyboard(msg.from_user.id)
        )
        return

    text = "🚫 <b>FIRIBGARLAR VA MASHENNIKLAR RO'YXATI:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for r in rows:
        u_tag = f"@{esc_html(r['username'])}" if r["username"] else "mavjud emas"
        uid_tag = f"<code>{r['user_id']}</code>" if r["user_id"] else "nomalum"
        date_str = (r["created_at"] or "")[:10]
        text += (
            f"👤 <b>Username:</b> {u_tag}\n"
            f"🆔 <b>Telegram ID:</b> {uid_tag}\n"
            f"📝 <b>Sababi:</b> <i>{esc_html(r['reason'])}</i>\n"
            f"📅 <b>Sana:</b> {date_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
    text += "\n⚠️ <i>Ushbu shaxslarning Telegram ID raqamlari qat'iy bloklangan va ularga bot yopiq!</i>"
    await msg.answer(text, reply_markup=main_menu_keyboard(msg.from_user.id))

# ============================ 15. 6-TUGMA: 💾 ZAXIRA NUSXA (BACKUP) ============================
@router.message(F.text == "💾 Zaxira Nusxa (Backup)")
async def backup_menu_handler(msg: Message):
    text = (
        "💾 <b>MA'LUMOTLAR ZAXIRA NUSXASI (BACKUP)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Siz o'z hisobingizdagi barcha savdolar, arizalar va tarixingizni shifrlangan JSON ko'rinishida yuklab olishingiz "
        "yoki avvalgi zaxira faylingizni kiritishingiz mumkin.\n\n"
        "Kerakli amalni tanlang:"
    )
    kb = [
        [InlineKeyboardButton(text="📤 Zaxira faylini olish (Export)", callback_data="backup_export")],
        [InlineKeyboardButton(text="📥 Zaxira faylini kiritish (Import)", callback_data="backup_import")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "backup_export")
async def backup_export_cb(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    backup_data = db.export_user_backup(uid)
    json_bytes = json.dumps(backup_data, ensure_ascii=False, indent=2).encode("utf-8")
    doc = BufferedInputFile(json_bytes, filename=f"kafil_backup_{uid}.json")
    await call.message.answer_document(
        doc,
        caption="✅ <b>Sizning shaxsiy ma'lumotlar zaxirangiz tayyor!</b>\nUshbu faylni xavfsiz saqlab qo'ying."
    )

@router.callback_query(F.data == "backup_import")
async def backup_import_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(BackupFSM.upload_file)
    await call.message.answer(
        "📥 <b>Zaxira faylini kiritish uchun:</b>\n"
        "Avvalgi <code>.json</code> kengaytmali zaxira faylingizni shu yerga hujjat (file) sifatida yuboring:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(BackupFSM.upload_file, F.document)
async def process_backup_file_upload(msg: Message, state: FSMContext):
    doc = msg.document
    if not doc.file_name.endswith(".json"):
        await msg.answer("⚠️ Iltimos, faqat .json fayl yuboring:")
        return

    file_info = await bot.get_file(doc.file_id)
    downloaded = await bot.download_file(file_info.file_path)
    content = downloaded.read().decode("utf-8")

    try:
        data = json.loads(content)
        ok = db.import_user_backup(msg.from_user.id, data)
        await state.clear()
        if ok:
            await msg.answer("✅ <b>Zaxira ma'lumotlari muvaffaqiyatli tiklandi!</b>", reply_markup=main_menu_keyboard(msg.from_user.id))
        else:
            await msg.answer("❌ Fayl tuzilishi noto'g'ri bo'lganligi sababli tiklanmadi.")
    except Exception as e:
        await msg.answer(f"❌ Faylni o'qishda xatolik: {e}")

# ============================ 16. 7-TUGMA: 📑 ARIZALARIM ============================
@router.message(F.text == "📑 Arizalarim")
async def my_complaints_handler(msg: Message):
    uid = msg.from_user.id
    comps = db.get_user_complaints(uid)
    if not comps:
        await msg.answer(
            "📑 <b>Sizda hali hech qanday shikoyat arizalari mavjud emas.</b>",
            reply_markup=main_menu_keyboard(uid)
        )
        return

    text = "📑 <b>SIZ YUBORGAN ARIZALAR RO'YXATI:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    kb = []
    status_uz = {
        "pending": "⏳ Ko'rib chiqilmoqda (5 kunlik muddat)",
        "withdrawn": "↩️ Qaytarib olingan (Bekor)",
        "blacklisted": "🚫 Sotuvchi Qora ro'yxatga tushirildi"
    }
    for c in comps:
        st = status_uz.get(c["status"], c["status"])
        deadline_str = (c["deadline_at"] or "")[:16].replace("T", " ")
        text += (
            f"🆔 <b>Ariza #{c['id']}</b> | {st}\n"
            f"👤 <b>Kimning ustidan:</b> @{esc_html(c['accused_username'])}\n"
            f"📝 <b>Sabab:</b> <i>{esc_html(c['reason'])}</i>\n"
            f"⏱ <b>Muddat:</b> {deadline_str} gacha\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        if c["status"] == "pending":
            kb.append([InlineKeyboardButton(text=f"Arizani qaytarib olish (#{c['id']})", callback_data=f"withdraw_comp:{c['id']}")])

    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)

@router.callback_query(F.data.startswith("withdraw_comp:"))
async def withdraw_comp_cb(call: CallbackQuery):
    await call.answer()
    comp_id = int(call.data.split(":")[1])
    comp = db.q("SELECT * FROM complaints WHERE id=?", (comp_id,), one=True)
    if not comp or comp["complainant_id"] != call.from_user.id:
        await call.message.answer("Ariza topilmadi.")
        return

    db.withdraw_complaint(comp_id)
    await call.message.edit_text(
        f"✅ <b>ARIZA QAYTARIB OLINDI VA BEKOR BO'LDI! (#{comp_id})</b>\n"
        f"Sotuvchi @{esc_html(comp['accused_username'])} bilan masala hal qilingani qayd etildi."
    )
    if comp["accused_id"]:
        try:
            await bot.send_message(
                comp["accused_id"],
                f"ℹ️ @{esc_html(comp['complainant_username'])} sizning ustingizdan berilgan <b>#{comp_id}</b> arizani bekor qildi. "
                f"Xavf bartaraf etildi!"
            )
        except Exception:
            pass

# ============================ 17. 8-TUGMA: 👑 ADMIN PANEL ============================
@router.message(F.text == "👑 Admin Panel")
@router.message(Command("admin"))
async def admin_panel_handler(msg: Message):
    if not is_admin(msg.from_user.id):
        return

    total_u = db.q("SELECT COUNT(*) c FROM users", one=True)["c"]
    total_d = db.q("SELECT COUNT(*) c FROM deals", one=True)["c"]
    black_c = db.q("SELECT COUNT(*) c FROM blacklist", one=True)["c"]
    pend_w = db.q("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'", one=True)["c"]

    card = db.get_setting("card_number", DEFAULT_CARD_NUMBER)
    owner = db.get_setting("card_owner", DEFAULT_CARD_OWNER)
    comm = db.get_setting("commission_percent", "10")

    text = (
        f"👑 <b>KAFIL BOTI ADMIN BOSHQARUV PANELI</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Foydalanuvchilar: {total_u} ta\n"
        f"📦 Bitimlar: {total_d} ta\n"
        f"🚫 Qora ro'yxatdagilar: {black_c} ta\n"
        f"💸 Kutilayotgan to'lovlar: {pend_w} ta\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 Karta: <code>{card}</code> ({owner})\n"
        f"💼 Komissiya: {comm}%\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = [
        [InlineKeyboardButton(text="📦 Savdolar monitoringi", callback_data="adm_view_deals"),
         InlineKeyboardButton(text="🚫 Firibgarlar boshqaruvi", callback_data="adm_view_scammers")],
        [InlineKeyboardButton(text="📢 Reklama yuborish", callback_data="adm_broadcast"),
         InlineKeyboardButton(text="📢 Majburiy kanallar", callback_data="adm_channels")],
        [InlineKeyboardButton(text="🤖 AI Tekshirish / Jonli Chat", callback_data="adm_ai_chat"),
         InlineKeyboardButton(text="💸 Referal so'rovlari", callback_data="adm_withdrawals")],
        [InlineKeyboardButton(text="💳 Karta sozlash", callback_data="adm_edit_card"),
         InlineKeyboardButton(text="💼 Komissiya sozlash", callback_data="adm_edit_pct")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "adm_view_deals")
async def adm_view_deals_cb(call: CallbackQuery):
    await call.answer()
    deals = db.q("SELECT * FROM deals ORDER BY updated_at DESC LIMIT 10")
    if not deals:
        await call.message.answer("Savdolar mavjud emas.")
        return
    text = "📦 <b>OXIRGI SAVDOLAR:</b>\n\n"
    for d in deals:
        text += f"🆔 <b>#{d['deal_id']}</b> | {d['status']} | <b>{fmt(d['amount'])} so'm</b>\n"
    await call.message.answer(text)

@router.callback_query(F.data == "adm_view_scammers")
async def adm_view_scammers_cb(call: CallbackQuery):
    await call.answer()
    kb = [
        [InlineKeyboardButton(text="➕ Firibgar qo'shish", callback_data="adm_add_black")],
        [InlineKeyboardButton(text="📋 Barcha firibgarlar", callback_data="adm_list_black")]
    ]
    await call.message.answer("🚫 <b>FIRIBGARLARNI BOSHQARISH:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "adm_add_black")
async def adm_add_black_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.add_black_user)
    await call.message.answer("Qora ro'yxatga qo'shiladigan shaxsning @username yoki Telegram ID sini kiriting:")

@router.message(AdminFSM.add_black_user)
async def adm_add_black_user_step(msg: Message, state: FSMContext):
    target = msg.text.strip().lstrip("@")
    await state.update_data(black_target=target)
    await state.set_state(AdminFSM.add_black_reason)
    await msg.answer("Sababini kiriting:")

@router.message(AdminFSM.add_black_reason)
async def adm_add_black_reason_step(msg: Message, state: FSMContext):
    reason = msg.text.strip()
    data = await state.get_data()
    target = data["black_target"]
    await state.clear()

    uid = int(target) if target.isdigit() else None
    un = target if not target.isdigit() else None
    db.add_to_blacklist(user_id=uid, username=un, reason=reason, added_by="admin")
    await msg.answer(f"✅ @{esc_html(target)} muvaffaqiyatli qora ro'yxatga kiritildi.")

@router.callback_query(F.data == "adm_list_black")
async def adm_list_black_cb(call: CallbackQuery):
    await call.answer()
    rows = db.get_all_blacklisted(15)
    if not rows:
        await call.message.answer("Qora ro'yxat bo'sh.")
        return
    text = "🚫 <b>QORA RO'YXATDAGI SHAXSLAR:</b>\n\n"
    for r in rows:
        text += f"ID: <code>{r['user_id']}</code> | @{r['username']} | <i>{r['reason']}</i>\n"
    await call.message.answer(text)

@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.broadcast)
    await call.message.answer("Foydalanuvchilarga yuboriladigan xabarni (matn, rasm yoki forward) yuboring:")

@router.message(AdminFSM.broadcast)
async def adm_broadcast_process(msg: Message, state: FSMContext):
    await state.clear()
    users = db.q("SELECT user_id FROM users")
    sent, failed = 0, 0
    status_msg = await msg.answer("📢 Reklama tarqatilmoqda...")
    for u in users:
        try:
            await msg.copy_to(chat_id=u["user_id"])
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    await status_msg.edit_text(f"✅ Reklama yakunlandi!\nYetkazildi: <b>{sent} ta</b>\nYetkazilmadi: <b>{failed} ta</b>")

@router.callback_query(F.data == "adm_channels")
async def adm_channels_cb(call: CallbackQuery):
    await call.answer()
    chs = db.q("SELECT * FROM mandatory_channels")
    text = "📢 <b>MAJBURIIY OBUNA KANALLARI:</b>\n\n"
    for c in chs:
        text += f"• <b>{c['channel_title']}</b> (<code>{c['channel_id']}</code>) — <a href=\"{c['invite_link']}\">Havola</a>\n"
    kb = [
        [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="adm_add_channel")],
        [InlineKeyboardButton(text="🗑 Kanalni o'chirish", callback_data="adm_del_channel")]
    ]
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), disable_web_page_preview=True)

@router.callback_query(F.data == "adm_add_channel")
async def adm_add_channel_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.add_channel_id)
    await call.message.answer("Kanal ID yoki @username'ini kiriting (masalan: @kanalim yoki -100123456789):")

@router.message(AdminFSM.add_channel_id)
async def adm_proc_ch_id(msg: Message, state: FSMContext):
    await state.update_data(ch_id=msg.text.strip())
    await state.set_state(AdminFSM.add_channel_title)
    await msg.answer("Kanal nomini kiriting:")

@router.message(AdminFSM.add_channel_title)
async def adm_proc_ch_title(msg: Message, state: FSMContext):
    await state.update_data(ch_title=msg.text.strip())
    await state.set_state(AdminFSM.add_channel_link)
    await msg.answer("Kanalga ulanish havolasini (https://t.me/...) kiriting:")

@router.message(AdminFSM.add_channel_link)
async def adm_proc_ch_link(msg: Message, state: FSMContext):
    link = msg.text.strip()
    data = await state.get_data()
    await state.clear()
    db.q("INSERT INTO mandatory_channels (channel_id, channel_title, invite_link) VALUES (?, ?, ?)",
         (data["ch_id"], data["ch_title"], link), commit=True)
    await msg.answer("✅ Kanal majburiy ro'yxatga qo'shildi!")

@router.callback_query(F.data == "adm_del_channel")
async def adm_del_channel_cb(call: CallbackQuery):
    await call.answer()
    chs = db.q("SELECT * FROM mandatory_channels")
    if not chs:
        await call.message.answer("O'chirish uchun kanallar mavjud emas.")
        return
    kb = []
    for c in chs:
        kb.append([InlineKeyboardButton(text=f"🗑 {c['channel_title']}", callback_data=f"adm_remove_ch:{c['id']}")])
    await call.message.answer("O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("adm_remove_ch:"))
async def adm_remove_ch_cb(call: CallbackQuery):
    await call.answer()
    ch_id = int(call.data.split(":")[1])
    db.q("DELETE FROM mandatory_channels WHERE id=?", (ch_id,), commit=True)
    await call.message.edit_text("✅ Kanal ro'yxatdan o'chirildi.")

@router.callback_query(F.data == "adm_ai_chat")
async def adm_ai_chat_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.ai_chat)
    await call.message.answer(
        "🤖 <b>GEMINI AI ADMIN MONITORING VA JONLI TAHLIL:</b>\n\n"
        "Tizimdagi savdolar statistikasi va xavfli holatlar bo'yicha savolingizni bering "
        "(masalan: <i>'Savdolar holati qanday?', 'Firibgarlik xavfi bormi?'</i>):",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(AdminFSM.ai_chat)
async def adm_ai_chat_process(msg: Message, state: FSMContext):
    if msg.text == "❌ Bekor qilish":
        await state.clear()
        await msg.answer("Admin paneliga qaytildi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    query = msg.text.strip()
    u_cnt = db.q("SELECT COUNT(*) c FROM users", one=True)["c"]
    d_cnt = db.q("SELECT COUNT(*) c FROM deals", one=True)["c"]
    d_active = db.q("SELECT COUNT(*) c FROM deals WHERE status IN ('in_trade', 'waiting_payment')", one=True)["c"]
    d_completed = db.q("SELECT COUNT(*) c FROM deals WHERE status='completed'", one=True)["c"]
    b_cnt = db.q("SELECT COUNT(*) c FROM blacklist", one=True)["c"]

    summary = (
        f"Foydalanuvchilar: {u_cnt}, Jami savdolar: {d_cnt}, "
        f"Faol savdolar: {d_active}, Yakunlangan: {d_completed}, Qora ro'yxat: {b_cnt}."
    )
    ai_ans = await analyze_deals_for_admin(summary, query)
    await msg.answer(f"🤖 <b>GEMINI AI TAHLILI:</b>\n\n{ai_ans}")

@router.callback_query(F.data == "adm_withdrawals")
async def adm_withdrawals_cb(call: CallbackQuery):
    await call.answer()
    ws = db.q("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id ASC LIMIT 10")
    if not ws:
        await call.message.answer("Kutilayotgan to'lov so'rovlari yo'q.")
        return
    for w in ws:
        kb = [
            [InlineKeyboardButton(text="✅ To'landi deb tasdiqlash", callback_data=f"adm_pay_w:{w['id']}")],
            [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"adm_rej_w:{w['id']}")]
        ]
        await call.message.answer(
            f"💸 <b>TO'LOV SO'ROVI #{w['id']}</b>\n"
            f"Foydalanuvchi: ID <code>{w['user_id']}</code>\n"
            f"Summa: <b>{fmt(w['amount'])} so'm</b>\n"
            f"Karta: <code>{w['card_number']}</code> ({w['card_owner']})",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
        )

@router.callback_query(F.data.startswith("adm_pay_w:"))
async def adm_pay_w_cb(call: CallbackQuery):
    await call.answer()
    w_id = int(call.data.split(":")[1])
    db.q("UPDATE withdrawals SET status='approved' WHERE id=?", (w_id,), commit=True)
    w = db.q("SELECT * FROM withdrawals WHERE id=?", (w_id,), one=True)
    await call.message.edit_text(f"✅ #{w_id} so'rov tasdiqlandi!")
    if w:
        try:
            await bot.send_message(
                w["user_id"],
                f"🎉 <b>REFERAL PULLARINGIZ KARTANGIZGA O'TKAZILDI!</b>\n"
                f"Summa: <b>{fmt(w['amount'])} so'm</b>\n"
                f"Karta: <code>{w['card_number']}</code>\n\n"
                f"Kafil botidan foydalanganingiz uchun rahmat!"
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("adm_rej_w:"))
async def adm_rej_w_cb(call: CallbackQuery):
    await call.answer()
    w_id = int(call.data.split(":")[1])
    w = db.q("SELECT * FROM withdrawals WHERE id=?", (w_id,), one=True)
    if w:
        db.q("UPDATE users SET referral_balance=referral_balance+? WHERE user_id=?", (w["amount"], w["user_id"]), commit=True)
        db.q("UPDATE withdrawals SET status='rejected' WHERE id=?", (w_id,), commit=True)
        await call.message.edit_text(f"❌ #{w_id} so'rov rad etildi va mablag' qaytarildi.")
        try:
            await bot.send_message(
                w["user_id"],
                f"⚠️ #{w_id} so'rovingiz admin tomonidan rad etildi va {fmt(w['amount'])} so'm balansingizga qaytarildi."
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("adm_reply_ticket:"))
async def adm_reply_ticket_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    t_id = int(call.data.split(":")[1])
    await state.set_state(AdminFSM.reply_ticket_text)
    await state.update_data(reply_ticket_id=t_id)
    await call.message.answer(f"#{t_id} murojaat bo'yicha javobingizni yozing:")

@router.message(AdminFSM.reply_ticket_text)
async def adm_reply_ticket_send(msg: Message, state: FSMContext):
    admin_reply = msg.text.strip()
    data = await state.get_data()
    t_id = data["reply_ticket_id"]
    await state.clear()

    db.q("UPDATE ai_tickets SET status='admin_answered', admin_reply=? WHERE id=?", (admin_reply, t_id), commit=True)
    ticket = db.q("SELECT * FROM ai_tickets WHERE id=?", (t_id,), one=True)
    if ticket:
        try:
            await bot.send_message(
                ticket["user_id"],
                f"👨‍💼 <b>ADMIN JAVOBI:</b>\n\n"
                f"{admin_reply}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>Murojaatingiz: «{esc_html(ticket['question'])}»</i>"
            )
            await msg.answer("✅ Javob foydalanuvchiga muvaffaqiyatli yetkazildi.")
        except Exception as e:
            await msg.answer(f"Yetkazishda xatolik: {e}")

@router.callback_query(F.data == "adm_edit_card")
async def adm_edit_card_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.change_card)
    await call.message.answer("Yangi 16 xonali karta raqamini kiriting:")

@router.message(AdminFSM.change_card)
async def adm_proc_new_card(msg: Message, state: FSMContext):
    card = re.sub(r"\s+", "", msg.text.strip())
    await state.update_data(new_card=card)
    await state.set_state(AdminFSM.change_owner)
    await msg.answer("Karta egasining Ism-Familiyasini kiriting:")

@router.message(AdminFSM.change_owner)
async def adm_proc_new_owner(msg: Message, state: FSMContext):
    owner = msg.text.strip().upper()
    data = await state.get_data()
    await state.clear()
    db.set_setting("card_number", data["new_card"])
    db.set_setting("card_owner", owner)
    await msg.answer(f"✅ Karta yangilandi:\n<code>{data['new_card']}</code> ({owner})")

@router.callback_query(F.data == "adm_edit_pct")
async def adm_edit_pct_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.change_commission)
    await call.message.answer("Yangi komissiya foizini kiriting (masalan: 10):")

@router.message(AdminFSM.change_commission)
async def adm_proc_new_pct(msg: Message, state: FSMContext):
    txt = msg.text.strip()
    if not txt.isdigit():
        await msg.answer("Faqat butun son kiriting:")
        return
    db.set_setting("commission_percent", txt)
    await state.clear()
    await msg.answer(f"✅ Komissiya foizi {txt}% ga o'zgartirildi.")

# ============================ 18. BEKOR QILISH HANDLERLARI ============================
@router.callback_query(F.data.startswith("cancel_deal:"))
async def cancel_deal_cb(call: CallbackQuery):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = db.get_deal(d_id)
    if not deal:
        await call.message.answer("Bitim topilmadi.")
        return
    if deal["status"] not in ("created", "waiting_payment"):
        await call.message.answer("Muzlatilgan savdoni bekor qilib bo'lmaydi.")
        return
    now_s = datetime.now(TZ).isoformat()
    db.q("UPDATE deals SET status='cancelled', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)
    if deal.get("payment_id"):
        await cancel_payment_async(deal["payment_id"])
    await call.message.edit_text(f"❌ <b>#{d_id}</b> raqamli bitim bekor qilindi.")

# ============================ 19. RENDER DUMMY WEB SERVER ============================
async def run_render_web_server():
    try:
        async def handle_ping(request):
            return web.Response(text="OK: Kafil Escrow Bot with Gemini AI is running smoothly on Render!\n")

        app = web.Application()
        app.router.add_get("/", handle_ping)
        app.router.add_get("/health", handle_ping)
        app.router.add_get("/ping", handle_ping)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"Render HTTP server '0.0.0.0':{PORT} da muvaffaqiyatli ishga tushdi.")
    except Exception as e:
        logger.warning(f"Render HTTP server ishga tushirishda xato: {e}")

# ============================ 20. ASOSIY ISHGA TUSHIRISH (MAIN) ============================
async def main():
    await run_render_web_server()
    asyncio.create_task(complaints_auto_expire_loop())
    asyncio.create_task(referral_leave_checker_loop())
    logger.info(f"Kafil / Garant Boti to'liq ishga tushdi! Adminlar: {ADMIN_IDS}")
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            if "Unauthorized" in str(e):
                logger.critical("Bot tokeni yaroqsiz! Telegram @BotFather orqali tekshiring.")
                break
            logger.error(f"Telegram polling uzilishi: {e}. 5 soniyadan so'ng qayta ulanadi...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
