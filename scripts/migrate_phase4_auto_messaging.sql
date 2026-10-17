-- Phase 4: additive private templates, idempotent inbound inbox, shared automatic
-- ChatbotX DM/comment budget. NO seeded answers, outbound trigger, or cron job.
-- Existing auto_replies, chats, posts, Stories, and cron jobs are untouched.

CREATE TABLE IF NOT EXISTS public.reply_templates (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES public.instagram_accounts(id) ON DELETE CASCADE,
    keyword VARCHAR(160) NOT NULL CHECK (length(btrim(keyword)) > 0),
    reply TEXT NOT NULL CHECK (length(btrim(reply)) > 0 AND length(reply) <= 2000),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_reply_templates_account_id
    ON public.reply_templates (account_id, enabled, priority DESC, id);

-- inbox_messages already exists from migrate_zernio_queues.sql. Preserve ALL
-- prior columns, checks, unique(source,external_id), indexes, rows, and grants.
-- Old `status` remains 'ignored' for new ChatbotX rows so an old consumer
-- cannot enqueue them; only the new `state` column drives the Phase 4 worker.
ALTER TABLE public.inbox_messages
    ADD COLUMN IF NOT EXISTS account_id BIGINT REFERENCES public.instagram_accounts(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS event_key VARCHAR(64),
    ADD COLUMN IF NOT EXISTS channel VARCHAR(16) NOT NULL DEFAULT 'dm'
        CHECK (channel IN ('dm', 'comment', 'story_reply', 'story_mention')),
    ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(256) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS contact_id VARCHAR(256) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS context JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS state VARCHAR(28) NOT NULL DEFAULT 'unmapped'
        CHECK (state IN ('legacy_processing', 'legacy_sent', 'legacy_no_reply',
                        'legacy_quota', 'legacy_unknown', 'legacy_failed',
                        'unmapped', 'pending', 'processing', 'sent', 'no_reply',
                        'quota_expired', 'unknown', 'failed')),
    ADD COLUMN IF NOT EXISTS reply_text TEXT,
    ADD COLUMN IF NOT EXISTS reply_source VARCHAR(32),
    ADD COLUMN IF NOT EXISTS sent_provider VARCHAR(16),
    ADD COLUMN IF NOT EXISTS error_code VARCHAR(64),
    ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbox_source_event
    ON public.inbox_messages (source, event_key);
CREATE INDEX IF NOT EXISTS ix_inbox_messages_account_id
    ON public.inbox_messages (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_inbox_messages_state
    ON public.inbox_messages (state, ready_at, id);

CREATE TABLE IF NOT EXISTS public.auto_reply_slots (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES public.instagram_accounts(id) ON DELETE CASCADE,
    inbox_id BIGINT NOT NULL UNIQUE REFERENCES public.inbox_messages(id) ON DELETE CASCADE,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status VARCHAR(12) NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'sent', 'unknown', 'released')),
    provider VARCHAR(16),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_auto_reply_slots_account_id
    ON public.auto_reply_slots (account_id, reserved_at DESC);
CREATE INDEX IF NOT EXISTS ix_auto_reply_slots_active_window
    ON public.auto_reply_slots (account_id, reserved_at DESC)
    WHERE status IN ('reserved','sent','unknown');
-- Carry forward *successful* legacy ChatbotX webhook sends in today's/hour's
-- count; new code writes a different activity action to avoid double-counting.
CREATE INDEX IF NOT EXISTS ix_activity_chatbotx_outbound_window
    ON public.activity_logs (account_id, created_at DESC)
    WHERE action = 'chatbotx_outbound';

-- Atomic single source of truth for both the inline legacy path and the
-- (currently DISABLED) new worker. Transaction advisory lock serializes
-- reservations for the same page, including concurrent serverless requests.
-- Active/uncertain slots remain charged; definitive no-send slots are released.
-- Uses database time, Asia/Tehran day boundaries, and a rolling 3600s window.
CREATE OR REPLACE FUNCTION public.reserve_auto_reply_slot(
    p_account_id BIGINT, p_inbox_id BIGINT
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_day_start TIMESTAMPTZ;
    v_day_next TIMESTAMPTZ;
    v_state TEXT;
    v_day_count BIGINT;
    v_hour_count BIGINT;
BEGIN
    IF p_account_id IS NULL OR p_inbox_id IS NULL THEN
        RETURN 'invalid';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('gramma:auto_reply:' || p_account_id::text, 0));
    SELECT state INTO v_state FROM public.inbox_messages
      WHERE id = p_inbox_id AND account_id = p_account_id FOR UPDATE;
    IF v_state NOT IN ('legacy_processing', 'processing') OR v_state IS NULL THEN
        RETURN 'invalid';
    END IF;
    IF EXISTS (SELECT 1 FROM public.auto_reply_slots WHERE inbox_id = p_inbox_id) THEN
        RETURN 'already_reserved';
    END IF;

    v_day_start := (v_now AT TIME ZONE 'Asia/Tehran')::date AT TIME ZONE 'Asia/Tehran';
    v_day_next := ((v_now AT TIME ZONE 'Asia/Tehran')::date + 1) AT TIME ZONE 'Asia/Tehran';
    SELECT
      (SELECT count(*) FROM public.auto_reply_slots
       WHERE account_id = p_account_id AND status IN ('reserved','sent','unknown')
         AND reserved_at >= v_day_start AND reserved_at < v_day_next)
      +
      (SELECT count(*) FROM public.activity_logs
       WHERE account_id = p_account_id AND action = 'chatbotx_outbound'
         AND created_at >= v_day_start AND created_at <= v_now)
      INTO v_day_count;
    IF v_day_count >= 4500 THEN RETURN 'daily_limit'; END IF;

    SELECT
      (SELECT count(*) FROM public.auto_reply_slots
       WHERE account_id = p_account_id AND status IN ('reserved','sent','unknown')
         AND reserved_at > v_now - interval '1 hour' AND reserved_at <= v_now)
      +
      (SELECT count(*) FROM public.activity_logs
       WHERE account_id = p_account_id AND action = 'chatbotx_outbound'
         AND created_at > v_now - interval '1 hour' AND created_at <= v_now)
      INTO v_hour_count;
    IF v_hour_count >= 200 THEN RETURN 'hourly_limit'; END IF;

    INSERT INTO public.auto_reply_slots(account_id, inbox_id, reserved_at, status)
      VALUES (p_account_id, p_inbox_id, v_now, 'reserved');
    RETURN 'reserved';
END;
$fn$;

ALTER TABLE public.reply_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inbox_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.auto_reply_slots ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.reply_templates, public.inbox_messages, public.auto_reply_slots FROM PUBLIC;
REVOKE ALL ON SEQUENCE public.reply_templates_id_seq, public.inbox_messages_id_seq,
    public.auto_reply_slots_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION public.reserve_auto_reply_slot(BIGINT, BIGINT) FROM PUBLIC;
DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON public.reply_templates, public.inbox_messages, public.auto_reply_slots FROM anon;
        REVOKE ALL ON SEQUENCE public.reply_templates_id_seq, public.inbox_messages_id_seq,
            public.auto_reply_slots_id_seq FROM anon;
        REVOKE ALL ON FUNCTION public.reserve_auto_reply_slot(BIGINT, BIGINT) FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON public.reply_templates, public.inbox_messages, public.auto_reply_slots FROM authenticated;
        REVOKE ALL ON SEQUENCE public.reply_templates_id_seq, public.inbox_messages_id_seq,
            public.auto_reply_slots_id_seq FROM authenticated;
        REVOKE ALL ON FUNCTION public.reserve_auto_reply_slot(BIGINT, BIGINT) FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.reply_templates,
            public.inbox_messages, public.auto_reply_slots TO service_role;
        GRANT USAGE, SELECT ON SEQUENCE public.reply_templates_id_seq,
            public.inbox_messages_id_seq, public.auto_reply_slots_id_seq TO service_role;
        GRANT EXECUTE ON FUNCTION public.reserve_auto_reply_slot(BIGINT, BIGINT) TO service_role;
    END IF;
END $roles$;
