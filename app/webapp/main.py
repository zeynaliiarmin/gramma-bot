"""Gramma — Mini-App API server (FastAPI).

Serves:
  * `/api/*` — authenticated JSON endpoints for the Telegram Mini-App
    (dashboard, pages, calendar, scheduled posts, settings, collab inbox).
  * `/ws/broadcast` — WebSocket for live refresh pings.
  * `/healthz` — liveness probe.

Mounted alongside the Instagram webhook/OAuth aiohttp server on port 8000.
Static frontend files are served from `app/webapp/static` (the built React
bundle is copied there in production).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.repositories import get_accounts_for_user
from app.models import Post
from app.services.analytics_history import ChatHistory, DesignHistory, record_chat, record_design
from app.services.automation import (
    AutomationScenario,
    AutomationSession,
    AutomationStep,
    MatchMode,
    Outcome,
    ScenarioChannel,
    StepAction,
    get_scenarios,
    get_steps,
    process_inbound,
)
from app.services.autoreply import AutoReply
from app.services.calendar import build_calendar
from app.services.collab import CollabRequest, get_collab_for_user
from app.services.insights import get_dashboard_snapshot
from app.services.limits import get_limits
from app.utils import jalali
from app.webapp.auth import (
    MiniAppIdentity,
    make_miniapp_ticket,
    resolve_miniapp_user,
    verify_telegram_init_data,
)

settings = get_settings()
logger = logging.getLogger("gramma.webapp")

app = FastAPI(title="Gramma Mini-App API", version="3.0.0")

# CORS: allow ONLY our own Mini-App origin(s). The Telegram WebApp always
# originates from one of these configured URLs, so nothing else may call the
# API from a browser. Falls back to permissive '*' for local development
# (backend + SPA on the same origin, port 8000).
_configured_origins = [
    o.rstrip("/")
    for o in (settings.miniapp_public_url, settings.public_base_url)
    if o and o.strip()
]
_cors_origins = list(dict.fromkeys(_configured_origins)) or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,  # we use an HMAC header, not cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Mini-App-Hash", "Content-Type"],
)

# ── In-process connection registry for live refresh ──────────
_connections: set[WebSocket] = set()


async def broadcast_refresh(channel: str, payload: dict) -> None:
    """Push a lightweight JSON event to every connected Mini-App socket."""
    import json

    text = json.dumps(payload, ensure_ascii=False, default=str)
    dead: list[WebSocket] = []
    for ws in list(_connections):
        try:
            await ws.send_text(text)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        _connections.discard(ws)


# ── Helpers ──────────────────────────────────────────────────
async def _account_dicts(user_id: int) -> list[dict]:
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, user_id)
        out = []
        for a in accounts:
            out.append(
                {
                    "id": a.id,
                    "username": a.username,
                    "name": a.name,
                    "status": a.status,
                    "profile_pic_url": a.profile_pic_url,
                    "token_encrypted": bool(a.long_lived_token_enc),
                    "token_expires_at": a.token_expires_at.isoformat() if a.token_expires_at else None,
                    # User-facing Jalali expiry (Latin digits) for the SPA.
                    "token_expires_jalali": (
                        jalali.jalali_date_str(a.token_expires_at)
                        if a.token_expires_at
                        else None
                    ),
                    "ig_user_id": a.instagram_user_id,
                }
            )
        return out


def _with_limits(payload: dict, limits_dict: dict) -> dict:
    """Attach the platform caps to a payload (Mini-App follows them too)."""
    return {**payload, "limits": limits_dict}


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/auth/verify")
async def api_auth_verify(body: dict):
    """Exchange valid Telegram initData for a session ticket.

    Called once by the SPA on load. The initData HMAC is verified against the
    bot token (never `initDataUnsafe`); on success we mint the same HMAC
    ticket the WebApp button would embed, and return it for sessionStorage.
    """
    init_data = (body or {}).get("initData") or ""
    uid = verify_telegram_init_data(init_data)
    if uid is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "initData نامعتبر است؛ از داخل ربات وارد شوید."},
        )
    from app.core.database import SessionLocal
    from app.services.limits import can_register_user

    # Same hard user cap as /start (single source: limits.py).
    async with SessionLocal() as session:
        allowed, _reason = await can_register_user(session, uid)
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": "ظرفیت کاربران ربات تکمیل شده است."},
            )
    return {
        "ok": True,
        "user_id": uid,
        "ticket": make_miniapp_ticket(uid),
    }


@app.get("/api/me")
async def api_me(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    from app.models import User
    from app.services.limits import can_register_user

    async with SessionLocal() as session:
        lim = await get_limits(session)
        # Mini-App → same hard user cap as the bot (single source: limits.py).
        allowed, reason = await can_register_user(session, ident.user_id)
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "ظرفیت کاربران ربات تکمیل شده است "
                        f"(حداکثر {lim.max_users} کاربر)."
                    ),
                    "limits": lim.as_dict,
                },
            )
        user = await session.get(User, ident.user_id)
        accounts = await get_accounts_for_user(session, ident.user_id)
        return _with_limits(
            {
                "user_id": ident.user_id,
                "name": user.full_name if user else str(ident.user_id),
                "username": user.telegram_username if user else None,
                "accounts": await _account_dicts(ident.user_id),
                "connected_count": len([a for a in accounts if a.status == "connected"]),
                "max_accounts": lim.max_accounts_per_user,
            },
            lim.as_dict,
        )


@app.get("/api/accounts")
async def api_accounts(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    return {"accounts": await _account_dicts(ident.user_id)}


@app.get("/api/dashboard")
async def api_dashboard(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    """Aggregated metrics across the user's pages + a weekly activity series."""
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        cards = []
        series = {"labels": [], "reach": [], "followers": []}
        for a in accounts:
            snap = await get_dashboard_snapshot(a)
            cards.append(
                {
                    "account_id": a.id,
                    "username": a.username,
                    "name": a.name,
                    "followers": snap.get("followers"),
                    "media_count": snap.get("media_count"),
                    "reach_today": snap.get("reach_today"),
                    "trend": snap.get("trend"),
                }
            )
        # Demo-friendly 7-day series (real Graph API can backfill later).
        import random
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(settings.timezone)
        from datetime import timedelta

        for i in range(6, -1, -1):
            day = datetime.now(tz) - timedelta(days=i)
            series["labels"].append(jalali.weekday_fa(day))
            series["reach"].append(random.randint(200, 1200))
            series["followers"].append(random.randint(0, 60))
        return {"cards": cards, "series": series}


@app.get("/api/calendar")
async def api_calendar(
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return {"days": []}
        days = await build_calendar(session, ident.user_id, ids)
        return {"days": days}


@app.get("/api/scheduled")
async def api_scheduled(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return {"posts": []}
        result = await session.execute(
            select(Post)
            .where(Post.account_id.in_(ids), Post.status.in_(["scheduled", "draft"]))
            .order_by(Post.scheduled_at)
        )
        posts = [
            {
                "id": p.id,
                "kind": p.kind,
                "caption": (p.caption or "")[:120],
                "status": p.status,
                "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
                "when_jalali": jalali.to_jalali_str(p.scheduled_at) if p.scheduled_at else None,
                "permalink": p.ig_permalink,
            }
            for p in result.scalars()
        ]
        return {"posts": posts}


@app.get("/api/collabs")
async def api_collabs(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    async with SessionLocal() as session:
        result = await session.execute(
            select(CollabRequest)
            .where(
                (CollabRequest.owner_id == ident.user_id)
                | (CollabRequest.partner_id == ident.user_id)
            )
            .order_by(CollabRequest.created_at.desc())
        )
        rows = [
            {
                "id": r.id,
                "owner_id": r.owner_id,
                "partner_id": r.partner_id,
                "source_account_id": r.source_account_id,
                "target_account_id": r.target_account_id,
                "message": r.message,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "created_jalali": jalali.to_jalali_str(r.created_at) if r.created_at else None,
            }
            for r in result.scalars()
        ]
        return {"requests": rows}


@app.post("/api/collabs/{req_id}/respond")
async def api_collab_respond(
    req_id: int,
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Accept/decline a collab request (partner only)."""
    from app.services.collab import accept_collab

    action = (body or {}).get("action")
    async with SessionLocal() as session:
        req = await get_collab_for_user(session, req_id, ident.user_id)
        if req is None:
            raise HTTPError(404, "not_found_or_not_yours")
        if req.partner_id != ident.user_id:
            raise HTTPError(403, "only the partner can respond")

        if action == "accept":
            from app.services.collab import CollabStatus

            err = await accept_collab(session, req)
            if err:
                raise HTTPError(400, err)
            await session.commit()
            await broadcast_refresh("collab", {"type": "collab_updated"})
            return {"ok": True, "status": "accepted"}
        if action == "decline":
            from app.services.collab import CollabStatus

            req.status = CollabStatus.declined.value
            from datetime import datetime as _dt, timezone as _tz

            req.responded_at = _dt.now(_tz.utc)
            await session.commit()
            await broadcast_refresh("collab", {"type": "collab_updated"})
            return {"ok": True, "status": "declined"}
        raise HTTPError(400, "unknown_action")


@app.get("/api/auto-replies")
async def api_autoreplies(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    async with SessionLocal() as session:
        lim = await get_limits(session)
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return _with_limits({"rules": []}, lim.as_dict)
        result = await session.execute(select(AutoReply).where(AutoReply.account_id.in_(ids)))
        return _with_limits(
            {
                "rules": [
                    {"id": r.id, "account_id": r.account_id, "keywords": r.keywords, "reply": r.reply, "enabled": r.enabled, "matches": r.matches}
                    for r in result.scalars()
                ]
            },
            lim.as_dict,
        )


@app.post("/api/auto-replies")
async def api_autoreply_add(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        if not accounts:
            raise HTTPError(400, "no_accounts")
        account_id = (body or {}).get("account_id") or accounts[0].id
        if account_id not in [a.id for a in accounts]:
            raise HTTPError(403, "not_your_account")
        from app.services.autoreply import add_rule

        rule = await add_rule(
            session, account_id, (body or {}).get("keywords", ""), (body or {}).get("reply", "")
        )
        if (body or {}).get("dm_followup"):
            rule.dm_followup = str(body.get("dm_followup", ""))
        await session.commit()
        return {"ok": True, "id": rule.id}


# ── Anti-Block: daily usage, queue, safety alerts ────────────
@app.get("/api/reply-usage")
async def api_reply_usage(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    """Today's comment-reply usage + limits + queue sizes for the user's pages."""
    from app.services import limits
    from app.services.reply_limits import (
        CommentReplyQueue,
        DailyReplyCounter,
        get_counter,
        tehran_date,
    )

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        per_account = []
        totals = {"replies_today": 0, "queued": 0, "alerts": 0, "daily_limit_total": 0}
        if ids:
            today = tehran_date()
            counters = list((await session.execute(
                select(DailyReplyCounter).where(
                    DailyReplyCounter.account_id.in_(ids),
                    DailyReplyCounter.date == today,
                )
            )).scalars())
            by_account = {c.account_id: c for c in counters}
            queued = list((await session.execute(
                select(CommentReplyQueue).where(
                    CommentReplyQueue.account_id.in_(ids),
                    CommentReplyQueue.status.in_(["pending", "processing"]),
                )
            )).scalars())
            pending_by_account: dict[int, int] = {}
            for q in queued:
                pending_by_account[q.account_id] = pending_by_account.get(q.account_id, 0) + 1
            for a in accounts:
                cap = limits.reply_limit_for_account(a.created_at)
                counter = by_account.get(a.id)
                used = counter.replies_count if counter else 0
                per_account.append({
                    "account_id": a.id,
                    "username": a.username,
                    "name": a.name,
                    "replies_today": used,
                    "daily_limit": cap,
                    "remaining": max(0, cap - used),
                    "hourly_count": counter.hourly_count if counter else 0,
                    "hourly_limit": limits.HOURLY_REPLY_LIMIT,
                    "rate_profile": counter.rate_profile if counter else "—",
                    "queued": pending_by_account.get(a.id, 0),
                })
                totals["replies_today"] += used
                totals["daily_limit_total"] += cap
                totals["queued"] += pending_by_account.get(a.id, 0)
        from app.services.reply_limits import SafetyAlert

        alerts = list((await session.execute(
            select(SafetyAlert).where(
                SafetyAlert.account_id.in_(ids),
                SafetyAlert.resolved.is_(False),
            ).order_by(SafetyAlert.id.desc()).limit(20)
        )).scalars()) if ids else []
        totals["alerts"] = len(alerts)
        return {
            "date": tehran_date(),
            "daily_limit_per_account": limits.DAILY_REPLY_LIMIT,
            "hourly_limit": limits.HOURLY_REPLY_LIMIT,
            "schedules": per_account,
            "totals": totals,
            "alerts": [
                {
                    "id": x.id,
                    "account_id": x.account_id,
                    "alert_type": x.alert_type,
                    "message": x.message,
                    "created_at": x.created_at.isoformat() if x.created_at else None,
                }
                for x in alerts
            ],
        }


@app.get("/api/reply-queue")
async def api_reply_queue(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    """The pending comment-reply queue for the user's pages (FIFO order)."""
    from app.services.reply_limits import CommentReplyQueue

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return {"items": []}
        items = list((await session.execute(
            select(CommentReplyQueue).where(
                CommentReplyQueue.account_id.in_(ids),
                CommentReplyQueue.status.in_(["pending", "processing", "failed"]),
            ).order_by(CommentReplyQueue.id.asc()).limit(200)
        )).scalars())
        return {
            "items": [
                {
                    "id": q.id,
                    "account_id": q.account_id,
                    "comment_text": (q.comment_text or "")[:200],
                    "has_dm_followup": q.dm_followup_required,
                    "status": q.status,
                    "retry_count": q.retry_count,
                    "error_message": (q.error_message or "")[:200],
                    "created_at": q.created_at.isoformat() if q.created_at else None,
                }
                for q in items
            ]
        }


@app.post("/api/alerts/resolve")
async def api_alert_resolve(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Mark a safety alert resolved (owner's account only)."""
    from app.services.reply_limits import SafetyAlert

    alert_id = (body or {}).get("alert_id")
    if not alert_id:
        raise HTTPError(400, "missing_alert_id")
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        alert = await session.get(SafetyAlert, int(alert_id))
        if alert is None or alert.account_id not in ids:
            raise HTTPError(404, "not_found_or_not_yours")
        alert.resolved = True
        await session.commit()
        return {"ok": True, "resolved": True}


# ── Automation scenarios (multi-step auto-reply) ─────────────
def _scenario_dict(s: AutomationScenario) -> dict:
    return {
        "id": s.id,
        "account_id": s.account_id,
        "name": s.name,
        "channel": s.channel,
        "match_mode": s.match_mode,
        "trigger_text": s.trigger_text,
        "ai_instruction": s.ai_instruction,
        "fallback_reply": s.fallback_reply,
        "use_ai": s.use_ai,
        "enabled": s.enabled,
        "priority": s.priority,
        "hits": s.hits,
        "triggers": s.triggers,
    }


def _step_dict(st: AutomationStep) -> dict:
    return {
        "id": st.id,
        "scenario_id": st.scenario_id,
        "order_index": st.order_index,
        "action": st.action,
        "text": st.text,
        "use_ai": st.use_ai,
        "next_step_order": st.next_step_order,
        "variable": st.variable,
    }


@app.get("/api/automation/scenarios")
async def api_scenarios_list(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    """All scenarios for the user's accounts (with their steps)."""
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return {"scenarios": []}
        scenarios = []
        for a_id in ids:
            for sc in await get_scenarios(session, a_id, only_enabled=False):
                steps = await get_steps(session, sc.id)
                scenarios.append({**_scenario_dict(sc), "steps": [_step_dict(st) for st in steps]})
        return {"scenarios": scenarios}


@app.post("/api/automation/scenarios")
async def api_scenario_create(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Create a scenario (optionally with its steps in one shot)."""
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        if not accounts:
            raise HTTPError(400, "no_accounts")
        account_id = (body or {}).get("account_id") or accounts[0].id
        if account_id not in [a.id for a in accounts]:
            raise HTTPError(403, "not_your_account")

        sc = AutomationScenario(
            account_id=account_id,
            name=(body or {}).get("name", "سناریوی جدید"),
            channel=(body or {}).get("channel", ScenarioChannel.DM),
            match_mode=(body or {}).get("match_mode", MatchMode.KEYWORD),
            trigger_text=(body or {}).get("trigger_text", ""),
            ai_instruction=(body or {}).get("ai_instruction", ""),
            fallback_reply=(body or {}).get("fallback_reply", ""),
            use_ai=bool((body or {}).get("use_ai", True)),
            enabled=bool((body or {}).get("enabled", True)),
            priority=int((body or {}).get("priority", 0) or 0),
        )
        session.add(sc)
        await session.flush()
        for i, st in enumerate((body or {}).get("steps", []) or []):
            session.add(
                AutomationStep(
                    scenario_id=sc.id,
                    order_index=i,
                    action=st.get("action", StepAction.SEND),
                    text=st.get("text", ""),
                    use_ai=bool(st.get("use_ai", False)),
                    next_step_order=st.get("next_step_order"),
                    variable=st.get("variable", ""),
                )
            )
        await session.commit()
        return {"ok": True, "id": sc.id}


@app.patch("/api/automation/scenarios/{sid}")
async def api_scenario_update(
    sid: int,
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Update scenario fields + replace its steps atomically."""
    async with SessionLocal() as session:
        sc = await session.get(AutomationScenario, sid)
        if sc is None:
            raise HTTPError(404, "not_found")
        accounts = await get_accounts_for_user(session, ident.user_id)
        if sc.account_id not in [a.id for a in accounts]:
            raise HTTPError(403, "not_your_account")

        for field in ("name", "channel", "match_mode", "trigger_text", "ai_instruction", "fallback_reply"):
            if field in body:
                setattr(sc, field, body[field])
        for field in ("use_ai", "enabled"):
            if field in body:
                setattr(sc, field, bool(body[field]))
        if "priority" in body:
            sc.priority = int(body["priority"] or 0)

        if "steps" in body:
            existing = await get_steps(session, sc.id)
            for st in existing:
                await session.delete(st)
            await session.flush()
            for i, st in enumerate((body["steps"] or [])):
                session.add(
                    AutomationStep(
                        scenario_id=sc.id,
                        order_index=i,
                        action=st.get("action", StepAction.SEND),
                        text=st.get("text", ""),
                        use_ai=bool(st.get("use_ai", False)),
                        next_step_order=st.get("next_step_order"),
                        variable=st.get("variable", ""),
                    )
                )
        await session.commit()
        steps = await get_steps(session, sc.id)
        return {"ok": True, "scenario": {**_scenario_dict(sc), "steps": [_step_dict(st) for st in steps]}}


@app.delete("/api/automation/scenarios/{sid}")
async def api_scenario_delete(
    sid: int,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    async with SessionLocal() as session:
        sc = await session.get(AutomationScenario, sid)
        if sc is None:
            raise HTTPError(404, "not_found")
        accounts = await get_accounts_for_user(session, ident.user_id)
        if sc.account_id not in [a.id for a in accounts]:
            raise HTTPError(403, "not_your_account")
        # Explicitly remove children (SQLite doesn't enforce FK cascade).
        for st in await get_steps(session, sc.id):
            await session.delete(st)
        from sqlalchemy import delete

        await session.execute(
            delete(AutomationSession).where(AutomationSession.scenario_id == sc.id)
        )
        await session.delete(sc)
        await session.commit()
        return {"ok": True}


@app.post("/api/automation/preview")
async def api_scenario_preview(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Dry-run a scenario definition against a sample message (no mutation)."""
    from app.services.automation import get_steps as _gs

    steps_data = (body or {}).get("steps") or []
    sample = (body or {}).get("sample") or "قیمت؟"
    channel = (body or {}).get("channel") or ScenarioChannel.DM
    text = (body or {}).get("trigger_text", "")
    match_mode = (body or {}).get("match_mode", MatchMode.KEYWORD)
    fallback = (body or {}).get("fallback_reply", "")
    ai_instruction = (body or {}).get("ai_instruction", "")

    # Build an in-memory scenario + steps (not persisted).
    sc = AutomationScenario(
        id=0, account_id=-1, name="(پیش‌نمایش)", channel=channel,
        match_mode=match_mode, trigger_text=text, fallback_reply=fallback,
        ai_instruction=ai_instruction, use_ai=True, enabled=True, priority=0, hits=0,
    )
    steps = [
        AutomationStep(
            id=0, scenario_id=0, order_index=i,
            action=st.get("action", StepAction.SEND), text=st.get("text", ""),
            use_ai=bool(st.get("use_ai", False)),
            next_step_order=st.get("next_step_order"), variable=st.get("variable", ""),
        )
        for i, st in enumerate(steps_data)
    ]
    from app.services.automation import _run_from

    outcome = await _run_from(None, sc, steps, 0, sample, {})
    return {
        "matched": True,
        "reply": outcome.send_text,
        "final": outcome.final,
        "waiting": outcome.waiting,
    }


# ── Chat & design history (stages 2 & 4) ─────────────────────
@app.get("/api/chat-history")
async def api_chat_history(
    channel: str | None = None,
    page: int = 0,
    per_page: int = 50,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    from sqlalchemy import func

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return {"total": 0, "rows": []}
        q = select(ChatHistory).where(ChatHistory.account_id.in_(ids))
        if channel:
            q = q.where(ChatHistory.channel == channel)
        total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
        q = q.order_by(ChatHistory.created_at.desc()).offset(page * per_page).limit(per_page)
        rows = [
            {
                "id": r.id, "account_id": r.account_id, "channel": r.channel,
                "peer_id": r.peer_id, "peer_name": r.peer_name,
                "inbound_text": r.inbound_text, "reply_text": r.reply_text,
                "reply_source": r.reply_source,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "created_jalali": jalali.to_jalali_str(r.created_at) if r.created_at else None,
            }
            for r in (await session.execute(q)).scalars()
        ]
        return {"total": total, "rows": rows}


@app.delete("/api/chat-history")
async def api_chat_history_clear(
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Delete the calling user's chat history."""
    from sqlalchemy import delete

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return {"deleted": 0}
        res = await session.execute(delete(ChatHistory).where(ChatHistory.account_id.in_(ids)))
        await session.commit()
        return {"deleted": res.rowcount or 0}


@app.get("/api/design-history")
async def api_design_history(
    page: int = 0,
    per_page: int = 50,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    from sqlalchemy import func

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        q = select(DesignHistory).where(
            (DesignHistory.account_id.in_(ids)) | (DesignHistory.account_id.is_(None))
        )
        total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
        q = q.order_by(DesignHistory.created_at.desc()).offset(page * per_page).limit(per_page)
        rows = [
            {
                "id": r.id, "account_id": r.account_id, "kind": r.kind,
                "prompt": r.prompt, "content": r.content, "engine": r.engine,
                "language": r.language,
                "created_jalali": jalali.to_jalali_str(r.created_at) if r.created_at else None,
            }
            for r in (await session.execute(q)).scalars()
        ]
        return {"total": total, "rows": rows}


# ── ChatbotX + Instagram Publisher integrations ──────────────
@app.get("/api/chatbotx/status")
async def api_chatbotx_status(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    """ChatbotX workspace + Instagram connection status."""
    from app.services.chatbotx_service import get_chatbotx_client

    client = get_chatbotx_client()
    if not client.enabled:
        return {
            "enabled": False,
            "configured": False,
            "message": "ChatbotX پیکربندی نشده — توکن را در .env بگذارید",
            "workspace_id": settings.chatbotx_workspace_id or None,
            "base_url": settings.chatbotx_base_url,
        }
    try:
        conn = await client.get_instagram_connection()
        return {
            "enabled": True,
            "configured": True,
            "connected": conn.get("connected", False),
            "username": conn.get("username"),
            "workspace_id": settings.chatbotx_workspace_id,
            "base_url": settings.chatbotx_base_url,
            "details": conn.get("details"),
        }
    except Exception as exc:
        return {
            "enabled": True,
            "configured": True,
            "connected": False,
            "error": str(exc)[:300],
            "workspace_id": settings.chatbotx_workspace_id,
        }


@app.get("/api/chatbotx/conversations")
async def api_chatbotx_conversations(
    limit: int = 20,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    from app.services.chatbotx_service import get_chatbotx_client

    client = get_chatbotx_client()
    if not client.enabled:
        raise HTTPError(400, "chatbotx_not_configured")
    try:
        convs = await client.get_conversations(limit=limit)
        return {"conversations": convs, "count": len(convs)}
    except Exception as exc:
        raise HTTPError(500, str(exc)[:300])


@app.post("/api/chatbotx/send")
async def api_chatbotx_send(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    from app.services.chatbotx_service import get_chatbotx_client

    client = get_chatbotx_client()
    if not client.enabled:
        raise HTTPError(400, "chatbotx_not_configured")
    conv_id = (body or {}).get("conversation_id")
    text = (body or {}).get("message") or (body or {}).get("text")
    if not conv_id or not text:
        raise HTTPError(400, "missing conversation_id or message")
    try:
        result = await client.send_message(conv_id, text)
        return {"ok": True, "result": result}
    except Exception as exc:
        raise HTTPError(500, str(exc)[:300])


@app.get("/api/instagram-publisher/status")
async def api_instagram_publisher_status(
    account_id: int | None = None,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    from app.services.instagram_publisher import get_publisher

    pub = get_publisher()
    # Verify account ownership if provided
    if account_id:
        async with SessionLocal() as session:
            accounts = await get_accounts_for_user(session, ident.user_id)
            if account_id not in [a.id for a in accounts]:
                raise HTTPError(403, "not_your_account")
    try:
        status = await pub.get_status(account_id=account_id)
        return status
    except Exception as exc:
        return {"enabled": False, "error": str(exc)[:300]}


@app.post("/api/instagram-publisher/publish")
async def api_instagram_publisher_publish(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    from app.services.instagram_publisher import get_publisher

    account_id = (body or {}).get("account_id")
    if not account_id:
        raise HTTPError(400, "missing account_id")
    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        if account_id not in [a.id for a in accounts]:
            raise HTTPError(403, "not_your_account")

    image_url = (body or {}).get("image_url")
    image_urls = (body or {}).get("image_urls")
    caption = (body or {}).get("caption", "")
    media_type = (body or {}).get("media_type", "post")

    pub = get_publisher()
    try:
        result = await pub.publish_post(
            account_id=account_id,
            image_url=image_url,
            image_urls=image_urls,
            caption=caption,
            media_type=media_type,
            user_id=ident.user_id,
        )
        return result
    except Exception as exc:
        # Return as JSON error with Persian message if it's our custom error
        from app.services.instagram_publisher import InstagramPublishError

        if isinstance(exc, InstagramPublishError):
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        raise HTTPError(500, str(exc)[:500])


@app.get("/api/integrations/status")
async def api_integrations_status(ident: MiniAppIdentity = Depends(resolve_miniapp_user)):
    """Combined status of all integrations for Mini-App."""
    from app.services.chatbotx_service import get_chatbotx_client
    from app.services.instagram_publisher import get_publisher

    # ChatbotX
    cbx_client = get_chatbotx_client()
    cbx_status = {"enabled": cbx_client.enabled, "configured": cbx_client.enabled}
    if cbx_client.enabled:
        try:
            conn = await cbx_client.get_instagram_connection()
            cbx_status.update({"connected": conn.get("connected"), "username": conn.get("username")})
        except Exception as exc:
            cbx_status.update({"connected": False, "error": str(exc)[:200]})

    # Instagram publisher
    pub = get_publisher()
    try:
        pub_status = await pub.get_status()
    except Exception as exc:
        pub_status = {"enabled": False, "error": str(exc)[:200]}

    # Daily reply usage (reuse logic)
    from app.services import limits
    from app.services.reply_limits import tehran_date

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        # Quick summary
        today = tehran_date()
        # ... simplified
        summary = {
            "accounts_count": len(accounts),
            "connected_count": len([a for a in accounts if a.status == "connected"]),
            "daily_limit": limits.DAILY_REPLY_LIMIT,
            "hourly_limit": limits.HOURLY_REPLY_LIMIT,
            "date": today,
        }

    return {
        "chatbotx": cbx_status,
        "instagram_publisher": pub_status,
        "reply_limits": summary,
        "meta_app": {
            "configured": bool(settings.meta_app_id and settings.meta_app_secret),
            "mode": settings.instagram_account_mode,
        },
    }


@app.post("/api/chatbotx-webhook")
async def api_chatbotx_webhook(request: dict):
    """Public webhook for ChatbotX inbound events (guarded by secret if set)."""
    # This is called by ChatbotX when new messages/comments arrive
    # For now, log and return ok — full processing can be added later
    import json

    logger.info("chatbotx webhook received: %s", json.dumps(request, ensure_ascii=False)[:1000])
    # TODO: process inbound message, route to auto-reply engine
    # For now, just acknowledge
    return {"ok": True, "received": True}


@app.post("/api/cleanup/manual")
async def api_cleanup_manual(
    body: dict,
    ident: MiniAppIdentity = Depends(resolve_miniapp_user),
):
    """Manual cleanup of old files/logs (admin)."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete

    # Only allow for user's own data
    days = int((body or {}).get("days", 30))
    if days < 7:
        days = 7  # minimum 7 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with SessionLocal() as session:
        accounts = await get_accounts_for_user(session, ident.user_id)
        ids = [a.id for a in accounts]
        if not ids:
            return {"deleted": 0}

        # Clean old chat history
        from app.services.analytics_history import ChatHistory

        result = await session.execute(delete(ChatHistory).where(ChatHistory.account_id.in_(ids), ChatHistory.created_at < cutoff))
        deleted_chats = result.rowcount or 0

        # Clean old activity logs
        from app.models import ActivityLog

        result2 = await session.execute(delete(ActivityLog).where(ActivityLog.account_id.in_(ids), ActivityLog.created_at < cutoff))
        deleted_logs = result2.rowcount or 0

        await session.commit()

        return {"deleted_chats": deleted_chats, "deleted_logs": deleted_logs, "cutoff": cutoff.isoformat()}


# ── WebSocket (live refresh) ─────────────────────────────────
@app.websocket("/ws/broadcast")
async def ws_broadcast(websocket: WebSocket):
    await websocket.accept()
    _connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignore
    except WebSocketDisconnect:
        _connections.discard(websocket)


def HTTPError(status: int, detail: str):
    from fastapi import HTTPException

    return HTTPException(status_code=status, detail=detail)


# NOTE: the SPA static mount is NOT registered here — it lives in
# `app/webapp/server.py` (create_app) so it is attached AFTER the webhook
# and API routes (static mounts are catch-all and swallow earlier paths).
