"""Gramma — one-shot idempotent migration for the reply-hardening phase.

Applies to the Supabase project (anpffdyooizfmqqouyvs / gramma-bot):

  1. the three new tables (comment_reply_queue, daily_reply_counters,
     safety_alerts) with RLS enabled and no data loss (IF NOT EXISTS);
  2. the ``auto_replies.dm_followup`` column (DM follow-up after a comment
     reply) — added only when absent;
  3. pg_cron jobs for the background ticks:
       * process-comment-queue    every 5 minutes
       * check-instagram-health   every 10 minutes
       * reset-daily-counters     every day at 00:00 Asia/Tehran
       * daily-report             every day at 23:59 Asia/Tehran
  4. installs the pg_net extension (fixes the pre-existing
     gramma_publish_due job that has been failing with
     ``schema "net" does not exist`` every minute).

All timestamps are stored in UTC and the two Tehran-boundary jobs are
expressed directly in UTC because Iran has no DST:

  * Tehran 00:00 == 20:30 UTC  →  ``30 20 * * *``
  * Tehran 23:59 == 20:29 UTC  →  ``29 20 * * *``

Safe to re-run: every statement is idempotent (IF NOT EXISTS / column guard
/ cron.unschedule-then-schedule).
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

STATEMENTS: list[str] = []

# ── 1) Extension that makes ALL pg_cron HTTP jobs work ───────
STATEMENTS += [
    "CREATE EXTENSION IF NOT EXISTS pg_net;",
]

# ── 2) Table: comment_reply_queue ────────────────────────────
STATEMENTS += [
    """
    CREATE TABLE IF NOT EXISTS public.comment_reply_queue (
        id BIGSERIAL PRIMARY KEY,
        account_id BIGINT NOT NULL,
        comment_id VARCHAR(64) NOT NULL DEFAULT '',
        comment_text TEXT NOT NULL DEFAULT '',
        auto_reply_id BIGINT,
        dm_followup_required BOOLEAN NOT NULL DEFAULT FALSE,
        dm_message TEXT NOT NULL DEFAULT '',
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        retry_count INTEGER NOT NULL DEFAULT 0,
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        scheduled_for TIMESTAMPTZ,
        processed_at TIMESTAMPTZ
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_comment_reply_queue_account_status "
    "ON public.comment_reply_queue (account_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_comment_reply_queue_id "
    "ON public.comment_reply_queue (id);",
    "ALTER TABLE public.comment_reply_queue ENABLE ROW LEVEL SECURITY;",
]

# ── 3) Table: daily_reply_counters ───────────────────────────
STATEMENTS += [
    """
    CREATE TABLE IF NOT EXISTS public.daily_reply_counters (
        id BIGSERIAL PRIMARY KEY,
        account_id BIGINT NOT NULL,
        date VARCHAR(10) NOT NULL DEFAULT '',
        replies_count INTEGER NOT NULL DEFAULT 0,
        hourly_count INTEGER NOT NULL DEFAULT 0,
        hourly_window_start TIMESTAMPTZ,
        rate_profile VARCHAR(16) NOT NULL DEFAULT 'medium',
        rest_at_action INTEGER NOT NULL DEFAULT 0,
        last_action_at TIMESTAMPTZ,
        error_count INTEGER NOT NULL DEFAULT 0,
        backoff_until TIMESTAMPTZ,
        backoff_runs INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_daily_reply_account_date UNIQUE (account_id, date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_reply_counters_account_date "
    "ON public.daily_reply_counters (account_id, date);",
    "ALTER TABLE public.daily_reply_counters ENABLE ROW LEVEL SECURITY;",
]

# ── 4) Table: safety_alerts ──────────────────────────────────
STATEMENTS += [
    """
    CREATE TABLE IF NOT EXISTS public.safety_alerts (
        id BIGSERIAL PRIMARY KEY,
        account_id BIGINT,
        alert_type VARCHAR(48) NOT NULL DEFAULT 'info',
        message TEXT NOT NULL DEFAULT '',
        raw_error JSONB,
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_safety_alerts_account_resolved "
    "ON public.safety_alerts (account_id, resolved);",
    "ALTER TABLE public.safety_alerts ENABLE ROW LEVEL SECURITY;",
]

# ── 5) Column: auto_replies.dm_followup (DM after comment reply) ──
STATEMENTS += [
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'auto_replies'
              AND column_name = 'dm_followup'
        ) THEN
            ALTER TABLE public.auto_replies
                ADD COLUMN dm_followup TEXT NOT NULL DEFAULT '';
        END IF;
    END $$;
    """,
]

# ── 6) pg_cron jobs (idempotent unschedule → schedule) ────────
# Helper SQL: the CRON_SECRET and target URL are exported from the env.
_cron_install = [
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
            CREATE EXTENSION pg_cron;
        END IF;
    END $$;
    """,
]


def cron_job(name: str, schedule: str, url: str, secret: str, body: dict) -> list[str]:
    import json

    _ = body
    payload = json.dumps(body)
    command = (
        "SELECT net.http_post("
        f"url := '{url}'"
        f", headers := jsonb_build_object('content-type','application/json','x-cron-secret','{secret}')"
        f", body := '{payload}'::jsonb"
        ", timeout_milliseconds := 30000);"
    )
    return [
        f"SELECT cron.unschedule('{name}') WHERE EXISTS "
        f"(SELECT 1 FROM cron.job WHERE jobname = '{name}');",
        f"SELECT cron.schedule('{name}', '{schedule}', $$ {command} $$);",
    ]


def build_all() -> list[str]:
    target = os.environ.get("CRON_TARGET_URL", "https://miniapp-five-inky.vercel.app")
    secret = os.environ.get("CRON_SECRET", "")
    stmts = list(STATEMENTS) + _cron_install
    # Every 5 min → process the comment-reply queue.
    stmts += cron_job(
        "gramma_process_comment_queue", "*/5 * * * *",
        f"{target}/api/cron/process-comment-queue", secret, {"job": "process-comment-queue"},
    )
    # Every 10 min → health check.
    stmts += cron_job(
        "gramma_check_instagram_health", "*/10 * * * *",
        f"{target}/api/cron/check-instagram-health", secret, {"job": "check-instagram-health"},
    )
    # Daily reset at Tehran midnight. Iran is fixed UTC+3:30 (no DST), so
    # 00:00 Asia/Tehran == 20:30 UTC every day of the year.
    stmts += cron_job(
        "gramma_reset_daily_counters", "30 20 * * *",
        f"{target}/api/cron/reset-daily-counters", secret, {"job": "reset-daily-counters"},
    )
    # Daily admin report at 23:59 Tehran == 20:29 UTC.
    stmts += cron_job(
        "gramma_daily_report", "29 20 * * *",
        f"{target}/api/cron/daily-report", secret, {"job": "daily-report"},
    )
    # Re-schedule the pre-existing minute publish job once pg_net exists, so
    # its embedded x-cron-secret matches today's CRON_SECRET.
    stmts += cron_job(
        "gramma_publish_due", "* * * * *",
        f"{target}/api/cron/publish", secret, {"job": "publish"},
    )
    # Keep-alive: ping /healthz every 5 min so the serverless function stays
    # warm (Vercel Hobby cold starts are otherwise frequent after inactivity).
    keepalive = (
        "SELECT net.http_get("
        f"url := '{target}/healthz'"
        ", timeout_milliseconds := 20000);"
    )
    stmts += [
        "SELECT cron.unschedule('gramma_healthz_keepalive') WHERE EXISTS "
        "(SELECT 1 FROM cron.job WHERE jobname = 'gramma_healthz_keepalive');",
        f"SELECT cron.schedule('gramma_healthz_keepalive', '*/5 * * * *', $$ {keepalive} $$);",
    ]
    return stmts


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


async def apply() -> None:
    _load_env()
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg", "postgresql")
    conn = await asyncpg.connect(url, statement_cache_size=0)
    stmts = build_all()
    print(f"applying {len(stmts)} statements …")
    for i, stmt in enumerate(stmts, 1):
        # Replace the outer $$..$$ escaping used for cron args with a
        # dollar-quote that survives asyncpg statement splitting.
        clean = stmt.strip()
        try:
            await conn.execute(clean)
            print(f"  [{i}/{len(stmts)}] ok")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(stmts)}] FAILED: {str(exc)[:160]}")
    jobs = await conn.fetch(
        "SELECT jobid, jobname, schedule, active FROM cron.job WHERE jobname LIKE 'gramma_%' ORDER BY jobname"
    )
    print("\npg_cron jobs now:")
    for j in jobs:
        print("   ", dict(j))
    await conn.close()


if __name__ == "__main__":
    asyncio.run(apply())
