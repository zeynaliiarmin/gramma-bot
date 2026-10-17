"""Add ONLY Telegram media drafts + the separate album/settings minute cron.

Run --migrate before deploying matching code. Run --install-cron ONLY after the
new Git SHA's Vercel Production deployment is READY; do not run either Worker.
Uses the permitted Supabase Management SQL API and an ignored .env credential.
Never prints secrets, SQL, legacy cron commands, or provider data.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REF = "anpffdyooizfmqqouyvs"
VAULT_NAME = "gramma_telegram_album_cron_secret"
CRON_NAME = "gramma_telegram_albums_only"
EXISTING_POST_JOB = "gramma_zernio_outbound_post"
ENDPOINT = "https://miniapp-five-inky.vercel.app/api/cron/telegram-albums"


def _quote(value: str) -> str:
    """SQL literal; its result is never printed or logged."""
    return "'" + value.replace("'", "''") + "'"


def _query(client: httpx.Client, token: str, sql: str) -> list[dict]:
    response = client.post(
        f"https://api.supabase.com/v1/projects/{EXPECTED_REF}/database/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": sql},
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Scoped Supabase SQL request failed (HTTP {response.status_code}); response withheld")
    result = response.json()
    return result if isinstance(result, list) else []


def _migrate(client: httpx.Client, token: str) -> None:
    sql = (ROOT / "scripts" / "migrate_telegram_post_drafts.sql").read_text(encoding="utf-8")
    _query(client, token, "BEGIN;\n" + sql + "\nCOMMIT;")
    rows = _query(client, token, """
        SELECT to_regclass('public.telegram_post_drafts') IS NOT NULL AS installed,
               (SELECT relrowsecurity FROM pg_class
                 WHERE oid = to_regclass('public.telegram_post_drafts')) AS rls
    """)
    if len(rows) != 1 or not rows[0]["installed"] or not rows[0]["rls"]:
        raise RuntimeError("Draft table or RLS not visible after additive migration")
    print("Telegram draft table and RLS installed; no queue or publishing job executed.")


def _install_cron(client: httpx.Client, token: str, cron_secret: str) -> None:
    if len(cron_secret) < 16:
        raise RuntimeError("CRON_SECRET missing or too short; refusing album scheduling")
    rows = _query(client, token, f"""
        SELECT jobid, active, schedule FROM cron.job
         WHERE jobname = '{EXISTING_POST_JOB}'
    """)
    if len(rows) != 1 or not rows[0]["active"] or rows[0]["schedule"] != "*/5 * * * *":
        raise RuntimeError("Existing five-minute post job not active; album job not installed")
    existing_job_id = rows[0]["jobid"]
    present = _query(client, token, """
        SELECT to_regclass('public.telegram_post_drafts') IS NOT NULL AS ready
    """)
    if len(present) != 1 or not present[0]["ready"]:
        raise RuntimeError("Draft migration missing; cron not installed")

    # The scheduled text references only a Vault name, NEVER plaintext.
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
    setup = f"""
        BEGIN;
        DO $album$
        BEGIN
            IF EXISTS (SELECT 1 FROM vault.secrets WHERE name = '{VAULT_NAME}') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM vault.decrypted_secrets
                     WHERE name = '{VAULT_NAME}'
                       AND decrypted_secret = {_quote(cron_secret)}
                ) THEN
                    RAISE EXCEPTION 'Album Vault secret mismatch; no implicit rotation';
                END IF;
            ELSE
                PERFORM vault.create_secret(
                    {_quote(cron_secret)}, '{VAULT_NAME}',
                    'Dedicated Vault reference for Telegram album assembly (no publishing)'
                );
            END IF;
        END $album$;
        SELECT cron.unschedule('{CRON_NAME}')
         WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = '{CRON_NAME}');
        SELECT cron.schedule('{CRON_NAME}', '* * * * *', $job$
{command}
        $job$);
        COMMIT;
    """
    _query(client, token, setup)
    verified = _query(client, token, f"""
        SELECT jobname, active, schedule,
               position('{VAULT_NAME}' IN command)>0 AS uses_vault,
               position({_quote(cron_secret)} IN command)=0 AS omits_plaintext,
               position('{ENDPOINT}' IN command)>0 AS correct_endpoint
          FROM cron.job WHERE jobname='{CRON_NAME}'
    """)
    post = _query(client, token, f"""
        SELECT jobid, active, schedule FROM cron.job
         WHERE jobname = '{EXISTING_POST_JOB}'
    """)
    if (len(verified) != 1 or verified[0]["schedule"] != "* * * * *"
            or not all(verified[0][key] for key in
                       ("active", "uses_vault", "omits_plaintext", "correct_endpoint"))
            or len(post) != 1 or post[0]["jobid"] != existing_job_id
            or not post[0]["active"] or post[0]["schedule"] != "*/5 * * * *"):
        raise RuntimeError("Album cron verification failed; inspect safe metadata only")
    print("Separate Vault-backed album/settings cron active every minute; original five-minute post job unchanged.")
    print("No publication Worker, Telegram test message, or Zernio POST was invoked by this installer.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--migrate", action="store_true", help="create private Telegram draft table")
    group.add_argument("--install-cron", action="store_true", help="install separate minute job after READY")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if os.getenv("SUPABASE_PROJECT_REF") != EXPECTED_REF or not os.getenv("SUPABASE_PAT"):
        print("Permitted project ref or Management token missing; no changes made.")
        return 2
    try:
        with httpx.Client(timeout=90.0) as client:
            if args.migrate:
                _migrate(client, os.environ["SUPABASE_PAT"])
            else:
                _install_cron(client, os.environ["SUPABASE_PAT"], os.getenv("CRON_SECRET", ""))
    except Exception as exc:  # noqa: BLE001
        # Never print HTTP/SQL response bodies or exception strings: the
        # transaction sent to Management contains a secret when installing.
        print(f"Album setup failed ({type(exc).__name__}); check private diagnostics. No Worker invoked.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
