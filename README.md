# Gramma v3 🤖

<div dir="rtl">

**ربات فوق‌حرفه‌ای مدیریت هم‌زمان پیج‌های اینستاگرام از تلگرام** — با پنل وب/مینی‌اپ اختصاصی، تقویم شمسی، پست کلبریشن و دستیار هوش مصنوعی.

Gramma v3 یک پلتفرم چندکاربره (Multi-Tenant) است که به کاربران (صاحبان پیج‌ها) اجازه می‌دهد پیج اینستاگرام Business/Creator خود را با اتصال امن OAuth 2.0 وصل کنند و از داخل تلگرام و یک **مینی‌اپ وب اختصاصی** محتوا منتشر کنند، با مخاطبان تعامل داشته باشند و سلامت پیج را رصد کنند. داده‌های هر کاربر **کاملاً ایزوله** و توکن‌ها **رمزنگاری‌شده** هستند.

</div>

---

## ✨ قابلیت‌های v3

| دسته | قابلیت‌ها |
|---|---|
| 📤 انتشار | عکس، ویدیو، ریلز، کاروسل، استوری + **زمان‌بندی** + انتخاب از چند پیج |
| 📅 تقویم محتوا | نمای **شمسی (جلالی)** پست‌های زمان‌بندی‌شده |
| 🤝 کلبریشن | درخواست → اعلان به شریک → تایید → **کپی مشترک پست** |
| 💬 کامیونیتی | کامنت‌ها، پاسخ، مخفی/حذف، **Private Reply** |
| 📥 دایرکت | صندوق پیام، دسته‌بندی هوشمند، **Auto-Reply کلیدواژه‌ای + هوش مصنوعی** |
| 🔍 جستجوی معنایی | نرمال‌سازی فارسی + stemming در کامنت‌ها/دایرکت‌ها |
| 🌐 جستجوی وب | **Tavily** برای الهام محتوایی |
| 🤖 دستیار AI | تولید کپشن فارسی/انگلیسی با **AvalAI** (Fallback بدون هزینه) |
| 🧩 الگوهای پست | کتابخانه قالب + قالب‌های اختصاصی کاربر |
| 📄 گزارش PDF | خروجی گزارش سلامت پیج |
| 🔔 نوتیفیکیشن هوشمند | فقط هشدارهای مهم (ضد اسپم) |
| 🖥 مینی‌اپ | داشبورد نموداری، لیست پیج‌ها، تقویم، کلبریشن، تنظیمات + **حالت تاریک/روشن** |
| 🛡 امنیت | AES-256، انزوای داده، PKCE ضد CSRF، رفرش خودکار توکن، Audit Log، بکاپ خودکار |

### محدودیت‌های سخت‌گیرانه

- هر کاربر تلگرام: **حداکثر ۳ پیج**
- کل ربات (حالت توسعه): **۲۴ پیج هم‌زمان** (با افزودن ۲۴ کاربر به‌عنوان Tester)
- متمرکز در `app/services/limits.py` — هم ربات و هم مینی‌اپ از آن تبعیت می‌کنند.

---

## 🧱 معماری

```
          ┌─────────────────────────────────────────┐
          │        تلگرام (بات + Telegram WebApp)    │
          └───────────────┬─────────────────────────┘
                          │ long-polling
           ┌──────────────▼──────────────┐
           │  AIOGRAM BOT (app/bot)      │
           │  handlers + FSM + middleware│
           └──────┬───────────────┬──────┘
                  │               │
    ┌─────────────▼───────┐  ┌────▼─────────────────────────────┐
    │  SERVICES (domain)  │  │  UNIFIED API (FastAPI :8000)      │
    │  publisher/calendar │  │  /api/*        → Mini-App backend  │
    │  collab/search/ai   │  │  /webhook/instagram[/callback]     │
    │  autoreply/templates│  │  /ws/broadcast → live refresh      │
    │  web_search/backup  │  │  (SPA also served from Docker)     │
    └─────────────┬───────┘  └─────────────────────────────────┘
                  │
    ┌─────────────▼──────────────┐
    │  REPOSITORIES (tenant)     │
    └─────────────┬──────────────┘
                  │
    ┌─────────────▼──────────────┐
    │  PostgreSQL 16 (Docker) / SQLite (dev) │
    │  توکن‌ها AES-256 + key_version         │
    └────────────────────────────┘

   زمان‌بند: Celery+Redis (تولید) | APScheduler (توسعه)
   مینی‌اپ:  React+Vite → سرو روی Vercel و/یا Docker بک‌اند
```

**تکنولوژی:**
- **بک‌اند:** Python + Aiogram v3 + FastAPI + SQLAlchemy async
- **دیتابیس:** PostgreSQL 16 + Redis (Celery) — **بدون Supabase**
- **مینی‌اپ:** React + Vite + JavaScript/JSX + CSS مدرن (بدون CDN)
- **استقرار:** Docker Compose (بک‌اند) + Vercel (مینی‌اپ)

---

## 🚀 اجرای محلی

```bash
cd gramma-bot

# 1) وابستگی‌ها
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) تنظیمات
cp .env.example .env
#   TELEGRAM_BOT_TOKEN  را از @BotFather بگذارید
python scripts/generate_cipher_key.py   # ENCRYPTION_KEY تولید کنید

# 3) اجرا (بات + زمان‌بند + API مینی‌اپ روی پورت 8000)
python run.py
```

> `INSTAGRAM_ACCOUNT_MODE=simulation` یعنی بدون اپ متا هم همه‌چیز قابل تست است.

### تست‌ها

```bash
pytest tests/ -q                  # ۱۹ تست واحد
python scripts/smoke_test.py      # تست خط لوله اصلی
```

---

## 🖥 مینی‌اپ

### توسعه
```bash
cd miniapp
npm install
npm run dev            # Vite با پراکسی /api → localhost:8000
```
بک‌اند هم‌زمان اجرا باشد. برای تست بدون تلگرام، یک بلیط بسازید:
```bash
python -c "from app.webapp.auth import make_miniapp_ticket; print(make_miniapp_ticket(YOUR_ID))"
# http://localhost:5173/?_token=<خروجی>
```

### بیلد
```bash
cd miniapp
npm run build:backend      # خروجی → app/webapp/static (برای Docker)
npm run build              # خروجی → dist (برای Vercel)
```

روی Vercel، متغیر `VITE_API_BASE` آدرس بک‌اند عمومی (https://your-backend) را مشخص می‌کند؛ مینی‌اپ از همان origin بک‌اند هم قابل سرو است (Docker).

### امنیت مینی‌اپ
- ورود با **بلیط HMAC** (`_token`) که سرور می‌سازد (۲۴ ساعت اعتبار، امضاشده).
- هر درخواست با هدر `X-Mini-App-Hash` امضا می‌شود؛ کاربر از همان استخراج و همه کوئری‌ها scope می‌شوند.
- `initData تلگرام` فقط نمایشی است؛ بلیط ما منبع اصلی اعتماد است.

---

## 🐳 Docker (تولید)

```bash
# .env تولیدی + سپس:
docker compose up -d --build
```

| سرویس | نقش |
|---|---|
| `bot` | بات + API مینی‌اپ + وبهوک/OAuth (پورت 8000) |
| `worker` | Celery worker (انتشار، گزارش، رفرش توکن، بکاپ) |
| `beat` | Celery beat |
| `db` | PostgreSQL 16 |
| `redis` | Redis 7 |

---

## 🔑 متغیرهای کلیدی `.env`

| متغیر | توضیح |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `ADMIN_TELEGRAM_IDS` | توکن ربات + ادمین‌ها |
| `ENCRYPTION_KEY` | کلید AES-256 (الزامی + بکاپ) |
| `DATABASE_URL` / `REDIS_URL` | sqlite یا postgresql+asyncpg |
| `INSTAGRAM_ACCOUNT_MODE` | simulation / production |
| `META_APP_ID` / `META_APP_SECRET` / `META_VERIFY_TOKEN` / `WEBHOOK_BASE_URL` | مشخصات متا |
| `MAX_ACCOUNTS_PER_USER`=3 / `MAX_TOTAL_ACCOUNTS_DEV`=24 | محدودیت‌ها |
| `AVALAI_API_KEY` / `AVALAI_MODEL` | تولید کپشن |
| `TAVILY_API_KEY` | جستجوی وب |
| `AUTO_BACKUP_ENABLED` / `AUTO_BACKUP_HOUR` / `BACKUP_DIR` | بکاپ شبانه |
| `SEMANTIC_SEARCH_ENABLED` / `SMART_NOTIFICATIONS_ENABLED` | پرچم‌های v3 |
| `S3_*` | CDN رسانه (اختیاری) |

---

## 📖 مستندات

- 📘 [راهنمای گام‌به‌گام Meta و Instagram Graph API](docs/META_SETUP_FA.md)
- 👥 [افزودن کاربران Tester به اپ متا (تا ۲۴ کاربر)](docs/META_TESTERS_FA.md)

---

## 🗂 ساختار

```
gramma-bot/
├── app/
│   ├── bot/                 # handlers (account, publish, schedule, calendar,
│   │                        #  collab, community, direct, search, insights,
│   │                        #  ai, studio, navigation)
│   ├── core/                # config, database, logging, security(crypto/oauth)
│   ├── db/                  # repositories (tenant-scoped)
│   ├── models/              # schema (User, InstagramAccount, Post, Media, …)
│   ├── services/            # limits, publisher, calendar, collab, search,
│   │                        #  autoreply, templates, web_search, pdf_report,
│   │                        #  smart_notifications, ai, token_refresh, backup, …
│   ├── tasks/               # Celery
│   ├── webapp/              # Mini-App API + auth + static(SPA)
│   ├── webhook/             # Instagram OAuth + webhook
│   └── workers/             # APScheduler
├── miniapp/                 # React + Vite (deployable on Vercel)
├── scripts/                 # generate_cipher_key, smoke_test, webhook subscription
├── tests/                   # pytest (19 test)
├── docs/                    # Meta guides (FA)
├── Dockerfile / docker-compose.yml / vercel.json
├── run.py / requirements.txt / .env.example
└── README.md
```

---

## 🔒 امنیت (خلاصه)

1. توکن‌های اینستاگرام فقط `enc:<base64>` (AES-256/Fernet).
2. انزوای داده با طراحی (repository از `user_id` شروع می‌کند).
3. OAuth با PKCE + state امضاشده ضد CSRF.
4. Audit Log کامل + رفرش خودکار توکن (حاشیه ۴۸ ساعت).
5. بکاپ خودکار شبانه با چرخش ۱۴ نسخه.
6. اپ متا در **Development Mode** — فقط Testerها دسترسی دارند.

---

<div dir="rtl">
ساخته‌شده با ❤️ — نسخه 3.0.0
</div>
