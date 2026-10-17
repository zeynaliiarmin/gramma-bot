"""Install ONLY the approved post worker functions and five-minute pg_cron job.

Run after code review, against the existing permitted Supabase project:
    .venv/bin/python scripts/install_zernio_post_queue.py --migrate
    # Deploy this Git commit to the existing Vercel project first.
    .venv/bin/python scripts/install_zernio_post_queue.py --install-cron

Direct Postgres may be unreachable from a restricted workstation, so this
uses Supabase's existing Management SQL API (not any publishing endpoint).
The cron credential comes from the ignored .env, is stored encrypted in Vault,
and is referenced by NAME in cron.job; neither SQL nor errors are printed.
Never run --install-cron before the matching Production deployment is READY.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REF = "anpffdyooizfmqqouyvs"
VAULT_NAME = "gramma_outbound_post_cron_secret"
CRON_NAME = "gramma_zernio_outbound_post"
ENDPOINT = "https://miniapp-five-inky.vercel.app/api/cron/outbound-post"


def _quote(value: str) -> str:
    """SQL literal from a private in-memory string; never print its result."""
    return "'" + value.replace("'", "''") + "'"


def _query(client: httpx.Client, ref: str, token: str, sql: str) -> list[dict]:
    response = client.post(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": sql},
    )
    if response.status_code not in (200, 201):
        # Management errors can contain SQL text, including the secret.
        raise RuntimeError(f"Supabase SQL request failed with HTTP {response.status_code}; response withheld")
    result = response.json()
    return result if isinstance(result, list) else []


def _install_migration(client: httpx.Client, ref: str, token: str) -> None:
    sql = (ROOT / "scripts" / "migrate_zernio_post_worker.sql").read_text(encoding="utf-8")
    _query(client, ref, token, "BEGIN;\n" + sql + "\nCOMMIT;")
    rows = _query(client, ref, token, """
        SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
         WHERE n.nspname='public' AND p.proname IN
             ('claim_next_zernio_post_job', 'reserve_zernio_post_slot')
         ORDER BY proname
    """)
    if {r["proname"] for r in rows} != {"claim_next_zernio_post_job", "reserve_zernio_post_slot"}:
        raise RuntimeError("Post worker functions were not visible after migration")
    print("Post-only claim and atomic page-rate functions are installed; no queue jobs were run.")


def _install_cron(client: httpx.Client, ref: str, token: str, cron_secret: str) -> None:
    if not cron_secret or len(cron_secret) < 16:
        raise RuntimeError("Missing/short CRON_SECRET; refusing to schedule publishing")
    # The existing jobs and Vercel use this exact private cron credential.
    # Compare inside SQL and expose only boolean counts, never job commands.
    checks = _query(client, ref, token, f"""
        SELECT count(*) AS known_jobs,
               count(*) FILTER (WHERE position({_quote(cron_secret)} IN command)>0) AS matching_jobs
          FROM cron.job
         WHERE active AND jobname IN ('gramma_publish_due', 'gramma_process_comment_queue')
    """)
    if not checks or checks[0]["known_jobs"] != 2 or checks[0]["matching_jobs"] != 2:
        raise RuntimeError("Local cron credential does not match existing scheduled jobs")

    command = f"""
        SELECT net.http_post(
            url := '{ENDPOINT}',
            headers := jsonb_build_object(
                'Content-Type', 'application/json',
                'X-Cron-Secret',
                    (SELECT decrypted_secret FROM vault.decrypted_secrets
                     WHERE name = '{VAULT_NAME}' LIMIT 1)
            ),
            body := '{{}}'::jsonb,
            timeout_milliseconds := 120000
        );
    """
    # One database transaction: Vault creation/verification and cron
    # registration either both commit, or neither does. The secret appears
    # only in this TLS-protected setup query, never in cron.job's command.
    sql = f"""
        BEGIN;
        DO $setup$
        BEGIN
            IF EXISTS (SELECT 1 FROM vault.secrets WHERE name = '{VAULT_NAME}') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM vault.decrypted_secrets
                    WHERE name = '{VAULT_NAME}'
                      AND decrypted_secret = {_quote(cron_secret)}
                ) THEN
                    RAISE EXCEPTION 'Vault secret mismatch; refusing to rotate it implicitly';
                END IF;
            ELSE
                PERFORM vault.create_secret(
                    {_quote(cron_secret)}, '{VAULT_NAME}',
                    'Dedicated credential reference for the Zernio post cron job'
                );
            END IF;
        END $setup$;
        SELECT cron.unschedule('{CRON_NAME}')
         WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = '{CRON_NAME}');
        SELECT cron.schedule('{CRON_NAME}', '*/5 * * * *', $job$
{command}
        $job$);
        COMMIT;
    """
    _query(client, ref, token, sql)
    rows = _query(client, ref, token, f"""
        SELECT jobname, schedule, active,
               position('{VAULT_NAME}' IN command)>0 AS uses_vault,
               position({_quote(cron_secret)} IN command)=0 AS omits_plaintext
          FROM cron.job WHERE jobname='{CRON_NAME}'
    """)
    if len(rows) != 1 or rows[0]["schedule"] != "*/5 * * * *" or not all(
        rows[0][field] for field in ("active", "uses_vault", "omits_plaintext")
    ):
        raise RuntimeError("Post cron registration was not verified; inspect safe metadata only")
    print("Post cron active every 5 minutes, authenticated from Vault; job command contains no plaintext credential.")
    print("The installer did not call any worker or publish endpoint.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--migrate", action="store_true", help="add post-only worker functions")
    group.add_argument("--install-cron", action="store_true", help="schedule the approved 5-minute job")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    ref = os.getenv("SUPABASE_PROJECT_REF", "")
    token = os.getenv("SUPABASE_PAT", "")
    if ref != EXPECTED_REF or not token:
        print("Supabase project scope or Management token is missing; no changes made.")
        return 2
    try:
        with httpx.Client(timeout=90.0) as client:
            if args.migrate:
                _install_migration(client, ref, token)
            else:
                _install_cron(client, ref, token, os.getenv("CRON_SECRET", ""))
    except Exception as exc:  # noqa: BLE001
        # DO NOT print str(exc): it might contain SQL or a URL with secrets.
        print(f"Installation did not complete safely ({type(exc).__name__}); check permitted metadata.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
