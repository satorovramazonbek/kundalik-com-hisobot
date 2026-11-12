# Kundalik.com (emaktab.uz) choraklik hisobot boti

Telegram bot: o'qituvchi login/paroli, sinf va chorakni oladi, emaktab.uz (sobiq kundalik.com) ga kirib
choraklik (yakuniy) baholar hisobotini Excel (.xlsx) qilib yuboradi.

## O'rnatish
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env   # BOT_TOKEN ni yozing
.venv/bin/python bot.py
```

## Foydalanish
Botda `/start` → login → parol → sinf (masalan `5-A`) → chorak. `/bekor` — bekor qilish.

## Sozlash (.env)
- `ALLOWED_USERS` — ruxsat etilgan Telegram ID lar (vergul bilan)
- `HEADLESS=0` — brauzerni ko'rinadigan qilib ishga tushirish
- `SAVE_DEBUG=1` — har qadamda `debug/` ga skrinshot va HTML saqlash
- `KUNDALIK_REPORT_URL` — hisobot sahifasining aniq manzili (avtomatik topilmasa)

Terminaldan sinash: `.venv/bin/python scraper.py LOGIN PAROL 5-A 2`

## Buyruqlar
- `/start` — boshlash (saqlangan seans bo'lsa, login so'ramaydi)
- `/bekor` — joriy amalni bekor qilish
- `/chiqish` — tizimdan chiqish va saqlangan seansni o'chirish
- `/jurnal` — kirishlar jurnali (faqat `ADMIN_IDS` dagi adminlar uchun)

## Ma'lumotlar (`data/`, gitga tushmaydi)
- `data/jurnal.jsonl` — kim (Telegram), qachon, qaysi eMaktab profiliga kirgani va qanday hisobot olgani. Parol yozilmaydi.
- `data/sessions/<telegram_id>.json` — saqlangan seans (cookie), `COOKIE_TTL` muddatgacha. Fayl huquqi 600.
