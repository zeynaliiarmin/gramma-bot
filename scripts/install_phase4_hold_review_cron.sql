-- Nightly DB-only Hold expiry/supersession review. Never invokes a sender.
-- 22:40 UTC = 02:10 Tehran while project cron.timezone remains GMT.
-- Separate from the existing minute auto-replies job (ID 17), which alone
-- evaluates due Holds at 13:00, fresh messages first, with atomic quota.
-- Install after the migration and updated Vercel code are READY.
DO $guard$
BEGIN
    IF to_regclass('public.inbox_messages') IS NULL OR
       to_regclass('public.auto_reply_slots') IS NULL OR
       NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='inbox_messages'
                     AND column_name='hold_attempts') THEN
        RAISE EXCEPTION 'Priority/time-window migration is required';
    END IF;
    IF current_setting('cron.timezone', true) NOT IN ('GMT', 'UTC') THEN
        RAISE EXCEPTION 'Cron timezone changed; check schedule';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM vault.secrets
                   WHERE name='gramma_outbound_post_cron_secret') THEN
        RAISE EXCEPTION 'Existing encrypted Vault cron credential is missing';
    END IF;
    IF EXISTS (SELECT 1 FROM cron.job WHERE jobname='gramma_hold_nightly_review') THEN
        RAISE EXCEPTION 'Hold review job already registered; refusing duplicate';
    END IF;
END $guard$;

SELECT cron.schedule(
    'gramma_hold_nightly_review', '40 22 * * *',
    $job$
    SELECT net.http_post(
        url := 'https://miniapp-five-inky.vercel.app/api/cron/hold-review',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Cron-Secret',
                (SELECT decrypted_secret FROM vault.decrypted_secrets
                 WHERE name='gramma_outbound_post_cron_secret' LIMIT 1)
        ),
        body := '{}'::jsonb,
        timeout_milliseconds := 120000
    );
    $job$
);
