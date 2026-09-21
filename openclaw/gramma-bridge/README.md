# Gramma AI Bridge (OpenClaw plugin)

این پلاگین مسیر `POST /api/ask` را روی گیت‌وی OpenClaw ثبت می‌کند تا ربات گراما
بتواند مغز هوش مصنوعی را صدا بزند. قرارداد:

- درخواست: `{"message": "...", "context": {...}, "task": "..."}`
- پاسخ: `{"reply": "..."}`
- احراز هویت: گیت‌وی OpenClaw (`Authorization: Bearer <token>`)

## نصب

```bash
openclaw plugins install ./openclaw/gramma-bridge --force --accept-capabilities
openclaw gateway restart
```

## بیلد (در صورت تغییر سورس)

```bash
npm install --include=dev
npx tsc -p tsconfig.json
```

نکته: `package.json` دیگر وابستگی زمان اجرا ندارد؛ `openclaw` صرفاً
peer-dependency برای تایپ‌هاست.
