"""
AI_HELPER.PY - Google GenAI SDK (Gemini AI) va Kuchli Mahalliy Bilimlar Bazasi
O'zbek tilidagi eng to'liq, xatosiz va tushunarli javoblar generatori.
"""
import asyncio
import logging
import re
from typing import Optional
from google import genai
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_FALLBACK_MODELS

logger = logging.getLogger("kafil_ai")

# Google GenAI mijozini xavfsiz ishga tushirish
_client: Optional[genai.Client] = None

def get_genai_client() -> Optional[genai.Client]:
    global _client
    if _client is None:
        try:
            if GEMINI_API_KEY and GEMINI_API_KEY.strip() and not GEMINI_API_KEY.startswith("AQ."):
                _client = genai.Client(api_key=GEMINI_API_KEY)
                logger.info("Google GenAI mijozi muvaffaqiyatli ishga tushirildi.")
        except Exception as e:
            logger.warning(f"GenAI mijozini yaratishda xato: {e}")
    return _client

def smart_local_brain(prompt: str) -> str:
    """
    O'zbek tilidagi eng keng qamrovli Kafil / Garant intellektual bilimlar bazasi.
    Savolni chuqur tahlil qilib, foydalanuvchiga qalin, chiroyli va stikerlar bilan bezatilgan javob qaytaradi.
    Faqatgina foydalanuvchi qat'iy adminga yozmoqchi bo'lsagina ADMIN_GA_UZATISH deb belgilaydi.
    """
    p = prompt.lower().strip()

    # 1. FOYDALANUVCHI QAT'IY ADMINGA MUROJAAT QILGANDA
    admin_explicit_triggers = [
        "adminga yoz", "adminga yubor", "adminga jonat", "adminga jo'nat",
        "admin bilan gaplash", "operator bilan", "inson bilan", "operatorga",
        "arizamni adminga", "shikoyatni adminga", "admin raqami", "admin lichkasi"
    ]
    if any(t in p for t in admin_explicit_triggers):
        return (
            "ADMIN_GA_UZATISH\n"
            "👨‍💼 <b>Sizning murojaatingiz Garant Administratsiyasiga uzatilmoqda.</b>\n"
            "Admin tez orada xabaringizni ko'rib chiqadi va javob yo'llaydi."
        )

    # 2. BOT QANDAY ISHLAYDI / KAFIL TIZIMI HAQIDA
    if any(w in p for w in ["qanday ishlaydi", "kafil nima", "garant nima", "tizim qanday", "qoidalar", "bot vazifasi", "sxema"]):
        return (
            "🛡 <b>KAFIL / GARANT BOTI QANDAY ISHLAYDI?</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Bizning bot — O'zbekistondagi eng xavfsiz va ishonchli <b>Kafolatli Savdo (Escrow)</b> tizimidir. "
            "U xaridor va sotuvchi o'rtasidagi firibgarlikning oldini oladi:\n\n"
            "1️⃣ <b>Savdo yaratish:</b> Xaridor «🤝 Yangi Savdo Ochish» tugmasini bosib, tovar nomi, narxi va sotuvchining @username'ini kiritadi.\n"
            "2️⃣ <b>To'lov va muzlatish:</b> Xaridor kafil kartasiga to'lov qiladi. Mablag' sotuvchiga emas, balki kafil xavfsiz hisobida <b>muzlatiladi (bloklanadi)</b>.\n"
            "3️⃣ <b>Tovar topshirish:</b> Sotuvchi tovar yoki xizmatni xaridorga to'liq topshiradi.\n"
            "4️⃣ <b>Tekshiruv va tasdiq:</b> Xaridor tovarni to'liq tekshirib, rozilik berganidan keyingina pul sotuvchiga o'tkazib beriladi.\n\n"
            "🎁 <b>BIRINCHI SAVDO — 0% MUTLAQO BEPUL!</b>\n"
            "Keyingi savdolarda esa atigi 10% komissiya olinadi yoki 10% keshbek beriladi."
        )

    # 3. BIRINCHI SAVDO VA KESHBEK TIZIMI
    if any(w in p for w in ["keshbek", "cashback", "bepul", "tekin", "komissiya", "foiz", "0%", "10%"]):
        return (
            "💎 <b>KESHBEK VA KOMISSIYA TIZIMI:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 <b>Birinchi savdo:</b>\n"
            "Har bir yangi foydalanuvchi uchun birinchi bitim <b>MUTLAQO BEPUL (0% komissiya)</b>!\n\n"
            "💰 <b>10% Keshbek:</b>\n"
            "Keyingi barcha muvaffaqiyatli yakunlangan savdolardan so'ng, tizim tomonidan sizning "
            "ichki balansingizga savdo summasidan <b>10% Keshbek</b> qo'shib beriladi!\n\n"
            "🛍 Keshbekingizni keyingi savdolarda chegirma sifatida bemalol ishlatishingiz mumkin."
        )

    # 4. TO'LOV QILISH VA TOLOVAPI HAQIDA
    if any(w in p for w in ["to'lov", "tulov", "pul o'tkazish", "karta", "pul tushmadi", "5 minut", "5 daqiqa", "qanday to'layman"]):
        return (
            "💳 <b>TO'LOV QILISH TARTIBI VA QOIDALARI:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "To'lovlar avtomatlashtirilgan xavfsiz <b>TolovAPI.uz</b> orqali qabul qilinadi:\n\n"
            "📌 <b>Asosiy qoidalar:</b>\n"
            "• Bot sizga to'lov uchun 16 xonali karta raqami va aniq summani chiqarib beradi.\n"
            "• ⚠️ <b>DIQQAT:</b> Ko'rsatilgan summadan 1 so'm ham kam yoki ortiq to'lamang! Aynan belgilangan summani o'tkazing.\n"
            "• ⏱ To'lov uchun <b>5 daqiqa</b> vaqt beriladi. 5 daqiqa ichida to'lov qilinmasa, bitim avtomatik bekor bo'ladi.\n"
            "• Pulni o'tkazganingizdan so'ng, «✅ To'lov qildim (Tekshirish)» tugmasini bossangiz, tizim to'lovni tasdiqlaydi."
        )

    # 5. FIRIBGARLIK, ALDOV VA NIZOLAR
    if any(w in p for w in ["aldadi", "shikoyat", "nizo", "firibgar", "mashenik", "tovarni bermadi", "aldandim", "qora ro'yxat"]):
        return (
            "🚨 <b>FIRIBGARLIK VA NIZOLARNI HAL QILISH:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Agar sotuvchi sizni aldasa, tovarni bermasa yoki kelishuvni buzsa:\n\n"
            "1️⃣ Xaridor «✅ Tovarni oldim» deb tasdiqlamaguncha, <b>pul sotuvchiga o'tmaydi</b> va kafil hisobida xavfsiz saqlanadi.\n"
            "2️⃣ «🆘 SOS (Yordam)» bo'limidagi <b>«🚨 Meni sotuvchi aldadi»</b> tugmasi orqali sotuvchining @username'ini yozib ariza qoldiring.\n"
            "3️⃣ Bot sotuvchiga darhol ogohlantirish yuboradi. Sotuvchiga masalani hal qilish uchun <b>5 kun</b> vaqt beriladi.\n"
            "4️⃣ Agar 5 kun ichida muammoni hal qilmasa, uning Telegram ID raqami butunlay <b>QORA RO'YXATGA</b> kiritiladi va pul xaridorga qaytarib (refund) beriladi!"
        )

    # 6. REFERAL TIZIMI VA PUL YECHISH
    if any(w in p for w in ["referal", "do'st", "pul ishlash", "100 so'm", "10000", "yechish", "pul yechish", "taklif"]):
        return (
            "👥 <b>REFERAL TIZIMI VA PUL YECHISH:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Siz do'stlaringizni taklif qilib sof daromad topishingiz mumkin:\n\n"
            "💵 <b>Har bir do'st uchun:</b> +100 so'm beriladi.\n"
            "📌 <b>Shart:</b> Taklif qilingan do'stingiz botga kirib, majburiy homiy kanallarga to'liq a'zo bo'lishi lozim.\n"
            "⚡️ <b>Kunlik limit:</b> Kuniga maksimal 20 tagacha do'stingizni qo'sha olasiz.\n"
            "⚠️ <b>Jarima:</b> Agar chaqirgan odamingiz kanaldan chiqib ketsa, balansingizdan -200 so'm jarima ayiriladi.\n"
            "💳 <b>Pul yechish:</b> Minimal summa qat'iy <b>10 000 so'm</b>. Unga yetgach «💸 Pulni yechish» tugmasi orqali kartangizga chiqarib olasiz."
        )

    # 7. GURUHDA SAVDO QILISH VA KAFIL KODI
    if any(w in p for w in ["guruh", "chat", "kafil-", "kod", "guruhda savdo", "admin qilish"]):
        return (
            "🤝 <b>GURUHDA AI YORDAMIDA SAVDO QILISH:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Botda «🤝 Yangi Savdo Ochish» -> «👥 AI yordamida guruhda» bandini tanlang.\n"
            "2️⃣ Bot sizga unikal <b>KAFIL-XXXXX</b> kodini beradi.\n"
            "3️⃣ Botni o'zingizning savdo guruhingizga qo'shing va unga <b>Adminlik huquqini</b> bering.\n"
            "4️⃣ Guruhga bot bergan <b>KAFIL-XXXXX</b> kodini yuboring.\n"
            "5️⃣ Bot guruhda faollashadi, barcha xabarlarni kuzatadi va savdoni xavfsiz yakunlaydi!"
        )

    # 8. SALOMLASHISH VA UMUMIY SAVOLLAR
    if any(w in p for w in ["salom", "assalom", "qalaysiz", "kim bu", "yordam ber", "start"]):
        return (
            "👋 <b>Assalomu alaykum!</b>\n"
            "Men <b>Kafil (Garant)</b> rasmiy intellektual assistentiman 🤖\n\n"
            "Qanday savolingiz bor? Men sizga quyidagilar bo'yicha to'liq yordam bera olaman:\n"
            "• 🤝 Xavfsiz savdo ochish va yakunlash tartibi\n"
            "• 💳 To'lov qilish va kafil hisobida pulni muzlatish\n"
            "• 🎁 0% tekin birinchi savdo va 10% keshbek\n"
            "• 👥 Referal orqali pul ishlash va kartaga yechish\n"
            "• 🚨 Firibgarlardan himoyalanish va ariza qoldirish\n\n"
            "<i>Savolingizni bemalol yozib qoldiring!</i>"
        )

    # 9. BOSHQACHA SAVOLLAR UCHUN UNIVERSAL INTELLEKTUAL JAVOB
    return (
        f"🤖 <b>KAFIL ASSISTENTI JAVOBI:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Sizning savolingiz: <i>«{prompt}»</i>\n\n"
        f"💡 Bizning Kafolatli Savdo tizimimizda barcha bitimlar 100% xavfsiz o'tkaziladi. "
        f"Xaridor pul to'laganda mablag' kafil hisobida xavfsiz muzlatiladi va faqat tovar to'liq tekshirib "
        f"olingachgina sotuvchiga beriladi.\n\n"
        f"📌 Agar sizga botning aniq qoidalari kerak bo'lsa:\n"
        f"• <b>Savdo bo'yicha:</b> «🤝 Yangi Savdo Ochish» menyusidan foydalaning.\n"
        f"• <b>Shikoyat bo'yicha:</b> «🚨 Meni sotuvchi aldadi» tugmasini bosing.\n"
        f"• <b>Admin yordami kerak bo'lsa:</b> Quyidagi «👨‍💼 Adminga yuborish» tugmasini bosing."
    )

def _call_gemini_sync(prompt: str, system_instruction: str = "") -> str:
    client = get_genai_client()
    if not client:
        return smart_local_brain(prompt)

    models_to_try = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    for m in models_to_try:
        try:
            full_prompt = prompt
            if system_instruction:
                full_prompt = f"System Instruction: {system_instruction}\n\nUser Question: {prompt}"
            res = client.models.generate_content(model=m, contents=full_prompt)
            if res and res.text:
                return res.text.strip()
        except Exception as e:
            logger.warning(f"Model {m} bilan xato: {e}")

    return smart_local_brain(prompt)

async def ask_gemini_ai(prompt: str, system_instruction: str = "") -> str:
    """Asinxron Gemini AI va Smart Local Brain integratsiyasi."""
    sys_inst = (
        "Siz O'zbekistondagi eng mashhur Kafil / Garant (Escrow) botining rasmiy aqlli assistentisiz. "
        "Foydalanuvchilarga do'stona, aniq, qalin sarlavhalar va chiroyli emojilar bilan to'liq o'zbek tilida javob bering. "
        "Matnlarda ortiqcha kod (pre/code) ishlatmang. "
        "Faqat foydalanuvchi qat'iy adminga yozishni so'raganida yoki jiddiy nizo bo'lganda 'ADMIN_GA_UZATISH' kalit so'zini kiriting."
    )
    if system_instruction:
        sys_inst = f"{sys_inst} {system_instruction}"

    return await asyncio.to_thread(_call_gemini_sync, prompt, sys_inst)

async def check_chat_safety(message_text: str) -> Tuple[bool, str]:
    """Guruhdagi xabarlarni firibgarlik, so'kinish va noqonuniy harakatlarga tekshirish."""
    p = message_text.lower()
    danger_words = [
        "parol", "kodni ayt", "sms kod", "aldash", "tashlab beraman keyin",
        "oldindan tashla", "karta paroli", "cvv", "login parol", "yozib ber"
    ]
    for w in danger_words:
        if w in p:
            return False, f"⚠️ Xabarda shubhali kalit so'z aniqlandi: «{w}». Ehtiyot bo'ling!"
    return True, "Xavfsiz"

async def analyze_deals_for_admin() -> str:
    """Admin uchun savdolar statistikasi hisoboti."""
    import database as db
    total_u = db.q("SELECT COUNT(*) c FROM users", one=True)["c"]
    total_d = db.q("SELECT COUNT(*) c FROM deals", one=True)["c"]
    comp_d = db.q("SELECT COUNT(*) c FROM deals WHERE status='completed'", one=True)["c"]
    active_d = db.q("SELECT COUNT(*) c FROM deals WHERE status IN ('in_trade', 'waiting_payment')", one=True)["c"]
    total_sum = db.q("SELECT SUM(amount) s FROM deals WHERE status='completed'", one=True)["s"] or 0

    return (
        "📊 <b>KAFIL BOTI STATISTIK HISOBOTI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Jami a'zolar:</b> {total_u} nafar\n"
        f"📦 <b>Jami yaratilgan bitimlar:</b> {total_d} ta\n"
        f"✅ <b>Muvaffaqiyatli yakunlangan:</b> {comp_d} ta\n"
        f"🔒 <b>Hozirda faol/muzlatilgan:</b> {active_d} ta\n"
        f"💰 <b>Aylangan umumiy summa:</b> <b>{total_sum:,} so'm</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡️ Tizim to'liq barqaror ishlamoqda."
    )
