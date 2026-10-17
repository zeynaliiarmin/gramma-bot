-- Owner-approved Phase 4 activation. Schedule ONLY the ChatbotX inbox Worker.
-- Does not alter jobs 14/15/16, send a message, run the Worker, or print secrets.
-- The command references the pre-existing encrypted Vault credential by name;
-- CRON_SECRET is never embedded in cron.job or Git. Run only after the
-- additive inference-lease migration and Production flag are configured.

DO $guard$
BEGIN
    IF to_regclass('public.inbox_messages') IS NULL
       OR to_regclass('public.auto_reply_slots') IS NULL THEN
        RAISE EXCEPTION 'Phase 4 private queue not installed';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'inbox_messages'
           AND column_name = 'claim_version'
    ) THEN
        RAISE EXCEPTION 'Phase 4 inference lease migration is required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM vault.secrets
         WHERE name = 'gramma_outbound_post_cron_secret'
    ) THEN
        RAISE EXCEPTION 'Existing Vault-backed cron credential is missing';
    END IF;
    IF EXISTS (
        SELECT 1 FROM cron.job
         WHERE jobname = 'gramma_chatbotx_auto_replies'
    ) THEN
        RAISE EXCEPTION 'Auto-reply job already exists; refusing duplicate schedule';
    END IF;
END $guard$;

SELECT cron.schedule(
    'gramma_chatbotx_auto_replies', '* * * * *',
    $job$
    SELECT net.http_post(
        url := 'https://miniapp-five-inky.vercel.app/api/cron/auto-replies',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Cron-Secret',
                (SELECT decrypted_secret FROM vault.decrypted_secrets
                  WHERE name = 'gramma_outbound_post_cron_secret' LIMIT 1)
        ),
        body := '{}'::jsonb,
        timeout_milliseconds := 240000
    );
    $job$
);
