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
        return "Great question — we'll get back to you with the details shortly!"
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

    Priority: OpenClaw gateway → AvalAI (direct) → built-in template.
    Any failure degrades gracefully to the next layer; the bot never hangs.
    """
    # 1) OpenClaw (the centralized AI brain)
    if settings.openclaw_base_url:
        try:
            from app.services.openclaw import generate_caption_via_openclaw

            return await generate_caption_via_openclaw(
                prompt, tone=tone, language=language, hashtags=hashtags,
                user_id=user_id, account_id=account_id,
            )
        except Exception:  # noqa: BLE001 — fall through to next engine
            pass

    # 2) AvalAI / OpenAI-compatible direct
    if settings.ai_api_key:
        try:
            return await _generate_caption_llm(prompt, tone, language, hashtags)
        except Exception:
            pass

    # 3) Offline template
    return _generate_caption_template(prompt, tone, language, hashtags)


async def _generate_caption_llm(prompt, tone, language, hashtags) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.ai_api_key,
        base_url=settings.ai_base_url or None,
        timeout=25.0,
    )
    system = (
        "You are a professional Instagram copywriter. Write a single, "
        "engaging caption for the given post topic. Return ONLY the caption "
        "text, no commentary. Use emojis and short paragraphs. "
        f"Language: {language}. Tone: {tone}."
    )
    if hashtags:
        system += " Finish with 5-8 relevant hashtags."
    resp = await client.chat.completions.create(
        model=settings.ai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.85,
    )
    return resp.choices[0].message.content.strip()


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
    if rule == "spam" or not settings.ai_api_key:
        return rule
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url or None,
            timeout=12.0,
        )
        resp = await client.chat.completions.create(
            model=settings.ai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify the Instagram DM into exactly one of: "
                        "spam, needs_reply, replied. Reply with the label only."
                    ),
                },
                {"role": "user", "content": text[:1000]},
            ],
            temperature=0,
        )
        label = (resp.choices[0].message.content or "").strip().lower()
        if label in {"spam", "needs_reply", "replied"}:
            return label
    except Exception:
        pass
    return rule


async def draft_reply(
    incoming_text: str,
    locale: str | None = None,
    timeout: float = 20.0,
) -> str | None:
    """Draft an AI reply to an incoming DM (AvalAI). Returns None on failure.

    ``timeout`` bounds the LLM call — the serverless webhook path passes a
    short 5s budget so an incoming message can never hang the function.
    """
    if not settings.ai_api_key:
        return _default_reply(incoming_text, locale)
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url or None,
            timeout=timeout,
        )
        lang_hint = "Persian (فارسی)" if (locale or "").startswith("fa") else "English"
        resp = await client.chat.completions.create(
            model=settings.ai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a social-media manager. Write ONE short, friendly "
                        f"reply in {lang_hint} to the following message from a follower. "
                        "Return only the reply text, no commentary."
                    ),
                },
                {"role": "user", "content": incoming_text[:800]},
            ],
            temperature=0.7,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception:  # noqa: BLE001
        return _default_reply(incoming_text, locale)


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
