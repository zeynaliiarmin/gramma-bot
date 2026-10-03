"""Gramma — OpenClaw AI gateway client (single point of integration).

OpenClaw runs on the SAME server as a local HTTP gateway (port 18789) whose
"brain" is DeepSeek/AvalAI. Gramma treats it as its central intelligence
engine so the end user sees ONE bot (Gramma) that occasionally calls OpenClaw
for smart work.

Architecture rules enforced here:
  * Telegram belongs to Gramma only — OpenClaw's Telegram channel stays
    disabled (see options.yaml); this client ONLY speaks HTTP to /api/ask.
  * All smart requests funnel through this module (no duplicated HTTP code).
  * 30-second timeout (configurable) + graceful timeout handling so the bot
    never hangs when OpenClaw is down.
  * Every request/response is recorded in activity_logs for consumption
    tracking.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.utils.jalali import current_jalali_context as get_jalali_context

settings = get_settings()
logger = logging.getLogger("gramma.openclaw")


class OpenClawError(Exception):
    """Raised when OpenClaw is unreachable, times out, or answers badly."""


async def record_openclaw_activity(
    user_id: Optional[int],
    account_id: Optional[int],
    action: str,
    detail: str,
    level: str = "info",
) -> None:
    """Write an audit row for one OpenClaw interaction (consumption tracking).

    Skipped (silently) when neither a user nor an account is known — e.g. in
    pure unit tests that exercise the transport layer in isolation.
    """
    if user_id is None and account_id is None:
        return
    try:
        from app.core.database import SessionLocal
        from app.models import ActivityLog

        async with SessionLocal() as session:
            session.add(
                ActivityLog(
                    user_id=user_id,
                    account_id=account_id,
                    action=action,
                    detail=detail[:2000],
                    level=level,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — logging must never break the flow
        logger.warning("openclaw activity log failed: %s", exc)


class OpenClawClient:
    """Async HTTP client for the OpenClaw gateway.

    A `transport` may be injected for hermetic unit tests.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        transport: Any | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.openclaw_base_url or "").rstrip("/")
        self.token = token if token is not None else settings.openclaw_token
        self.timeout = timeout if timeout is not None else settings.openclaw_timeout
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def ask(
        self,
        message: str,
        context: dict | None = None,
        task: str = "general",
        user_id: Optional[int] = None,
        account_id: Optional[int] = None,
    ) -> str:
        """Send {message, context} to POST /api/ask and return {reply}.

        Raises OpenClawError on any failure (disabled / timeout / bad body),
        and always records the attempt in activity_logs.
        """
        if not self.enabled:
            await record_openclaw_activity(
                user_id, account_id, f"openclaw_{task}",
                "skipped: OPENCLAW_BASE_URL not set", "warning",
            )
            raise OpenClawError("OpenClaw is disabled (OPENCLAW_BASE_URL not set)")

        # Product policy for the serverless runtime (VERCEL=1):
        #  * AI text tasks (captions/replies/ideas/…) must go DIRECTLY to
        #    AvalAI — never through the OpenClaw gateway.
        #  * The ONLY allowed gateway task is Instagram publishing
        #    (``task="instagram_publish"``), which drives Browser Use Cloud.
        import os

        if os.environ.get("VERCEL") == "1" and task != "instagram_publish":
            await record_openclaw_activity(
                user_id, account_id, f"openclaw_{task}",
                "skipped: AI tasks use AvalAI directly on serverless (policy)", "info",
            )
            raise OpenClawError("در محیط سرورلس، کارهای متنی مستقیم به AvalAI می‌روند")

        payload = {
            "message": message,
            # Always attach the current Jalali date/time snapshot so OpenClaw
            # reasons in the user's Persian calendar (Asia/Tehran).
            "context": {**get_jalali_context(), **(dict(context or {}))},
            "task": task,
        }
        url = f"{self.base_url}/api/ask"

        try:
            if self._transport is not None:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                    resp = await client.post(url, json=payload, headers=self._headers())
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            await record_openclaw_activity(
                user_id, account_id, f"openclaw_{task}_error",
                f"unreachable/timeout ({self.timeout}s): {exc}", "warning",
            )
            raise OpenClawError(
                f"OpenClaw در دسترس نیست (timeout={self.timeout}s): {exc}"
            ) from exc

        if resp.status_code != 200:
            await record_openclaw_activity(
                user_id, account_id, f"openclaw_{task}_error",
                f"http {resp.status_code}: {resp.text[:200]}", "warning",
            )
            raise OpenClawError(f"OpenClaw returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            await record_openclaw_activity(
                user_id, account_id, f"openclaw_{task}_error", "non-JSON body", "warning",
            )
            raise OpenClawError("OpenClaw returned a non-JSON body") from exc

        reply = ""
        if isinstance(data, str):
            reply = data
        elif isinstance(data, dict):
            reply = (
                data.get("reply")
                or data.get("response")
                or data.get("text")
                or data.get("answer")
                or ""
            )
        if not reply:
            await record_openclaw_activity(
                user_id, account_id, f"openclaw_{task}_error",
                "empty reply", "warning",
            )
            raise OpenClawError("OpenClaw returned an empty reply")

        await record_openclaw_activity(
            user_id, account_id, f"openclaw_{task}",
            f"ok ({len(reply)} chars) ← {message[:160]}", "info",
        )
        return reply


_client: OpenClawClient | None = None


def get_openclaw_client() -> OpenClawClient:
    global _client
    if _client is None:
        _client = OpenClawClient()
    return _client


# ── High-level smart capabilities (task → natural-language prompt) ──

async def generate_caption_via_openclaw(
    prompt: str,
    tone: str = "friendly",
    language: str = "fa",
    hashtags: bool = True,
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> str:
    """1) Caption generation."""
    lang = "فارسی" if str(language).startswith("fa") else "انگلیسی"
    extra = " در پایان ۵-۸ هشتگ مرتبط هم اضافه کن." if hashtags else ""
    message = (
        f"برای این موضوع یک کپشن اینستاگرام جذاب و حرفه‌ای به زبان {lang} بنویس "
        f"(لحن: {tone}):\n«{prompt}»" + extra
    )
    return await get_openclaw_client().ask(
        message,
        context={"kind": "caption", "tone": tone, "language": language, "hashtags": hashtags},
        task="caption",
        user_id=user_id,
        account_id=account_id,
    )


async def suggest_comment_reply(
    comment_text: str,
    username: str = "",
    language: str = "fa",
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> str:
    """2) Suggest a professional reply to a comment."""
    lang = "فارسی" if str(language).startswith("fa") else "انگلیسی"
    message = (
        f"یک پاسخ کوتاه، حرفه‌ای و دوستانه به زبان {lang} برای این کامنت اینستاگرام "
        f"بنویس (فقط متن پاسخ، بدون توضیح):\n"
        f"کاربر: {username or 'فالوور'}\nکامنت: «{comment_text}»"
    )
    return await get_openclaw_client().ask(
        message,
        context={"kind": "comment_reply", "username": username, "language": language},
        task="comment_reply",
        user_id=user_id,
        account_id=account_id,
    )


async def suggest_dm_reply(
    message_text: str,
    sender_name: str = "",
    language: str = "fa",
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> str:
    """3) Suggest a professional reply to a DM."""
    lang = "فارسی" if str(language).startswith("fa") else "انگلیسی"
    message = (
        f"یک پاسخ کوتاه، صمیمی و حرفه‌ای به زبان {lang} برای این پیام دایرکت "
        f"بنویس (فقط متن پاسخ):\n"
        f"فرستنده: {sender_name or 'کاربر'}\nپیام: «{message_text}»"
    )
    return await get_openclaw_client().ask(
        message,
        context={"kind": "dm_reply", "sender": sender_name, "language": language},
        task="dm_reply",
        user_id=user_id,
        account_id=account_id,
    )


# ── Instagram publishing via OpenClaw gateway + Browser Use Cloud ──
# NOTE: intentionally NOT gated by the VERCEL=1 AI guard. The gateway is the
# official publishing path (reaches Instagram through Browser Use Cloud),
# while AI text tasks stay on AvalAI per product policy.

_MEDIA_FA = {
    "post": "پست معمولی (خوراک)",
    "story": "استوری",
    "reel": "ریلز",
    "carousel": "پست چندتصویره (کاروسل)",
}


async def publish_post_via_gateway(
    image_url: str,
    caption: str,
    media_type: str = "post",
    image_urls: list[str] | None = None,
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
    username_hint: str = "",
) -> str:
    """Ask the OpenClaw gateway to publish to Instagram via Browser Use Cloud.

    The gateway drives a real browser session: login (stored credentials),
    upload media from the public URL, paste the caption and publish.

    Returns the gateway's reply text. Raises OpenClawError on any failure.
    Uses a dedicated longer timeout (``OPENCLAW_PUBLISH_TIMEOUT``).
    """
    client = get_openclaw_client()
    if not client.enabled:
        raise OpenClawError("OpenClaw gateway is not configured (OPENCLAW_BASE_URL)")

    import os

    urls = list(image_urls or [])
    if image_url:
        urls.insert(0, image_url)
    media_lines = "\n".join(f"  - {u}" for u in urls) or "  - (آدرسی داده نشده!)"
    kind = _MEDIA_FA.get(media_type, media_type)

    message = (
        f"با مرورگر وارد اینستاگرام شو (نام کاربری پیج مقصد: {username_hint or 'zeynalikids'}) "
        f"و یک «{kind}» جدید منتشر کن.\n"
        f"مراحل دقیق:\n"
        f"۱) اگر لاگین نیستی، با اعتبارنامه‌های ذخیره‌شده وارد اینستاگرام شو.\n"
        f"۲) رسانه(ها) را از این آدرس(ها) بارگذاری کن:\n{media_lines}\n"
        f"۳) این کپشن را دقیقاً وارد کن:\n«\"\"{caption}\"\"»\n"
        f"۴) دکمه انتشار (Share/پست) را بزن و صبر کن تا تأیید انتشار را ببینی.\n"
        f"۵) در پایان فقط یک خط گزارش بده: موفق یا ناموفق + دلیل/لینک پست."
    )

    context = {
        "kind": "instagram_publish",
        "media_type": media_type,
        "image_urls": urls,
        "caption": caption,
        "instagram_username": username_hint or "",
        "browser_use": bool(settings.browser_use_api_key),
    }

    # Longer timeout for browser automation (own client instance).
    pub_client = OpenClawClient(timeout=settings.openclaw_publish_timeout)
    if os.environ.get("VERCEL") == "1":
        await record_openclaw_activity(
            user_id, account_id, "openclaw_instagram_publish",
            f"publish start type={media_type} urls={len(urls)} via public gateway", "info",
        )
    reply = await pub_client.ask(
        message,
        context=context,
        task="instagram_publish",
        user_id=user_id,
        account_id=account_id,
    )
    return reply


async def generate_post_ideas(
    topic: str,
    count: int = 5,
    language: str = "fa",
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> str:
    """4) Content ideas for a topic."""
    lang = "فارسی" if str(language).startswith("fa") else "انگلیسی"
    message = (
        f"{count} ایده پست اینستاگرام برای موضوع «{topic}» به زبان {lang} پیشنهاد بده؛ "
        f"هر ایده را در یک خط با شماره بنویس و برای هرکدام یک کپشن کوتاه هم بگذار."
    )
    return await get_openclaw_client().ask(
        message,
        context={"kind": "post_ideas", "topic": topic, "count": count, "language": language},
        task="post_ideas",
        user_id=user_id,
        account_id=account_id,
    )


async def web_search_via_openclaw(
    query: str,
    language: str = "fa",
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> str:
    """5) Web search (Tavily is attached to OpenClaw)."""
    lang = "فارسی" if str(language).startswith("fa") else "انگلیسی"
    message = (
        f"در وب جستجو کن و خلاصه‌ای از مهم‌ترین نتایج با لینک درباره «{query}» "
        f"به زبان {lang} بنویس."
    )
    return await get_openclaw_client().ask(
        message,
        context={"kind": "web_search", "tool": "tavily", "language": language},
        task="web_search",
        user_id=user_id,
        account_id=account_id,
    )


async def analyze_page(
    metrics: dict,
    language: str = "fa",
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> str:
    """6) Analyze page metrics and give improvement suggestions."""
    lang = "فارسی" if str(language).startswith("fa") else "انگلیسی"
    import json

    message = (
        f"این آمار پیج اینستاگرام من را تحلیل کن و ۴-۵ پیشنهاد عملی و اولویت‌بندی‌شده "
        f"برای رشد پیج به زبان {lang} بده:\n{json.dumps(metrics, ensure_ascii=False)}"
    )
    return await get_openclaw_client().ask(
        message,
        context={"kind": "analytics", "language": language},
        task="analytics",
        user_id=user_id,
        account_id=account_id,
    )
