# گزارش نهایی: یکپارچه‌سازی OpenClaw با ربات گراما

**تاریخ:** ۲۱ سپتامبر ۲۰۲۶ (۳۰ شهریور ۱۴۰۵)
**مخزن:** `zeynaliiarmin/gramma-bot` — کامیت `d235446`

---

## خلاصه

OpenClaw به‌عنوان **مغز هوش مصنوعی محلی** در کنار ربات گراما مستقر و وصل شد.
کاربر نهایی فقط **یک ربات (گراما)** می‌بیند؛ تمام درخواست‌های هوشمند از یک نقطه‌ی
مرکزی (`app/services/openclaw.py`) به `POST {base}/api/ask` می‌روند و اگر OpenClaw
در دسترس نباشد، گراما به موتور الگویی / Tavily برمی‌گردد — بدون هیچ hang.

---

## ۱) آنچه انجام شد

### ۱.۱ سرویس مرکزی — `app/services/openclaw.py`

- `OpenClawClient`: کلاینت async httpx با تایم‌اوت ۳۰ ثانیه، header اختیاری
  `Authorization: Bearer <token>`، و قرارداد
  `{"message", "context", "task"}` → `{"reply"}` (با پشتیبانی از کلیدهای
  `response`/`text`/`answer`).
- `OpenClawError`: خطای یکپارچه برای down/timeout/بدنه‌ی بد/پاسخ خالی.
- `record_openclaw_activity`: ثبت هر درخواست/پاسخ در جدول `activity_logs`.
- شش تابع سطح بالا: `generate_caption_via_openclaw`، `suggest_comment_reply`،
  `suggest_dm_reply`، `generate_post_ideas`، `web_search_via_openclaw`، `analyze_page`.

### ۱.۲ تنظیمات

- `app/core/config.py`: افزودن `openclaw_base_url` (پیش‌فرض `http://127.0.0.1:18789`)،
  `openclaw_token`، `openclaw_timeout` (پیش‌فرض ۳۰).
- `.env` / `.env.example`: افزودن `OPENCLAW_BASE_URL` / `OPENCLAW_TOKEN` / `OPENCLAW_TIMEOUT`.

### ۱.۳ اتصال شش قابلیت به رابط کاربری ربات

| # | قابلیت | وضعیت | مسیر در ربات |
|---|--------|-------|--------------|
| ۱ | تولید کپشن از متن کاربر | ✅ وصل | منوی «دستیار هوش مصنوعی» → انتخاب پیج → نوشتن موضوع |
| ۲ | پیشنهاد پاسخ به کامنت | ✅ وصل | کامیونیتی → دکمه ✨ کنار هر کامنت |
| ۳ | پیشنهاد پاسخ به دایرکت | ✅ وصل | دایرکت → دکمه ✨ کنار هر گفتگو |
| ۴ | ایده‌ی پست از موضوع | ✅ وصل | منوی «دستیار هوش مصنوعی» → «ایده پست از موضوع» |
| ۵ | جستجوی وب (Tavily متصل به OpenClaw) | ✅ وصل | منوی «دستیار هوش مصنوعی» → «جستجوی وب (AI)» |
| ۶ | تحلیل پیج و پیشنهاد بهبود | ✅ وصل | منوی «آمار و امنیت» → «تحلیل هوشمند پیج (AI)» |

هر شش قابلیت دارای **fallback محلی** هستند (پیام فارسی مناسب + الگو/تخویلی مستقیم)
تا در نبود OpenClaw تجربه خراب نشود.

### ۱.۴ قواعد امنیتی (طبق خواسته)

- کانال تلگرام OpenClaw **غیرفعال** است (`channels.telegram.enabled=false`) و OpenClaw
  هرگز تلگرام را poll نمی‌کند؛ **توکن تلگرام فقط متعلق به گراماست.**
- گیت‌وی OpenClaw فقط روی `127.0.0.1` بایند شد.
- هر درخواست OpenClaw در `activity_logs` ثبت می‌شود.

---

## ۲) نصب و تست واقعی OpenClaw

- OpenClaw **2026.9.5** با Node 24 نصب شد و گیت‌وی آن روی پورت **18789** اجرا شد
  (`curl /health` → `{"ok":true,"status":"live"}`).
- پلاگین **`gramma-bridge`** (در مسیر `openclaw/gramma-bridge/` همین مخزن) مسیر
  `POST /api/ask` را مطابق قرارداد روی گیت‌وی ثبت کرد.
- **تست پایان‌به‌پایان زنده** (`scripts/openclaw_smoke.py`): هر شش قابلیت در برابر
  OpenClaw واقعی اجرا و پاسخ گرفتند؛ **۶ ردیف** در `activity_logs` ثبت شد:
  ```
  openclaw_caption, openclaw_comment_reply, openclaw_dm_reply,
  openclaw_post_ideas, openclaw_web_search, openclaw_analytics
  ```
- **تست failover**: با بستن OpenClaw، تولید کپشن در **۰.۱۵ ثانیه** به الگو برگشت
  (بدون hang).

> ⚠️ نکته‌ی صادقانه: مغز زیرین OpenClaw در این محیط sandbox یک مدل
> «OpenAI‌compatible محلی (mock)» بود، چون endpoint تولیدی AvalAI از این sandbox
> در دسترس نیست (`HTTP 000` — تحریم/فایروال شبکه). خود OpenClaw، گیت‌وی، مسیر
> `/api/ask`، احراز هویت، ثبت log و زنجیره‌ی failover همگی **با نرم‌افزار واقعی
> OpenClaw** تست شدند. در سرور تولید کافی است `OPENCLaw` با کلید AvalAI/DeepSeek
> واقعی onboard شود (مستند در `docs/OPENCLAW_SETUP_FA.md`).

---

## ۳) تست‌ها

- تست‌های جدید OpenClaw (`tests/test_openclaw.py`): **۱۳ تست** — قرارداد،
  fallback کلیدها، خطاها (disabled/unreachable/non-200/non-JSON/empty)،
  ثبت `activity_logs`، عبور هر شش قابلیت از یک client، و زنجیره‌ی fallback کپشن.
- مجموع: **۳۲ تست پاس** (`pytest tests/ -q` → 32 passed).
- `python -m compileall` → OK. بوت زنده ربات → OK
  (`Run polling for bot @zeynalikid_farzandman_alerts_bot`).

---

## ۴) فایل‌های تغییر‌یافته/جدید

```
app/core/config.py            ← افزودن ۳ تنظیم OpenClaw
app/services/openclaw.py      ← سرویس مرکزی (جدید)
app/services/ai.py            ← generate_caption: اول OpenClaw، بعد AvalAI، بعد الگو
app/bot/handlers/ai.py        ← ایده پست + جستجوی وب (OpenClaw→Tavily fallback)
app/bot/handlers/community.py ← دکمه ✨ پیشنهاد پاسخ کامنت
app/bot/handlers/direct.py    ← دکمه ✨ پیشنهاد پاسخ دایرکت
app/bot/handlers/insights.py  ← «تحلیل هوشمند پیج» (analyze_page)
app/bot/keyboards.py          ← آیتم‌های منوی جدید
scripts/openclaw_smoke.py     ← تست زنده E2E (جدید)
tests/test_openclaw.py        ← ۱۳ تست هرمتیک (جدید)
docs/OPENCLAW_SETUP_FA.md     ← مستند راه‌اندازی فارسی (جدید)
openclaw/gramma-bridge/       ← پلاگین OpenClaw برای مسیر /api/ask (جدید)
README.md / .env.example / .gitignore
```

---

## ۵) جمع‌بندی

هر شش قابلیت هوشمند به OpenClaw متصل و در محیط تست اجرا/تأیید شدند، امنیت
(غیرفعال‌بودن کانال تلگرام OpenClaw + توکن فقط در گراما) رعایت شده، تایم‌اوت ۳۰
ثانیه و fallback بدون hang پیاده‌سازی شده، و هر درخواست در `activity_logs` ثبت
می‌شود. تغییرات commit و به مخزن `zeynaliiarmin/gramma-bot` push شد.
