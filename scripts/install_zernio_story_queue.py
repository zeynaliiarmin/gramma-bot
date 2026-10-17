"""Install ONLY additive Story FSM/queue schema and a NEW minute Story job.

Run --migrate before deploying matching code; --install-cron only after the
matching production deployment is READY. This tool NEVER invokes a Worker,
publishes, prints credentials, or alters the post/album cron jobs. It uses the
existing project's Management SQL API and the ignored .env.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REF = "anpffdyooizfmqqouyvs"
CRON_NAME = "gramma_zernio_outbound_story"
VAULT_NAME = "gramma_outbound_post_cron_secret"  # existing matching shared CRON_SECRET
ENDPOINT = "https://miniapp-five-inky.vercel.app/api/cron/outbound-story"
EXISTING = {"gramma_zernio_outbound_post": "*/5 * * * *",
            "gramma_telegram_albums_only": "* * * * *"}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _query(client: httpx.Client, token: str, sql: str) -> list[dict]:
    response = client.post(
        f"https://api.supabase.com/v1/projects/{EXPECTED_REF}/database/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": sql},
    )
    if response.status_code not in (200, 201):
        # The server may echo SQL/secret values; NEVER show its response body.
        raise RuntimeError(f"Scoped SQL request failed (HTTP {response.status_code}); response withheld")
    result = response.json()
    return result if isinstance(result, list) else []


def _check_existing(client: httpx.Client, token: str) -> dict[str, int]:
    rows = _query(client, token, """
        SELECT jobid, jobname, schedule, active FROM cron.job
         WHERE jobname IN ('gramma_zernio_outbound_post',
                           'gramma_telegram_albums_only')
    """)
    found = {row["jobname"]: row for row in rows}
    if (set(found) != set(EXISTING)
            or any(not found[name]["active"] or found[name]["schedule"] != cadence
                   for name, cadence in EXISTING.items())):
        raise RuntimeError("Existing publication/album cron metadata is not in the approved state")
    return {name: row["jobid"] for name, row in found.items()}


def _migrate(client: httpx.Client, token: str) -> None:
    sql = (ROOT / "scripts" / "migrate_story_schedule_cleanup.sql").read_text(encoding="utf-8")
    _query(client, token, "BEGIN;\n" + sql + "\nCOMMIT;")
    rows = _query(client, token, """
        SELECT to_regclass('public.telegram_story_sequences') IS NOT NULL AS sequences,
               to_regclass('public.telegram_story_slides') IS NOT NULL AS slides,
               (SELECT relrowsecurity FROM pg_class
                 WHERE oid = to_regclass('public.telegram_story_sequences')) AS private_fsm,
               (SELECT relrowsecurity FROM pg_class
                 WHERE oid = to_regclass('public.telegram_story_slides')) AS private_media,
               EXISTS(SELECT 1 FROM information_schema.columns
                       WHERE table_schema='public' AND table_name='telegram_post_drafts'
                         AND column_name='scheduled_for') AS scheduling
    """)
    if len(rows) != 1 or not all(rows[0].values()):
        raise RuntimeError("Story migration/RLS/scheduling not visible after DDL")
    print("Additive Story and scheduling schema installed; no Worker or provider called.")


def _install_cron(client: httpx.Client, token: str, cron_secret: str) -> None:
    if len(cron_secret) < 16:
        raise RuntimeError("CRON_SECRET missing/short; no cron change made")
    original = _check_existing(client, token)
    schema = _query(client, token, """
        SELECT to_regprocedure('public.claim_next_zernio_story_job()') IS NOT NULL AS installed
    """)
    if len(schema) != 1 or not schema[0]["installed"]:
        raise RuntimeError("Story-only claim function missing; no cron change made")
    vault = _query(client, token, f"""
        SELECT EXISTS(SELECT 1 FROM vault.decrypted_secrets
                      WHERE name = '{VAULT_NAME}'
                        AND decrypted_secret = {_quote(cron_secret)}) AS matches
    """)
    if len(vault) != 1 or vault[0]["matches"] is not True:
        raise RuntimeError("Existing Vault CRON_SECRET mismatch; refusing to rotate or schedule")
    existing_story = _query(client, token, f"""
        SELECT jobid, active, schedule,
               position('{VAULT_NAME}' IN command)>0 AS vault_backed,
               position('{ENDPOINT}' IN command)>0 AS correct_endpoint,
               position({_quote(cron_secret)} IN command)=0 AS no_plaintext
          FROM cron.job WHERE jobname = '{CRON_NAME}'
    """)
    if existing_story:
        row = existing_story[0]
        if len(existing_story) != 1 or row["schedule"] != "* * * * *" or not all(
            row[field] for field in ("active", "vault_backed", "correct_endpoint", "no_plaintext")
        ):
            raise RuntimeError("Existing Story cron differs; refusing to overwrite it")
    else:
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
        _query(client, token, f"""
            SELECT cron.schedule('{CRON_NAME}', '* * * * *', $job$
{command}
            $job$)
        """)
    verified = _query(client, token, f"""
        SELECT jobid, active, schedule,
               position('{VAULT_NAME}' IN command)>0 AS vault_backed,
               position('{ENDPOINT}' IN command)>0 AS correct_endpoint,
               position({_quote(cron_secret)} IN command)=0 AS no_plaintext
          FROM cron.job WHERE jobname = '{CRON_NAME}'
    """)
    after = _check_existing(client, token)
    if (after != original or len(verified) != 1 or verified[0]["schedule"] != "* * * * *"
            or not all(verified[0][key] for key in
                       ("active", "vault_backed", "correct_endpoint", "no_plaintext"))):
        raise RuntimeError("Story cron verification failed; original jobs were not modified by installer")
    print("Protected, separate Story-only minute cron active; original two job IDs/cadences unchanged.")
    print("No Worker was invoked and no Telegram/provider test was performed by this installer.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--migrate", action="store_true")
    group.add_argument("--install-cron", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if os.getenv("SUPABASE_PROJECT_REF") != EXPECTED_REF or not os.getenv("SUPABASE_PAT"):
        print("Permitted project ref or SQL credential missing; no changes made.")
        return 2
    try:
        with httpx.Client(timeout=90.0) as client:
            if args.migrate:
                _migrate(client, os.environ["SUPABASE_PAT"])
            else:
                _install_cron(client, os.environ["SUPABASE_PAT"], os.getenv("CRON_SECRET", ""))
    except Exception as exc:  # noqa: BLE001
        # Exception text or provider response might contain SQL/credential data.
        print(f"Story setup did not complete safely ({type(exc).__name__}); inspect private metadata.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
