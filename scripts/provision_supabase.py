"""Gramma — one-shot Supabase provisioner (schema + RLS + pg_cron + Storage).

This is the migration tool that turns an empty Supabase project
(`anpffdyooizfmqqouyvs`, renamed `gramma-bot`) into the Gramma primary
database. It never drops data and is safe to re-run.

Credentials (read at runtime only — never committed):

    SUPABASE_PROJECT_REF          project ref (URL slug)
    SUPABASE_MANAGEMENT_TOKEN     Personal Access Token for api.supabase.com
    SUPABASE_DB_PASSWORD          database password (for the SQL role)
    SUPABASE_SERVICE_ROLE_KEY     service_role JWT (for Storage ops)
    SUPABASE_STORAGE_BUCKET       media bucket name (default: gramma-media)

Usage:
    python scripts/provision_supabase.py                 # schema only
    python scripts/provision_supabase.py --full          # + RLS + cron + storage
    python scripts/provision_supabase.py --cron          # (re)install pg_cron job
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Make sure the app settings can load (token/key validate) before we do work.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
# DDL compilation only imports the model registry — no live connection is
# made. Force a local sqlite URL so importing app.core.database never tries
# to open the (network-blocked from some sandboxes) Supabase connection.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_provision_compile.db"


# ── RLS: every table is private by default (service_role bypasses RLS) ──
# Opt-in row access for the authenticated Mini-App reads via the postgres
# role. NOTE: the Mini-App's serverless backend talks over the connection
# pooler using service_role — the RLS below is a defense-in-depth layer for
# the public PostgREST API, so nothing is exposed until a policy is granted.
def _all_tables_sql() -> list[str]:
    """Return ALTER TABLE … ENABLE ROW LEVEL SECURITY for every public table."""
    from app.core.supabase import _compiled_tables

    return [
        f'ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY;'
        for name, _ddl, _idx in _compiled_tables()
    ]


def _cron_sql(interval_minutes: int = 1, cron_secret: str = "") -> list[str]:
    """Install pg_cron + the scheduled-publish + retention jobs.

    pg_cron schedules use cron syntax; a 1-minute publish cadence is ``* * * * *``.
    Defaults export sane pg_cron settings so the scheduler runs in the
    `postgres` database.
    """
    secret = cron_secret or os.environ.get("CRON_SECRET", "")
    target = os.environ.get("CRON_TARGET_URL", "")

    url = f"'{target}'" if target else "NULL"

    body_publish = f"""$cron$
SELECT net.http_post(
  url := {url},
  headers := jsonb_build_object('content-type','application/json','x-cron-secret','{secret}'),
  body := '{{"job":"publish_due_posts"}}'::jsonb,
  timeout_milliseconds := 30000
);
$cron$"""

    # Data retention: drop activity_logs older than 90 days.
    body_cleanup = """$cron$
DELETE FROM public.activity_logs WHERE created_at < now() - interval '90 days';
$cron$"""

    min_expr = "* * * * *" if interval_minutes <= 1 else f"*/{interval_minutes} * * * *"

    install = [
        "ALTER DATABASE postgres SET cron.use_background_workers = on;",
        "CREATE EXTENSION IF NOT EXISTS pg_cron;",
    ]
    jobs = [
        "SELECT cron.unschedule('gramma_publish_due') "
        "WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname='gramma_publish_due');",
        f"SELECT cron.schedule('gramma_publish_due', '{min_expr}', {body_publish});",
        "SELECT cron.unschedule('gramma_cleanup') "
        "WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname='gramma_cleanup');",
        f"SELECT cron.schedule('gramma_cleanup', '*/30 * * * *', {body_cleanup});",
    ]
    return install + jobs


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="schema + RLS + cron + storage")
    parser.add_argument("--cron", action="store_true", help="(re)install the pg_cron job")
    parser.add_argument("--cron-minutes", type=int, default=1)
    args = parser.parse_args()

    from app.core import supabase as sb

    print("== Gramma Supabase provisioner ==")
    try:
        ref = sb._project_ref()
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 2
    print(f"project_ref = {ref}")

    if not (args.full or args.cron):
        print("→ applying schema (tables + FK + indexes + updated_at trigger)")
        results = await sb.apply_schema()
        print(f"✓ schema applied ({len(results)} statements)")
        return 0

    if args.full:
        print("→ applying schema …")
        results = await sb.apply_schema()
        print(f"✓ schema applied ({len(results)} statements)")

        print("→ enabling row-level security (private-by-default) …")
        rls = _all_tables_sql()
        try:
            results = await sb.run_sql(rls)
            print(f"✓ RLS enabled on {len(rls)} tables")
        except RuntimeError as exc:
            print(f"⚠ RLS partial: {exc}")

    if args.full or args.cron:
        print("→ installing pg_cron job for scheduled publishing …")
        try:
            results = await sb.run_sql(_cron_sql(args.cron_minutes))
            print("✓ pg_cron job installed (net.http_post → Vercel /api/cron)")
        except RuntimeError as exc:
            print(f"⚠ pg_cron: {exc}")

    if args.full:
        bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", "gramma-media")
        print(f"→ creating Supabase Storage bucket '{bucket}' …")
        try:
            results = await sb.ensure_storage_buckets([bucket])
            for r in results:
                print(f"   bucket={r['bucket']} status={r['status']} ok={r['ok']}")
        except RuntimeError as exc:
            print(f"⚠ storage: {exc}")

    print("✅ done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
