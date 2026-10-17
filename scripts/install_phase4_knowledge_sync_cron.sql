-- Register ONLY the owner-authorized nightly READ-ONLY knowledge ingestion.
-- pg_cron timezone = GMT on this Supabase project. 22:45 UTC = 02:15 Tehran.
-- Do NOT register until migration + new code are READY. Do not run Worker
-- manually; existing job IDs 14/15/16/17 are left entirely unchanged.
-- No credential value is stored here; Vault supplies the existing cron secret.

DO $guard$
BEGIN
    IF to_regclass('public.manual_reply_candidates') IS NULL OR
       to_regclass('public.bot_sent_logs') IS NULL OR
       to_regclass('public.knowledge_sync_state') IS NULL THEN
        RAISE EXCEPTION 'Private knowledge migration must be installed first';
    END IF;
    IF current_setting('cron.timezone', true) NOT IN ('GMT', 'UTC') THEN
        RAISE EXCEPTION 'Cron timezone changed; check schedule before activating';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM vault.secrets
                   WHERE name = 'gramma_outbound_post_cron_secret') THEN
        RAISE EXCEPTION 'Existing encrypted Vault cron credential is missing';
    END IF;
    IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'gramma_knowledge_nightly_readonly') THEN
        RAISE EXCEPTION 'Knowledge job already registered; refusing a duplicate';
    END IF;
END $guard$;

SELECT cron.schedule(
    'gramma_knowledge_nightly_readonly', '45 22 * * *',
    $job$
    SELECT net.http_post(
        url := 'https://miniapp-five-inky.vercel.app/api/cron/knowledge-sync',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Cron-Secret',
                (SELECT decrypted_secret FROM vault.decrypted_secrets
                 WHERE name = 'gramma_outbound_post_cron_secret' LIMIT 1)
        ),
        body := '{}'::jsonb,
        timeout_milliseconds := 120000
    );
    $job$
);
