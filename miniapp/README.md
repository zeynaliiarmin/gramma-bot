# Mini-App استقرار و توسعه

<div dir="rtl">

مینیاپ Gramma یک اپ React + Vite است که خروجی build آن داخل `app/webapp/static` قرار می‌گیرد و توسط همان سرور پایتون (FastAPI) همان‌تریجین سرو می‌شود. یعنی Telegram WebApp و API هر دو روی یک origin (پورت 8000) هستند — بدون هیچ مشکل CORS.

## توسعه محلی

```bash
cd miniapp
npm install
npm run dev        # Vite با پراکسی /api به localhost:8000
```

سپس در یک ترمینال دیگر، بک‌اند را اجرا کنید (توکن تلگرام فقط برای بوت لازم است؛ API مستقل است):

```bash
python run.py --no-scheduler
```

در محیط توسعه به `http://localhost:5173` بروید. چون تلگرام در مرورگر دسکتاپ نیست، می‌توانید یک `_token` آزمایشی بسازید:

```bash
python - <<'EOF'
import asyncio
from app.webapp.auth import make_miniapp_ticket
print(make_miniapp_ticket(USER_TELEGRAM_ID))
EOF
```

و آدرس `http://localhost:5173/?_token=<خروجی>` را باز کنید.

## بیلد برای تولید

```bash
cd miniapp
npm run build      # خروجی در ../app/webapp/static
```

فایل‌های ساخته‌شده (index.html + assets) را همراه تصویر Docker ارسال کنید (در Dockerfile، `COPY miniapp/dist ./app/webapp/static` یا همان مسیر build). سپس `/` همان پنل مدیریت است و `/api/...` API مینیاپ.

## امنیت

- ورود فقط با بلیط HMAC (`_token`) که سرور ساخته و ۲۴ ساعت اعتبار دارد.
- هر درخواست API با هدر `X-Mini-App-Hash` امضا می‌شود؛ سرور کاربر را از روی آن شناسایی و همه داده‌ها را scope می‌کند.
- توکن خام هیچ‌وقت در localStorage ذخیره نمی‌شود (فقط sessionStorage با هش).
- `initData تلگرام` صرفاً برای نمایش است و به‌عنوان منبع اعتماد استفاده نمی‌شود.

</div>
