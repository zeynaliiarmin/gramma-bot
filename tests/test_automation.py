"""E2E tests for the multi-step Auto-Reply automation engine (v4 stage 1).

Scenario under test (ManyChat-style):
    قیمت → [SEND قیمت‌ها] → [WAIT انتخاب] → [COLLECT انتخاب] → [GOTO ارسال]
         → [SEND تایید + هزینه ارسال] → [END]
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

os.environ["TELEGRAM_BOT_TOKEN"] = "000:test"
os.environ["ENCRYPTION_KEY"] = "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA="
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_auto.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")


def _price_scenario() -> dict:
    """The canonical multi-step flow used in tests + the SPA builder."""
    return {
        "name": "فروش و ثبت سفارش",
        "channel": "dm",
        "match_mode": "keyword",
        "trigger_text": "قیمت, هزینه, خرید",
        "fallback_reply": "متوجه نشدم؛ لطفاً دوباره بفرمایید 🙏",
        "steps": [
            {"action": "send", "text": "سلام! 👋\nقیمت‌ها:\n1️⃣ پکیج برنزی 500 هزارتومان\n2️⃣ پکیج نقره‌ای 900\n3️⃣ پکیج طلایی 1500\nکدوم رو می‌خوای؟"},
            {"action": "wait"},
            {"action": "collect", "variable": "choice"},
            {"action": "goto", "next_step_order": 4},
            {"action": "send", "text": "در حال ثبت سفارش {choice}…"},
            {"action": "send", "text": "✅ ثبت شد! همکار ما به‌زودی برای پرداخت و ارسال پیام می‌دهد. هزینه ارسال رایگان است 🚚"},
            {"action": "end"},
        ],
    }


@pytest.fixture
def scenario_def():
    return _price_scenario()


def test_scenario_triggers_parse():
    from app.services.automation import AutomationScenario

    s = AutomationScenario(trigger_text="قیمت, هزینه, خرید")
    assert s.triggers == ["قیمت", "هزینه", "خرید"]


def test_keyword_match_priority():
    from app.services.automation import AutomationScenario, _keyword_hits

    s = AutomationScenario(trigger_text="قیمت, هزینه", match_mode="keyword")
    assert _keyword_hits(s, "سلام قیمت چنده؟")
    assert not _keyword_hits(s, "سلام وقت بخیر")


@pytest.mark.asyncio
async def test_multi_step_flow_orders_register():
    """A follower walks the price scenario to a completed order."""
    from app.core.database import SessionLocal, init_db
    from app.services.automation import (
        AutomationScenario,
        AutomationSession,
        AutomationStep,
        StepAction,
        process_inbound,
    )

    await init_db()
    async with SessionLocal() as session:
        from app.models import InstagramAccount
        from sqlalchemy import select as _sel

        # clean prior runs
        for model in (AutomationSession, AutomationStep, AutomationScenario):
            for row in (await session.execute(_sel(model))).scalars():
                await session.delete(row)
        await session.flush()

        sc = AutomationScenario(
            account_id=1, name="فروش", channel="dm", match_mode="keyword",
            trigger_text="قیمت, هزینه, خرید", fallback_reply="متوجه نشدم 🙏",
        )
        session.add(sc)
        await session.flush()
        for order, st in enumerate(_price_scenario()["steps"]):
            session.add(
                AutomationStep(
                    scenario_id=sc.id, order_index=order,
                    action=st["action"], text=st.get("text", ""),
                    next_step_order=st.get("next_step_order"), variable=st.get("variable", ""),
                )
            )
        await session.commit()

    # (1) follower says "قیمت؟" → scenario starts, first SEND returned
    async with SessionLocal() as session:
        out = await process_inbound(session, 1, "dm", "peer9", "سلام قیمت چنده؟")
        assert out is not None and out.send_text
        assert "قیمت" in out.send_text
        assert out.waiting is True
        assert out.final is False
        await session.commit()

    # (2) follower replies "پکیج نقره‌ای" → collect + goto + final SEND, flow ends
    async with SessionLocal() as session:
        out = await process_inbound(session, 1, "dm", "peer9", "پکیج نقره‌ای رو می‌خوام")
        assert out is not None
        assert out.send_text and "ثبت شد" in out.send_text
        assert out.final is True
        await session.commit()

    # (3) no active session should remain
    async with SessionLocal() as session:
        from sqlalchemy import select as _sel

        remaining = (
            await session.execute(_sel(AutomationSession).where(AutomationSession.peer_id == "peer9"))
        ).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_no_match_returns_none():
    from app.core.database import SessionLocal
    from app.services.automation import process_inbound

    async with SessionLocal() as session:
        res = await process_inbound(session, 999, "dm", "nobody", "just a hi message")
        assert res is None


@pytest.mark.asyncio
async def test_ai_fallback_used_when_no_ai_config():
    """draft_automation_reply returns fallback when no AI key is configured."""
    from app.services.automation import draft_automation_reply

    out = await draft_automation_reply("قیمت؟", "", "fallback-text")
    assert out == "fallback-text"
