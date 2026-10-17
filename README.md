# Gramma v3 🤖

<div dir="rtl">

**ربات فوق‌حرفه‌ای مدیریت هم‌زمان پیج‌های اینستاگرام از تلگرام** — با پنل وب/مینی‌اپ اختصاصی، تقویم شمسی، پست کلبریشن و دستیار هوش مصنوعی.

Gramma v3 یک پلتفرم چندکاربره (Multi-Tenant) است: تعامل و پاسخ‌دهی با ChatbotX یا اتصال متا انجام می‌شود، و انتشار محتوا فقط برای پیج‌های مجازِ متصل از مسیر Zernio صورت می‌گیرد. بات و مینی‌اپ وب، تقویم و ابزارهای مدیریت پیج را فراهم می‌کنند. داده‌های هر کاربر **کاملاً ایزوله** و توکن‌ها **رمزنگاری‌شده** هستند.

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

سه سقف توسعه (محیط‌های dev) — همه در `app/services/limits.py` متمرکز و
هم ربات و هم مینی‌اپ از آن تبعیت می‌کنند:

| متغیر محیطی | مقدار | معنی |
|---|---|---|
| `MAX_TOTAL_USERS` | ۲۴ | حداکثر ۲۴ کاربر می‌توانند به ربات وصل شوند |
| `MAX_TOTAL_INSTAGRAM_ACCOUNTS` | ۲۴ | حداکثر ۲۴ پیج اینستاگرام در کل |
| `MAX_ACCOUNTS_PER_USER` | ۳ | هر کاربر حداکثر ۳ پیج |

- سقف کاربران در `/start` (بات) و `/api/me` (مینی‌اپ) اعمال می‌شود.
- سقف پیج‌ها در زمان اتصال (`/connect`) اعمال می‌شود.

---

## 🧱 معماری

```
          ┌─────────────────────────────────────────┐
          │        تلگرام (بات + Telegram WebApp)    │
          └───────────────┬─────────────────────────┘
                          │ وب‌هوک اصلی / polling پشتیبان
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
    │  Supabase PostgreSQL (اصلی) / PostgreSQL Docker (محلی) │
    │  توکن‌ها AES-256 + key_version         │
    └────────────────────────────┘

   زمان‌بند: Celery+Redis (تولید) | APScheduler (توسعه)
   مینی‌اپ:  React+Vite → سرو روی Vercel و/یا Docker بک‌اند
```

**تکنولوژی:**
- **بک‌اند:** Python + Aiogram v3 + FastAPI + SQLAlchemy async
- **دیتابیس:** Supabase PostgreSQL (اصلی)؛ PostgreSQL محلی در Docker + Redis/Celery (پشتیبان)
- **مینی‌اپ:** React + Vite + JavaScript/JSX + CSS مدرن (بدون CDN)
- **استقرار:** Docker Compose (بک‌اند) + Vercel (مینی‌اپ)

## 📤 انتشار فقط با Zernio

- `app/services/zernio_publishing.py` تنها اتصال انتشار است: پیش‌نویس، زمان‌بندی
  در Zernio و درخواست انتشار فوری واقعی. ChatbotX/Meta صرفاً مسیر پیام‌رسانی
  و آمار هستند، نه انتشار رسانه.
- در مینی‌اپ، فرم Zernio فقط برای مدیر و فقط برای پیش‌نویس/زمان‌بندی در دسترس است؛
  `app/services/publisher.py` نیز پست‌های زمان‌بندی‌شده داخلی را منحصراً از طریق
  Zernio می‌فرستد و رسانهٔ عمومی HTTPS می‌خواهد. Mock و API خصوصی حذف شده‌اند.
- درخواست دستی یک‌باره برای بررسی انتشار واقعی: `POST /api/test-publish-real`،
  با هدر `x-test-secret` برابر مقدار خصوصی `TEST_SECRET` و بدون بدنه. این مسیر
  فقط برای پیج متصلِ متعلق به مدیر اجرا می‌شود و `test_key_002` را پیش از ارسال
  به صورت یکتا در دیتابیس ثبت می‌کند. **هیچ درخواست خودکاری به این مسیر زده
  نمی‌شود؛ تکرار POST ممکن است پست واقعی تکراری ایجاد کند و مسدود است.**
- شش جدول جدید شامل `zernio_chat_history` بدون دستکاری `chat_history` موجود،
  RLS پیش‌فرضِ بسته و توابع صف/نرخ در `scripts/migrate_zernio_queues.sql` تعریف
  شده‌اند. برای Supabase مجاز، اسکریپت باید در یک تراکنش اجرا شود.

## 🤖 دستیار محتوا و پیام‌رسانی

کپشن، ایده، تحلیل و پیشنهاد پاسخ از AvalAI (OpenAI-compatible) با فال‌بک
الگویی تولید می‌شوند. جستجوی وب مستقیماً از Tavily انجام می‌شود. وب‌هوک
Telegram روی Vercel مسیر اصلی است و polling فقط پشتیبان محلی باقی می‌ماند.

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
- دکمه WebApp یک **بلیط HMAC** (`_token`) امضا‌شده با `ENCRYPTION_KEY` می‌سازد (۲۴ ساعت اعتبار).
- وقتی URL بدون بلیط باز شود، مینی‌اپ `initData` تلگرام را می‌گیرد و به `POST /api/auth/verify` می‌فرستد؛ سرور **HMAC-SHA256** را با توکن ربات اعتبارسنجی می‌کند (مطابق راهنمای رسمی تلگرام) و فقط در صورت معتبر بودن، بلیط می‌سازد. `initDataUnsafe.user.id` هرگز بدون چک hash قابل اعتماد نیست.
- باز شدن مستقیم در مرورگر یا از ربات دیگر → پیام «دسترسی ممکن نیست» و **هیچ داده‌ای نمایش داده نمی‌شود**.
- بلیط نشست فقط در `sessionStorage` نگه‌داری می‌شود (نه localStorage نه کوکی) و به‌صورت هدر `X-Mini-App-Hash` در هر درخواست ارسال می‌شود.
- CORS فقط برای دامنه `MINIAPP_PUBLIC_URL` / `PUBLIC_BASE_URL` باز است.

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
| `MAX_ACCOUNTS_PER_USER`=3 / `MAX_TOTAL_INSTAGRAM_ACCOUNTS`=24 / `MAX_TOTAL_USERS`=24 | محدودیت‌های پلتفرم (متمرکز در `app/services/limits.py`) |
| `MINIAPP_PUBLIC_URL` | آدرس عمومی مینی‌اپ (Vercel) برای ساخت URL دکمه WebApp |
| `AVALAI_API_KEY` / `AVALAI_MODEL` | تولید کپشن (مسیر مستقیم / فال‌بک) |
| `TAVILY_API_KEY` | جستجوی مستقیم وب |
| `ZERNIO_API_KEY` | کلید خصوصی تنها ارائه‌دهندهٔ انتشار محتوا |
| `TEST_SECRET` | کلید خصوصی هدر `x-test-secret` برای POST یک‌بارهٔ انتشار واقعی |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | دیتابیس/Storage اصلی (محرمانه) |
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
