# راه‌اندازی و یکپارچه‌سازی OpenClaw با گراما

این مستند نحوه‌ی اتصال **OpenClaw** به عنوان *مغز هوش مصنوعی محلی* پشت ربات
گراما را توضیح می‌دهد. ربات گراما و OpenClaw روی **یک سرور** اجرا می‌شوند و
کاربر نهایی فقط **یک ربات** می‌بیند (گراما).

## ۱) معماری و دلیل آن

```
┌─────────────────────────────┐        HTTP (loopback)         ┌──────────────────────────┐
│  Gramma Bot (Aiogram)       │ ─── POST {base}/api/ask ────▶  │  OpenClaw Gateway :18789  │
│  app/services/openclaw.py   │                                │  (مغز AI: DeepSeek/AvalAI)│
│  توکن تلگرام فقط همین‌جاست │ ◀── {reply} ────────────────── │  کانال تلگرام: disabled   │
└─────────────────────────────┘                                └──────────────────────────┘
```

- **توکن تلگرام فقط متعلق به گراماست.** OpenClaw هرگز نباید poll کردن تلگرام را
  شروع کند. برای این کار `channels.telegram.enabled=false` تنظیم می‌شود.
- گراما OpenClaw را فقط از طریق HTTP صدا می‌زند (بدون هیچ کانال پیام‌رسان).
- اگر OpenClaw down باشد یا تایم‌اوت شود (پیش‌فرض ۳۰ ثانیه)، گراما پیام مناسب
  فارسی نمایش می‌دهد و به موتور الگویی / Tavily برمی‌گردد؛ **هرگز hang نمی‌شود.**

## ۲) نصب OpenClaw (نسخه 2026.9.5+)

نیازمندی: **Node.js ≥ 24.16** (یا ≥ 26.1).

```bash
npm install -g openclaw@latest
openclaw --version
```

## ۳) پیکربندی اولیه (headless)

```bash
openclaw onboard --non-interactive --accept-risk \
  --agent-name gramma \
  --auth-choice custom-api-key \
  --custom-provider-id myllm \
  --custom-base-url "https://api.provider.example/v1" \
  --custom-model-id MODEL_ID \
  --custom-api-key "..." \
  --custom-text-input
```

> برای AvalAI: `--custom-base-url "https://api.avalai.ir/v1"` با مدل
> `gpt-4o-mini` و `--custom-api-key "aa-..."` (کلید AvalAI).

## ۴) غیرفعال‌سازی کانال تلگرام OpenClaw (الزامی)

```bash
openclaw config set channels.telegram.enabled false
openclaw config get channels.telegram.enabled   # باید false برگردد
```

## ۵) مسیر HTTP `POST /api/ask`

قراردادی که گراما انتظار دارد روی گیت‌وی ثبت شده باشد:

- **درخواست:** `{"message": "...", "context": {...}, "task": "..."}`
- **پاسخ:** `{"reply": "..."}`
- **احراز هویت:** `Authorization: Bearer <gateway-token>` (اختیاری اگر توکن خالی است)

پیاده‌سازی مرجع به صورت پلاگین OpenClaw در این مخزن موجود است (پوشه‌ی
`gramma-bridge/`). نصب:

```bash
openclaw plugins install ./gramma-bridge --force --accept-capabilities
```

## ۶) اجرای گیت‌وی

```bash
openclaw gateway run          # پیش‌زمینه (لوکال، port 18789)
# یا به عنوان سرویس:
openclaw gateway install
```

تأیید:

```bash
curl -s http://127.0.0.1:18789/health
# {"ok":true,"status":"live"}
```

## ۷) تنظیم گراما (`.env`)

```bash
OPENCLAW_BASE_URL="http://127.0.0.1:18789"
OPENCLAW_TOKEN="<gateway-token از openclaw gateway auth-token>"
OPENCLAW_TIMEOUT="30"
```

> `OPENCLAW_BASE_URL=""` → قابلیت OpenClaw غیرفعال و فال‌بک‌های الگویی فعال می‌شوند.

## ۸) تست پایان‌به‌پایان

```bash
cd gramma-bot
.venv/bin/python scripts/openclaw_smoke.py
```

این اسکریپت هر شش قابلیت را در برابر OpenClaw واقعی اجرا و ثبت
`activity_logs` را تأیید می‌کند.

## ۹) امنیت

- گیت‌وی فقط روی `127.0.0.1` (loopback) بایند شود؛ هرگز روی `0.0.0.0`.
- توکن گیت‌وی فقط در `.env` گراما (و هرگز در سورس/گیت) نگهداری شود.
- هر درخواست OpenClaw توسط گراما در `activity_logs` ثبت می‌شود.
