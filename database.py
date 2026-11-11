"""
DATABASE.PY - Ma'lumotlar bazasi boshqaruvi (SQLite)
Barcha 10 ta jadval, tranzaksiyalar, migratsiyalar va yordamchi funksiyalar.
"""
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple
from config import DB_PATH, TZ, DEFAULT_CARD_NUMBER, DEFAULT_CARD_OWNER

logger = logging.getLogger("kafil_db")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def q(query: str, args: tuple = (), one: bool = False, commit: bool = False) -> Any:
    """Xavfsiz SQL so'rovlarini bajaruvchi universal funksiya."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(query, args)
        if commit:
            conn.commit()
            return cur.lastrowid
        if one:
            return cur.fetchone()
        return cur.fetchall()
    finally:
        conn.close()

# ==================== SOZLAMALAR ====================
def get_setting(key: str, default: str = "") -> str:
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default

def set_setting(key: str, value: str):
    q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, str(value)), commit=True)

def init_db():
    """Barcha jadvallarni yaratish va migratsiyalarni bajarish."""
    conn = get_db()
    cur = conn.cursor()

    # 1. users (Foydalanuvchilar)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        completed_deals INTEGER DEFAULT 0,
        cashback_balance INTEGER DEFAULT 0,
        referral_balance INTEGER DEFAULT 0,
        referred_by INTEGER DEFAULT NULL,
        is_banned INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)

    # 2. deals (Savdolar va bitimlar)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deals (
        deal_id TEXT PRIMARY KEY,
        buyer_id INTEGER,
        buyer_username TEXT,
        seller_id INTEGER,
        seller_username TEXT,
        title TEXT,
        amount INTEGER,
        fee INTEGER,
        cashback_used INTEGER DEFAULT 0,
        total_to_pay INTEGER,
        cashback_earned INTEGER DEFAULT 0,
        mode TEXT DEFAULT 'bot',
        group_chat_id INTEGER DEFAULT NULL,
        group_code TEXT DEFAULT NULL,
        group_buyer_ready INTEGER DEFAULT 0,
        group_seller_ready INTEGER DEFAULT 0,
        status TEXT,
        payment_id TEXT,
        payment_created_at TEXT,
        seller_card TEXT,
        seller_card_owner TEXT,
        buyer_statement TEXT,
        seller_statement TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    # 3. blacklist (Firibgarlar / Qora ro'yxat)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS blacklist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        reason TEXT,
        deal_id TEXT,
        added_by TEXT,
        created_at TEXT
    )
    """)

    # 4. complaints (Arizalar va shikoyatlar)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS complaints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id TEXT,
        complainant_id INTEGER,
        complainant_username TEXT,
        accused_id INTEGER,
        accused_username TEXT,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        deadline_at TEXT,
        created_at TEXT
    )
    """)

    # 5. referrals (Referallar jurnali)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )
    """)

    # 6. withdrawals (Referal pul yechish so'rovlari)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        card_number TEXT,
        card_owner TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )
    """)

    # 7. mandatory_channels (Majburiy obuna kanallari)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS mandatory_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT,
        channel_title TEXT,
        invite_link TEXT
    )
    """)

    # 8. deal_messages (Chat yozishmalari logi)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deal_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id TEXT,
        sender_id INTEGER,
        sender_tag TEXT,
        role TEXT,
        message_text TEXT,
        created_at TEXT
    )
    """)

    # 9. ai_tickets (Yordam va SOS murojaatlari)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ai_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        user_name TEXT,
        question TEXT,
        status TEXT DEFAULT 'open',
        admin_reply TEXT,
        created_at TEXT
    )
    """)

    # 10. settings (Tizim sozlamalari)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # Mavjud bazalar uchun ustunlarni tekshirish va qo'shish
    user_cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "referral_balance" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN referral_balance INTEGER DEFAULT 0")
    if "referred_by" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")

    deal_cols = [r[1] for r in cur.execute("PRAGMA table_info(deals)").fetchall()]
    for col, col_type in [
        ("mode", "TEXT DEFAULT 'bot'"),
        ("group_chat_id", "INTEGER DEFAULT NULL"),
        ("group_code", "TEXT DEFAULT NULL"),
        ("group_buyer_ready", "INTEGER DEFAULT 0"),
        ("group_seller_ready", "INTEGER DEFAULT 0"),
        ("payment_created_at", "TEXT"),
        ("buyer_statement", "TEXT"),
        ("seller_statement", "TEXT")
    ]:
        if col not in deal_cols:
            cur.execute(f"ALTER TABLE deals ADD COLUMN {col} {col_type}")

    conn.commit()
    conn.close()

    # Boshlang'ich sozlamalarni o'rnatish
    if not get_setting("card_number"):
        set_setting("card_number", DEFAULT_CARD_NUMBER)
    if not get_setting("card_owner"):
        set_setting("card_owner", DEFAULT_CARD_OWNER)
    if not get_setting("commission_percent"):
        set_setting("commission_percent", "10")

init_db()

# ==================== FOYDALANUVCHILAR ====================
def ensure_user(user_id: int, username: Optional[str], full_name: Optional[str], referred_by: Optional[int] = None):
    u = q("SELECT user_id FROM users WHERE user_id=?", (user_id,), one=True)
    now_s = datetime.now(TZ).isoformat()
    clean_un = (username or "").lstrip("@")
    if not u:
        q("""
        INSERT INTO users (user_id, username, full_name, completed_deals, cashback_balance, referral_balance, referred_by, is_banned, created_at)
        VALUES (?, ?, ?, 0, 0, 0, ?, 0, ?)
        """, (user_id, clean_un, full_name or "", referred_by, now_s), commit=True)
    else:
        q("UPDATE users SET username=?, full_name=? WHERE user_id=?", (clean_un, full_name or "", user_id), commit=True)

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    return q("SELECT * FROM users WHERE user_id=?", (user_id,), one=True)

# ==================== QORA RO'YXAT / FIRIBGARLAR ====================
def is_blacklisted(user_id: Optional[int] = None, username: Optional[str] = None) -> Tuple[bool, str]:
    """Foydalanuvchi qora ro'yxatda bor-yo'qligini tekshiradi."""
    if user_id:
        r = q("SELECT reason FROM blacklist WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,), one=True)
        if r:
            return True, r["reason"] or "Firibgarlik harakati"
    if username:
        clean_un = username.lstrip("@").strip().lower()
        if clean_un:
            r = q("SELECT reason FROM blacklist WHERE LOWER(username)=? ORDER BY id DESC LIMIT 1", (clean_un,), one=True)
            if r:
                return True, r["reason"] or "Firibgarlik harakati"
    return False, ""

def add_to_blacklist(user_id: Optional[int] = None, username: Optional[str] = None, reason: str = "", deal_id: str = "", added_by: str = "system"):
    """Foydalanuvchini Telegram ID va username orqali qora ro'yxatga kiritadi."""
    now_s = datetime.now(TZ).isoformat()
    clean_un = (username or "").lstrip("@").strip()
    q("INSERT INTO blacklist (user_id, username, reason, deal_id, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      (user_id, clean_un, reason, deal_id, added_by, now_s), commit=True)
    if user_id:
        q("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,), commit=True)

def remove_from_blacklist(blacklist_id: int):
    row = q("SELECT user_id FROM blacklist WHERE id=?", (blacklist_id,), one=True)
    if row and row["user_id"]:
        q("UPDATE users SET is_banned=0 WHERE user_id=?", (row["user_id"],), commit=True)
    q("DELETE FROM blacklist WHERE id=?", (blacklist_id,), commit=True)

def get_all_blacklisted(limit: int = 20) -> List[sqlite3.Row]:
    return q("SELECT * FROM blacklist ORDER BY id DESC LIMIT ?", (limit,))

# ==================== SAVDOLAR (DEALS) ====================
def create_deal(deal_id: str, buyer_id: int, buyer_username: str, seller_username: str,
                title: str, amount: int, fee: int, total_to_pay: int, mode: str, payment_id: Optional[str]) -> str:
    now_s = datetime.now(TZ).isoformat()
    q("""
    INSERT INTO deals (deal_id, buyer_id, buyer_username, seller_username, title, amount, fee, total_to_pay, mode, status, payment_id, payment_created_at, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting_payment', ?, ?, ?, ?)
    """, (deal_id, buyer_id, buyer_username, seller_username, title, amount, fee, total_to_pay, mode, payment_id, now_s, now_s, now_s), commit=True)
    return deal_id

def get_deal(deal_id: str) -> Optional[sqlite3.Row]:
    return q("SELECT * FROM deals WHERE deal_id=?", (deal_id,), one=True)

def get_active_deal_for_user(user_id: int) -> Optional[sqlite3.Row]:
    return q("SELECT * FROM deals WHERE buyer_id=? AND status IN ('created', 'waiting_payment', 'in_trade', 'goods_delivered', 'buyer_statement', 'seller_statement', 'buyer_confirmed', 'dispute') ORDER BY updated_at DESC LIMIT 1", (user_id,), one=True)

def log_deal_message(deal_id: str, sender_id: int, sender_tag: str, role: str, text: str):
    now_s = datetime.now(TZ).isoformat()
    q("INSERT INTO deal_messages (deal_id, sender_id, sender_tag, role, message_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      (deal_id, sender_id, sender_tag, role, text, now_s), commit=True)

def get_deal_messages(deal_id: str, limit: int = 50) -> List[sqlite3.Row]:
    return q("SELECT * FROM deal_messages WHERE deal_id=? ORDER BY id ASC LIMIT ?", (deal_id, limit))

# ==================== PUL YECHISH (WITHDRAWALS) ====================
def create_withdrawal(user_id: int, amount: int, card_number: str, card_owner: str) -> int:
    now_s = datetime.now(TZ).isoformat()
    return q("""
    INSERT INTO withdrawals (user_id, amount, card_number, card_owner, status, created_at)
    VALUES (?, ?, ?, ?, 'pending', ?)
    """, (user_id, amount, card_number, card_owner, now_s), commit=True)

def get_pending_withdrawals(limit: int = 10) -> List[sqlite3.Row]:
    return q("SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at ASC LIMIT ?", (limit,))

def complete_withdrawal(withdrawal_id: int) -> bool:
    q("UPDATE withdrawals SET status='completed' WHERE id=?", (withdrawal_id,), commit=True)
    return True

# ==================== ARIZALAR (COMPLAINTS) ====================
def create_complaint(deal_id: str = "", complainant_id: int = 0, complainant_username: str = "",
                     accused_id: Optional[int] = None, accused_username: str = "", reason: str = "",
                     deadline_s: Optional[str] = None) -> int:
    now = datetime.now(TZ)
    now_s = now.isoformat()
    if not deadline_s:
        deadline_s = (now + timedelta(days=5)).isoformat()
    return q("""
    INSERT INTO complaints (deal_id, complainant_id, complainant_username, accused_id, accused_username, reason, status, deadline_at, created_at)
    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (deal_id or "", complainant_id, complainant_username or "", accused_id, accused_username or "", reason, deadline_s, now_s), commit=True)

def get_user_complaints(user_id: int) -> List[sqlite3.Row]:
    return q("SELECT * FROM complaints WHERE complainant_id=? ORDER BY id DESC", (user_id,))

def withdraw_complaint(complaint_id: int) -> bool:
    q("UPDATE complaints SET status='withdrawn' WHERE id=?", (complaint_id,), commit=True)
    return True

# ==================== ZAXIRA NUSXA (BACKUP & RESTORE) ====================
def export_user_backup(user_id: int) -> dict:
    u = get_user(user_id)
    deals = [dict(d) for d in q("SELECT * FROM deals WHERE buyer_id=? OR seller_id=?", (user_id, user_id))]
    complaints = [dict(c) for c in q("SELECT * FROM complaints WHERE complainant_id=? OR accused_id=?", (user_id, user_id))]
    return {
        "version": "3.0",
        "user_id": user_id,
        "exported_at": datetime.now(TZ).isoformat(),
        "profile": dict(u) if u else {},
        "deals": deals,
        "complaints": complaints
    }

def import_user_backup(user_id: int, data: dict) -> bool:
    try:
        profile = data.get("profile", {})
        if profile:
            q("""
            UPDATE users SET completed_deals=?, cashback_balance=?, referral_balance=?
            WHERE user_id=?
            """, (profile.get("completed_deals", 0), profile.get("cashback_balance", 0),
                  profile.get("referral_balance", 0), user_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Backup tiklashda xato: {e}")
        return False

def export_user_backup_json(user_id: int) -> bytes:
    data = export_user_backup(user_id)
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

def import_user_backup_json(user_id: int, json_data: Any) -> Tuple[bool, int]:
    try:
        if isinstance(json_data, bytes):
            json_data = json_data.decode("utf-8")
        data = json.loads(json_data)
        ok = import_user_backup(user_id, data)
        deals_count = len(data.get("deals", []))
        return ok, deals_count
    except Exception as e:
        logger.error(f"Backup import xatosi: {e}")
        return False, 0
