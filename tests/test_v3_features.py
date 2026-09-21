"""Gramma v3 — unit tests for templates, smart notifications, PDF, web search fallback."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_v3.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")


def test_template_render():
    from app.services.templates import render_template

    assert "کفش" in render_template("{title} موجود شد", "کفش")
    # missing placeholder? no crash
    assert render_template("سلام دنیا", "x") == "سلام دنیا"


def test_default_templates_available():
    from app.services.templates import DEFAULT_TEMPLATES

    assert len(DEFAULT_TEMPLATES) >= 3
    assert all("{title}" in t["caption"] for t in DEFAULT_TEMPLATES)


def test_smart_notifications_gates():
    import asyncio

    from app.services import smart_notifications as sn

    # high-signal events always pass
    assert sn.should_notify("publish_failed") is True
    assert sn.should_notify("collab_request") is True
    assert sn.should_notify("daily_report") is True
    # throttled event passes once then is suppressed
    assert sn.should_notify("webhook_event") is True
    assert sn.should_notify("webhook_event") is False


def test_pdf_bytes_valid():
    from app.services.pdf_report import build_health_pdf_bytes

    pdf = build_health_pdf_bytes("Health Report\nreached 1200 users")
    assert pdf.startswith(b"%PDF-1.4")
    assert b"%%EOF" in pdf
    assert len(pdf) > 300


def test_web_search_no_key_returns_empty():
    import asyncio

    from app.services.web_search import web_search

    # TAVILY_API_KEY may be set from env; we test the no-key path by monkeypatching
    from app.core.config import get_settings

    settings = get_settings()
    old = settings.tavily_api_key
    settings.tavily_api_key = ""
    try:
        result = asyncio.run(web_search("test"))
        assert result == []
    finally:
        settings.tavily_api_key = old


def test_ai_draft_reply_falls_back_without_key():
    import asyncio

    from app.core.config import get_settings

    settings = get_settings()
    old = settings.ai_api_key
    # ai_api_key is a property; patch the underlying fields
    old_a, old_o = settings.avalai_api_key, settings.openai_api_key
    settings.avalai_api_key = ""
    settings.openai_api_key = ""
    try:
        from app.services.ai import draft_reply

        reply = asyncio.run(draft_reply("سلام", locale="fa"))
        assert isinstance(reply, str) and len(reply) > 3
    finally:
        settings.avalai_api_key = old_a
        settings.openai_api_key = old_o
