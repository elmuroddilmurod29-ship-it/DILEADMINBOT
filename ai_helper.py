"""
AI_HELPER.PY - Google GenAI SDK (Gemini AI) integratsiyasi va xavfsizlik nazorati
"""
import asyncio
import logging
from typing import Tuple, Optional
from google import genai
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_FALLBACK_MODELS

logger = logging.getLogger("kafil_ai")

# Google GenAI mijozini xavfsiz ishga tushirish
_client: Optional[genai.Client] = None

def get_genai_client() -> Optional[genai.Client]:
    global _client
    if _client is None:
        try:
            if GEMINI_API_KEY and GEMINI_API_KEY.strip():
                _client = genai.Client(api_key=GEMINI_API_KEY)
                logger.info("Google GenAI mijozi muvaffaqiyatli ishga tushirildi.")
        except Exception as e:
            logger.warning(f"GenAI mijozini yaratishda xato: {e}")
    return _client

def rule_based_fallback(prompt: str) -> str:
    """Tarmoq yoki API xatoliklarida javob beruvchi aqlli mahalliy zaxira dvigateli."""
    p = prompt.lower()
    if any(w in p for w in ["qanday ishlaydi", "kafil", "garant", "qoida", "tizim"]):
        return (
            "🛡 <b>Kafil (Garant) boti qanday ishlaydi?</b>\n\n"
            "1. <b>Savdo ochish:</b> Xaridor «🤝 Yangi Savdo Ochish» orqali tovar nomi, narxi va sotuvchini kiritadi.\n"
            "2. <b>To'lov va muzlatish:</b> Xaridor ko'rsatilgan kafil kartasiga to'lov qiladi. Mablag' xavfsiz muzlatiladi.\n"
            "3. <b>Tovar topshirish:</b> Sotuvchi tovarni topshiradi. Guruhda AI qorovul xavfsizlikni tekshirib turadi.\n"
            "4. <b>Rasmiy kafolat arizalari:</b> Har ikki tomon tilxat-ariza topshiradi.\n"
            "5. <b>Yakunlash:</b> Xaridor to'liq tekshirib 'Oldim' deb rozilik bergach, pul sotuvchiga o'tkaziladi.\n\n"
            "🎁 <b>Birinchi savdo — 0% MUTLAQO BEPUL!</b> Keyingi savdolarda 10% komissiya olinadi."
        )
    elif any(w in p for w in ["referal", "pul ishlash", "100", "10000", "yechish"]):
        return (
            "👥 <b>Referal tizimi qoidalari:</b>\n\n"
            "• Taklif qilgan har bir faol do'stingiz uchun: <b>+100 so'm</b>\n"
            "• Do'stingiz botga kirib, majburiy kanallarga to'liq a'zo bo'lishi shart.\n"
            "• Kunlik taklif limiti: <b>20 ta</b>.\n"
            "• Agar do'stingiz kanaldan chiqib ketsa: <b>-200 so'm jarima</b>.\n"
            "• Minimal pul yechib olish summasi: <b>10 000 so'm</b>."
        )
    elif any(w in p for w in ["aldadi", "shikoyat", "nizo", "firibgar", "mashenik", "bermadi"]):
        return (
            "🚨 <b>Firibgarlik holati bo'yicha yordam:</b>\n\n"
            "Agar sotuvchi sizni aldagan bo'lsa yoki tovarni bermay qochgan bo'lsa:\n"
            "1. '📜 Tarix' bo'limida o'sha savdo ostidagi 'Ariza berish' tugmasini bosing.\n"
            "2. Yoki SOS bo'limida sotuvchining @username'ini ko'rsatib ariza qoldiring.\n\n"
            "Sotuvchiga 5 kunlik muddat beriladi. Agar muammoni hal qilmasa, uning Telegram ID raqami "
            "butunlay QORA RO'YXATGA kiritiladi va unga bot yopiladi."
        )
    elif any(w in p for w in ["admin", "inson", "operator"]):
        return "ADMIN_GA_UZATISH: Sizning so'rovingiz to'g'ridan-to'g'ri administratorga uzatilmoqda."

    return (
        "🤖 Men Kafil Botining sun'iy intellekt (Gemini AI) assistentiman. "
        "Savdolar, qoidalar, to'lovlar va xavfsizlik masalalarida sizga yordam bera olaman. "
        "Agar muammoingiz hal bo'lmasa, adminga yuborishim mumkin."
    )

def _call_gemini_sync(prompt: str, system_instruction: str = "") -> str:
    client = get_genai_client()
    if not client:
        return rule_based_fallback(prompt)

    models_to_try = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    last_error = None

    for m in models_to_try:
        try:
            full_prompt = prompt
            if system_instruction:
                full_prompt = f"System Instruction: {system_instruction}\n\nUser Question: {prompt}"
            res = client.models.generate_content(model=m, contents=full_prompt)
            if res and res.text:
                return res.text.strip()
        except Exception as e:
            last_error = e
            logger.warning(f"Model {m} bilan xatolik: {e}, keyingi model sinab ko'riladi...")

    logger.warning(f"Barcha Gemini modellari muvaffaqiyatsiz bo'ldi: {last_error}")
    return rule_based_fallback(prompt)

async def ask_gemini_ai(prompt: str, system_instruction: str = "") -> str:
    """Asinxron Gemini AI javobini qaytaradi (event-loopni bloklamaydi)."""
    sys_inst = (
        "Siz O'zbekistondagi Kafil / Garant (Escrow Middleman) Telegram botining rasmiy xavfsizlik "
        "va yordam AI assistentisiz. Foydalanuvchilarga do'stona, aniq, professional o'zbek tilida javob bering. "
        "Agar foydalanuvchi muammosi juda og'ir bo'lsa yoki admin aralashuvi shart bo'lsa, "
        "javobingiz oxirida 'ADMIN_GA_UZATISH' kalit so'zini qo'shing."
    )
    if system_instruction:
        sys_inst = f"{sys_inst} {system_instruction}"

    return await asyncio.to_thread(_call_gemini_sync, prompt, sys_inst)

def _check_safety_sync(message_text: str) -> Tuple[bool, str]:
    """Guruhdagi xabarni Gemini AI va lokal filtrlar orqali tekshiradi."""
    # 1. Tezkor lokal filtr
    t = message_text.lower()
    curse_words = ["haromi", "onangni", "itvachcha", "sikay", "sikaman", "jalab", "gandon", "dalbayob", "kot", "shavkat", "blya"]
    for c in curse_words:
        if c in t:
            return False, "Haqoratli yoki so'kinish so'zlari ishlatildi"

    scam_patterns = ["boshqa joyda gaplashaylik", "botdan chiqaylik", "aldab ketaman", "tashab ketaman", "kidal", "kidala"]
    for s in scam_patterns:
        if s in t:
            return False, "Shubhali yoki firibgarlik harakati alomati aniqlandi"

    # 2. Gemini AI orqali tekshirish
    client = get_genai_client()
    if client:
        prompt = (
            f"Quyidagi matn savdo guruhidagi xabar: '{message_text}'. "
            "Ushbu matnda so'kinish, haqorat, firibgarlik, savdoni botdan tashqariga olib chiqish "
            "yoki aldash niyati bormi? Faqat 'XAVFSIZ' yoki 'XAVFLI: [sababi]' formatida javob ber."
        )
        for m in [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS:
            try:
                res = client.models.generate_content(model=m, contents=prompt)
                if res and res.text:
                    ans = res.text.strip()
                    if ans.startswith("XAVFLI"):
                        reason = ans.replace("XAVFLI:", "").strip() or "Qoidabuzarlik aniqlandi"
                        return False, reason
                    return True, ""
            except Exception:
                pass

    return True, ""

async def check_chat_safety(message_text: str) -> Tuple[bool, str]:
    """Guruhdagi xabarlarni AI bilan tekshiruvchi asinxron funksiya."""
    return await asyncio.to_thread(_check_safety_sync, message_text)

async def analyze_deals_for_admin(stats_summary: str, question: str) -> str:
    """Admin uchun Gemini AI jonli tahlil hisobotini tayyorlaydi."""
    prompt = (
        f"Kafil botining joriy statistikasi:\n{stats_summary}\n\n"
        f"Admin savoli: {question}\n\n"
        "Iltimos, professional tahlil, xavflar va takliflar bilan batafsil hisobot bering."
    )
    system_inst = "Siz Kafil Escrow tizimi boshqaruvchisi uchun professional Senior Biznes va Xavfsizlik tahlilchisisiz."
    return await asyncio.to_thread(_call_gemini_sync, prompt, system_inst)
