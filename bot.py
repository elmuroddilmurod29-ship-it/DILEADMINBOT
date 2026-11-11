"""
BOT.PY - Kafil / Garant (Escrow Middleman) Telegram Boti
Aiogram 3.x asosida to'liq, xatosiz, modulli arxitektura va TolovAPI rasmiy integratsiyasi.
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
from typing import Optional, List, Dict, Any, Tuple

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
    TOLOV_API_URL, SHOP_ID, SHOP_KEY, PORT, TZ, esc_html, fmt, is_admin, format_card
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
        [KeyboardButton(text="📑 Arizalarim"), KeyboardButton(text="👑 Admin Panel")]
    ]
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
    deal_complaint_reason = State()

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
async def tolovapi_request(path: str, params: dict, method: str = "POST") -> dict:
    """TolovAPI.uz rasmiy API mijoz funksiyasi (X-Shop-Key sarlavhasi bilan)."""
    url = f"{TOLOV_API_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {"X-Shop-Key": SHOP_KEY}
    full_params = {"shop_id": int(SHOP_ID) if str(SHOP_ID).isdigit() else SHOP_ID, **params}
    timeout = ClientTimeout(total=15)
    try:
        async with ClientSession(timeout=timeout) as session:
            if method.upper() == "GET":
                async with session.get(url, params=full_params, headers=headers) as resp:
                    return await resp.json()
            else:
                async with session.post(url, json=full_params, headers=headers) as resp:
                    return await resp.json()
    except Exception as exc:
        logger.error(f"TolovAPI xatosi ({path}): {exc}")
        return {"ok": False, "error": str(exc)}

async def create_payment_async(amount: int, order_id: Optional[str] = None, user_id: Optional[int] = None) -> Optional[dict]:
    """TolovAPI orqali yangi to'lov hisobini ochish."""
    oid = str(order_id) if order_id else f"ORD{random.randint(100000, 999999)}"
    res = await tolovapi_request("payment/create", {"amount": int(amount), "order_id": oid})

    if res.get("error_code") == "amount_busy" or res.get("error") == "amount_busy":
        suggested = res.get("suggested_amount")
        if suggested and int(suggested) != amount:
            logger.info(f"TolovAPI: {amount} summa band, taklif: {suggested} bilan qayta ochilmoqda...")
            res = await tolovapi_request("payment/create", {"amount": int(suggested), "order_id": oid})

    if res.get("ok") or res.get("payment_id"):
        return {
            "ok": True,
            "payment_id": str(res.get("payment_id", "")),
            "amount": int(res.get("amount", amount)),
            "card_number": res.get("card_number") or db.get_setting("card_number", DEFAULT_CARD_NUMBER),
            "card_owner": DEFAULT_CARD_OWNER,
            "expires_in": res.get("expires_in", 300),
            "payment_url": res.get("payment_url", ""),
            "raw": res
        }
    logger.warning(f"TolovAPI create muvaffaqiyatsiz bo'ldi: {res}")
    return None

async def check_payment_status_async(payment_id: str) -> Optional[dict]:
    """TolovAPI orqali to'lov holatini tekshirish (GET orqali)."""
    if not payment_id:
        return None
    res = await tolovapi_request("payment/status", {"payment_id": str(payment_id)}, method="GET")
    st = str(res.get("status", "")).lower()
    return {
        "ok": res.get("ok", False) or bool(st),
        "paid": st == "paid",
        "status": st,
        "raw": res
    }

async def cancel_payment_async(payment_id: str) -> bool:
    """TolovAPI orqali to'lovni bekor qilish."""
    if not payment_id:
        return False
    res = await tolovapi_request("payment/cancel", {"payment_id": str(payment_id)}, method="POST")
    return res.get("ok", False) or res.get("status") == "cancel"

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
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Siz taklif qilgan foydalanuvchi (@{esc_html(u['username'] or str(referred_id))}) botdan to'liq ro'yxatdan o'tdi.\n"
            f"💰 Hisobingizga <b>+100 so'm</b> qo'shildi!\n"
            f"Bugun qo'shgan do'stlaringiz: <b>{today_count + 1}/20 ta</b>"
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

    new_bal = referrer["referral_balance"] - 200
    db.q("UPDATE users SET referral_balance=? WHERE user_id=?", (new_bal, referrer_id), commit=True)
    db.q("UPDATE referrals SET status='penalized' WHERE id=?", (ref["id"],), commit=True)

    try:
        left_u = db.get_user(user_id)
        tag = f"@{left_u['username']}" if left_u and left_u['username'] else f"ID {user_id}"
        await bot.send_message(
            referrer_id,
            f"⚠️ <b>REFERAL JARIMASI: -200 so'm!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Siz taklif qilgan {tag} kanaldan chiqib ketdi.\n"
            f"Qoidalarga muvofiq hisobingizdan <b>200 so'm</b> jarima ayirildi.\n"
            f"Joriy balansingiz: <b>{fmt(new_bal)} so'm</b>"
        )
    except Exception:
        pass

# ============================ 6. ASINXRON FON TAYMERLARI ============================
async def deal_5min_timer(deal_id: str):
    """5 daqiqalik asinxron to'lov taymeri."""
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
                f"⏱ <b>5 DAQIQA VAQT TUGADI! (#{deal_id})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Belgilangan 5 daqiqa ichida to'lov qilinmagani sababli bitim avtomatik <b>bekor qilindi</b>."
            )
        except Exception:
            pass

async def complaints_auto_expire_loop():
    """5 kunda arizaga javob bermagan sotuvchini avtomatik qora ro'yxatga kirituvchi loop."""
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
                        f"⚖️ <b>ARIZA BO'YICHA QAROR: #{c['id']}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Sotuvchi @{esc_html(c['accused_username'])} 5 kun ichida javob bermadi va "
                        f"butunlay <b>QORA RO'YXATGA</b> kiritildi!"
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"complaints loop xatosi: {e}")

async def referral_leave_checker_loop():
    """Kanaldan chiqib ketgan referallarni tekshiruvchi loop."""
    while True:
        try:
            await asyncio.sleep(1800)
            channels = db.q("SELECT * FROM mandatory_channels")
            if not channels:
                continue
            active_refs = db.q("SELECT * FROM referrals WHERE status='rewarded'")
            for r in active_refs:
                uid = r["referred_id"]
                is_sub, _ = await check_user_subscribed(uid)
                if not is_sub:
                    await penalize_referral_if_left(uid)
        except Exception as e:
            logger.error(f"referral checker loop xatosi: {e}")

# ============================ 7. BUYRUQLAR VA BEKOR QILISH ============================
@router.message(F.text.contains("Bekor") | F.text.startswith("❌"))
async def universal_cancel_handler(msg: Message, state: FSMContext):
    """Har qanday amaliyotni bekor qilib asosiy menyuga qaytaruvchi universal handler."""
    await state.clear()
    await msg.answer(
        "❌ <b>Barcha amallar bekor qilindi.</b>\n"
        "Siz asosiy menyudasiz. Kerakli bo'limni tanlang:",
        reply_markup=main_menu_keyboard(msg.from_user.id)
    )

@router.callback_query(F.data == "cancel_fsm")
async def cancel_fsm_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Bekor qilindi.")
    await call.message.edit_text("❌ Amaliyot bekor qilindi.")

@router.message(CommandStart())
async def start_cmd(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    username = msg.from_user.username or ""
    full_name = msg.from_user.full_name or "Foydalanuvchi"

    args = msg.text.split(maxsplit=1)[1] if len(msg.text.split()) > 1 else ""
    referred_by = None

    if args.startswith("ref_"):
        ref_raw = args.replace("ref_", "").strip()
        if ref_raw.isdigit() and int(ref_raw) != uid:
            referred_by = int(ref_raw)
            exists = db.q("SELECT id FROM referrals WHERE referred_id=?", (uid,), one=True)
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
                f"Xaridor tovarni tekshirib olgach rozilik berishi bilan pul sizga o'tkaziladi.",
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
@router.message(F.text.contains("Tarix"))
async def history_handler(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    deals = db.q("SELECT * FROM deals WHERE buyer_id=? OR seller_id=? ORDER BY updated_at DESC LIMIT 10", (uid, uid))
    if not deals:
        await msg.answer(
            "📂 <b>Sizda hali hech qanday savdolar tarixi mavjud emas.</b>\n\n"
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
            f"📦 <b>Tovar:</b> <b>{esc_html(d['title'])}</b>\n"
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
        f"📄 <b>SAVDO TAFSILOTLARI VA YOZISHMALAR LOGI:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Bitim ID:</b> <b>#{deal['deal_id']}</b>\n"
        f"📦 <b>Tovar/Xizmat:</b> <b>{esc_html(deal['title'])}</b>\n"
        f"💵 <b>Kelishilgan summa:</b> <b>{fmt(deal['amount'])} so'm</b>\n"
        f"💼 <b>Komissiya:</b> <b>{fmt(deal['fee'])} so'm</b>\n"
        f"💰 <b>Jami to'langan summa:</b> <b>{fmt(deal['total_to_pay'])} so'm</b>\n"
        f"🔒 <b>Holati:</b> <b>{deal['status'].upper()}</b>\n"
        f"📅 <b>Boshlangan sana:</b> {created_date}\n"
        f"🔄 <b>Oxirgi yangilanish:</b> {updated_date}\n"
        f"🛒 <b>Xaridor:</b> @{esc_html(deal['buyer_username'] or 'mavjud emas')} (ID: <code>{deal['buyer_id']}</code>)\n"
        f"💼 <b>Sotuvchi:</b> @{esc_html(deal['seller_username'] or 'mavjud emas')} (ID: <code>{deal['seller_id'] or 'nomalum'}</code>)\n"
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

@router.callback_query(F.data.startswith("file_comp:"))
async def file_comp_deal_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    d_id = call.data.split(":")[1]
    deal = db.get_deal(d_id)
    if not deal:
        await call.message.answer("Bitim topilmadi.")
        return

    uid = call.from_user.id
    is_buyer = (uid == deal["buyer_id"])
    target_username = deal["seller_username"] if is_buyer else deal["buyer_username"]
    target_id = deal["seller_id"] if is_buyer else deal["buyer_id"]

    await state.set_state(SOSFSM.deal_complaint_reason)
    await state.update_data(comp_deal_id=d_id, comp_target_un=target_username, comp_target_id=target_id)

    await call.message.answer(
        f"🚨 <b>#{d_id} RAQAMLI BITIM BO'YICHA ARIZA QOLDIRISH</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Savdo: <b>{esc_html(deal['title'])}</b>\n"
        f"Summa: <b>{fmt(deal['amount'])} so'm</b>\n"
        f"Ikkinchi tomon: @{esc_html(target_username or 'nomalum')}\n\n"
        f"📝 <b>Ushbu savdo bo'yicha qanday muammo yoki aldov yuz berdi? Batafsil yozib bering:</b>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(SOSFSM.deal_complaint_reason)
async def process_deal_comp_reason(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    reason = msg.text.strip()
    data = await state.get_data()
    d_id = data["comp_deal_id"]
    target_un = data["comp_target_un"]
    target_id = data["comp_target_id"]
    await state.clear()

    comp_id = db.create_complaint(
        deal_id=d_id,
        complainant_id=msg.from_user.id,
        complainant_username=msg.from_user.username or "",
        accused_id=target_id,
        accused_username=target_un,
        reason=reason
    )
    now_s = datetime.now(TZ).isoformat()
    db.q("UPDATE deals SET status='dispute', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)

    await msg.answer(
        f"🚨 <b>ARIZANGIZ QABUL QILINDI! (#{comp_id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Bitim: <b>#{d_id}</b> nizoli (dispute) holatga o'tkazildi va pul kafil hisobida muzlatildi.\n"
        f"Ikkinchi tomonga rasmiy ogohlantirish yuborildi. Unga masalani hal qilish uchun <b>5 kun</b> vaqt berildi.\n"
        f"Agar 5 kunda hal qilmasa, uning hisobi QORA RO'YXATGA kiritiladi va pulingiz qaytarib beriladi.",
        reply_markup=main_menu_keyboard(msg.from_user.id)
    )

    if target_id:
        try:
            await bot.send_message(
                target_id,
                f"⚠️ <b>DIQQAT: SIZNING USTINGIZDAN #{d_id} BITIM BO'YICHA ARIZA TUSHDI! (#{comp_id})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Ariza beruvchi: @{esc_html(msg.from_user.username or 'Foydalanuvchi')}\n"
                f"Sabab: <i>{esc_html(reason)}</i>\n\n"
                f"❗️ <b>OGOHLANTIRISH:</b> Xaridor bilan zudlik bilan bog'lanib masalani hal qiling! Sizga <b>5 kun</b> muhlat berildi."
            )
        except Exception:
            pass

    for a_id in ADMIN_IDS:
        try:
            await bot.send_message(
                a_id,
                f"🚨 <b>YANGI ARIZA (Bitim #{d_id}): #{comp_id}</b>\n"
                f"Kimdan: @{esc_html(msg.from_user.username or '')} (ID: <code>{msg.from_user.id}</code>)\n"
                f"Kimga: @{esc_html(target_un)} (ID: <code>{target_id or 'nomalum'}</code>)\n"
                f"Sabab: <i>{esc_html(reason)}</i>"
            )
        except Exception:
            pass

# ============================ 9. 2-TUGMA: 🆘 SOS (YORDAM) ============================
@router.message(F.text.contains("SOS") | F.text.contains("Yordam"))
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
        "🤖 <b>GEMINI AI ASSISTENTI:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Qanday yordam bera olaman? Muammoingizni yoki savolingizni batafsil yozib qoldiring:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(SOSFSM.ai_query)
async def process_ai_query(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("❌ Amaliyot bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    user_query = msg.text.strip()
    wait_msg = await msg.answer("🤖 <i>Kafil AI assistenti javob tayyorlamoqda...</i>")

    ai_resp = await ask_gemini_ai(user_query)
    try:
        await wait_msg.delete()
    except Exception:
        pass

    ticket_id = db.q(
        "INSERT INTO ai_tickets (user_id, user_name, question, status, created_at) VALUES (?, ?, ?, 'open', ?)",
        (msg.from_user.id, msg.from_user.full_name or "Foydalanuvchi", user_query, datetime.now(TZ).isoformat()),
        commit=True
    )

    # Agar AI javobida ADMIN_GA_UZATISH bo'lsa (foydalanuvchi qat'iy adminga yozishni so'raganida)
    if "ADMIN_GA_UZATISH" in ai_resp:
        clean_resp = ai_resp.replace("ADMIN_GA_UZATISH", "").strip()
        await msg.answer(
            f"{clean_resp}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>Murojaatingiz administratorga yetkazildi. Admin javob berishi bilan bot sizga xabar yo'llaydi.</i>",
            reply_markup=main_menu_keyboard(msg.from_user.id)
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Javob berish", callback_data=f"adm_reply_ticket:{ticket_id}")]
        ])
        for a_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    a_id,
                    f"🆘 <b>YANGI MUROJAAT (Adminga uzatildi): #{ticket_id}</b>\n"
                    f"Foydalanuvchi: <b>{esc_html(msg.from_user.full_name)}</b> (ID: <code>{msg.from_user.id}</code>)\n"
                    f"Murojaat:\n<i>{esc_html(user_query)}</i>",
                    reply_markup=admin_kb
                )
            except Exception:
                pass
        await state.clear()
        return

    # Normal javob bo'lsa: Foydalanuvchiga to'liq va chiroyli javob beriladi
    kb = [
        [InlineKeyboardButton(text="👨‍💼 Adminga yuborish", callback_data=f"fwd_admin:{ticket_id}")],
        [InlineKeyboardButton(text="✅ Tushunarli, rahmat", callback_data="ai_resolved")]
    ]
    await msg.answer(
        f"{ai_resp}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Agar javob sizni qoniqtirmasa yoki qo'shimcha yordam kerak bo'lsa, quyidagi tugma orqali adminga yo'llashingiz mumkin.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

@router.callback_query(F.data == "ai_resolved")
async def ai_resolved_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Yordam berganimizdan xursandmiz!")
    await call.message.edit_text("✅ <b>Murojaatingiz yakunlandi.</b>\nYana qandaydir savollaringiz bo'lsa bemalol murojaat qiling!")

@router.callback_query(F.data.startswith("fwd_admin:"))
async def fwd_admin_cb(call: CallbackQuery, state: FSMContext):
    await call.answer("Murojaat adminga yuborildi.")
    t_id = int(call.data.split(":")[1])
    ticket = db.q("SELECT * FROM ai_tickets WHERE id=?", (t_id,), one=True)
    if not ticket:
        await call.message.answer("Murojaat topilmadi.")
        return

    await call.message.edit_text(
        "✅ <b>Murojaatingiz administratorga yetkazildi!</b>\n"
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
        f"📞 <b>ADMIN BILAN TO'G'RIDAN-TO'G'RI BOG'LANISH:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
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
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
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
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    reason = msg.text.strip()
    data = await state.get_data()
    seller_tag = data["target_seller"]
    await state.clear()

    seller_u = db.get_user_by_username(seller_tag)
    accused_id = seller_u["user_id"] if seller_u else None

    deadline = (datetime.now(TZ) + timedelta(days=5)).isoformat()
    comp_id = db.create_complaint(
        complainant_id=msg.from_user.id,
        complainant_username=msg.from_user.username or "",
        accused_username=seller_tag,
        accused_id=accused_id,
        reason=reason,
        deal_id=""
    )
    db.q("UPDATE complaints SET deadline_at=? WHERE id=?", (deadline, comp_id), commit=True)

    await msg.answer(
        f"🚨 <b>ARIZANGIZ QABUL QILINDI! (#{comp_id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Sotuvchi <b>@{esc_html(seller_tag)}</b> ga rasmiy ariza xabarnomasi yuborildi.\n"
        f"Unga masalani hal qilish uchun <b>5 kun</b> muhlat berildi.\n"
        f"Agar 5 kun ichida hal qilmasa, uning hisobi butunlay <b>QORA RO'YXATGA</b> kiritiladi.",
        reply_markup=main_menu_keyboard(msg.from_user.id)
    )

    if accused_id:
        try:
            await bot.send_message(
                accused_id,
                f"⚠️ <b>DIQQAT: SIZNING USTINGIZDAN ARIZA TUSHDI! (#{comp_id})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Ariza beruvchi: @{esc_html(msg.from_user.username or 'Xaridor')}\n"
                f"Sabab: <i>{esc_html(reason)}</i>\n\n"
                f"❗️ <b>OGOHLANTIRISH:</b> Foydalanuvchi bilan zudlik bilan bog'lanib masalani hal qiling!\n"
                f"Sizga <b>5 kun</b> vaqt berildi. Aks holda profilingiz QORA RO'YXATGA olinadi va bot bloklanadi."
            )
        except Exception:
            pass

    for a_id in ADMIN_IDS:
        try:
            await bot.send_message(
                a_id,
                f"🚨 <b>YANGI ARIZA: #{comp_id}</b>\n"
                f"Kimdan: @{esc_html(msg.from_user.username or '')} (ID: <code>{msg.from_user.id}</code>)\n"
                f"Kimga: @{esc_html(seller_tag)} (ID: <code>{accused_id or 'nomalum'}</code>)\n"
                f"Sabab: <i>{esc_html(reason)}</i>"
            )
        except Exception:
            pass

# ============================ 10. 3-TUGMA: 👥 REFERAL TIZIMI ============================
@router.message(F.text.contains("Referal"))
async def referral_menu_handler(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    u = db.get_user(uid)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"

    today_prefix = datetime.now(TZ).strftime("%Y-%m-%d")
    today_count = db.q("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='rewarded' AND created_at LIKE ?",
                       (uid, f"{today_prefix}%"), one=True)["c"]
    total_refs = db.q("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='rewarded'", (uid,), one=True)["c"]

    ref_bal = u["referral_balance"] if u else 0
    text = (
        f"👥 <b>DO'STLARNI TAKLIF QILISH VA PUL ISHLASH</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Referal balansingiz:</b> <b>{fmt(ref_bal)} so'm</b>\n"
        f"👥 <b>Jami taklif qilganlaringiz:</b> <b>{total_refs} ta</b>\n"
        f"📅 <b>Bugungi takliflar:</b> <b>{today_count}/20 ta</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Qoidalar:</b>\n"
        f"• Har bir qo'shilgan do'stingiz uchun <b>+100 so'm</b> olasiz.\n"
        f"• Do'stingiz botga kirib, kanallarga a'zo bo'lgach pul beriladi.\n"
        f"• Kunlik limit: <b>20 ta</b> do'st.\n"
        f"• Agar do'stingiz kanaldan chiqib ketsa: <b>-200 so'm jarima</b>.\n"
        f"• Minimal pul yechish: <b>10 000 so'm</b>.\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>Sizning shaxsiy referal havolangiz:</b>\n"
        f"👉 <code>{ref_link}</code>\n"
        f"<i>(Ustiga bosing, avtomatik nusxalanadi!)</i>"
    )
    kb = [
        [InlineKeyboardButton(text="💸 Pulni yechib olish", callback_data="ref_withdraw")],
        [InlineKeyboardButton(text="📊 Mening referallarim", callback_data="ref_stats")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "ref_stats")
async def ref_stats_callback(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    refs = db.q("SELECT * FROM referrals WHERE referrer_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
    if not refs:
        await call.message.answer("Sizda hali referallar mavjud emas.")
        return
    text = "👥 <b>OXIRGI TAKLIF QILINGANLAR:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for r in refs:
        date_s = (r["created_at"] or "")[:16].replace("T", " ")
        st = "✅ Mukofot berilgan" if r["status"] == "rewarded" else ("⚠️ Jarima" if r["status"] == "penalized" else "⏳ Kutilmoqda")
        text += f"👤 ID: <code>{r['referred_id']}</code> | {st} ({date_s})\n"
    await call.message.answer(text)

@router.callback_query(F.data == "ref_withdraw")
async def ref_withdraw_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    uid = call.from_user.id
    u = db.get_user(uid)
    bal = u["referral_balance"] if u else 0

    if bal < 10000:
        await call.message.answer(
            f"⚠️ <b>MABLAG' YETARLI EMAS!</b>\n\n"
            f"Minimal yechish summasi: <b>10 000 so'm</b>.\n"
            f"Sizning balansingiz: <b>{fmt(bal)} so'm</b>.\n\n"
            f"Mablag'ni yechish uchun do'stlaringizni taklif qilib balansingizni 10 000 so'mga yetkazing."
        )
        return

    await state.set_state(WithdrawFSM.card_number)
    await state.update_data(withdraw_amount=bal)
    await call.message.answer(
        f"💸 <b>PULNI YECHIB OLISH: {fmt(bal)} so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"To'lovni qabul qiluvchi <b>16 xonali plastik karta raqamini</b> kiriting:\n"
        f"<i>(Masalan: 8600 0000 0000 0000 yoki 9860 ...)</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(WithdrawFSM.card_number)
async def withdraw_proc_card(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    card = re.sub(r"\D", "", msg.text.strip())
    if len(card) != 16:
        await msg.answer("⚠️ Iltimos, 16 xonali karta raqamini to'g'ri kiriting:")
        return

    await state.update_data(card_number=card)
    await state.set_state(WithdrawFSM.card_owner)
    await msg.answer(
        f"Karta: <code>{format_card(card)}</code>\n\n"
        f"👤 <b>Karta egasining Ism-Familiyasini kiriting:</b>\n"
        f"<i>(Masalan: ALIYEV VALI)</i>"
    )

@router.message(WithdrawFSM.card_owner)
async def withdraw_proc_owner(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    owner = msg.text.strip().upper()
    data = await state.get_data()
    amount = data["withdraw_amount"]
    card = data["card_number"]
    uid = msg.from_user.id
    await state.clear()

    u = db.get_user(uid)
    if not u or u["referral_balance"] < amount:
        await msg.answer("Xatolik: Balans yetarli emas.", reply_markup=main_menu_keyboard(uid))
        return

    db.q("UPDATE users SET referral_balance=referral_balance-? WHERE user_id=?", (amount, uid), commit=True)
    w_id = db.create_withdrawal(uid, amount, card, owner)

    await msg.answer(
        f"✅ <b>SO'ROV ADMINGA YUBORILDI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"So'rov raqami: <b>#{w_id}</b>\n"
        f"Yechilayotgan summa: <b>{fmt(amount)} so'm</b>\n"
        f"Karta: <code>{format_card(card)}</code> ({owner})\n\n"
        f"Admin tez orada pulingizni tashlab beradi. Rahmat!",
        reply_markup=main_menu_keyboard(uid)
    )

    for a_id in ADMIN_IDS:
        try:
            adm_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ To'landi deb belgilash", callback_data=f"adm_pay_w:{w_id}")]
            ])
            await bot.send_message(
                a_id,
                f"💸 <b>YANGI PUL YECHISH SO'ROVI: #{w_id}</b>\n"
                f"Foydalanuvchi: ID <code>{uid}</code> (@{esc_html(msg.from_user.username or '')})\n"
                f"Summa: <b>{fmt(amount)} so'm</b>\n"
                f"Karta: <code>{format_card(card)}</code> ({owner})",
                reply_markup=adm_kb
            )
        except Exception:
            pass

# ============================ 11. 4-TUGMA: 🤝 YANGI SAVDO OCHISH ============================
@router.message(F.text.contains("Yangi Savdo") | F.text.contains("Savdo Ochish"))
async def create_deal_menu_handler(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id

    # Faol to'lov kutilayotgan bitim bor-yo'qligini tekshirish
    active = db.q("SELECT * FROM deals WHERE buyer_id=? AND status='waiting_payment'", (uid,), one=True)
    if active:
        await msg.answer(
            f"⚠️ Sizda to'lov kutilayotgan bitim mavjud: <b>#{active['deal_id']}</b>.\n"
            f"Avval ushbu bitimni yakunlang yoki bekor qiling.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Ushbu bitimni bekor qilish", callback_data=f"cancel_deal:{active['deal_id']}")]
            ])
        )
        return

    text = (
        "🤝 <b>YANGI KAFOLATLI SAVDO YARATISH:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Savdoni qaysi usulda o'tkazmoqchisiz?\n\n"
        "1️⃣ <b>👥 AI yordamida guruhda</b> — Bot va Gemini AI qorovuli sizning guruhingizga qo'shiladi va savdoni guruh ichida nazorat qiladi.\n"
        "2️⃣ <b>🤖 Botning o'zida</b> — Savdo to'liq bot ichidagi xavfsiz shifrlangan chatda amalga oshiriladi."
    )
    kb = [
        [InlineKeyboardButton(text="👥 AI yordamida guruhda", callback_data="deal_mode_group")],
        [InlineKeyboardButton(text="🤖 Botning o'zida", callback_data="deal_mode_bot")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.in_(["deal_mode_group", "deal_mode_bot"]))
async def select_deal_mode_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    mode = "group" if call.data == "deal_mode_group" else "bot"
    await state.set_state(CreateDealFSM.title)
    await state.update_data(trade_mode=mode)
    await call.message.answer(
        "📦 <b>Mahsulot yoki xizmat nomini kiriting:</b>\n"
        "<i>(Masalan: Telegram Kanal, PUBG Akkaunt, Python bot kodi yoki dizayn xizmati)</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(CreateDealFSM.title)
async def process_deal_title(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    title = msg.text.strip()
    await state.update_data(deal_title=title)
    await state.set_state(CreateDealFSM.seller)
    await msg.answer(
        f"Mahsulot: <b>{esc_html(title)}</b>\n\n"
        f"🛒 <b>Siz xaridorsiz! Sotuvchining @username'ini kiriting:</b>\n"
        f"<i>(Masalan: @sotuvchi_user)</i>"
    )

@router.message(CreateDealFSM.seller)
async def process_deal_seller(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    seller_tag = msg.text.strip().lstrip("@")
    if not seller_tag:
        await msg.answer("Iltimos, to'g'ri username kiriting:")
        return

    # Qora ro'yxatdan tekshirish
    banned, reason = db.is_blacklisted(username=seller_tag)
    if banned:
        await msg.answer(
            f"🚫 <b>DIQQAT: @{esc_html(seller_tag)} foydalanuvchisi QORA RO'YXATDA!</b>\n\n"
            f"Sababi: <i>{esc_html(reason)}</i>\n"
            f"Ushbu sotuvchi bilan savdo qilish xavfli va qat'iyan taqiqlanadi!",
            reply_markup=main_menu_keyboard(msg.from_user.id)
        )
        await state.clear()
        return

    await state.update_data(seller_username=seller_tag)
    await state.set_state(CreateDealFSM.amount)

    buyer = db.get_user(msg.from_user.id)
    is_first = (buyer and buyer["completed_deals"] == 0)
    fee_notice = "🎁 <b>Siz birinchi marta foydalanayotganingiz uchun xizmat haqi 0% MUTLAQO BEPUL!</b>" if is_first else "ℹ️ Xizmat komissiyasi: 10%"

    await msg.answer(
        f"Sotuvchi: <b>@{esc_html(seller_tag)}</b> (Xavfsiz ✅)\n\n"
        f"💵 <b>To'lov summasini kiriting (so'mda):</b>\n"
        f"<i>(Eng kam summa 1 000 so'm, eng ko'p summa 100 000 so'm)</i>\n\n"
        f"{fee_notice}"
    )

@router.message(CreateDealFSM.amount)
async def process_deal_amount(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
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

    card_num = format_card((pay_res and pay_res.get("card_number")) or db.get_setting("card_number", DEFAULT_CARD_NUMBER))
    card_own = db.get_setting("card_owner", DEFAULT_CARD_OWNER)
    fee_label = "0 so'm (🎁 1-savdo bepul!)" if fee == 0 else f"{fmt(fee)} so'm (10%)"

    payment_card = (
        f"💳 <b>KAFOLATLI TO'LOV HISOBI OCHILDI! (#{deal_id})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Mahsulot:</b> <b>{esc_html(title)}</b>\n"
        f"👤 <b>Sotuvchi:</b> @{esc_html(seller_tag)}\n"
        f"💵 <b>Kelishilgan summa:</b> <b>{fmt(amount)} so'm</b>\n"
        f"💼 <b>Komissiya:</b> <b>{fee_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>TO'LOV KARTASI:</b>\n"
        f"👉 <code>{card_num}</code>\n"
        f"<i>(Karta ustiga bosing, avtomatik nusxalanadi!)</i>\n"
        f"👤 <b>Qabul qiluvchi:</b> <b>{esc_html(card_own)}</b>\n\n"
        f"💰 <b>O'TKAZILADIGAN ANIQ SUMMA:</b>\n"
        f"👉 <b>{fmt(exact_amount)} so'm</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>DIQQAT! JUDA MUHIM OGOHLANTIRISH:</b>\n"
        f"<b>Ko'rsatilgan summadan 1 so'm ham kam yoki ortiq to'lamang! Aynan {fmt(exact_amount)} so'mni aniq o'tkazing, aks holda to'lov tizim tomonidan avtomatik tasdiqlanmay qolishi mumkin.</b>\n\n"
        f"⏱ <b>Siz 5 minut ichida to'lov qilmasangiz to'lov avtomatik bekor qilinadi.</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = []
    if pay_res and pay_res.get("payment_url"):
        kb.append([InlineKeyboardButton(text="💳 Havola orqali to'lash", url=pay_res["payment_url"])])
    kb.append([InlineKeyboardButton(text="✅ To'lov qildim (Tekshirish)", callback_data=f"i_paid:{deal_id}")])
    kb.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel_deal:{deal_id}")])

    await msg.answer(payment_card, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("i_paid:"))
async def i_paid_callback_step(call: CallbackQuery, state: FSMContext):
    d_id = call.data.split(":")[1]
    deal = db.get_deal(d_id)
    if not deal or deal["status"] != "waiting_payment":
        await call.answer("⚠️ Bitim holati o'zgargan yoki muddati tugagan.", show_alert=True)
        return

    payment_id = deal["payment_id"]
    is_paid = False
    if payment_id:
        chk = await check_payment_status_async(payment_id)
        if chk and chk.get("paid"):
            is_paid = True

    if not is_paid:
        await call.answer("⏳ To'lov hali kelib tushmadi!\n\nIltimos, ko'rsatilgan aniq summani kartaga o'tkazib so'ng qayta tekshiring.", show_alert=True)
        return

    await call.answer("✅ To'lov muvaffaqiyatli qabul qilindi!", show_alert=True)
    now_s = datetime.now(TZ).isoformat()
    db.q("UPDATE deals SET status='in_trade', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)

    if deal["mode"] == "group":
        await state.set_state(CreateDealFSM.group_link)
        await state.update_data(active_deal_id=d_id)
        await call.message.edit_text(
            f"✅ <b>TO'LOV AMALGA OSHDI! (#{d_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Endi meni guruhingizga qo'shishingiz va menga <b>ADMIN HUQUQINI</b> berishingiz kerak.\n\n"
            f"👉 <b>Menga guruhingizning taklif havolasini (linkini) yuboring:</b>\n"
            f"<i>(Masalan: https://t.me/+xxxxxx yoki https://t.me/guruh_nomi)</i>"
        )
    else:
        bot_info = await bot.get_me()
        invite_link = f"https://t.me/{bot_info.username}?start=deal_{d_id}"
        await call.message.edit_text(
            f"✅ <b>TO'LOV AMALGA OSHDI VA MUZLATILDI! (#{d_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Xaridor to'lagan <b>{fmt(deal['total_to_pay'])} so'm</b> pul kafil hisobida xavfsiz muzlatildi.\n\n"
            f"Endi sotuvchi @{esc_html(deal['seller_username'])} ga quyidagi ulanish havolasini yuboring:\n"
            f"👉 <code>{invite_link}</code>\n"
            f"<i>(Ustiga bosing, avtomatik nusxalanadi!)</i>\n\n"
            f"Sotuvchi ushbu havola orqali botga kirishi bilan bot ichidagi xavfsiz chat ishga tushadi."
        )

@router.message(CreateDealFSM.group_link)
async def process_deal_group_link(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    link = msg.text.strip()
    data = await state.get_data()
    deal_id = data["active_deal_id"]
    await state.clear()

    group_code = generate_group_code()
    db.q("UPDATE deals SET group_code=? WHERE deal_id=?", (group_code, deal_id), commit=True)

    await msg.answer(
        f"✅ <b>GURUH SAVDOSI BO'YICHA BIR MARTALIK KODINGIZ TAYYOR:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"1️⃣ Botni o'sha guruhga admin qilib qo'shing.\n"
        f"2️⃣ Guruh chatiga quyidagi maxsus kodni yuboring:\n\n"
        f"👉 <code>{group_code}</code>\n"
        f"<i>(Kod ustiga bosing, avtomatik nusxalanadi!)</i>\n\n"
        f"Kodni yuborganingizdan so'ng, Kafil boti va Gemini AI qorovuli guruhda savdoni nazorat qilishni boshlaydi!",
        reply_markup=main_menu_keyboard(msg.from_user.id)
    )

# ============================ 12. GURUHDA SAVDO VA AI QOROVUL ============================
@router.message(F.chat.type.in_(["group", "supergroup"]))
async def group_trade_moderator(msg: Message):
    chat_id = msg.chat.id
    text = (msg.text or "").strip()

    # Bir martalik kod orqali faollashtirish
    if text.startswith("KAFIL-"):
        code = text.split()[0].upper()
        deal = db.q("SELECT * FROM deals WHERE group_code=? AND status='in_trade'", (code,), one=True)
        if not deal:
            return

        try:
            bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
            if bot_member.status not in ("administrator", "creator"):
                await msg.answer("⚠️ <b>Siz meni ushbu guruhda admin qilmadingiz!</b>\nIltimos, avval menga adminlik huquqini bering.")
                return
        except Exception:
            await msg.answer("⚠️ Bot admin ekanligini tekshirib bo'lmadi.")
            return

        db.q("UPDATE deals SET group_chat_id=?, group_code=NULL WHERE deal_id=?", (chat_id, deal["deal_id"]), commit=True)
        await msg.answer(
            f"🛡 <b>KAFIL BOTI VA GEMINI AI QOROVULI GURUHGA MUVOFIQ ULANDI! (#{deal['deal_id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Tovar:</b> <b>{esc_html(deal['title'])}</b>\n"
            f"🛒 <b>Xaridor:</b> @{esc_html(deal['buyer_username'])}\n"
            f"💼 <b>Sotuvchi:</b> @{esc_html(deal['seller_username'])}\n"
            f"💰 <b>Kafolatlangan summa:</b> <b>{fmt(deal['amount'])} so'm</b> (Muzlatilgan 🔒)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Savdoni boshlash uchun ikkala tomon ham <b>«boshladik»</b> yoki <b>«tayyorman»</b> deb yozsin."
        )
        return

    # Guruhdagi faol savdoni topish
    deal = db.q("SELECT * FROM deals WHERE group_chat_id=? AND status IN ('in_trade', 'goods_delivered', 'buyer_statement', 'seller_statement', 'buyer_confirmed')", (chat_id,), one=True)
    if not deal:
        return

    # Gemini AI xavfsizlik nazorati
    is_safe, warn_reason = await check_chat_safety(text)
    if not is_safe:
        await msg.reply(f"🚨 <b>GEMINI AI QOROVUL OGOHLANTIRISHI:</b>\n{warn_reason}")

    user_un = (msg.from_user.username or "").lower()
    is_buyer = (msg.from_user.id == deal["buyer_id"] or user_un == (deal["buyer_username"] or "").lower())
    is_seller = (msg.from_user.id == deal["seller_id"] or user_un == (deal["seller_username"] or "").lower())

    # Ikki tomonning tayyorligini tekshirish
    if deal["status"] == "in_trade" and (deal["group_buyer_ready"] == 0 or deal["group_seller_ready"] == 0):
        if any(w in text.lower() for w in ["tayyor", "boshladik", "start"]):
            if is_buyer:
                db.q("UPDATE deals SET group_buyer_ready=1 WHERE deal_id=?", (deal["deal_id"],), commit=True)
            if is_seller:
                db.q("UPDATE deals SET group_seller_ready=1 WHERE deal_id=?", (deal["deal_id"],), commit=True)

            deal = db.get_deal(deal["deal_id"])
            if deal["group_buyer_ready"] == 1 and deal["group_seller_ready"] == 1:
                await msg.answer(
                    f"🔒 <b>To'lov kafolatlangan va kafil hisobida ushlab turilibdi.</b>\n"
                    f"Oluvchi @{esc_html(deal['buyer_username'])} <b>{fmt(deal['amount'])} so'm</b> to'lagan.\n"
                    f"Sotuvchi @{esc_html(deal['seller_username'])}, siz bemalol mahsulotni topshirishingiz mumkin. "
                    f"Men xavfsizligingizni tekshirib turaman."
                )
            else:
                missing = "sotuvchi" if deal["group_buyer_ready"] == 1 else "oluvchi"
                await msg.answer(f"Sizda {missing} tayyorligini bildirmagan, u yozgandagina savdo boshlanadi.")
            return

    # Mahsulot topshirilgach xaridor tasdig'i ("oldim")
    if is_buyer and any(w in text.lower() for w in ["men oldim", "oldim", "tekshirdim", "qabul qildim"]):
        db.q("UPDATE deals SET status='buyer_statement' WHERE deal_id=?", (deal["deal_id"],), commit=True)
        await msg.answer(
            f"✅ <b>Xaridor mahsulot o'tganini tasdiqladi!</b>\n\n"
            f"1️⃣ <b>OLUVCHIDAN RASMIY TILXAT-ARIZA:</b>\n"
            f"Xaridor @{esc_html(deal['buyer_username'])}, quyidagi matnni yozib yuboring:\n\n"
            f"✍️ <b>«Men {esc_html(deal['title'])}ni to'liq qabul qilib oldim, agar keyinchalik sotuvchi qaytarib olsa yoki aldasa, bot javob bermaydi»</b>"
        )
        return

    # Oluvchi arizasi qabul qilinishi
    if deal["status"] == "buyer_statement" and is_buyer and "bot javob bermaydi" in text.lower():
        db.q("UPDATE deals SET buyer_statement=?, status='seller_statement' WHERE deal_id=?", (text, deal["deal_id"]), commit=True)
        await msg.answer(
            f"✅ <b>Oluvchi arizasi qabul qilindi!</b>\n\n"
            f"2️⃣ <b>SOTUVCHIDAN RASMIY TILXAT-ARIZA:</b>\n"
            f"Sotuvchi @{esc_html(deal['seller_username'])}, quyidagi matnni yozib yuboring:\n\n"
            f"✍️ <b>«Men {esc_html(msg.from_user.full_name)} agar sotilgan narsani qaytarib olsam yoki aldov qilsam, qora ro'yxatga solishingizni so'rayman»</b>"
        )
        return

    # Sotuvchi arizasi qabul qilinishi
    if deal["status"] == "seller_statement" and is_seller and "qora ro'yxat" in text.lower():
        db.q("UPDATE deals SET seller_statement=?, status='buyer_confirmed' WHERE deal_id=?", (text, deal["deal_id"]), commit=True)
        await msg.answer(
            f"✅ <b>Sotuvchi arizasi ham qabul qilindi!</b>\n\n"
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
            f"💳 <b>KARTANGIZ QABUL QILINDI:</b> <code>{format_card(card_num)}</code> ({owner_name})\n"
            f"Mablag' kafil tomonidan kartangizga o'tkazib beriladi. Kafil botidan foydalanganingiz uchun rahmat!"
        )
        for a_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    a_id,
                    f"✅ <b>GURUH SAVDOSI YAKUNLANDI! SOTUVCHIGA PUL O'TKAZING:</b>\n"
                    f"Bitim: <b>#{deal['deal_id']}</b>\n"
                    f"Sotuvchi: ID <code>{deal['seller_id']}</code> (@{esc_html(deal['seller_username'])})\n"
                    f"Karta: <code>{format_card(card_num)}</code> ({owner_name})\n"
                    f"Summa: <b>{fmt(deal['amount'])} so'm</b>\n"
                    f"Admin komissiyasi: <b>{fmt(deal['fee'])} so'm</b>"
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
                f"🚨 <b>DIQQAT! SOTUVCHI GURUHNI TARK ETDI!</b>\n"
                f"Sotuvchi @{esc_html(left_user.username or str(left_user.id))} guruhdan chiqib ketdi.\n\n"
                f"Xaridor @{esc_html(deal['buyer_username'])}, sotuvchi sizni aldamoqchi bo'ldimi?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
            )

@router.callback_query(F.data.startswith("scam_confirm:"))
async def scam_confirm_cb(call: CallbackQuery):
    await call.answer()
    parts = call.data.split(":")
    d_id = parts[1]
    seller_uid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    deal = db.get_deal(d_id)
    if not deal:
        return

    db.add_to_blacklist(
        user_id=seller_uid,
        username=deal["seller_username"],
        reason=f"Guruhdagi savdoni tugatmay qochib ketgan (Bitim #{d_id})",
        deal_id=d_id,
        added_by="group_monitor"
    )
    now_s = datetime.now(TZ).isoformat()
    db.q("UPDATE deals SET status='dispute', updated_at=? WHERE deal_id=?", (now_s, d_id), commit=True)

    await call.message.edit_text(
        f"🚫 <b>SOTUVCHI QORA RO'YXATGA KIRITILDI VA BOT BLOKLANDI!</b>\n"
        f"Foydalanuvchi: @{esc_html(deal['seller_username'])} (ID: <code>{seller_uid}</code>)\n\n"
        f"Xaridorning puli muzlatilgan va refund qilib qaytarib beriladi."
    )

@router.callback_query(F.data.startswith("scam_cancel:"))
async def scam_cancel_cb(call: CallbackQuery):
    await call.answer("Ogohlantirish bekor qilindi.")
    await call.message.edit_text("ℹ️ Sotuvchi qayta qo'shilishi kutilmoqda.")

# ============================ 13. 5-TUGMA: 🚫 FIRIBGARLAR RO'YXATI ============================
@router.message(F.text.contains("Firibgar"))
async def blacklist_menu_handler(msg: Message, state: FSMContext):
    await state.clear()
    rows = db.get_all_blacklisted(25)
    if not rows:
        await msg.answer(
            "🛡 <b>FIRIBGARLAR VA MASHENNIKLAR RO'YXATI:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ <b>Hozircha firibgarlar aniqlanmagan!</b>\n\n"
            "Kafil tizimi barcha savdolarni 100% xavfsiz nazorat qiladi. "
            "Agar kimdir kelishuvni buzsa yoki tovarni bermasa, uning Telegram ID raqami bir umrga shu ro'yxatga kiritiladi.",
            reply_markup=main_menu_keyboard(msg.from_user.id)
        )
        return

    text = "🚫 <b>FIRIBGARLAR VA MASHENNIKLAR RO'YXATI:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for r in rows:
        u_tag = f"@{esc_html(r['username'])}" if r["username"] else "mavjud emas"
        uid_tag = f"<code>{r['user_id']}</code>" if r["user_id"] else "nomalum"
        date_str = (r["created_at"] or "")[:10]
        text += (
            f"👤 <b>Foydalanuvchi:</b> {u_tag}\n"
            f"🆔 <b>Telegram ID:</b> {uid_tag}\n"
            f"📝 <b>Sababi:</b> <i>{esc_html(r['reason'])}</i>\n"
            f"📅 <b>Sana:</b> {date_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
    text += "\n⚠️ <i>Ushbu shaxslarning Telegram ID raqamlari qat'iy bloklangan va ularga Kafil xizmatlari butunlay yopiq!</i>"
    await msg.answer(text, reply_markup=main_menu_keyboard(msg.from_user.id))

# ============================ 14. 6-TUGMA: 💾 ZAXIRA NUSXA (BACKUP) ============================
@router.message(F.text.contains("Zaxira") | F.text.contains("Backup"))
async def backup_menu_handler(msg: Message, state: FSMContext):
    await state.clear()
    text = (
        "💾 <b>MA'LUMOTLAR ZAXIRA NUSXASI (BACKUP)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Siz o'z hisobingizdagi barcha savdolar, arizalar va tarixingizni JSON ko'rinishida yuklab olishingiz "
        "yoki avvalgi zaxira faylingizni kiritishingiz mumkin.\n\n"
        "Quyidagi amallardan birini tanlang:"
    )
    kb = [
        [InlineKeyboardButton(text="📥 Zaxira nusxani yuklab olish", callback_data="backup_download")],
        [InlineKeyboardButton(text="📤 Zaxira faylini tiklash", callback_data="backup_restore")]
    ]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "backup_download")
async def backup_download_cb(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    json_bytes = db.export_user_backup_json(uid)
    now_str = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    doc = BufferedInputFile(json_bytes, filename=f"kafil_backup_{uid}_{now_str}.json")
    await call.message.answer_document(
        document=doc,
        caption=(
            f"✅ <b>ZAXIRA NUSXANGIZ TAYYOR!</b>\n"
            f"Fayl: <code>kafil_backup_{uid}_{now_str}.json</code>\n\n"
            f"Ushbu fayl orqali botdagi tarixingiz va ma'lumotlaringizni xohlagan vaqtda qayta tiklashingiz mumkin."
        )
    )

@router.callback_query(F.data == "backup_restore")
async def backup_restore_cb(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(BackupFSM.upload_file)
    await call.message.answer(
        "📤 <b>ZAXIRA NUSXANI TIKLASH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Avvalgi <b>.json</b> kengaytmali zaxira faylingizni shu yerga hujjat (file) sifatida yuboring:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(BackupFSM.upload_file, F.document)
async def process_backup_doc(msg: Message, state: FSMContext):
    doc = msg.document
    if not doc.file_name.endswith(".json"):
        await msg.answer("Iltimos, faqat .json fayl yuboring:")
        return

    file_obj = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file_obj.file_path)
    ok, count = db.import_user_backup_json(msg.from_user.id, file_bytes.read())
    await state.clear()
    if ok:
        await msg.answer(f"✅ <b>Muvaffaqiyatli tiklandi!</b>\n{count} ta bitim qayta yuklandi.", reply_markup=main_menu_keyboard(msg.from_user.id))
    else:
        await msg.answer("❌ Fayl formati yaroqsiz.", reply_markup=main_menu_keyboard(msg.from_user.id))

# ============================ 15. 7-TUGMA: 📑 ARIZALARIM ============================
@router.message(F.text.contains("Arizalarim") | F.text.contains("Ariza"))
async def my_complaints_handler(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    comps = db.q("SELECT * FROM complaints WHERE complainant_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
    if not comps:
        await msg.answer(
            "📑 <b>Sizda hali hech qanday shikoyat yoki arizalar mavjud emas.</b>\n"
            "Tizim barcha savdolarni tinch va halol o'tishini ta'minlaydi.",
            reply_markup=main_menu_keyboard(uid)
        )
        return

    text = "📑 <b>SIZNING ARIZALARINGIZ VA ULARNING HOLATI:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    kb = []
    st_map = {
        "pending": "⏳ Ko'rib chiqilmoqda (5 kunlik muddat)",
        "resolved": "✅ Hal qilindi",
        "blacklisted": "🚫 Sotuvchi qora ro'yxatga tushgan",
        "withdrawn": "↩️ Qaytarib olingan (Bekor)"
    }
    for c in comps:
        st = st_map.get(c["status"], c["status"])
        c_date = (c["created_at"] or "")[:10]
        text += (
            f"🆔 <b>Ariza #{c['id']}</b> | {st}\n"
            f"👤 Sotuvchi: @{esc_html(c['accused_username'])}\n"
            f"📝 Sabab: <i>{esc_html(c['reason'])}</i>\n"
            f"📅 Sana: {c_date}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        if c["status"] == "pending":
            kb.append([InlineKeyboardButton(text=f"↩️ Arizani qaytarib olish: #{c['id']}", callback_data=f"withdraw_comp:{c['id']}")])

    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else main_menu_keyboard(uid))

@router.callback_query(F.data.startswith("withdraw_comp:"))
async def withdraw_comp_cb(call: CallbackQuery):
    await call.answer()
    comp_id = int(call.data.split(":")[1])
    comp = db.q("SELECT * FROM complaints WHERE id=?", (comp_id,), one=True)
    if not comp or comp["complainant_id"] != call.from_user.id:
        await call.message.answer("Ariza topilmadi.")
        return

    db.q("UPDATE complaints SET status='withdrawn' WHERE id=?", (comp_id,), commit=True)
    await call.message.edit_text(
        f"✅ <b>ARIZA QAYTARIB OLINDI VA BEKOR BO'LDI! (#{comp_id})</b>\n"
        f"Sotuvchi ustidan berilgan shikoyat bekor qilindi."
    )
    if comp["accused_id"]:
        try:
            await bot.send_message(
                comp["accused_id"],
                f"ℹ️ @{esc_html(comp['complainant_username'])} sizning ustingizdan berilgan <b>#{comp_id}</b> arizani qaytarib oldi."
            )
        except Exception:
            pass

# ============================ 16. 8-TUGMA: 👑 ADMIN PANEL ============================
@router.message(F.text.contains("Admin") | Command("admin"))
async def admin_panel_handler(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    if not is_admin(uid):
        await msg.answer(
            f"👑 <b>ADMIN BOSHQARUV PANELI</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>Kechirasiz, siz ushbu botning administratori emassiz.</i>\n\n"
            f"🆔 <b>Sizning Telegram ID raqamingiz:</b> <code>{uid}</code>\n"
            f"<i>(ID ustiga bosing, avtomatik nusxalanadi)</i>\n\n"
            f"📌 <i>Agar siz bot egasi bo'lsangiz, ushbu ID raqamni sozlamalarga kiriting.</i>",
            reply_markup=main_menu_keyboard(uid)
        )
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
        f"👥 <b>Foydalanuvchilar:</b> <b>{total_u} ta</b>\n"
        f"📦 <b>Jami bitimlar:</b> <b>{total_d} ta</b>\n"
        f"🚫 <b>Qora ro'yxat:</b> <b>{black_c} ta</b>\n"
        f"💸 <b>Yechish so'rovlari:</b> <b>{pend_w} ta</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Karta:</b> <code>{format_card(card)}</code>\n"
        f"👤 <b>Karta egasi:</b> <b>{owner}</b>\n"
        f"💼 <b>Komissiya foizi:</b> <b>{comm}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Boshqarish uchun quyidagi tugmalardan birini tanlang:</i>"
    )
    kb = [
        [InlineKeyboardButton(text="📦 Savdolar monitoringi", callback_data="adm_view_deals"),
         InlineKeyboardButton(text="🚫 Firibgarlar boshqaruvi", callback_data="adm_view_scammers")],
        [InlineKeyboardButton(text="📢 Xabar tarqatish (Reklama)", callback_data="adm_broadcast"),
         InlineKeyboardButton(text="📢 Majburiy kanallar", callback_data="adm_channels")],
        [InlineKeyboardButton(text="📊 AI Tahliliy hisobot", callback_data="adm_ai_chat"),
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
    text = "📦 <b>OXIRGI SAVDOLAR:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for d in deals:
        text += f"🆔 <b>#{d['deal_id']}</b> | {d['status']} | <b>{fmt(d['amount'])} so'm</b>\n"
    await call.message.answer(text)

@router.callback_query(F.data == "adm_view_scammers")
async def adm_view_scammers_cb(call: CallbackQuery):
    await call.answer()
    kb = [
        [InlineKeyboardButton(text="➕ Firibgar qo'shish", callback_data="adm_add_scammer")],
        [InlineKeyboardButton(text="📋 Ro'yxatni ko'rish", callback_data="adm_list_scammers")]
    ]
    await call.message.answer("🚫 <b>Firibgarlar boshqaruvi:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "adm_add_scammer")
async def adm_add_scammer_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.add_black_user)
    await call.message.answer("Bloklanadigan shaxsning Telegram ID yoki @username'ini kiriting:")

@router.message(AdminFSM.add_black_user)
async def adm_proc_scammer_user(msg: Message, state: FSMContext):
    val = msg.text.strip().lstrip("@")
    await state.update_data(black_target=val)
    await state.set_state(AdminFSM.add_black_reason)
    await msg.answer("Bloklash sababini kiriting:")

@router.message(AdminFSM.add_black_reason)
async def adm_proc_scammer_reason(msg: Message, state: FSMContext):
    reason = msg.text.strip()
    data = await state.get_data()
    target = data["black_target"]
    await state.clear()

    uid = int(target) if target.isdigit() else None
    un = target if not target.isdigit() else ""
    db.add_to_blacklist(user_id=uid, username=un, reason=reason, added_by="admin")
    await msg.answer(f"✅ <b>{target}</b> qora ro'yxatga qo'shildi va bloklandi.")

@router.callback_query(F.data == "adm_list_scammers")
async def adm_list_scammers_cb(call: CallbackQuery):
    await call.answer()
    rows = db.get_all_blacklisted(20)
    if not rows:
        await call.message.answer("Qora ro'yxat bo'sh.")
        return
    text = "🚫 <b>QORA RO'YXAT:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for r in rows:
        text += f"ID: <code>{r['user_id']}</code> | @{r['username']} | <i>{r['reason']}</i>\n"
    await call.message.answer(text)

@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.broadcast)
    await call.message.answer(
        "📢 Barcha bot foydalanuvchilariga yuboriladigan xabarni (matn, rasm yoki post) yuboring:",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    )

@router.message(AdminFSM.broadcast)
async def adm_proc_broadcast(msg: Message, state: FSMContext):
    if any(w in msg.text.lower() for w in ["bekor", "cancel"]) or msg.text.startswith("❌"):
        await state.clear()
        await msg.answer("Bekor qilindi.", reply_markup=main_menu_keyboard(msg.from_user.id))
        return

    users = db.q("SELECT user_id FROM users")
    await state.clear()
    sent, failed = 0, 0
    prog = await msg.answer(f"Xabar yuborilmoqda... 0/{len(users)}")
    for u in users:
        try:
            await msg.copy_to(chat_id=u["user_id"])
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await prog.edit_text(f"✅ Xabar tarqatish yakunlandi!\nYetkazildi: {sent} ta\nYetib bormadi: {failed} ta")

@router.callback_query(F.data == "adm_channels")
async def adm_channels_menu(call: CallbackQuery):
    await call.answer()
    channels = db.q("SELECT * FROM mandatory_channels")
    text = "📢 <b>MAJBURiY OBUNA KANALLARI:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for c in channels:
        text += f"• <b>{c['channel_title']}</b> (<code>{c['channel_id']}</code>) — <a href=\"{c['invite_link']}\">Havola</a>\n"
    if not channels:
        text += "Hozircha majburiy kanallar qo'shilmagan.\n"
    kb = [
        [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="adm_add_ch")],
        [InlineKeyboardButton(text="🗑 Barcha kanallarni tozalash", callback_data="adm_clear_ch")]
    ]
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), disable_web_page_preview=True)

@router.callback_query(F.data == "adm_clear_ch")
async def adm_clear_ch_cb(call: CallbackQuery):
    await call.answer()
    db.q("DELETE FROM mandatory_channels", commit=True)
    await call.message.answer("Barcha majburiy kanallar o'chirildi.")

@router.callback_query(F.data == "adm_add_ch")
async def adm_add_ch_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminFSM.add_channel_id)
    await call.message.answer("Kanal ID raqamini kiriting (masalan: -1001234567890):")

@router.message(AdminFSM.add_channel_id)
async def adm_proc_ch_id(msg: Message, state: FSMContext):
    cid = msg.text.strip()
    await state.update_data(ch_id=cid)
    await state.set_state(AdminFSM.add_channel_title)
    await msg.answer("Kanal nomini kiriting:")

@router.message(AdminFSM.add_channel_title)
async def adm_proc_ch_title(msg: Message, state: FSMContext):
    title = msg.text.strip()
    await state.update_data(ch_title=title)
    await state.set_state(AdminFSM.add_channel_link)
    await msg.answer("Kanalga taklif havolasini (link) kiriting:")

@router.message(AdminFSM.add_channel_link)
async def adm_proc_ch_link(msg: Message, state: FSMContext):
    link = msg.text.strip()
    data = await state.get_data()
    await state.clear()
    db.q("INSERT INTO mandatory_channels (channel_id, channel_title, invite_link) VALUES (?, ?, ?)",
         (data["ch_id"], data["ch_title"], link), commit=True)
    await msg.answer(f"✅ Kanal muvaffaqiyatli qo'shildi: <b>{data['ch_title']}</b>")

@router.callback_query(F.data == "adm_ai_chat")
async def adm_ai_chat_report(call: CallbackQuery):
    await call.answer()
    report = await analyze_deals_for_admin()
    await call.message.answer(report)

@router.callback_query(F.data == "adm_withdrawals")
async def adm_withdrawals_cb(call: CallbackQuery):
    await call.answer()
    rows = db.q("SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at ASC LIMIT 10")
    if not rows:
        await call.message.answer("Kutilayotgan pul yechish so'rovlari yo'q.")
        return
    for w in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ To'landi deb belgilash", callback_data=f"adm_pay_w:{w['id']}")]
        ])
        await call.message.answer(
            f"💸 <b>SO'ROV #{w['id']}</b>\n"
            f"Foydalanuvchi: ID <code>{w['user_id']}</code>\n"
            f"Summa: <b>{fmt(w['amount'])} so'm</b>\n"
            f"Karta: <code>{format_card(w['card_number'])}</code> ({w['card_owner']})",
            reply_markup=kb
        )

@router.callback_query(F.data.startswith("adm_pay_w:"))
async def adm_pay_w_cb(call: CallbackQuery):
    await call.answer()
    w_id = int(call.data.split(":")[1])
    db.q("UPDATE withdrawals SET status='completed' WHERE id=?", (w_id,), commit=True)
    w = db.q("SELECT * FROM withdrawals WHERE id=?", (w_id,), one=True)
    await call.message.edit_text(f"✅ So'rov #{w_id} to'landi deb belgilandi.")
    if w:
        try:
            await bot.send_message(
                w["user_id"],
                f"🎉 <b>PULINGIZ O'TKAZIB BERILDI! (#{w_id})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Summa: <b>{fmt(w['amount'])} so'm</b>\n"
                f"Karta: <code>{format_card(w['card_number'])}</code>\n\n"
                f"Kafil xizmatidan foydalanganingiz uchun rahmat!"
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("adm_reply_ticket:"))
async def adm_reply_ticket_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    t_id = int(call.data.split(":")[1])
    await state.set_state(AdminFSM.reply_ticket_text)
    await state.update_data(reply_ticket_id=t_id)
    await call.message.answer(f"Murojaat #{t_id} uchun javob matnini kiriting:")

@router.message(AdminFSM.reply_ticket_text)
async def adm_proc_ticket_reply(msg: Message, state: FSMContext):
    data = await state.get_data()
    t_id = data["reply_ticket_id"]
    reply_text = msg.text.strip()
    await state.clear()

    ticket = db.q("SELECT * FROM ai_tickets WHERE id=?", (t_id,), one=True)
    if ticket:
        db.q("UPDATE ai_tickets SET status='answered', admin_response=? WHERE id=?", (reply_text, t_id), commit=True)
        try:
            await bot.send_message(
                ticket["user_id"],
                f"👨‍💼 <b>ADMINISTRATOR JAVOBI:</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{reply_text}\n"
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
    card = re.sub(r"\D", "", msg.text.strip())
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
    await msg.answer(f"✅ Karta yangilandi:\n<code>{format_card(data['new_card'])}</code> ({owner})")

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

# ============================ 17. BEKOR QILISH CALLBACK ============================
@router.callback_query(F.data.startswith("cancel_deal:"))
async def cancel_deal_cb(call: CallbackQuery):
    await call.answer("Bitim bekor qilinmoqda...")
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

# ============================ 18. BOT ICHIDAGI SHIFRLANGAN CHAT ============================
@router.message(F.chat.type == "private", F.text)
async def private_trade_chat_router(msg: Message, state: FSMContext):
    cur_st = await state.get_state()
    if cur_st is not None or msg.text.startswith("/"):
        return

    # Asosiy menyu tugmalari bo'lsa o'tkazib yuborish
    if any(k in msg.text for k in ["Tarix", "SOS", "Yordam", "Referal", "Savdo", "Firibgar", "Backup", "Zaxira", "Ariza", "Admin", "Bekor"]):
        return

    uid = msg.from_user.id
    deal = db.q("SELECT * FROM deals WHERE (buyer_id=? OR seller_id=?) AND mode='bot' AND status IN ('in_trade', 'goods_delivered', 'buyer_statement', 'seller_statement', 'buyer_confirmed') ORDER BY updated_at DESC LIMIT 1", (uid, uid), one=True)
    if not deal:
        await msg.answer(
            "Assalomu alaykum! Kerakli bo'limni tanlash uchun pastdagi menyudan foydalaning:",
            reply_markup=main_menu_keyboard(uid)
        )
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
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Tovar: <b>{esc_html(deal['title'])}</b>\n"
            f"Summa: <b>{fmt(deal['amount'])} so'm</b>\n\n"
            f"Kafolatli Garant xizmatidan foydalanganingiz uchun rahmat!"
        )
        await msg.answer(finish_text, reply_markup=main_menu_keyboard(uid))
        if deal["seller_id"]:
            try:
                seller_finish = (
                    f"🎉 <b>XARIDOR TOVARNI QABUL QILDI! SAVDO YAKUNLANDI. (#{deal['deal_id']})</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Sizga o'tkaziladigan summa: <b>{fmt(deal['amount'])} so'm</b>\n\n"
                    f"💳 To'lovni olish uchun 16 xonali plastik karta raqamingiz va Ism-Familiyangizni shu yerga yozing:"
                )
                await bot.send_message(deal["seller_id"], seller_finish)
            except Exception:
                pass

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
