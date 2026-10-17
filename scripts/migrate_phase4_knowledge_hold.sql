-- Phase 4 extension: durable LLM hold, private reviewed knowledge and model discovery.
-- Additive only: existing messages, quotas, posts/stories and cron jobs are preserved.
-- Apply before the corresponding application deployment. No provider calls or sends.

ALTER TABLE public.inbox_messages
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS manual_reviewed_at TIMESTAMPTZ;

ALTER TABLE public.inbox_messages DROP CONSTRAINT IF EXISTS inbox_messages_state_check;
ALTER TABLE public.inbox_messages ADD CONSTRAINT inbox_messages_state_check CHECK (
    state IN ('legacy_processing', 'legacy_sent', 'legacy_no_reply',
              'legacy_quota', 'legacy_unknown', 'legacy_failed',
              'unmapped', 'pending', 'generating', 'processing', 'sent',
              'no_reply', 'quota_expired', 'unknown', 'failed',
              'hold', 'manual_review')
);
CREATE INDEX IF NOT EXISTS ix_inbox_hold_scheduled
    ON public.inbox_messages(scheduled_for, id)
    WHERE state = 'hold';

-- Upgrade pre-existing unsent LLM retries without consuming a send slot.
-- If their 24-hour window has elapsed, require manual review instead.
UPDATE public.inbox_messages AS inbox
   SET state = CASE WHEN inbox.received_at <= now() - interval '24 hours'
                    THEN 'manual_review' ELSE 'hold' END,
       error_code = CASE WHEN inbox.received_at <= now() - interval '24 hours'
                    THEN 'reply_window_expired' ELSE 'llm_unavailable' END,
       scheduled_for = CASE WHEN inbox.received_at <= now() - interval '24 hours'
                    THEN NULL ELSE (((now() AT TIME ZONE 'Asia/Tehran')::date + 1)
                        + time '13:00') AT TIME ZONE 'Asia/Tehran' END,
       ready_at = CASE WHEN inbox.received_at <= now() - interval '24 hours'
                    THEN now() ELSE (((now() AT TIME ZONE 'Asia/Tehran')::date + 1)
                        + time '13:00') AT TIME ZONE 'Asia/Tehran' END
 WHERE inbox.source = 'chatbotx' AND inbox.state = 'pending'
   AND inbox.error_code = 'llm_unavailable'
   AND NOT EXISTS (SELECT 1 FROM public.auto_reply_slots AS slot WHERE slot.inbox_id = inbox.id);

-- A committed/uncertain bot send is excluded from candidate learning even if
-- the provider's Instagram echo has no attribution. No raw customer text here.
CREATE TABLE IF NOT EXISTS public.bot_sent_logs (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES public.instagram_accounts(id) ON DELETE CASCADE,
    inbox_id BIGINT UNIQUE REFERENCES public.inbox_messages(id) ON DELETE SET NULL,
    provider VARCHAR(16) NOT NULL,
    channel VARCHAR(16) NOT NULL,
    delivery_status VARCHAR(12) NOT NULL CHECK (delivery_status IN ('sent','unknown')),
    provider_message_id VARCHAR(256),
    platform_message_id VARCHAR(256),
    content_hash CHAR(64) NOT NULL,
    peer_id_hash CHAR(64),
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_sent_provider_message
    ON public.bot_sent_logs(account_id, provider, provider_message_id)
    WHERE provider_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_sent_platform_message
    ON public.bot_sent_logs(account_id, platform_message_id)
    WHERE platform_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_bot_sent_hash_time
    ON public.bot_sent_logs(account_id, channel, content_hash, sent_at);

-- Only OWNER-APPROVED rows (status=approved) are ever exposed to an LLM.
-- Native Instagram echoes and comments lack reliable human attribution:
-- no log match means 'candidate for review', never 'proved human'.
CREATE TABLE IF NOT EXISTS public.manual_reply_candidates (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES public.instagram_accounts(id) ON DELETE CASCADE,
    source VARCHAR(16) NOT NULL,
    source_message_id VARCHAR(256) NOT NULL,
    source_platform_id VARCHAR(256),
    channel VARCHAR(16) NOT NULL CHECK (channel IN ('dm','comment','story_reply')),
    origin VARCHAR(16) NOT NULL CHECK (origin IN ('human','unknown')),
    question_text TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','bot_excluded')),
    style_status VARCHAR(16) NOT NULL DEFAULT 'not_started'
        CHECK (style_status IN ('not_started','pending','ready')),
    fact_approved BOOLEAN NOT NULL DEFAULT false,
    fact_expires_at TIMESTAMPTZ,
    source_created_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    UNIQUE(account_id, source, channel, source_message_id)
);
CREATE INDEX IF NOT EXISTS ix_manual_replies_review
    ON public.manual_reply_candidates(account_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_manual_replies_approved
    ON public.manual_reply_candidates(account_id, channel, approved_at DESC)
    WHERE status = 'approved';

CREATE TABLE IF NOT EXISTS public.page_style_guides (
    account_id BIGINT PRIMARY KEY REFERENCES public.instagram_accounts(id) ON DELETE CASCADE,
    guide JSONB NOT NULL DEFAULT '{}'::jsonb,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.llm_model_catalogues (
    provider VARCHAR(16) PRIMARY KEY,
    models JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(24) NOT NULL DEFAULT 'unknown',
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.llm_model_blacklist (
    provider VARCHAR(16) NOT NULL,
    model_id VARCHAR(180) NOT NULL,
    blocked_until TIMESTAMPTZ NOT NULL,
    reason VARCHAR(24) NOT NULL,
    PRIMARY KEY(provider, model_id)
);

CREATE TABLE IF NOT EXISTS public.knowledge_sync_state (
    account_id BIGINT PRIMARY KEY REFERENCES public.instagram_accounts(id) ON DELETE CASCADE,
    dm_cursor TEXT,
    comments_cursor TEXT,
    last_sync_at TIMESTAMPTZ,
    last_status VARCHAR(32) NOT NULL DEFAULT 'not_started',
    last_error_code VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PostgreSQL backend and service-role only; Mini-App reads through its own
-- HMAC-authenticated, owner-scoped API. No direct anon/authenticated access.
DO $rls$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'bot_sent_logs','manual_reply_candidates','page_style_guides',
        'llm_model_catalogues','llm_model_blacklist','knowledge_sync_state'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', table_name);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon', table_name);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM authenticated', table_name);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO service_role', table_name);
        END IF;
    END LOOP;
END $rls$;
DO $seq$
DECLARE
    sequence_name TEXT;
BEGIN
    FOREACH sequence_name IN ARRAY ARRAY[
        'bot_sent_logs_id_seq','manual_reply_candidates_id_seq'
    ] LOOP
        EXECUTE format('REVOKE ALL ON SEQUENCE public.%I FROM PUBLIC', sequence_name);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
            EXECUTE format('REVOKE ALL ON SEQUENCE public.%I FROM anon', sequence_name);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
            EXECUTE format('REVOKE ALL ON SEQUENCE public.%I FROM authenticated', sequence_name);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
            EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE public.%I TO service_role', sequence_name);
        END IF;
    END LOOP;
END $seq$;
