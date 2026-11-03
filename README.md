# Tap.az iPhone Sahibindən İzləmə Botu

Bu bot Tap.az saytında yeni yerləşdirilən iPhone elanlarını 7/24 izləyir və **ancaq şəxsi sahiblər tərəfindən** paylaşılan elanları dərhal Telegram-a göndərir.

## 🎯 Dəstəklənən Modellər
- iPhone 13
- iPhone 13 Pro
- iPhone 14
- iPhone 14 Pro
- iPhone 15
- iPhone 15 Pro
- iPhone 16 (Yalnız sadə model)

*(Pro Max, Plus, Mini və köhnə modellər avtomatik filtirlənir)*

## 🛡️ Anti-Mağaza və Anti-Alverçi Filtrləri
- "Mağaza" nişanı olan bütün elanlar bloklanır.
- İş saatları olan və ya 4-dən çox elanı olan hesablar alverçi kimi tanınaraq rədd edilir.
- Qara siyahı: Kredit, birkart, tamkart, zəmanət, çatdırılma, topdan, 28 may, filial və s. kimi açar sözləri olan elanlar Telegram-a göndərilmir.

## 🚀 Quraşdırma və İşə Salma
1. Asılılıqları quraşdırın:
```bash
pip install -r requirements.txt
```

2. `.env` faylını tənzimləyin:
```env
TELEGRAM_BOT_TOKEN=sizin_bot_token
TELEGRAM_CHAT_ID=sizin_chat_id
CHECK_INTERVAL=30
PORT=10000
```

3. İcra edin:
```bash
python bot.py
```

