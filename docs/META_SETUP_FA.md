# راهنمای گام‌به‌گام دریافت مجوز از Meta و اتصال به Instagram Graph API

<div dir="rtl">

این راهنما شما را قدم‌به‌قدم از «ساخت اپ فیسبوک» تا «انتشار اولین پست از طریق ربات» می‌برد.

> ⏱ زمان تقریبی: ۳۰ تا ۶۰ دقیقه (بسته به سرعت تایید متا)

---

## ۰) پیش‌نیازها

- یک حساب **Facebook** شخصی فعال
- پیج **Business یا Creator** اینستاگرام که به آن دسترسی کامل دارید
- سرور با آدرس عمومی **HTTPS** (برای وبهوک و OAuth)
- App Mode ربات روی `production` (در فایل `.env`)

---

## ۱) ساخت اپلیکیشن متا (Meta App)

1. وارد سایت [developers.facebook.com](https://developers.facebook.com) شوید.
2. روی **My Apps** → **Create App** کلیک کنید.
3. Use case را **"Other"** انتخاب کنید (برای دسترسی به Instagram API) و نوع اپ را **Business** بگذارید.
4. نام اپ، ایمیل و Business Portfolio (اختیاری) را وارد کنید.
5. پس از ساخت، از منوی سمت چپ وارد **Products** شوید و محصولات زیر را اضافه کنید:
   - **Instagram** (الزامی)
   - **Facebook Login for Business** (برای OAuth کاربران — الزامی)
   - **Webhooks** (برای دریافت رویدادها)

---

## ۲) تنظیم Facebook Login (برای OAuth اتصال کاربران)

1. به **Facebook Login → Settings** بروید.
2. در بخش **Valid OAuth Redirect URIs** آدرس زیر را اضافه کنید (مطابق `WEBHOOK_BASE_URL` خودتان):

   ```
   https://your-domain.example/webhook/instagram/callback
   ```

3. ذخیره کنید. ❗ این آدرس باید **دقیقاً** با `redirect_uri` که ربات تولید می‌کند یکی باشد.

---

## ۳) دریافت App ID و App Secret

1. از **App Settings → Basic** مقدار **App ID** و **App Secret** را کپی کنید.
2. آن‌ها را در `.env` قرار دهید:

   ```env
   META_APP_ID="1234567890"
   META_APP_SECRET="abc123..."
   META_VERIFY_TOKEN="یک-رشته-تصادفی-طولانی"
   INSTAGRAM_ACCOUNT_MODE="production"
   WEBHOOK_BASE_URL="https://your-domain.example"
   ```

   > 💡 برای امنیت بیشتر می‌توانید App Secret را رمزنگاری‌شده بگذارید؛
   > کافی است مقدار `enc:<base64>` را به‌جای متن ساده بنویسید (مطابق ساختار خود پروژه).

---

## ۴) افزودن تسترها (Test Users)

از آنجایی که اپ در حالت Development فقط به «نقش‌های اپ» اجازه استفاده می‌دهد:

1. به **App Roles → Roles** بروید.
2. هر ۵ کاربر نهایی (صاحبان پیج‌ها) را با ایمیل فیسبوکشان به‌عنوان **Tester** یا **Developer** اضافه کنید.
3. سپس در بخش **Instagram → API setup with Instagram Login** یا **Advanced Access** اطمینان بگیرید که صفحات **Instagram** متصل است.

---

## ۵) اتصال پیج و دریافت Instagram Business ID

هر کاربر باید پیج خود را وصل کند. دو روش:

### روش الف — از داخل خود ربات (پیشنهادی)
1. کاربر در تلگرام `/connect` می‌زند.
2. لینک امن OAuth را باز می‌کند و با اکانتش لاگین می‌کند.
3. پیج خود را انتخاب و مجوزها را تایید می‌کند.
4. ربات به‌صورت خودکار توکن بلندمدت (Long-Lived ≈ ۶۰ روز) را می‌گیرد و **رمزنگاری‌شده** ذخیره می‌کند. ✅

### روش ب — دستی (برای عیب‌یابی)
1. به **Meta Business Suite → Settings → Business Assets → Instagram accounts** بروید.
2. پیج را به اکانت فیسبوک با نقش ادمین لینک کنید.
3. `Instagram Business ID` را از **App Settings → Instagram → ...** یا از طریق Graph Explorer بدست آورید.

---

## ۶) راه‌اندازی وبهوک (Webhook)

ربات Gramma به‌صورت خودکار مسیر `/webhook/instagram` را پاسخ می‌دهد. برای فعال‌سازی اشتراک:

1. مطمئن شوید ربات با `python run.py --web` یا `docker compose up` اجراست (پورت 8000 عمومی).
2. اسکریپت اشتراک را اجرا کنید:

   ```bash
   python scripts/install_webhook_subscription.py \
       --app-token "APP_ACCESS_TOKEN" \
       --ig-user-id "1784140xxxxxxxx" \
       --callback "https://your-domain.example/webhook/instagram"
   ```

   > برای گرفتن `APP_ACCESS_TOKEN` می‌توانید از دستور زیر استفاده کنید:
   > `https://graph.facebook.com/oauth/access_token?client_id=APP_ID&client_secret=APP_SECRET&grant_type=client_credentials`

3. فیلدهای اشتراک مهم: `comments`، `messages`، `mention`.

---

## ۷) انتقال به Advanced Access (برای انتشار واقعی)

برای انتشار پست/استوری/ریلز واقعی:

1. به **App Review → Permissions and Features** بروید.
2. برای هر یک از مجوزهای زیر، درخواست **Advanced Access** بدهید:
   - `instagram_business_basic`
   - `instagram_business_content_publish` (برای انتشار)
   - `instagram_business_manage_messages` (برای دایرکت)
   - `instagram_business_manage_comments` (برای مدیریت کامنت)
   - `instagram_business_manage_insights`
   - `pages_read_engagement` و `pages_show_list`
3. متا معمولاً ویدیوی کوتاهی از نحوه استفاده ربات می‌خواهد؛ صفحه ضبط (Screen recording) از ربات هنگام کار تهیه و آپلود کنید.
4. پس از تایید، انتشار برای **همه کاربران** فعال می‌شود (نه فقط تسترها).

> ⚠️ **مهم:** بدون Advanced Access، فقط «تسترهای اپ» می‌توانند انتشار آزمایشی انجام دهند.
> اینستاگرام اجازه انتشار از اپ در حالت Development برای کاربران عادی را نمی‌دهد.

---

## ۸) رسانه (Media) — بارگذاری روی CDN

Instagram Graph API برای انتشار، یک **URL عمومی** می‌خواهد (فایل محلی تلگرام قابل استفاده نیست).

- در `.env` متغیرهای `S3_*` را پیکربندی کنید (Bucket سازگار با S3 مثل AWS S3، DigitalOcean Spaces، ArvanCloud و…).
- ربات به‌صورت خودکار فایل دریافتی از تلگرام را روی CDN می‌گذارد و URL عمومی را به اینستاگرام می‌دهد.

---

## ۹) عیب‌یابی سریع

| مشکل | راه‌حل |
|---|---|
| `Invalid OAuth access token` | توکن منقضی/باطل شده — دوباره `/connect` بزنید یا منتظر رفرش خودکار بمانید |
| کاربران غیرتستر نمی‌توانند پست بگذارند | اپ هنوز Advanced Access نگرفته (گام ۷) |
| وبهوک تایید نمی‌شود | `WEBHOOK_BASE_URL` باید HTTPS و عمومی باشد؛ `META_VERIFY_TOKEN` را چک کنید |
| `redirect_uri` نامعتبر | آدرس کال‌بک را دقیقاً در Facebook Login → Valid OAuth Redirect URIs ثبت کنید |
| استوری/ریلز خطا می‌دهد | فقط پیج‌های **Creator** + Advanced Access (گام ۷) |
| پیام «Media URL invalid» | فایل روی CDN نیست؛ `S3_*` را تنظیم کنید |

---

## 🔐 چک‌لیست امنیتی نهایی

- [ ] `ENCRYPTION_KEY` تولید و **بکاپ** شده
- [ ] `.env` از گیت خارج است
- [ ] `META_APP_SECRET` به‌صورت `enc:` یا حداقل محرمانه نگهداری می‌شود
- [ ] هر کاربر فقط نقش Tester/Developer خود را دارد
- [ ] `WEBHOOK_BASE_URL` روی HTTPS
- [ ] لاگ‌های ممیزی در `activity_logs` بررسی می‌شوند

بعد از تکمیل این مراحل، می‌توانید از تلگرام پیج را وصل کنید و اولین پست را بزنید 🎉

</div>
