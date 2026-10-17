-- Additive private Telegram Story FSM + scheduled Telegram post drafts.
-- Does not execute Workers, touch ChatbotX, or modify existing cron jobs.
-- The only queue change is adding 'story' to its existing action_type check.

ALTER TABLE public.telegram_post_drafts
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;

-- The retired 'separate first comment' control must not survive in an open
-- draft. Keep firstComment ONLY if it is the original caption deliberately moved.
UPDATE public.telegram_post_drafts
   SET options = options - 'firstComment', updated_at = NOW(),
       prompt_kind = NULL, prompt_message_id = NULL
 WHERE status IN ('collecting', 'ready')
   AND NOT caption_moved_to_comment AND options ? 'firstComment';
UPDATE public.telegram_post_drafts
   SET prompt_kind = NULL, prompt_message_id = NULL, updated_at = NOW()
 WHERE status IN ('collecting', 'ready') AND prompt_kind = 'first_comment';

CREATE TABLE IF NOT EXISTS public.telegram_story_sequences (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    local_account_id BIGINT NOT NULL,
    expected_username VARCHAR(64) NOT NULL,
    declared_count INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'awaiting_count',
    last_album_at TIMESTAMPTZ,
    invalid_media_group_id VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '7 days'),
    CONSTRAINT ck_story_private CHECK (chat_id = owner_id),
    CONSTRAINT ck_story_declared CHECK (declared_count IS NULL OR declared_count BETWEEN 1 AND 30),
    CONSTRAINT ck_story_status CHECK
        (status IN ('awaiting_count','collecting','queued','cancelled','invalid','expired'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_active_story
    ON public.telegram_story_sequences (chat_id)
    WHERE status IN ('awaiting_count','collecting');
CREATE INDEX IF NOT EXISTS ix_story_sequence_owner
    ON public.telegram_story_sequences (owner_id, status);

CREATE TABLE IF NOT EXISTS public.telegram_story_slides (
    id BIGSERIAL PRIMARY KEY,
    sequence_id BIGINT NOT NULL REFERENCES public.telegram_story_sequences(id),
    owner_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    media_group_id VARCHAR(255),
    kind VARCHAR(8) NOT NULL,
    url TEXT,
    status VARCHAR(12) NOT NULL DEFAULT 'uploading',
    error_text TEXT,
    queue_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_telegram_story_slide_message UNIQUE (chat_id, message_id),
    CONSTRAINT ck_story_slide_private CHECK (chat_id = owner_id),
    CONSTRAINT ck_story_slide_kind CHECK (kind IN ('image', 'video')),
    CONSTRAINT ck_story_slide_status CHECK (status IN ('uploading', 'ready', 'failed'))
);
CREATE INDEX IF NOT EXISTS ix_story_slide_sequence
    ON public.telegram_story_slides (sequence_id, message_id);

ALTER TABLE public.telegram_story_sequences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_story_slides ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.telegram_story_sequences, public.telegram_story_slides FROM PUBLIC;
REVOKE ALL ON SEQUENCE public.telegram_story_sequences_id_seq,
    public.telegram_story_slides_id_seq FROM PUBLIC;
DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON public.telegram_story_sequences, public.telegram_story_slides FROM anon;
        REVOKE ALL ON SEQUENCE public.telegram_story_sequences_id_seq,
            public.telegram_story_slides_id_seq FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON public.telegram_story_sequences, public.telegram_story_slides FROM authenticated;
        REVOKE ALL ON SEQUENCE public.telegram_story_sequences_id_seq,
            public.telegram_story_slides_id_seq FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.telegram_story_sequences,
            public.telegram_story_slides TO service_role;
        GRANT USAGE, SELECT ON SEQUENCE public.telegram_story_sequences_id_seq,
            public.telegram_story_slides_id_seq TO service_role;
    END IF;
END $roles$;

ALTER TABLE public.outbound_queue DROP CONSTRAINT IF EXISTS outbound_queue_action_type_check;
ALTER TABLE public.outbound_queue ADD CONSTRAINT outbound_queue_action_type_check
    CHECK (action_type IN ('post','story','reply_comment','send_dm'));

-- This separate claim cannot see post, ChatbotX replies or DMs. Child story
-- jobs are blocked until an authenticated GET confirms their predecessor's
-- Instagram target as published and explicitly releases the child.
CREATE OR REPLACE FUNCTION public.claim_next_zernio_story_job()
RETURNS public.outbound_queue
LANGUAGE plpgsql
SET search_path = public, pg_temp AS $$
DECLARE claimed public.outbound_queue;
BEGIN
    UPDATE public.outbound_queue
       SET status = 'processing', attempts = attempts + 1, updated_at = NOW()
     WHERE id = (
         SELECT id FROM public.outbound_queue
          WHERE provider = 'zernio' AND action_type = 'story'
            AND status = 'pending' AND scheduled_for <= NOW()
            AND attempts < max_attempts
            AND payload->>'story_released' = 'true'
          ORDER BY scheduled_for ASC, id ASC
          LIMIT 1 FOR UPDATE SKIP LOCKED
     ) RETURNING * INTO claimed;
    RETURN claimed;
END; $$;

-- Reserved separately from feed/reel post caps, before any provider POST.
-- A crash spends its slot conservatively rather than risking duplicate sends.
CREATE OR REPLACE FUNCTION public.reserve_zernio_story_slot(
    p_page_key TEXT, p_daily_limit INT, p_spacing_seconds INT
)
RETURNS TABLE (allowed BOOLEAN, retry_after TIMESTAMPTZ)
LANGUAGE plpgsql
SET search_path = public, pg_temp AS $$
DECLARE
    v_now TIMESTAMPTZ;
    v_day_start TIMESTAMPTZ;
    v_next_day TIMESTAMPTZ;
    v_day_key TEXT;
    v_gap_key TEXT;
    v_count INT;
    v_window TIMESTAMPTZ;
    v_last TIMESTAMPTZ;
BEGIN
    IF p_page_key IS NULL OR p_page_key !~ '^[a-z0-9._]{1,30}$'
       OR p_daily_limit IS NULL OR p_daily_limit < 1
       OR p_spacing_seconds IS NULL OR p_spacing_seconds < 1 THEN
        RAISE EXCEPTION 'invalid Zernio story rate configuration';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('zernio:story:' || p_page_key, 0));
    v_now := clock_timestamp();
    v_day_start := (((v_now AT TIME ZONE 'Asia/Tehran')::DATE)::TIMESTAMP
                    AT TIME ZONE 'Asia/Tehran');
    v_next_day := ((((v_now AT TIME ZONE 'Asia/Tehran')::DATE + 1)::TIMESTAMP)
                   AT TIME ZONE 'Asia/Tehran');
    v_day_key := 'zernio:story:day:' || p_page_key;
    v_gap_key := 'zernio:story:gap:' || p_page_key;

    SELECT b.count, b.window_start INTO v_count, v_window
      FROM public.rate_limit_buckets b WHERE b.bucket_key = v_day_key;
    IF v_window = v_day_start AND v_count >= p_daily_limit THEN
        RETURN QUERY SELECT FALSE, v_next_day;
        RETURN;
    END IF;
    SELECT b.window_start INTO v_last
      FROM public.rate_limit_buckets b WHERE b.bucket_key = v_gap_key;
    IF v_last IS NOT NULL AND v_last + make_interval(secs => p_spacing_seconds) > v_now THEN
        RETURN QUERY SELECT FALSE, v_last + make_interval(secs => p_spacing_seconds);
        RETURN;
    END IF;
    INSERT INTO public.rate_limit_buckets (bucket_key, count, window_start)
    VALUES (v_day_key, 1, v_day_start)
    ON CONFLICT (bucket_key) DO UPDATE
       SET count = CASE WHEN rate_limit_buckets.window_start = EXCLUDED.window_start
                        THEN rate_limit_buckets.count + 1 ELSE 1 END,
           window_start = EXCLUDED.window_start, updated_at = v_now;
    INSERT INTO public.rate_limit_buckets (bucket_key, count, window_start)
    VALUES (v_gap_key, 1, v_now)
    ON CONFLICT (bucket_key) DO UPDATE
       SET count = 1, window_start = EXCLUDED.window_start, updated_at = v_now;
    RETURN QUERY SELECT TRUE, v_now;
END; $$;

REVOKE ALL ON FUNCTION public.claim_next_zernio_story_job() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.reserve_zernio_story_slot(TEXT, INT, INT) FROM PUBLIC;
DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.claim_next_zernio_story_job() FROM anon;
        REVOKE ALL ON FUNCTION public.reserve_zernio_story_slot(TEXT, INT, INT) FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON FUNCTION public.claim_next_zernio_story_job() FROM authenticated;
        REVOKE ALL ON FUNCTION public.reserve_zernio_story_slot(TEXT, INT, INT) FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION public.claim_next_zernio_story_job() TO service_role;
        GRANT EXECUTE ON FUNCTION public.reserve_zernio_story_slot(TEXT, INT, INT) TO service_role;
    END IF;
END $grant$;
