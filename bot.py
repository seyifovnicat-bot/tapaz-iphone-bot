import os
import re
import json
import time
import html
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urljoin
import requests
from dotenv import load_dotenv

load_dotenv()

RECENT_LOGS = deque(maxlen=25)

class RingLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            RECENT_LOGS.append(msg)
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TapAzBot")
ring_handler = RingLogHandler()
ring_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(ring_handler)

BAKU_TZ = timezone(timedelta(hours=4))

def get_baku_now():
    """Dəqiq Bakı vaxtı (UTC+4)"""
    return datetime.now(BAKU_TZ)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873977210:AAGayQLy11TeRvM-42ZTd_H254FbywjBtB4")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7998954235"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "acountnew1")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://tapaz-iphone-bot-1.onrender.com")

# Real-vaxtda yeni düşən iPhone elanları linki
TAPAZ_APPLE_URL = "https://tap.az/elanlar/elektronika/telefonlar?keywords=iphone"
BASE_URL = "https://tap.az"

BOT_STATS = {
    "started_at": get_baku_now().strftime("%d.%m.%Y %H:%M:%S"),
    "last_check_at": "Hələ yoxlanmayıb",
    "total_checks": 0,
    "last_found_ad": "Hələ tapılmayıb",
    "status": "Aktiv"
}

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
    "açılmamış qutu", "acilmamis qutu", "qutuda yeni",
    "gsm", "telecom", "mobile", "aksesuar", "accessories", "official",
    "service", "servis", "satış", "satis", "butik", "baku electronics",
    "kontakt", "irshad", "irşad", "apple store", "apple house", "smart store"
]

DB_FILE = "bot_system.db"
BACKUP_FILE = "users_backup.json"

# ==================== DATABASE & BACKUP ====================
def backup_users_to_json():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, full_name, plan_name, expires_at, is_active FROM users")
        rows = cur.fetchall()
        conn.close()
        data = []
        for r in rows:
            data.append({
                "user_id": r[0], "username": r[1], "full_name": r[2],
                "plan_name": r[3], "expires_at": r[4], "is_active": r[5]
            })
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Backup xətası: {e}")

def restore_users_from_backup():
    if not os.path.exists(BACKUP_FILE):
        return
    try:
        with open(BACKUP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        for u in data:
            cur.execute("""
                INSERT OR IGNORE INTO users (user_id, username, full_name, plan_name, expires_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (u["user_id"], u.get("username",""), u.get("full_name",""), u["plan_name"], u["expires_at"], u["is_active"]))
        conn.commit()
        conn.close()
        logger.info(f"Bərpa: {len(data)} istifadəçi fayldan bərpa edildi.")
    except Exception as e:
        logger.warning(f"Bərpa xətası: {e}")

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

    restore_users_from_backup()
    add_or_update_user(ADMIN_ID, "Admin", "Admin", "Sahibkar (Limitsiz)", days=3650, is_active=1)

def cleanup_old_database_records():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        # 48 saatdan (2 gündən) köhnə qeydlər silinir ki, baza illərlə yüngül qalsın və sistem heç vaxt donmasın
        cur.execute("DELETE FROM seen_ads WHERE created_at < datetime('now', '-2 days')")
        deleted_count = cur.rowcount
        conn.commit()
        cur.execute("VACUUM")
        conn.close()
        logger.info(f"Avtomatik 24-saatlıq təmizləmə: {deleted_count} köhnə elan qeydi silindi və VACUUM edildi.")
    except Exception as e:
        logger.error(f"Təmizləmə xətası: {e}")

def run_daily_cleaner():
    # Bot başlayan kimi dərhal 1 dəfə təmizləyir
    time.sleep(10)
    cleanup_old_database_records()
    while True:
        time.sleep(86400) # Hər 24 saatdan bir dövri təmizləyir
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
    backup_users_to_json()

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
    backup_users_to_json()
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
    backup_users_to_json()

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
    BOT_STATS["last_found_ad"] = f"{ad['model']} ({ad['price']}) - {get_baku_now().strftime('%H:%M:%S')}"

    photo_caption = caption[:1020]
    for uid in subscribers:
        try:
            sent = False
            if ad.get("img_url"):
                sent = send_telegram_photo(uid, ad["img_url"], photo_caption)
            if not sent:
                send_telegram_msg(uid, caption)
            time.sleep(0.05)
        except Exception as e:
            logger.error(f"İstifadəçiyə ({uid}) göndərilə bilmədi: {e}")

# ==================== TELEGRAM KOMANDALARI & MÜTŞƏKKİL /START ====================
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

                # 1. INLINE DÜYMƏ TIKLANMASI (ADMIN TƏRƏFİNDƏN)
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

                # --- /START DƏQİQ MƏNTİQİ ---
                if text == "/start":
                    # 1. Admin özü yazanda
                    if user_id == ADMIN_ID:
                        send_telegram_msg(ADMIN_ID, "👑 <b>Salam Sahibkar!</b> Siz Adminsiniz və sisteminizdə limitsiz aktivsiniz.\n\nƏmrlər üçün /admin yazın.")
                        continue

                    existing = get_user(user_id)

                    # 2. Əgər istifadəçi artıq sistemdə varsa:
                    if existing:
                        p_name = existing[3]
                        exp_str = existing[4]
                        is_act = existing[5]

                        try:
                            exp_dt = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BAKU_TZ)
                            is_alive = is_act == 1 and exp_dt > baku_now_dt
                        except Exception:
                            is_alive = False

                        # A) Əgər istifadəçi artıq AKTİV qoşulubsa:
                        if is_alive:
                            exp_show = exp_dt.strftime("%d.%m.%Y %H:%M")
                            already_active_msg = (
                                f"✅ <b>Siz artıq sistemə qoşulmusunuz!</b>\n\n"
                                f"📦 Abunəlik növü: <b>{p_name}</b>\n"
                                f"📅 Bitmə vaxtı: <b>{exp_show} (Bakı vaxtı ilə)</b>\n\n"
                                f"⚡ <i>Yeni sahibindən iPhone elanları sayta düşən saniyədə avtomatik sizə çatdırılır.</i>\n\n"
                                f"ℹ️ Əlavə sual, təklif və ya məlumat üçün Admin ilə əlaqə saxlaya bilərsiniz:\n"
                                f"👤 Admin: @{ADMIN_USERNAME}"
                            )
                            send_telegram_msg(user_id, already_active_msg)
                            # QƏTİYYƏN ADMİNƏ BİLDİRİŞ GÖNDƏRMİRİK!
                            continue

                        # B) Əgər hələ təsdiq gözləyirsə (Admin hələ düyməyə basmayıb):
                        elif p_name == "Təsdiq Gözləyir" or is_act == 0:
                            pending_wait_msg = (
                                f"⏳ <b>Müraciətiniz artıq qeydə alınıb!</b>\n\n"
                                f"🆔 Sizin Telegram ID: <code>{user_id}</code>\n"
                                f"Sorğunuz adminin təsdiqini gözləyir. Təsdiq olunduqda bot sizə avtomatik bildiriş göndərəcək.\n\n"
                                f"Tələsirsinizsə, admin ilə əlaqə saxlaya bilərsiniz:\n"
                                f"👤 Admin: @{ADMIN_USERNAME}"
                            )
                            send_telegram_msg(user_id, pending_wait_msg)
                            # Artıq müraciət edibsə, admini təkrar narahat etmirik!
                            continue

                        # C) Əgər abunəlik müddəti bitibsə:
                        else:
                            expired_msg = (
                                f"⚠️ <b>Sizin abunəlik müddətiniz başa çatıb!</b>\n\n"
                                f"Yenidən elanları almaq və ya abunəliyi uzatmaq üçün zəhmət olmasa admin ilə əlaqə saxlayın:\n"
                                f"👤 Admin: @{ADMIN_USERNAME}\n\n"
                                f"💳 <b>Tariflər:</b>\n"
                                f"• 1 Aylıq Abunə\n"
                                f"• 3 Aylıq Abunə\n"
                                f"• 1 İllik VIP Abunə"
                            )
                            send_telegram_msg(user_id, expired_msg)
                            continue

                    # 3. YALNIZ İLK DƏFƏ /START EDƏN TAM YENİ İSTİFADƏÇİ:
                    register_pending_user(user_id, username, full_name)

                    pending_text = (
                        f"👋 <b>Salam, {full_name or 'Dəyərli İstifadəçi'}!</b>\n\n"
                        f"🔥 <b>Tap.az iPhone Sahibindən Radar Botuna xoş gəlmisiniz!</b>\n\n"
                        f"⏳ <b>Müraciətiniz qeydə alındı!</b>\n"
                        f"🆔 Sizin Telegram ID: <code>{user_id}</code>\n\n"
                        f"<i>Admin təsdiq etdikdən sonra bot sizin üçün dərhal aktivləşəcək və elanlar gəlməyə başlayacaq.</i>\n\n"
                        f"Əlaqə üçün Admin: @{ADMIN_USERNAME}"
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

                # --- ADMIN KOMANDALARI ---
                elif user_id == ADMIN_ID:
                    if text in ["/admin", "admin"]:
                        subs = get_active_subscribers()
                        baku_current_str = baku_now_dt.strftime("%d.%m.%Y %H:%M:%S")
                        admin_menu = (
                            f"👑 <b>ADMİN İDARƏETMƏ PANELİ</b>\n\n"
                            f"🕒 <b>Cari Bakı Vaxtı:</b> {baku_current_str}\n"
                            f"📊 <b>Aktiv Abunəçilər:</b> {len(subs)} nəfər\n"
                            f"⏱ <b>Yoxlama İntervalı:</b> {CHECK_INTERVAL} saniyə\n"
                            f"🔍 <b>Son Axtarış:</b> {BOT_STATS['last_check_at']}\n"
                            f"📱 <b>Son Elan:</b> {BOT_STATS['last_found_ad']}\n\n"
                            f"🛠 <b>Əmrlər:</b>\n"
                            f"• /test və ya /yoxla (Canlı status)\n"
                            f"• <code>/qosh ID 3d</code> (3 günlük)\n"
                            f"• <code>/qosh ID 1m</code> (1 aylıq)\n"
                            f"• <code>/qosh ID 3m</code> (3 aylıq)\n"
                            f"• <code>/qosh ID 1y</code> (1 illik)\n"
                            f"• <code>/sil ID</code> (Dayandırmaq)\n"
                            f"• /istifadeciler (Bütün siyahı)\n"
                            f"• <code>/duyuru Mətn</code> (Kütləvi mesaj)"
                        )
                        send_telegram_msg(ADMIN_ID, admin_menu)

                    elif text in ["/test", "test", "/yoxla", "yoxla", "/status"]:
                        subs = get_active_subscribers()
                        baku_current_str = baku_now_dt.strftime("%d.%m.%Y %H:%M:%S")
                        test_reply = (
                            f"⚡ <b>BOT HAZIRDIR VƏ İZLƏYİR!</b>\n\n"
                            f"🕒 <b>Cari Bakı Vaxtı:</b> {baku_current_str}\n"
                            f"📊 <b>Aktiv Abunəçilər:</b> {len(subs)} nəfər\n"
                            f"🔄 <b>Ümumi Skan Sayı:</b> #{BOT_STATS['total_checks']}\n"
                            f"⏱ <b>Son Skan:</b> {BOT_STATS['last_check_at']}\n"
                            f"📱 <b>Son Elan:</b> {BOT_STATS['last_found_ad']}\n"
                            f"🌐 <b>Canlı Panel:</b> {RENDER_EXTERNAL_URL}"
                        )
                        send_telegram_msg(ADMIN_ID, test_reply)

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

    # Pro Max, Plus, Mini və ehtiyat hissələri / aksesuarlar qəti qadağandır
    excluded_parts = [
        "pro max", "promax", "plus", "mini", 
        "ekran", "korpus", "plata", "kamera", "şüşə", "suse", 
        "batareya", "battery", "adapter", "kabel", "nausnik", 
        "qutu", "karobka", "case", "kabro"
    ]
    if any(k in t for k in excluded_parts):
        return None

    # iPhone 16 — YALNIZ sadə model (Pro/Pro Max qadağandır)
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
        if len(kw) <= 4:
            if re.search(r'\b' + re.escape(kw) + r'\b', t):
                return True
        else:
            if kw in t:
                return True
    return False

TAPAZ_GRAPHQL_URL = "https://tap.az/graphql"
GRAPHQL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://tap.az",
    "Referer": "https://tap.az/elanlar/elektronika/telefonlar"
}

GRAPHQL_QUERY = """
query SearchIphones($keywords: String, $first: Int!) {
  ads(
    source: APP
    keywords: $keywords
    first: $first
  ) {
    nodes {
      id
      title
      price
      hits
      path
      region
      contact {
        name
      }
      photo {
        url
      }
      user {
        name
        isShop
        publishedAdsCount
      }
    }
  }
}
"""

def check_tapaz_ads():
    BOT_STATS["last_check_at"] = get_baku_now().strftime("%d.%m.%Y %H:%M:%S")
    BOT_STATS["total_checks"] += 1
    logger.info(f"Tap.az GraphQL API canlı yoxlanılır (Dövr #{BOT_STATS['total_checks']})...")

    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {
            "keywords": "iPhone",
            "first": 40
        }
    }

    try:
        resp = requests.post(TAPAZ_GRAPHQL_URL, json=payload, headers=GRAPHQL_HEADERS, timeout=12)
        if resp.status_code != 200:
            logger.error(f"GraphQL API status xətası: {resp.status_code}")
            return

        data = resp.json()
        ads = data.get("data", {}).get("ads", {}).get("nodes", [])
        logger.info(f"GraphQL API-dən {len(ads)} ədəd canlı elan alındı.")

        for ad in reversed(ads):
            # 1. Mağaza filtri: isShop True olanlar dərhal atılır
            user_info = ad.get("user") or {}
            is_shop = user_info.get("isShop", False)
            if is_shop:
                continue

            # 2. ALVERÇİ FİLTRİ: İstifadəçi tələbi: Maksimum 2 elanı ola bilər!
            # 2-dən çox elanı olanlar gizli alverçidir və qətiyyən buraxılmır!
            published_count = user_info.get("publishedAdsCount") or 1
            if published_count > 2:
                continue

            raw_path = ad.get("path") or ""
            ad_id_match = re.search(r'/(\d+)(?:\?|$)', raw_path)
            if not ad_id_match:
                continue
            ad_id = ad_id_match.group(1)

            if is_ad_seen(ad_id):
                continue

            title = (ad.get("title") or "").strip()
            matched_model = match_requested_model(title)
            if not matched_model:
                mark_ad_seen(ad_id, "UNMATCHED", title)
                continue

            contact_info = ad.get("contact") or {}
            seller_name = (contact_info.get("name") or user_info.get("name") or "Sahibi").strip()

            # Qara siyahı açar sözləri satıcı və ya başlıqda varmı?
            if contains_blacklist(seller_name) or contains_blacklist(title):
                mark_ad_seen(ad_id, matched_model, title)
                continue

            price_val = ad.get("price")
            if price_val is not None:
                price_str = f"{int(price_val) if price_val == int(price_val) else price_val} ₼"
            else:
                price_str = "Razılaşma ilə"

            views = ad.get("hits") or 0
            # YALNIZ YENİ DÜŞMÜŞ TƏZƏ ELANLAR: Maksimum 15 baxış (0, 1, 2, 3, 5, ..., 15 baxış)
            if views > 15:
                mark_ad_seen(ad_id, matched_model, title, price_str)
                continue

            photo_obj = ad.get("photo") or {}
            img_url = photo_obj.get("url")
            full_url = urljoin(BASE_URL, raw_path)
            region = ad.get("region") or "Bakı"

            logger.info(f"YENİ SAHİBİNDƏN İPHONE: {matched_model} - {title} ({price_str}) | Satıcı: {seller_name} ({published_count} elanı)")

            ad_data = {
                "id": ad_id,
                "model": matched_model,
                "title": title,
                "price": price_str,
                "url": full_url,
                "author": f"{seller_name} (Şəxsi Sahib - {published_count} elan)",
                "description": f"📍 Şəhər: {region}\n👁 Baxış sayı: {views}\n📦 Satıcının ümumi elan sayı: {published_count} (Şəxsi)",
                "img_url": img_url
            }

            broadcast_ad(ad_data)
            mark_ad_seen(ad_id, matched_model, title, price_str)
            time.sleep(0.3)

    except Exception as e:
        logger.error(f"GraphQL sorğusu zamanı xəta: {e}")

# ==================== RENDER KEEP-ALIVE PINGER ====================
def keep_render_alive():
    time.sleep(60)
    while True:
        try:
            requests.get(RENDER_EXTERNAL_URL, timeout=15)
        except Exception:
            pass
        time.sleep(360)

# ==================== WEB & DIAGNOSTICS SERVER ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/test_alert":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            test_ad = {
                "id": "manual_test_" + str(int(time.time())),
                "model": "iPhone 15 Pro",
                "title": "Apple iPhone 15 Pro Natural Titanium 128GB",
                "price": "1 650 AZN",
                "url": "https://tap.az",
                "author": "Şəxsi Sahib",
                "description": "Sınaq bildirişi: Bot 100% işləyir və hər skanda yeni elanları dərhal çatdırır.",
                "img_url": None
            }
            broadcast_ad(test_ad)
            self.wfile.write("<h3>✅ Test elanı Telegram-a uğurla göndərildi! Telegram botunuzu yoxlayın.</h3><a href='/'>Geri qayıt</a>".encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

        subs_count = len(get_active_subscribers())
        baku_time = get_baku_now().strftime("%d.%m.%Y %H:%M:%S")

        logs_formatted = "\n".join(html.escape(l) for l in list(RECENT_LOGS)[-20:])

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Tap.az iPhone SaaS Bot - Live Dashboard</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="15">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 25px; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 24px; max-width: 700px; margin: 0 auto; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }}
        h1 {{ color: #58a6ff; font-size: 22px; margin-top: 0; display: flex; align-items: center; justify-content: space-between; }}
        .badge {{ background: #238636; color: white; padding: 4px 10px; border-radius: 20px; font-weight: bold; font-size: 13px; }}
        .row {{ display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #21262d; }}
        .label {{ color: #8b949e; }}
        .val {{ font-weight: 600; color: #f0f6fc; }}
        .btn {{ display: inline-block; background: #1f6feb; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-weight: bold; font-size: 13px; margin-top: 15px; text-align: center; }}
        .btn:hover {{ background: #388bfd; }}
        .log-box {{ background: #010409; border: 1px solid #30363d; border-radius: 8px; padding: 12px; font-family: 'Consolas', 'Courier New', monospace; font-size: 12px; color: #7ee787; max-height: 250px; overflow-y: auto; white-space: pre-wrap; margin-top: 15px; }}
        .log-title {{ font-size: 13px; font-weight: bold; color: #8b949e; margin-top: 20px; margin-bottom: 5px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Tap.az iPhone Radar Bot <span class="badge">7/24 OYAQ</span></h1>
        <div class="row"><span class="label">Status:</span><span class="val" style="color:#3fb950">Aktiv və İzləyir</span></div>
        <div class="row"><span class="label">Cari Bakı Vaxtı:</span><span class="val">{baku_time}</span></div>
        <div class="row"><span class="label">Son Skan Saatı:</span><span class="val">{BOT_STATS['last_check_at']}</span></div>
        <div class="row"><span class="label">Ümumi Skan Sayı:</span><span class="val">#{BOT_STATS['total_checks']}</span></div>
        <div class="row"><span class="label">Aktiv Abunəçilər:</span><span class="val">{subs_count} nəfər</span></div>
        <div class="row"><span class="label">Son Aşkarlanan Elan:</span><span class="val">{BOT_STATS['last_found_ad']}</span></div>
        <div class="row"><span class="label">Yoxlama İntervalı:</span><span class="val">{CHECK_INTERVAL} saniyə</span></div>
        
        <div style="text-align: center;">
            <a href="/test_alert" class="btn">📲 Telegram-a Canlı Sınaq Bildirişi Göndər</a>
        </div>

        <div class="log-title">Canlı Sistem Jurnalı (Real-Time Logs):</div>
        <div class="log-box">{logs_formatted if logs_formatted else 'Jurnal hələ toplanır...'}</div>
    </div>
</body>
</html>
"""
        self.wfile.write(html_content.encode("utf-8"))

    def log_message(self, format, *args):
        return

def run_web_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logger.info(f"Threading veb server {PORT} portunda işə salındı.")
    server.serve_forever()

def main():
    init_db()
    now_baku = get_baku_now().strftime("%d.%m.%Y %H:%M:%S")
    logger.info(f"Tap.az iPhone SaaS Bot Başladı (GraphQL API, Bakı: {now_baku}, İnterval: {CHECK_INTERVAL}s)...")

    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    updates_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
    updates_thread.start()

    cleaner_thread = threading.Thread(target=run_daily_cleaner, daemon=True)
    cleaner_thread.start()

    pinger_thread = threading.Thread(target=keep_render_alive, daemon=True)
    pinger_thread.start()

    while True:
        try:
            check_tapaz_ads()
        except Exception as e:
            logger.error(f"Əsas dövr xətası: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
