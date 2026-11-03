import os
import re
import time
import logging
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Ətraf mühit dəyişənlərini yüklə
load_dotenv()

# Loq konfiqurasiyası
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TapAzBot")

# Telegram məlumatları
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8873977210:AAGayQLy11TeRvM-42ZTd_H254FbywjBtB4")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7998954235")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30")) # saniyə
PORT = int(os.getenv("PORT", "10000")) # Render üçün port

# Tap.az Apple telefonlar kateqoriyası
TAPAZ_APPLE_URL = "https://tap.az/elanlar/elektronika/telefonlar?p%5B749%5D=3855"
BASE_URL = "https://tap.az"

# Qara siyahı — Mağaza və Alverçi sözləri
BLACKLIST_KEYWORDS = [
    # Mağaza & Şirkət sözləri
    "mağaza", "magaza", "store", "shop", "şirkət", "sirket", "şəbəkə", "sebeke",
    "ünvan:", "unvan:", "ünvanımız", "unvanimiz", "ünvan ", "unvan ",
    "filial", "metro çıxışı", "metro cixisi", "28 may", "nəsimi", "nesimi", 
    "af mall", "mall", "tarqovı", "tarqovu", "sederek", "sədərək", "bazarstore",

    # Kredit & Ödəniş sistemləri (yalnız mağazalar təklif edir)
    "kredit", "birkart", "bir kart", "tamkart", "tam kart", "bolkart", "bol kart",
    "albalı", "albali", "faizsiz", "taksit", "kreditlə", "kreditle", "aylıq", "ayliq",

    # Çatdırılma & Servis
    "çatdırılma", "catdirilma", "kuryer", "qeydiyyat daxil", "qeydiyyatli", "qeydiyyatlı",

    # Zəmanət (Mağaza zəmanəti)
    "rəsmi zəmanət", "resmi zemanet", "yazılı zəmanət", "yazili zemanet",
    "1 il zəmanət", "1 il zemanet", "12 ay zəmanət", "14 gün yoxlama", "14 gun yoxlama",
    "mağaza tərəfindən", "magaza terefinden", "servis zəmanəti",

    # Hədiyyələr (Mağaza marketinqi)
    "orijinal adaptor hədiyyə", "adaptor hediyye", "qoruyucu şüşə hədiyyə",
    "suse hediyye", "kabro hədiyyə", "üzlük hədiyyə", "hediyye", "hədiyyə",

    # Topdan & Sayla satış
    "topdan", "topdansatış", "topdansatis", "bütün rənglər var", "butun rengler var",
    "say var", "say çoxdur", "say coxdur", "bağlı qutu", "bagli qutu",
    "açılmamış qutu", "acilmamis qutu", "qutuda yeni"
]

# Database idarəetməsi
DB_FILE = "seen_ads.db"

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
    conn.commit()
    conn.close()

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

def match_requested_model(title):
    t = title.lower()

    if "iphone" not in t:
        return None

    # Pro Max, Plus, Mini istisna olunur
    if "pro max" in t or "promax" in t:
        return None
    if "plus" in t:
        return None
    if "mini" in t:
        return None

    # iPhone 16 — YALNIZ sadə model (Pro ola bilməz)
    if re.search(r'\b16\b', t):
        if "pro" in t:
            return None
        return "iPhone 16"

    # iPhone 15 və iPhone 15 Pro
    if re.search(r'\b15\s*pro\b', t):
        return "iPhone 15 Pro"
    if re.search(r'\b15\b', t):
        if "pro" not in t:
            return "iPhone 15"

    # iPhone 14 və iPhone 14 Pro
    if re.search(r'\b14\s*pro\b', t):
        return "iPhone 14 Pro"
    if re.search(r'\b14\b', t):
        if "pro" not in t:
            return "iPhone 14"

    # iPhone 13 və iPhone 13 Pro
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
        schedule = soup.find(attrs={"data-testid": "author-info-working-schedule"})
        if schedule:
            logger.info(f"Rədd edildi (İş saatları - Mağazadır): {ad_url}")
            return None

        # 2. Satıcı adı
        author_elem = soup.find(attrs={"data-testid": "author-info-name"})
        author_name = author_elem.text.strip() if author_elem else "Sahibi"
        if contains_blacklist(author_name):
            logger.info(f"Rədd edildi (Satıcı adında mağaza sözü): {author_name}")
            return None

        # 3. Satıcının elan sayı (> 4 olarsa alverçidir)
        author_link = soup.find(attrs={"data-testid": "author-info-link"})
        if author_link:
            count_match = re.search(r'(\d+)\s*elan', author_link.text.lower())
            if count_match:
                elan_count = int(count_match.group(1))
                if elan_count > 4:
                    logger.info(f"Rədd edildi (Satıcının {elan_count} elanı var - Alverçi): {ad_url}")
                    return None

        # 4. Təsvir mətni
        desc_elem = soup.find("div", class_=lambda c: c and "DescriptionContainer" in c)
        description = desc_elem.text.strip() if desc_elem else ""

        if contains_blacklist(description):
            logger.info(f"Rədd edildi (Qara siyahı sözü tapıldı): {ad_url}")
            return None

        # 5. Əsas şəkil
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

def send_telegram_alert(ad):
    caption = (
        f"📱 <b>YENİ SAHİBİNDƏN İPHONE ELANI!</b>\n\n"
        f"🔹 <b>Model:</b> {ad['model']}\n"
        f"💰 <b>Qiymət:</b> {ad['price']}\n"
        f"👤 <b>Satıcı:</b> {ad['author']}\n\n"
        f"📝 <b>Məlumat:</b>\n<i>{ad['description']}</i>\n\n"
        f"🔗 <a href=\"{ad['url']}\">Elana Tap.az-da Baxmaq Üçün Toxun</a>"
    )

    url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        if ad.get("img_url"):
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": ad["img_url"],
                "caption": caption,
                "parse_mode": "HTML"
            }
            res = requests.post(url_photo, json=data, timeout=15).json()
            if res.get("ok"):
                logger.info(f"Telegram-a göndərildi (Foto ilə): {ad['title']}")
                return True

        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        res = requests.post(url_msg, json=data, timeout=15).json()
        if res.get("ok"):
            logger.info(f"Telegram-a göndərildi (Mətn): {ad['title']}")
            return True
        else:
            logger.error(f"Telegram xətası: {res}")
            return False
    except Exception as e:
        logger.error(f"Telegram göndərmə xətası: {e}")
        return False

def check_tapaz_ads(page):
    logger.info("Tap.az yoxlanılır (Playwright ilə)...")
    try:
        page.goto(TAPAZ_APPLE_URL, wait_until="domcontentloaded", timeout=35000)
        # Cloudflare yoxlaması olarsa avtomatik həll olunmasını gözləyirik
        page.wait_for_timeout(4000)

        soup = BeautifulSoup(page.content(), "html.parser")
        
        latest_section = soup.find(attrs={"data-testid": "latest-ads-section"})
        cards = latest_section.find_all(attrs={"data-testid": "ad-card"}) if latest_section else soup.find_all(attrs={"data-testid": "ad-card"})

        logger.info(f"Səhifədə {len(cards)} ədəd kart tapıldı.")

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

            if send_telegram_alert(ad_data):
                mark_ad_seen(ad_id, matched_model, title, price)
            
            time.sleep(2)

    except Exception as e:
        logger.error(f"Skanlama zamanı xəta: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Tap.az iPhone Bot 7/24 Aktivdir!")

    def log_message(self, format, *args):
        return

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logger.info(f"Sağlamlıq veb serveri {PORT} portunda işə salındı.")
    server.serve_forever()

def main():
    init_db()
    logger.info("Tap.az iPhone İzləmə Botu Başladı...")

    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

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
