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
        await session.commit()
        return {"ok": True, "id": rule.id}


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
