import os
import re
import time
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TapAzBot")

# Telegram & Admin konfiqurasiyası
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873977210:AAGayQLy11TeRvM-42ZTd_H254FbywjBtB4")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7998954235"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "20")) # Sürətli yoxlama: 20 saniyə
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
    # Görülmüş elanlar
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_ads (
            id TEXT PRIMARY KEY,
            model TEXT,
            title TEXT,
            price TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Abunəçilər cədvəli
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            plan_name TEXT,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    # Admini həmişə aktiv saxla
    add_or_update_user(ADMIN_ID, "Admin", "Admin", "Sahibkar (Limitsiz)", days=3650)

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
    cur.execute("DELETE FROM seen_ads WHERE id NOT IN (SELECT id FROM seen_ads ORDER BY created_at DESC LIMIT 3000)")
    conn.commit()
    conn.close()

def add_or_update_user(user_id, username, full_name, plan_name, days=3):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    expires_at = datetime.now() + timedelta(days=days)
    cur.execute("""
        INSERT INTO users (user_id, username, full_name, plan_name, expires_at, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            plan_name=excluded.plan_name,
            expires_at=excluded.expires_at,
            is_active=1
    """, (user_id, username or "", full_name or "", plan_name, expires_at.strftime("%Y-%m-%d %H:%M:%S")))
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
    cur.execute("""
        SELECT user_id FROM users 
        WHERE is_active = 1 AND datetime(expires_at) > datetime('now')
    """)
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
    # Əgər şəkillə göndərilməsə mətn kimi göndər
    return send_telegram_msg(chat_id, caption)

# ==================== BROADCAST (BÜTÜN ABUNƏÇİLƏRƏ) ====================
def broadcast_ad(ad):
    subscribers = get_active_subscribers()
    if not subscribers:
        logger.warning("Aktiv abunəçi tapılmadı!")
        return

    caption = (
        f"⚡ <b>İLK BİZDƏ! YENİ SAHİBİNDƏN İPHONE!</b>\n\n"
        f"📱 <b>Model:</b> {ad['model']}\n"
        f"💰 <b>Qiymət:</b> {ad['price']}\n"
        f"👤 <b>Satıcı:</b> {ad['author']}\n\n"
        f"📝 <b>Məlumat:</b>\n<i>{ad['description']}</i>\n\n"
        f"🔗 <a href=\"{ad['url']}\">Elana Tap.az-da Baxmaq Üçün Toxun</a>"
    )

    logger.info(f"Elan {len(subscribers)} aktiv abunəçiyə göndərilir: {ad['title']}")
    for uid in subscribers:
        try:
            if ad.get("img_url"):
                send_telegram_photo(uid, ad["img_url"], caption)
            else:
                send_telegram_msg(uid, caption)
            time.sleep(0.05) # Telegram flood limitindən qorunma
        except Exception as e:
            logger.error(f"İstifadəçiyə ({uid}) göndərilə bilmədi: {e}")

# ==================== TELEGRAM BOT KOMANDALARI ====================
def handle_telegram_updates():
    """
    İstifadəçilərin /start və Adminin /admin, /qosh komandalarını qəbul edir.
    """
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

                # --- 1. /START KOMANDASI ---
                if text == "/start":
                    existing = get_user(user_id)
                    if not existing:
                        # 3 Günlük Pulsuz Sınaq Aktivləşdir
                        exp = add_or_update_user(user_id, username, full_name, "3 Günlük Pulsuz Sınaq", days=3)
                        welcome_text = (
                            f"👋 <b>Salam, {full_name or 'Dəyərli İstifadəçi'}!</b>\n\n"
                            f"🔥 <b>Tap.az iPhone Sahibindən Radar Botuna xoş gəlmisiniz!</b>\n\n"
                            f"🎁 <b>Sizə 3 GÜNLÜK PULSUZ SINAQ aktiv edildi!</b>\n"
                            f"📅 Bitmə vaxtı: <b>{exp.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
                            f"⚡ <i>Sayta yeni düşən iPhone 13, 14, 15 və 16 elanları dərhal şəxsi sahibindən olan kimi ilk sizə göndəriləcək!</i>"
                        )
                        send_telegram_msg(user_id, welcome_text)

                        # Adminə məlumat ver
                        admin_notice = (
                            f"🔔 <b>YENİ İSTİFADƏÇİ QOŞULDU!</b>\n"
                            f"👤 Ad: {full_name}\n"
                            f"🔗 Username: @{username}\n"
                            f"🆔 ID: <code>{user_id}</code>\n"
                            f"🎁 3 günlük sınaq başladı."
                        )
                        send_telegram_msg(ADMIN_ID, admin_notice)
                    else:
                        exp_str = existing[4]
                        try:
                            exp_dt = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
                            is_valid = exp_dt > datetime.now() and existing[5] == 1
                        except Exception:
                            is_valid = False

                        if is_valid:
                            status_text = (
                                f"✅ <b>Abunəliyiniz Aktivdir!</b>\n\n"
                                f"📦 Tarif: <b>{existing[3]}</b>\n"
                                f"📅 Bitmə tarixi: <b>{exp_str}</b>\n\n"
                                f"📱 <i>Elanlar avtomatik olaraq bura daxil olur.</i>"
                            )
                        else:
                            status_text = (
                                f"⚠️ <b>Abunəlik Müddətiniz Başa Çatıb!</b>\n\n"
                                f"Elanları almağa davam etmək üçün zəhmət olmasa admin ilə əlaqə saxlayın:\n"
                                f"👤 Admin: @acountnew1\n\n"
                                f"💳 <b>Tariflər:</b>\n"
                                f"• 1 Aylıq Abunə\n"
                                f"• 3 Aylıq Abunə\n"
                                f"• 1 İllik VIP Abunə"
                            )
                        send_telegram_msg(user_id, status_text)

                # --- 2. ADMIN KOMANDALARI (Yalnız ADMIN_ID üçün) ---
                elif user_id == ADMIN_ID:
                    # /admin menyusu
                    if text in ["/admin", "admin"]:
                        subs = get_active_subscribers()
                        admin_menu = (
                            f"👑 <b>ADMİN İDARƏETMƏ PANELİ</b>\n\n"
                            f"📊 <b>Aktiv Abunəçilər:</b> {len(subs)} nəfər\n\n"
                            f"🛠 <b>Əmrlər:</b>\n"
                            f"➕ <b>İstifadəçini abunə etmək:</b>\n"
                            f"<code>/qosh ID MÜDDƏT</code>\n"
                            f"<i>Nümunələr:</i>\n"
                            f"• <code>/qosh 123456789 3d</code> (3 günlük)\n"
                            f"• <code>/qosh 123456789 1m</code> (1 aylıq)\n"
                            f"• <code>/qosh 123456789 3m</code> (3 aylıq)\n"
                            f"• <code>/qosh 123456789 1y</code> (1 illik)\n"
                            f"• <code>/qosh 123456789 30</code> (30 gün)\n\n"
                            f"👥 <b>Siyahıya baxmaq:</b> /istifadeciler\n"
                            f"❌ <b>Abunəni ləğv etmək:</b> <code>/sil ID</code>\n"
                            f"📢 <b>Hamıya bildiriş:</b> <code>/duyuru Mətn</code>"
                        )
                        send_telegram_msg(ADMIN_ID, admin_menu)

                    # /qosh <user_id> <muddet>
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
                            elif duration_str.isdigit():
                                days = int(duration_str)
                                plan_title = f"{days} Günlük Abunə"

                            exp = add_or_update_user(target_id, "", "", plan_title, days=days)
                            send_telegram_msg(ADMIN_ID, f"✅ İstifadəçi <code>{target_id}</code> üçün <b>{plan_title}</b> aktiv edildi!\n📅 Bitmə: {exp.strftime('%d.%m.%Y %H:%M')}")
                            
                            # Həmin istifadəçiyə də təbrik mesajı göndər
                            send_telegram_msg(target_id, f"🎉 <b>Təbriklər!</b>\n\nSizin <b>{plan_title}</b> uğurla aktivləşdirildi!\n📅 Bitmə tarixi: <b>{exp.strftime('%d.%m.%Y %H:%M')}</b>\n\nElanlar gəlməyə davam edəcək!")
                        else:
                            send_telegram_msg(ADMIN_ID, "❌ Format: <code>/qosh ID 1m</code> və ya <code>/qosh ID 3m</code>")

                    # /sil <user_id>
                    elif text.startswith("/sil"):
                        parts = text.split()
                        if len(parts) >= 2:
                            target_id = int(parts[1])
                            remove_user(target_id)
                            send_telegram_msg(ADMIN_ID, f"❌ İstifadəçi <code>{target_id}</code> abunəlikdən çıxarıldı.")
                            send_telegram_msg(target_id, "⚠️ Sizin abunəliyiniz dayandırıldı.")

                    # /istifadeciler
                    elif text in ["/istifadeciler", "/users"]:
                        all_users = get_all_users_list()
                        if not all_users:
                            send_telegram_msg(ADMIN_ID, "Heç bir istifadəçi tapılmadı.")
                        else:
                            resp_text = f"👥 <b>BÜTÜN İSTİFADƏÇİLƏR ({len(all_users)} nəfər):</b>\n\n"
                            for u in all_users[:25]:
                                u_id, u_name, f_name, p_name, exp, is_act = u
                                status_emoji = "🟢" if is_act and exp > datetime.now().strftime("%Y-%m-%d %H:%M:%S") else "🔴"
                                resp_text += f"{status_emoji} <b>{f_name or u_name}</b> (ID: <code>{u_id}</code>)\n   📦 {p_name} | ⏳ {exp}\n\n"
                            send_telegram_msg(ADMIN_ID, resp_text)

                    # /duyuru <mesaj>
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

def get_ad_details(page, ad_url):
    try:
        page.goto(ad_url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(1500)
        soup = BeautifulSoup(page.content(), "html.parser")

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

def check_tapaz_ads(page):
    logger.info("Tap.az yoxlanılır...")
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
            logger.info(f"Uyğun model tapıldı: {matched_model} - {title} ({price}) | Sahib yoxlanılır...")

            details = get_ad_details(page, full_url)
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

            # Bütün aktiv abunəçilərə dərhal göndər
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
    logger.info("Tap.az iPhone SaaS Bot Başladı...")

    # 1. Render üçün Health Server
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # 2. Telegram Komanda və Abunəlik Dinləyicisi
    updates_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
    updates_thread.start()

    # 3. Playwright Skraper
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

        while True:
            try:
                check_tapaz_ads(page)
            except Exception as e:
                logger.error(f"Əsas dövr xətası: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
