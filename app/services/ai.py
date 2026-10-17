"""Gramma — AI caption engine + DM auto-classifier.

* Caption generation: uses an OpenAI-compatible endpoint when configured,
  otherwise falls back to a built-in template engine (no external cost, and
  the whole feature still works offline).
* DM classification: rule-based + (optional) LLM with a graceful fallback so
  DMs can always be sorted into spam / needs_reply / replied.
"""

from __future__ import annotations

import json
import re
from typing import AsyncIterator

from app.core.config import get_settings
from app.services.llm import generate_text

settings = get_settings()

_SPAM_WORDS = {
    "en": ["buy followers", "free money", "click here", "promo", "crypto", "lottery", "winner", "gift card", "sex", "casino", "loan"],
    "fa": ["دنبال‌کننده بخر", "فالوور رایگان", "کلیک کن", "برنده شدی", "قرعه کشی", "کازینو", "شرط بندی", "وام فوری", "درآمد میلیونی"],
}


def _words_for(locale: str | None) -> list[str]:
    words: list[str] = []
    for key in _SPAM_WORDS:
        words.extend(_SPAM_WORDS[key])
    if locale and locale in _SPAM_WORDS:
        words = _SPAM_WORDS[locale]
    return words


def _has_spam(text: str, locale: str | None = None) -> bool:
    low = text.lower()
    return any(w in low for w in _words_for(locale))


def _default_reply(text: str, locale: str | None = None) -> str:
    """Fallback canned reply when no AI is available."""
    thank = "Thank you for your message! Our team will get back to you soon."
    if (locale or "").startswith("fa"):
        thank = "ممنون از پیام شما! به‌زودی پاسخ می‌دهیم."
    if _has_spam(text, locale):
        return (
            "This looks like an automated message and we ignore those."
            if not (locale or "").startswith("fa")
            else "به نظر پیام تبلیغاتی می‌رسد و پاسخی برای آن ارسال نمی‌شود."
        )
    if "?" in text or "؟" in text or re.search(r"\b(question|help)\b", text, re.I):
        return (
            "سؤال خوبی است؛ به‌زودی با جزئیات پاسخ می‌دهیم!"
            if (locale or "").startswith("fa")
            else "Great question — we'll get back to you with the details shortly!"
        )
    return thank


# ── Captions ──────────────────────────────────────────────────
async def generate_caption(
    prompt: str,
    tone: str = "friendly",
    language: str = "en",
    hashtags: bool = True,
    user_id: int | None = None,
    account_id: int | None = None,
) -> str:
    """Generate a caption.

    Gemini Flash → OpenRouter :free → Mistral Free mode → offline template.
    Nothing generated here is automatically sent to Instagram.
    """
    system = (
        "You are a professional Instagram copywriter. Write a single, "
        "engaging caption for the given post topic. Return ONLY the caption "
        "text, no commentary. Use emojis and short paragraphs. "
        f"Language: {language}. Tone: {tone}."
    )
    if hashtags:
        system += " Finish with 5-8 relevant hashtags."
    result = await generate_text(system, prompt, temperature=0.85, timeout=12.0)
    return result or _generate_caption_template(prompt, tone, language, hashtags)


def _generate_caption_template(prompt, tone, language, hashtags) -> str:
    """Deterministic offline caption engine."""
    is_fa = (language or "").startswith("fa")
    openers = {
        "friendly": ["Hey everyone! 👋", "سلام به همه! 👋"],
        "professional": ["We're excited to share this with you.", "با افتخار تقدیم می‌کنیم."],
        "fun": ["You won't believe this one! 😄", "این یکی را از دست ندهید! 😄"],
    }
    openers.setdefault(tone, openers["friendly"])
    opener = openers[tone][1 if is_fa else 0]
    tags = "\n\n" + (hashtag_list(is_fa)) if hashtags else ""
    body = prompt.strip() or ("خبر خوب برای شما!" if is_fa else "Great news for you!")
    return f"{opener}\n\n{body}{tags}"


def hashtag_list(fa: bool = False) -> str:
    if fa:
        return "#اینستاگرام #محتوا #پیج #فالوور #رشد"
    return "#instagram #content #growth #socialmedia #brand"


# ── DM classification ─────────────────────────────────────────
def classify_dm(text: str, locale: str | None = None) -> str:
    """Rule-based classification → needs_reply | spam | replied."""
    if _has_spam(text, locale):
        return "spam"
    return "needs_reply"


async def classify_dm_async(text: str, locale: str | None = None) -> str:
    """Async classification with optional LLM refinement."""
    rule = classify_dm(text, locale)
    if rule == "spam" or not settings.llm_configured:
        return rule
    label = await generate_text(
        "Classify the Instagram DM into exactly one of: spam, needs_reply, "
        "replied. Reply with the label only.", text[:1000],
        temperature=0, timeout=5.0, max_output_tokens=40,
    )
    if label and label.strip().lower() in {"spam", "needs_reply", "replied"}:
        return label.strip().lower()
    return rule


async def draft_reply(
    incoming_text: str,
    locale: str | None = None,
    timeout: float = 20.0,
) -> str | None:
    """Draft only; existing ChatbotX policy still controls actual replies.

    The webhook passes a short budget; on provider failure the existing safe
    offline reply remains available instead of blocking the webhook.
    """
    lang_hint = "Persian (فارسی)" if (locale or "").startswith("fa") else "English"
    result = await generate_text(
        "You are a social-media manager. Write ONE short, friendly "
        f"reply in {lang_hint} to a follower. Return only the reply text.",
        incoming_text[:800], temperature=0.7, timeout=timeout,
    )
    return result or _default_reply(incoming_text, locale)


async def _short_ai_task(system: str, prompt: str) -> str | None:
    """Bounded provider-neutral request for ideas and insights."""
    return await generate_text(system, prompt[:1800], temperature=0.7, timeout=10.0)


async def generate_post_ideas(topic: str, count: int = 5, language: str = "fa") -> str:
    """Suggest post topics through the configured LLM or the offline fallback."""
    count = max(1, min(count, 10))
    ideas = await _short_ai_task(
        f"Suggest exactly {count} distinct Instagram post ideas in {language}. "
        "Use Latin digits for numbering. Return only the numbered list.",
        topic,
    )
    if ideas:
        return ideas
    return (
        "ایده‌هایی برای شروع:\n"
        f"1. معرفی موضوع «{topic[:80]}» با یک مثال واقعی\n"
        "2. پشت‌صحنه کار و فرآیند آماده‌سازی را نشان دهید\n"
        "3. به سؤال‌های پرتکرار دنبال‌کنندگان پاسخ دهید\n"
        "4. یک نکته کاربردی را با تصویر یا ویدیو آموزش دهید\n"
        "5. تجربه و بازخورد مشتریان را به اشتراک بگذارید"
    )


async def analyze_page(metrics: dict, language: str = "fa") -> str:
    """Summarize page metrics without exposing them to an obsolete gateway."""
    analysis = await _short_ai_task(
        f"You are an Instagram growth advisor. Analyze the page metrics in {language}. "
        "Give three actionable recommendations; use Latin digits, not Gregorian dates.",
        json.dumps(metrics, ensure_ascii=False, default=str),
    )
    if analysis:
        return analysis
    return (
        "تحلیل پایهٔ پیج:\n"
        f"👥 دنبال‌کننده: {metrics.get('followers', '—')}\n"
        f"🖼 پست: {metrics.get('media_count', '—')}\n"
        f"📈 روند: {metrics.get('trend', '—')}\n\n"
        "پیشنهاد: انتشار منظم و تعامل فعال با کامنت‌ها."
    )


async def stream_captions(prompt: str, count: int, tone: str, language: str) -> AsyncIterator[str]:
    """Yield `count` caption variants (used by Retry/More buttons)."""
    seen: set[str] = set()
    for _ in range(count):
        caption = await generate_caption(
            prompt, tone=tone, language=language, hashtags=True
        )
        if caption not in seen:
            seen.add(caption)
            yield caption
