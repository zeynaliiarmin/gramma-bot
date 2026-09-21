"""Gramma — OpenClaw gateway client tests (hermetic, no real server).

Covers:
  * config wiring (OPENCLAW_BASE_URL / _TOKEN / _TIMEOUT)
  * the {message, context, task} → /api/ask → {reply} contract
  * reply-key fallbacks (reply|response|text|answer)
  * graceful OpenClawError on: disabled / unreachable / non-200 / non-JSON / empty
  * activity_logs recording for every request
  * all six smart capabilities routing through the same client
  * generate_caption chain: OpenClaw first, template fallback when down
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_openclaw.db")

import json

import httpx
import pytest


# ── helpers ──────────────────────────────────────────────────────
def make_transport(status: int = 200, body=None, exc: Exception | None = None):
    """Build an httpx.MockTransport with a recording handler."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content or b"{}")
        if exc is not None:
            raise exc
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body or "")

    return httpx.MockTransport(handler), captured


def new_client(transport=None, base_url="http://127.0.0.1:18789", token="", timeout=30.0):
    from app.services.openclaw import OpenClawClient

    return OpenClawClient(base_url=base_url, token=token, timeout=timeout, transport=transport)


# ── config wiring ────────────────────────────────────────────────
def test_config_openclaw_defaults():
    from app.core.config import get_settings

    s = get_settings()
    # Defaults (no .env overrides) are the OpenClaw gateway defaults…
    assert s.openclaw_base_url == "http://127.0.0.1:18789"
    assert s.openclaw_timeout == 30.0
    assert isinstance(s.openclaw_token, str)  # may be "" (default) or a real token


# ── contract / client ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_client_sends_contract_and_parses_reply():
    transport, captured = make_transport(body={"reply": "کپشن تستی ✨"})
    client = new_client(transport)
    out = await client.ask("موضوع", context={"kind": "caption"}, task="caption")
    assert out == "کپشن تستی ✨"
    assert captured["url"] == "http://127.0.0.1:18789/api/ask"
    assert captured["json"]["message"] == "موضوع"
    # The client now always injects the current Jalali date/time snapshot
    # into the context (user requirement: OpenClaw receives the Shamsi date).
    ctx = captured["json"]["context"]
    assert ctx["kind"] == "caption"
    assert "current_date_jalali" in ctx
    assert "current_time" in ctx
    assert "day_of_week" in ctx
    assert ctx.get("timezone") == "Asia/Tehran"
    assert captured["json"]["task"] == "caption"


@pytest.mark.asyncio
async def test_client_sends_bearer_token():
    transport, captured = make_transport(body={"reply": "ok"})
    client = new_client(transport, token="ogt_secret")
    await client.ask("hi")
    assert captured["headers"].get("authorization") == "Bearer ogt_secret"


@pytest.mark.asyncio
async def test_reply_key_fallbacks():
    from app.services.openclaw import OpenClawClient

    for key in ("response", "text", "answer"):
        transport, _ = make_transport(body={key: "پاسخ"})
        client = OpenClawClient(base_url="http://x", transport=transport)
        assert await client.ask("hi") == "پاسخ"


@pytest.mark.asyncio
async def test_disabled_raises():
    from app.services.openclaw import OpenClawError

    client = new_client(base_url="")
    with pytest.raises(OpenClawError):
        await client.ask("hi")


@pytest.mark.asyncio
async def test_unreachable_raises_and_never_hangs():
    import time

    from app.services.openclaw import OpenClawError

    transport, _ = make_transport(exc=httpx.ConnectError("connection refused"))
    client = new_client(transport, timeout=1.0)
    start = time.monotonic()
    with pytest.raises(OpenClawError):
        await client.ask("hi")
    assert time.monotonic() - start < 2.0


@pytest.mark.asyncio
async def test_non_200_raises():
    from app.services.openclaw import OpenClawError

    transport, _ = make_transport(status=500, body={"error": "boom"})
    client = new_client(transport)
    with pytest.raises(OpenClawError):
        await client.ask("hi")


@pytest.mark.asyncio
async def test_non_json_raises():
    from app.services.openclaw import OpenClawError

    transport, _ = make_transport(body="not json at all")
    client = new_client(transport)
    with pytest.raises(OpenClawError):
        await client.ask("hi")


@pytest.mark.asyncio
async def test_empty_reply_raises():
    from app.services.openclaw import OpenClawError

    transport, _ = make_transport(body={"reply": ""})
    client = new_client(transport)
    with pytest.raises(OpenClawError):
        await client.ask("hi")


# ── activity logging ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_success_is_logged_to_activity_logs():
    from sqlalchemy import select

    from app.core.database import SessionLocal, init_db
    from app.models import ActivityLog
    from app.services.openclaw import generate_caption_via_openclaw

    await init_db()
    transport, _ = make_transport(body={"reply": "کپشن"})
    client = new_client(transport)

    from app.services import openclaw as oc

    orig = oc.get_openclaw_client
    oc.get_openclaw_client = lambda: client
    try:
        out = await generate_caption_via_openclaw(
            "محصول جدید", user_id=777, account_id=123
        )
    finally:
        oc.get_openclaw_client = orig
    assert out == "کپشن"

    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(ActivityLog).where(ActivityLog.user_id == 777)))
            .scalars()
            .all()
        )
    assert any(r.action == "openclaw_caption" and r.account_id == 123 for r in rows)


# ── six capabilities route through the client ────────────────────
@pytest.mark.asyncio
async def test_six_capabilities_hit_api_ask():
    from app.services import openclaw as oc
    from app.services.openclaw import (
        analyze_page,
        generate_caption_via_openclaw,
        generate_post_ideas,
        suggest_comment_reply,
        suggest_dm_reply,
        web_search_via_openclaw,
    )

    transport, captured = make_transport(body={"reply": "### پاسخ"})
    client = new_client(transport)
    orig = oc.get_openclaw_client
    oc.get_openclaw_client = lambda: client
    try:
        await generate_caption_via_openclaw("موضوع", language="fa")
        await suggest_comment_reply("کامنت", username="u1")
        await suggest_dm_reply("پیام", sender_name="s1")
        await generate_post_ideas("موضوع", count=3)
        await web_search_via_openclaw("query")
        await analyze_page({"followers": 100})
    finally:
        oc.get_openclaw_client = orig

    # Every call reached the same client endpoint.
    assert captured["url"] == "http://127.0.0.1:18789/api/ask"
    # The last task recorded is "analytics"; each function sets its own task.
    assert captured["json"]["task"] == "analytics"


# ── high-level fallback chain ────────────────────────────────────
@pytest.mark.asyncio
async def test_generate_caption_falls_back_to_template_when_openclaw_down():
    from app.core.config import get_settings
    from app.services import openclaw as oc
    from app.services.ai import generate_caption

    settings = get_settings()
    old_url, old_timeout = settings.openclaw_base_url, settings.openclaw_timeout
    old_a, old_o = settings.avalai_api_key, settings.openai_api_key
    old_client = oc._client
    settings.openclaw_base_url = "http://127.0.0.1:1"
    settings.openclaw_timeout = 1.0
    settings.avalai_api_key = ""
    settings.openai_api_key = ""
    oc._client = None
    try:
        caption = await generate_caption("کفش چرم", language="fa")
    finally:
        settings.openclaw_base_url = old_url
        settings.openclaw_timeout = old_timeout
        settings.avalai_api_key = old_a
        settings.openai_api_key = old_o
        oc._client = old_client

    # Fallback produced a usable, non-empty caption (never a hang / never None).
    assert isinstance(caption, str) and len(caption) > 5


@pytest.mark.asyncio
async def test_suggest_comment_reply_falls_back_cleanly():
    from app.services.openclaw import OpenClawError, suggest_comment_reply

    transport, _ = make_transport(exc=httpx.ReadTimeout("slow"))
    client = new_client(transport, timeout=1.0)

    from app.services import openclaw as oc

    orig = oc.get_openclaw_client
    oc.get_openclaw_client = lambda: client
    try:
        with pytest.raises(OpenClawError):
            await suggest_comment_reply("عالی بود")
    finally:
        oc.get_openclaw_client = orig
