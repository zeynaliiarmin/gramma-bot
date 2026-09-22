"""Serverless migration tests: webhook entrypoint, secrets, and Supabase DDL.

These pin down the new v4 surface without needing real credentials:

  * the Vercel ASGI entrypoint (api/index.py) imports and answers /healthz;
  * the Telegram webhook route: 200 for supported updates, 403 for a wrong
    secret header / wrong bot token, 200 for foreign-bot-shaped content;
  * the pg_cron callback route honours CRON_SECRET and 404s unknown jobs;
  * build_ddl_sql compiles every model to valid Postgres with correct
    identity types and no dangling commas;
  * RUN_MODE defaults to "webhook".
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "zeynalikid_admin_bot")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_serverless.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")


def _make_client(monkeypatch, *, secret: str = "", cron_secret: str = ""):
    # Mutate the *same* cached Settings object every module already holds a
    # reference to (never cache_clear — that would orphan other modules'
    # import-time references and break cross-test isolation).
    from app.core.config import get_settings

    settings = get_settings()
    settings.run_mode = "webhook"
    settings.telegram_webhook_secret = secret
    settings.cron_secret = cron_secret
    if not settings.miniapp_public_url:
        settings.miniapp_public_url = "https://miniapp-five-inky.vercel.app"

    # Each test gets a clean per-process dedupe LRU (production keeps it warm
    # per Lambda container; tests must not leak update_ids into each other).
    import app.core.shared_state as shared

    shared.seen_update_ids._lru.clear()

    import api.index as apimod
    from starlette.testclient import TestClient

    return TestClient(apimod.app)


def _start_update(user_id: int):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "is_bot": False, "first_name": "A"},
            "chat": {"id": user_id, "type": "private", "first_name": "A"},
            "date": 1720000000,
            "text": "/start",
        },
    }


def test_run_mode_defaults_to_webhook():
    from app.core.config import get_settings

    assert get_settings().run_mode == "webhook"


def test_entrypoint_imports_and_healthz(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _make_client(monkeypatch)
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"


def test_telegram_webhook_200_without_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _make_client(monkeypatch)
    r = client.post("/api/telegram/webhook", json=_start_update(495432021))
    assert r.status_code == 200


def test_telegram_webhook_403_wrong_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _make_client(monkeypatch, secret="correct-secret")
    r = client.post(
        "/api/telegram/webhook",
        json=_start_update(495432021),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert r.status_code == 403


def test_telegram_webhook_200_right_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _make_client(monkeypatch, secret="correct-secret")
    r = client.post(
        "/api/telegram/webhook",
        json=_start_update(495432021),
        headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
    )
    assert r.status_code == 200


def test_telegram_webhook_rejects_foreign_bot_token(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _make_client(monkeypatch)
    foreign = _start_update(495432021)
    foreign["token"] = "111:OTHERBOT"
    r = client.post("/api/telegram/webhook", json=foreign)
    assert r.status_code == 403


def test_webhook_updates_counter(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _make_client(monkeypatch)
    import app.core.shared_state as shared

    before = shared.telegram_updates_seen
    client.post("/api/telegram/webhook", json=_start_update(495432021))
    assert shared.telegram_updates_seen == before + 1


def test_webhook_dedupes_repeated_update_id(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _make_client(monkeypatch)
    import app.core.shared_state as shared

    before = shared.telegram_updates_seen
    body = {"update_id": 999001, "message": {
        "message_id": 99, "from": {"id": 495432021, "is_bot": False, "first_name": "A"},
        "chat": {"id": 495432021, "type": "private", "first_name": "A"},
        "date": 1720000000, "text": "/start"}}
    r1 = client.post("/api/telegram/webhook", json=body)
    r2 = client.post("/api/telegram/webhook", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    assert shared.telegram_updates_seen == before + 1  # counted once


def test_cron_route_requires_secret(monkeypatch, tmp_path):
    import asyncio

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def _prep():
        from app.core.database import init_db

        await init_db()

    asyncio.run(_prep())

    client = _make_client(monkeypatch, cron_secret="s3cr3t")
    r = client.post("/api/cron/publish")
    assert r.status_code == 403
    r2 = client.post(
        "/api/cron/publish", headers={"X-Cron-Secret": "s3cr3t"}
    )
    # No due posts in an empty DB → summary with zero published.
    assert r2.status_code == 200
    assert r2.json().get("published") == 0


def test_cron_route_unknown_job(monkeypatch):
    client = _make_client(monkeypatch)
    r = client.post("/api/cron/other")
    assert r.status_code == 404


def test_build_ddl_sql_is_valid_postgres():
    from app.core.supabase import build_ddl_sql, _split_statements

    sql = build_ddl_sql()
    assert "CREATE TABLE users" in sql
    assert "CREATE TABLE post_reminders" in sql
    statements = _split_statements(sql)
    assert len(statements) >= 50
    # The users PK must stay BIGINT (no serial), the rest use BIGSERIAL.
    users_ddl = next(s for s in statements if s.startswith("CREATE TABLE users"))
    assert "\tid BIGINT NOT NULL," in users_ddl
    assert "BIGSERIAL" not in users_ddl
    rem_ddl = next(s for s in statements if s.startswith("CREATE TABLE post_reminders"))
    assert "BIGSERIAL" in rem_ddl
    # No dangling ", \n)" artifacts anywhere.
    assert ",\n)" not in sql
