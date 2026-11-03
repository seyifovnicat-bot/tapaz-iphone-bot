import os
import re
import time
import html
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

# Loq konfiqurasiyası
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TapAzBot")

# Bakı Saat Qurşağı (UTC+4)
BAKU_TZ = timezone(timedelta(hours=4))

def get_baku_now():
    """Dəqiq Bakı vaxtını qaytarır (UTC+4)"""
    return datetime.now(BAKU_TZ)

# Telegram & Admin konfiqurasiyası
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873977210:AAGayQLy11TeRvM-42ZTd_H254FbywjBtB4")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7998954235"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60")) # 60 saniyə
PORT = int(os.getenv("PORT", "10000"))

TAPAZ_APPLE_URL = "https://tap.az/elanlar/elektronika/telefonlar?p%5B749%5D=3855"
BASE_URL = "https://tap.az"

BLACKLIST_KEYWORDS = [
    "mağaza", "magaza", "store", "shop", "şirkət", "sirket", "şəbəkə", "sebeke",
    "ünvan:", "unvan:", "ünvanımız", "unvanimiz", "ünvan ", "unvan ",
    "filial", "metro çıxışı", "metro cixisi", "28 may", "nəsimi", "nesimi", 
    "af mall", "mall", "tarqovı", "tarqovu", "sederek", "sədərək", "bazarstore",
    "kredit", "birkart", "bir kart", "tamkart", "tam kart", "bolkart", "bol kart",
    "albalı", "albali", "faizsiz", "taksit", "kreditlə", "kreditle", "aylıq", "ayliq",
    "çatdırılma", "catdirilma", "kuryer", "qeydiyyat daxil", "qeydiyyatli", "qeydiyyatlı",
    "rəsmi zəmanət", "resmi zemanet", "yazılı zəmanət", "yazili zemanet",
    "1 il zəmanət", "1 il zemanet", "12 ay zəmanət", "14 gün yoxlama", "14 gun yoxlama",
    "mağaza tərəfindən", "magaza terefinden", "servis zəmanəti",
    "orijinal adaptor hədiyyə", "adaptor hediyye", "qoruyucu şüşə hədiyyə",
    "suse hediyye", "kabro hədiyyə", "üzlük hədiyyə", "hediyye", "hədiyyə",
    "topdan", "topdansatış", "topdansatis", "bütün rənglər var", "butun rengler var",
    "say var", "say çoxdur", "say coxdur", "bağlı qutu", "bagli qutu",
    "açılmamış qutu", "acilmamis qutu", "qutuda yeni"
]

DB_FILE = "bot_system.db"

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_ads (
            id TEXT PRIMARY KEY,
            model TEXT,
            title TEXT,
            price TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            plan_name TEXT,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    add_or_update_user(ADMIN_ID, "Admin", "Admin", "Sahibkar (Limitsiz)", days=3650, is_active=1)

def cleanup_old_database_records():
    """
    Sistemin illərlə donmadan, yüngül və sürətli işləməsi üçün
    hər 24 saatdan bir köhnə elanları bazadan təmizləyir.
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        # 3 gündən köhnə görülmüş elanları təmizlə
        cur.execute("DELETE FROM seen_ads WHERE created_at < datetime('now', '-3 days')")
        deleted_count = cur.rowcount
        conn.commit()
        # Bazanı sıxlaşdırıb yaddaşı azad et (VACUUM)
        cur.execute("VACUUM")
        conn.close()
        logger.info(f"Avtomatik 24-saatlıq təmizləmə: {deleted_count} köhnə qeyd silindi, baza optimallaşdırıldı.")
    except Exception as e:
        logger.error(f"Köhnə qeydlərin təmizlənməsində xəta: {e}")

def run_daily_cleaner():
    """Hər 24 saatdan bir təmizləməni icra edən arxa plan prosesi"""
    while True:
        time.sleep(86400) # 24 saat = 86400 saniyə
        cleanup_old_database_records()

def is_ad_seen(ad_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen_ads WHERE id = ?", (str(ad_id),))
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_ad_seen(ad_id, model="", title="", price=""):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO seen_ads (id, model, title, price) VALUES (?, ?, ?, ?)", (str(ad_id), model, title, price))
    conn.commit()
    conn.close()

def register_pending_user(user_id, username, full_name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    now_baku = get_baku_now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO users (user_id, username, full_name, plan_name, expires_at, is_active)
        VALUES (?, ?, ?, 'Təsdiq Gözləyir', ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name
    """, (user_id, username or "", full_name or "", now_baku))
    conn.commit()
    conn.close()

def add_or_update_user(user_id, username, full_name, plan_name, days=3, is_active=1):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    expires_at = get_baku_now() + timedelta(days=days)
    exp_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO users (user_id, username, full_name, plan_name, expires_at, is_active)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
            full_name=CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE users.full_name END,
            plan_name=excluded.plan_name,
            expires_at=excluded.expires_at,
            is_active=excluded.is_active
    """, (user_id, username or "", full_name or "", plan_name, exp_str, is_active))
    conn.commit()
    conn.close()
    return expires_at

def get_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, full_name, plan_name, expires_at, is_active FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_active_subscribers():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    baku_now_str = get_baku_now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        SELECT user_id FROM users 
        WHERE is_active = 1 AND expires_at > ?
    """, (baku_now_str,))
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_all_users_list():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, full_name, plan_name, expires_at, is_active 
        FROM users ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def remove_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ==================== TELEGRAM API ====================
def send_telegram_msg(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return requests.post(url, json=payload, timeout=10).json()
    except Exception as e:
        logger.error(f"Telegram mesaj xətası: {e}")
        return None

def answer_callback(callback_query_id, text=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        requests.post(url, json=payload, timeout=8)
    except Exception:
        pass

def edit_message_text(chat_id, message_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=8)
    except Exception:
        pass

def send_telegram_photo(chat_id, photo_url, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=12).json()
        if res.get("ok"):
            return True
    except Exception:
        pass
    return send_telegram_msg(chat_id, caption)

# ==================== BROADCAST ====================
def broadcast_ad(ad):
    subscribers = get_active_subscribers()
    if not subscribers:
        logger.warning("Aktiv abunəçi tapılmadı!")
        return

    # Xüsusi simvolları (<, >, &) təhlükəsiz et
    safe_model = html.escape(str(ad.get('model', 'iPhone')))
    safe_price = html.escape(str(ad.get('price', '')))
    safe_author = html.escape(str(ad.get('author', 'Sahibi')))
    safe_desc = html.escape(str(ad.get('description', '')))
    safe_url = html.escape(str(ad.get('url', 'https://tap.az')))

    caption = (
        f"⚡ <b>İLK BİZDƏ! YENİ SAHİBİNDƏN İPHONE!</b>\n\n"
        f"📱 <b>Model:</b> {safe_model}\n"
        f"💰 <b>Qiymət:</b> {safe_price}\n"
        f"👤 <b>Satıcı:</b> {safe_author}\n\n"
        f"📝 <b>Məlumat:</b>\n<i>{safe_desc}</i>\n\n"
        f"🔗 <a href=\"{safe_url}\">Elana Tap.az-da Baxmaq Üçün Toxun</a>"
    )

    logger.info(f"Elan {len(subscribers)} aktiv abunəçiyə göndərilir: {ad['title']}")
    for uid in subscribers:
        try:
            if ad.get("img_url"):
                send_telegram_photo(uid, ad["img_url"], caption)
            else:
                send_telegram_msg(uid, caption)
            time.sleep(0.05)
        except Exception as e:
            logger.error(f"İstifadəçiyə ({uid}) göndərilə bilmədi: {e}")

# ==================== TELEGRAM KOMANDALARI & TƏSDİQ SİSTEMİ ====================
def handle_telegram_updates():
    offset = 0
    logger.info("Telegram komanda dinləyicisi işə düşdü...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            res = requests.get(url, timeout=25).json()
            if not res.get("ok"):
                time.sleep(3)
                continue

            for update in res.get("result", []):
                offset = update["update_id"] + 1

                cq = update.get("callback_query")
                if cq:
                    cq_id = cq.get("id")
                    from_user = cq.get("from", {})
                    from_id = from_user.get("id")
                    data = cq.get("data", "")
                    msg = cq.get("message", {})
                    msg_id = msg.get("message_id")

                    if from_id == ADMIN_ID and data:
                        parts = data.split(":")
                        action = parts[0]
                        target_id = int(parts[1]) if len(parts) > 1 else None

                        if action == "grant" and target_id:
                            plan_code = parts[2]
                            days = 3
                            plan_title = "3 Günlük Sınaq"
                            if plan_code == "3d":
                                days = 3
                                plan_title = "3 Günlük Sınaq"
                            elif plan_code == "1m":
                                days = 30
                                plan_title = "1 Aylıq Abunə"
                            elif plan_code == "3m":
                                days = 90
                                plan_title = "3 Aylıq Abunə"
                            elif plan_code == "1y":
                                days = 365
                                plan_title = "1 İllik VIP Abunə"

                            exp = add_or_update_user(target_id, "", "", plan_title, days=days, is_active=1)
                            exp_baku_formatted = exp.strftime("%d.%m.%Y %H:%M")

                            answer_callback(cq_id, f"{plan_title} aktiv edildi!")
                            edit_message_text(ADMIN_ID, msg_id, f"✅ <b>TƏSDİQLƏNDİ!</b>\n🆔 ID: <code>{target_id}</code>\n📦 Tarif: <b>{plan_title}</b>\n📅 Bitmə: <b>{exp_baku_formatted} (Bakı vaxtı)</b>")

                            user_msg = (
                                f"🎉 <b>TƏBRİKLƏR! Müraciətiniz təsdiqləndi!</b>\n\n"
                                f"🎁 Sizin üçün <b>{plan_title}</b> aktivləşdirildi!\n"
                                f"📅 Bitmə tarixi: <b>{exp_baku_formatted} (Bakı vaxtı ilə)</b>\n\n"
                                f"⚡ <i>Tap.az-a yeni düşən iPhone elanları dərhal bura göndəriləcək!</i>"
                            )
                            send_telegram_msg(target_id, user_msg)

                        elif action == "reject" and target_id:
                            remove_user(target_id)
                            answer_callback(cq_id, "Müraciət rədd edildi.")
                            edit_message_text(ADMIN_ID, msg_id, f"❌ <b>RƏDD EDİLDİ!</b>\n🆔 ID: <code>{target_id}</code> üçün icazə verilmədi.")
                            send_telegram_msg(target_id, "❌ Təəssüf ki, müraciətiniz admin tərəfindən təsdiqlənmədi.")

                    continue

                msg = update.get("message")
                if not msg:
                    continue

                user = msg.get("from", {})
                user_id = user.get("id")
                username = user.get("username", "")
                full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                text = msg.get("text", "").strip()

                if not user_id or not text:
                    continue

                baku_now_dt = get_baku_now()

                if text == "/start":
                    if user_id == ADMIN_ID:
                        send_telegram_msg(ADMIN_ID, "👑 <b>Salam Sahibkar!</b> Siz Adminsiniz və sisteminizdə limitsiz aktivsiniz.\n\nƏmrlər üçün /admin yazın.")
                        continue

                    existing = get_user(user_id)
                    if existing and existing[5] == 1:
                        exp_str = existing[4]
                        try:
                            exp_dt = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BAKU_TZ)
                            is_valid = exp_dt > baku_now_dt
                        except Exception:
                            is_valid = False

                        if is_valid:
                            exp_show = exp_dt.strftime("%d.%m.%Y %H:%M")
                            status_text = (
                                f"✅ <b>Abunəliyiniz Aktivdir!</b>\n\n"
                                f"📦 Tarif: <b>{existing[3]}</b>\n"
                                f"📅 Bitmə vaxtı: <b>{exp_show} (Bakı vaxtı)</b>\n\n"
                                f"📱 <i>Yeni iPhone elanları avtomatik bura daxil olur.</i>"
                            )
                            send_telegram_msg(user_id, status_text)
                            continue

                    register_pending_user(user_id, username, full_name)

                    pending_text = (
                        f"👋 <b>Salam, {full_name or 'Dəyərli İstifadəçi'}!</b>\n\n"
                        f"🔥 <b>Tap.az iPhone Sahibindən Radar Botuna xoş gəlmisiniz!</b>\n\n"
                        f"⏳ <b>Müraciətiniz qeydə alındı!</b>\n"
                        f"🆔 Sizin Telegram ID: <code>{user_id}</code>\n\n"
                        f"<i>Admin təsdiq etdikdən sonra bot sizin üçün dərhal aktivləşəcək və elanlar gəlməyə başlayacaq.</i>"
                    )
                    send_telegram_msg(user_id, pending_text)

                    admin_keyboard = {
                        "inline_keyboard": [
                            [
                                {"text": "🎁 3 Günlük Sınaq", "callback_data": f"grant:{user_id}:3d"},
                                {"text": "📦 1 Aylıq", "callback_data": f"grant:{user_id}:1m"}
                            ],
                            [
                                {"text": "📦 3 Aylıq", "callback_data": f"grant:{user_id}:3m"},
                                {"text": "💎 1 İllik VIP", "callback_data": f"grant:{user_id}:1y"}
                            ],
                            [
                                {"text": "❌ Rədd Et", "callback_data": f"reject:{user_id}"}
                            ]
                        ]
                    }

                    admin_notice = (
                        f"🔔 <b>YENİ QOŞULMA MÜRACİƏTİ!</b>\n\n"
                        f"👤 <b>Ad:</b> {full_name}\n"
                        f"🔗 <b>Username:</b> @{username if username else 'Yoxdur'}\n"
                        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
                        f"İcazə vermək üçün aşağıdakı düymələrdən birini seçin:"
                    )
                    send_telegram_msg(ADMIN_ID, admin_notice, reply_markup=admin_keyboard)

                elif user_id == ADMIN_ID:
                    if text in ["/admin", "admin"]:
                        subs = get_active_subscribers()
                        baku_current_str = baku_now_dt.strftime("%d.%m.%Y %H:%M:%S")
                        admin_menu = (
                            f"👑 <b>ADMİN İDARƏETMƏ PANELİ</b>\n\n"
                            f"🕒 <b>Cari Bakı Vaxtı:</b> {baku_current_str}\n"
                            f"📊 <b>Aktiv Abunəçilər:</b> {len(subs)} nəfər\n"
                            f"⏱ <b>Yoxlama İntervalı:</b> {CHECK_INTERVAL} saniyə\n"
                            f"🧹 <b>Avto-Təmizləmə:</b> 24 saatdan bir aktivdir\n\n"
                            f"🛠 <b>Əmrlər:</b>\n"
                            f"• <code>/qosh ID 3d</code> (3 günlük)\n"
                            f"• <code>/qosh ID 1m</code> (1 aylıq)\n"
                            f"• <code>/qosh ID 3m</code> (3 aylıq)\n"
                            f"• <code>/qosh ID 1y</code> (1 illik)\n"
                            f"• <code>/sil ID</code> (Dayandırmaq)\n"
                            f"• /istifadeciler (Bütün siyahı)\n"
                            f"• <code>/duyuru Mətn</code> (Kütləvi mesaj)"
                        )
                        send_telegram_msg(ADMIN_ID, admin_menu)

                    elif text.startswith("/qosh"):
                        parts = text.split()
                        if len(parts) >= 3:
                            target_id = int(parts[1])
                            duration_str = parts[2].lower()
                            days = 30
                            plan_title = "1 Aylıq Abunə"
                            if duration_str in ["3d", "3gun", "3gün"]:
                                days = 3
                                plan_title = "3 Günlük Sınaq"
                            elif duration_str in ["1m", "1ay", "30"]:
                                days = 30
                                plan_title = "1 Aylıq Abunə"
                            elif duration_str in ["3m", "3ay", "90"]:
                                days = 90
                                plan_title = "3 Aylıq Abunə"
                            elif duration_str in ["1y", "1il", "365"]:
                                days = 365
                                plan_title = "1 İllik VIP Abunə"

                            exp = add_or_update_user(target_id, "", "", plan_title, days=days, is_active=1)
                            exp_baku_formatted = exp.strftime("%d.%m.%Y %H:%M")

                            send_telegram_msg(ADMIN_ID, f"✅ İstifadəçi <code>{target_id}</code> üçün <b>{plan_title}</b> aktiv edildi!\n📅 Bitmə (Bakı vaxtı): <b>{exp_baku_formatted}</b>")
                            send_telegram_msg(target_id, f"🎉 <b>Təbriklər!</b>\n\nSizin üçün <b>{plan_title}</b> aktivləşdirildi!\n📅 Bitmə: <b>{exp_baku_formatted} (Bakı vaxtı)</b>")
                        else:
                            send_telegram_msg(ADMIN_ID, "Format: <code>/qosh ID 1m</code>")

                    elif text.startswith("/sil"):
                        parts = text.split()
                        if len(parts) >= 2:
                            target_id = int(parts[1])
                            remove_user(target_id)
                            send_telegram_msg(ADMIN_ID, f"❌ İstifadəçi <code>{target_id}</code> abunəlikdən çıxarıldı.")
                            send_telegram_msg(target_id, "⚠️ Sizin abunəliyiniz dayandırıldı.")

                    elif text in ["/istifadeciler", "/users"]:
                        all_users = get_all_users_list()
                        if not all_users:
                            send_telegram_msg(ADMIN_ID, "Heç bir istifadəçi tapılmadı.")
                        else:
                            resp_text = f"👥 <b>BÜTÜN İSTİFADƏÇİLƏR ({len(all_users)} nəfər):</b>\n\n"
                            for u in all_users[:25]:
                                u_id, u_name, f_name, p_name, exp_raw, is_act = u
                                try:
                                    exp_dt = datetime.strptime(exp_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BAKU_TZ)
                                    exp_str = exp_dt.strftime("%d.%m.%Y %H:%M")
                                    is_alive = is_act and exp_dt > baku_now_dt
                                except Exception:
                                    exp_str = exp_raw
                                    is_alive = is_act
                                status_emoji = "🟢" if is_alive else "🔴"
                                resp_text += f"{status_emoji} <b>{f_name or u_name}</b> (ID: <code>{u_id}</code>)\n   📦 {p_name} | ⏳ {exp_str}\n\n"
                            send_telegram_msg(ADMIN_ID, resp_text)

                    elif text.startswith("/duyuru"):
                        announcement = text.replace("/duyuru", "").strip()
                        if announcement:
                            subs = get_active_subscribers()
                            for sid in subs:
                                send_telegram_msg(sid, f"📢 <b>RƏSMİ BİLDİRİŞ:</b>\n\n{announcement}")
                            send_telegram_msg(ADMIN_ID, f"✅ {len(subs)} abunəçiyə bildiriş çatdırıldı.")

        except Exception as e:
            logger.error(f"Updates xətası: {e}")
            time.sleep(2)

# ==================== MODEL FİLTRİ ====================
def match_requested_model(title):
    t = title.lower()

    if "iphone" not in t:
        return None

    if "pro max" in t or "promax" in t or "plus" in t or "mini" in t:
        return None

    # iPhone 16 — YALNIZ sadə
    if re.search(r'\b16\b', t):
        if "pro" in t:
            return None
        return "iPhone 16"

    # iPhone 15 və 15 Pro
    if re.search(r'\b15\s*pro\b', t):
        return "iPhone 15 Pro"
    if re.search(r'\b15\b', t):
        if "pro" not in t:
            return "iPhone 15"

    # iPhone 14 və 14 Pro
    if re.search(r'\b14\s*pro\b', t):
        return "iPhone 14 Pro"
    if re.search(r'\b14\b', t):
        if "pro" not in t:
            return "iPhone 14"

    # iPhone 13 və 13 Pro
    if re.search(r'\b13\s*pro\b', t):
        return "iPhone 13 Pro"
    if re.search(r'\b13\b', t):
        if "pro" not in t:
            return "iPhone 13"

    return None

def contains_blacklist(text):
    if not text:
        return False
    t = text.lower()
    for kw in BLACKLIST_KEYWORDS:
        if kw in t:
            return True
    return False

def get_ad_details(context, ad_url):
    """Detalları ayrıca təmiz tabda yoxlayır və dərhal bağlayır"""
    det_page = None
    try:
        det_page = context.new_page()
        det_page.goto(ad_url, wait_until="domcontentloaded", timeout=25000)
        det_page.wait_for_timeout(1500)
        soup = BeautifulSoup(det_page.content(), "html.parser")

        # 1. İş saatları varmı?
        if soup.find(attrs={"data-testid": "author-info-working-schedule"}):
            return None

        # 2. Satıcı adı
        author_elem = soup.find(attrs={"data-testid": "author-info-name"})
        author_name = author_elem.text.strip() if author_elem else "Sahibi"
        if contains_blacklist(author_name):
            return None

        # 3. Satıcının elan sayı (> 4 olarsa alverçi sayılır)
        author_link = soup.find(attrs={"data-testid": "author-info-link"})
        if author_link:
            count_match = re.search(r'(\d+)\s*elan', author_link.text.lower())
            if count_match and int(count_match.group(1)) > 4:
                return None

        # 4. Təsvir mətni
        desc_elem = soup.find("div", class_=lambda c: c and "DescriptionContainer" in c)
        description = desc_elem.text.strip() if desc_elem else ""
        if contains_blacklist(description):
            return None

        # 5. Şəkil
        img_elem = soup.find("img", class_=lambda c: c and "SliderImage" in c) or soup.find("div", class_=lambda c: c and "SliderWrapper" in c)
        img_url = None
        if img_elem:
            img_tag = img_elem if img_elem.name == "img" else img_elem.find("img")
            if img_tag and img_tag.get("src"):
                img_url = img_tag["src"]

        return {
            "author": author_name,
            "description": description[:600],
            "img_url": img_url
        }
    except Exception as e:
        logger.error(f"Elan detalları xətası: {e}")
        return None
    finally:
        if det_page:
            try:
                det_page.close()
            except Exception:
                pass

def check_tapaz_ads(page, context):
    logger.info("Tap.az yoxlanılır (Bakı vaxtı ilə)...")
    try:
        page.goto(TAPAZ_APPLE_URL, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(3000)

        soup = BeautifulSoup(page.content(), "html.parser")
        latest_section = soup.find(attrs={"data-testid": "latest-ads-section"})
        cards = latest_section.find_all(attrs={"data-testid": "ad-card"}) if latest_section else soup.find_all(attrs={"data-testid": "ad-card"})

        for card in reversed(cards):
            if card.find(string=lambda t: t and "Mağaza" in t):
                continue

            href = card.get("href")
            if not href:
                continue

            ad_id_match = re.search(r'/(\d+)(?:\?|$)', href)
            if not ad_id_match:
                continue
            ad_id = ad_id_match.group(1)

            if is_ad_seen(ad_id):
                continue

            title_elem = card.find(attrs={"data-testid": "ad-card-title"})
            title = title_elem.text.strip() if title_elem else ""

            matched_model = match_requested_model(title)
            if not matched_model:
                mark_ad_seen(ad_id, "UNMATCHED", title)
                continue

            price_elem = card.find(attrs={"data-testid": "ad-card-price"})
            price = price_elem.text.strip() if price_elem else "Göstərilməyib"

            full_url = urljoin(BASE_URL, href)
            logger.info(f"Uyğun model tapıldı: {matched_model} - {title} ({price}) | Yoxlanılır...")

            details = get_ad_details(context, full_url)
            if not details:
                mark_ad_seen(ad_id, matched_model, title, price)
                continue

            ad_data = {
                "id": ad_id,
                "model": matched_model,
                "title": title,
                "price": price,
                "url": full_url,
                "author": details["author"],
                "description": details["description"],
                "img_url": details["img_url"]
            }

            broadcast_ad(ad_data)
            mark_ad_seen(ad_id, matched_model, title, price)
            time.sleep(1)

    except Exception as e:
        logger.error(f"Skanlama zamanı xəta: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Tap.az iPhone SaaS Bot 7/24 Aktivdir!")

    def log_message(self, format, *args):
        return

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logger.info(f"Veb server {PORT} portunda işə salındı.")
    server.serve_forever()

def main():
    init_db()
    now_baku = get_baku_now().strftime("%d.%m.%Y %H:%M:%S")
    logger.info(f"Tap.az iPhone SaaS Bot Başladı (Bakı vaxtı: {now_baku}, İnterval: 60s)...")

    # 1. Render üçün Health Server
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # 2. Telegram Komanda və Təsdiq Dinləyicisi
    updates_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
    updates_thread.start()

    # 3. Hər 24 saatdan bir köhnə elanları təmizləyən avto-mexanizm
    cleaner_thread = threading.Thread(target=run_daily_cleaner, daemon=True)
    cleaner_thread.start()

    # 4. Playwright Skraper
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        cycle_count = 0
        while True:
            try:
                check_tapaz_ads(page, context)
            except Exception as e:
                logger.error(f"Əsas dövr xətası: {e}")

            cycle_count += 1
            # Hər 500 dövrdən bir (təqribən 8 saatda bir) Chromium brauzer yaddaşını sıfırla ki, RAM heç vaxt dolmasın
            if cycle_count >= 500:
                try:
                    logger.info("Brauzer yaddaşı yenilənir (Memory Optimization)...")
                    page.close()
                    page = context.new_page()
                    cycle_count = 0
                except Exception:
                    pass

            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
