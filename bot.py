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

# Sorğular üçün Header-lər
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "az,tr,en-US,en;q=0.9,ru;q=0.8",
    "Referer": "https://tap.az/",
}

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
    # Bazanı təmiz saxla (maksimum son 3000 elan)
    cur.execute("DELETE FROM seen_ads WHERE id NOT IN (SELECT id FROM seen_ads ORDER BY created_at DESC LIMIT 3000)")
    conn.commit()
    conn.close()

def match_requested_model(title):
    """
    Yalnız tələb olunan modelləri seçir:
    - iPhone 13, iPhone 13 Pro
    - iPhone 14, iPhone 14 Pro
    - iPhone 15, iPhone 15 Pro
    - iPhone 16 (Sadə)
    
    Qadağandır: Pro Max, Plus, Mini, 11, 12, 16 Pro, 16 Pro Max, XS və s.
    """
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

def get_ad_details(ad_url):
    """
    Elanın daxili səhifəsini yoxlayır:
    - Satıcı tipi (Mağazadırsa və ya iş saatları varsa rədd edir)
    - Təsvir mətni (Açar söz qara siyahısı)
    - Şəkil URL-i
    """
    try:
        resp = requests.get(ad_url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # 1. İş saatları varmı? (Mağazalarda olur)
        schedule = soup.find(attrs={"data-testid": "author-info-working-schedule"})
        if schedule:
            logger.info(f"Rədd edildi (İş saatları aşkarlandı - Mağazadır): {ad_url}")
            return None

        # 2. Satıcı adı
        author_elem = soup.find(attrs={"data-testid": "author-info-name"})
        author_name = author_elem.text.strip() if author_elem else "Sahibi"
        if contains_blacklist(author_name):
            logger.info(f"Rədd edildi (Satıcı adında mağaza sözü): {author_name}")
            return None

        # 3. Satıcının elan sayı (Məsələn: '45 elan' - çoxdursa alverçidir)
        author_link = soup.find(attrs={"data-testid": "author-info-link"})
        if author_link:
            count_match = re.search(r'(\d+)\s*elan', author_link.text.lower())
            if count_match:
                elan_count = int(count_match.group(1))
                if elan_count > 4: # 4-dən çox elanı olan şəxs alverçi/mağaza sayılır
                    logger.info(f"Rədd edildi (Satıcının {elan_count} elanı var - Alverçidir): {ad_url}")
                    return None

        # 4. Təsvir mətni (Description)
        desc_elem = soup.find("div", class_=lambda c: c and "DescriptionContainer" in c)
        description = desc_elem.text.strip() if desc_elem else ""

        # Mətndə qara siyahı sözləri varmı?
        if contains_blacklist(description):
            logger.info(f"Rədd edildi (Təsvirdə qara siyahı sözü tapıldı): {ad_url}")
            return None

        # 5. Əsas şəkil
        img_elem = soup.find("img", class_=lambda c: c and "SliderImage" in c) or soup.find("div", class_=lambda c: c and "SliderWrapper" in c)
        img_url = None
        if img_elem:
            img_tag = img_elem if img_elem.name == "img" else img_elem.find("img")
            if img_tag and img_tag.get("src"):
                img_url = img_tag["src"]

        # 6. Yer / Region
        props_elem = soup.find("div", class_=lambda c: c and "PropertiesBox" in c)
        props_text = props_elem.text.strip() if props_elem else ""

        return {
            "author": author_name,
            "description": description[:600],
            "img_url": img_url,
            "props": props_text
        }
    except Exception as e:
        logger.error(f"Elan detalları alınarkən xəta: {e}")
        return None

def send_telegram_alert(ad):
    """
    Telegram-a gözəl formatda elan göndərir.
    """
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
        # Əgər şəkil varsa sendPhoto ilə göndər
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

        # Şəkil alınmadısa və ya xəta verdisə adi mesaj göndər
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

def check_tapaz_ads():
    """
    Tap.az son iPhone elanlarını skan edir.
    """
    logger.info("Tap.az yoxlanılır...")
    try:
        resp = requests.get(TAPAZ_APPLE_URL, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Tap.az status kodu: {resp.status_code}")
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Son elanlar bölməsini tapırıq
        latest_section = soup.find(attrs={"data-testid": "latest-ads-section"})
        cards = latest_section.find_all(attrs={"data-testid": "ad-card"}) if latest_section else soup.find_all(attrs={"data-testid": "ad-card"})

        logger.info(f"Səhifədə {len(cards)} ədəd kart tapıldı.")

        # Ən köhnədən ən yeniyə ardıcıllıqla göndərmək üçün tərsinə çeviririk
        for card in reversed(cards):
            # 1. 'Mağaza' teqi kartın özündə varsa dərhal RƏDD ET
            if card.find(string=lambda t: t and "Mağaza" in t):
                continue

            href = card.get("href")
            if not href:
                continue

            # Elan ID-sini çıxar
            ad_id_match = re.search(r'/(\d+)(?:\?|$)', href)
            if not ad_id_match:
                continue
            ad_id = ad_id_match.group(1)

            # Əvvəl görülən elandırsa keç
            if is_ad_seen(ad_id):
                continue

            # Başlıq və Qiymət
            title_elem = card.find(attrs={"data-testid": "ad-card-title"})
            title = title_elem.text.strip() if title_elem else ""

            # Model filtri
            matched_model = match_requested_model(title)
            if not matched_model:
                mark_ad_seen(ad_id, "UNMATCHED", title)
                continue

            price_elem = card.find(attrs={"data-testid": "ad-card-price"})
            price = price_elem.text.strip() if price_elem else "Göstərilməyib"

            full_url = urljoin(BASE_URL, href)
            logger.info(f"Uyğun model tapıldı: {matched_model} - {title} ({price}) | Yoxlanılır...")

            # Ətraflı məlumatları və daxili filtrləri yoxla
            details = get_ad_details(full_url)
            if not details:
                # Alverçi və ya mağaza olduğu üçün və ya xəta səbəbilə ləğv edildi
                mark_ad_seen(ad_id, matched_model, title, price)
                continue

            # Hər şey təmizdir — Telegram-a göndər!
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
            
            # Sorğular arası qısa fasilə
            time.sleep(2)

    except Exception as e:
        logger.error(f"Skanlama zamanı xəta: {e}")

# Render Web Service üçün sadə HTTP Server (7/24 oyaq qalması üçün)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Tap.az iPhone Bot 7/24 Aktivdir!")

    def log_message(self, format, *args):
        # Konsolu doldurmamaq üçün adi sorğu loqlarını gizlət
        return

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logger.info(f"Sağlamlıq veb serveri {PORT} portunda işə salındı.")
    server.serve_forever()

def main():
    init_db()
    logger.info("Tap.az iPhone İzləmə Botu Başladı...")

    # Render üçün arxa planda HTTP server işə salırıq
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Əsas skan dövriyyəsi
    while True:
        try:
            check_tapaz_ads()
        except Exception as e:
            logger.error(f"Əsas dövr xətası: {e}")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()

