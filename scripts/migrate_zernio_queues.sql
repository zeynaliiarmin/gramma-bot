-- Zernio queue and inbox migration for the permitted gramma-bot Supabase project.
-- Existing public.chat_history is deliberately untouched (legacy app schema).
-- Execute all statements in a single transaction, stopping on the first error.

CREATE TABLE IF NOT EXISTS public.ig_accounts (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_account_id TEXT NOT NULL,
    ig_username TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_account_id)
);

CREATE TABLE IF NOT EXISTS public.outbound_queue (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    action_type TEXT NOT NULL CHECK (action_type IN ('post','reply_comment','send_dm')),
    provider TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','sent','failed','cancelled')),
    priority INT NOT NULL DEFAULT 0,
    scheduled_for TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    last_error TEXT,
    provider_job_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_outbound_pending
    ON public.outbound_queue (status, scheduled_for, priority DESC, id)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS public.inbox_messages (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    message_type TEXT NOT NULL CHECK (message_type IN ('comment','dm')),
    external_id TEXT NOT NULL,
    thread_id TEXT,
    parent_post_id TEXT,
    sender_id TEXT NOT NULL,
    sender_username TEXT,
    text TEXT,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','queued','replied','ignored','failed')),
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS ix_inbox_unprocessed
    ON public.inbox_messages (status, received_at)
    WHERE status IN ('new','queued');

-- Do not replace public.chat_history: existing app code still writes its legacy shape.
CREATE TABLE IF NOT EXISTS public.zernio_chat_history (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    provider_msg_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_zernio_chat_thread
    ON public.zernio_chat_history (thread_id, created_at);

CREATE TABLE IF NOT EXISTS public.rate_limit_buckets (
    bucket_key TEXT PRIMARY KEY,
    count INT NOT NULL DEFAULT 0,
    window_start TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key TEXT PRIMARY KEY,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '7 days')
);

CREATE OR REPLACE FUNCTION public.claim_next_outbound_job(p_provider TEXT DEFAULT NULL)
RETURNS public.outbound_queue LANGUAGE plpgsql
SET search_path = public, pg_temp AS $$
DECLARE claimed public.outbound_queue;
BEGIN
    UPDATE public.outbound_queue
    SET status = 'processing', attempts = attempts + 1, updated_at = NOW()
    WHERE id = (
        SELECT id FROM public.outbound_queue
        WHERE status = 'pending'
          AND scheduled_for <= NOW()
          AND attempts < max_attempts
          AND (p_provider IS NULL OR provider = p_provider)
        ORDER BY priority DESC, scheduled_for ASC, id ASC
        LIMIT 1 FOR UPDATE SKIP LOCKED
    )
    RETURNING * INTO claimed;
    RETURN claimed;
END; $$;

CREATE OR REPLACE FUNCTION public.try_acquire_rate_slot(
    p_bucket_key TEXT, p_window TIMESTAMPTZ, p_limit INT
)
RETURNS BOOLEAN LANGUAGE plpgsql
SET search_path = public, pg_temp AS $$
DECLARE current_count INT;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_bucket_key IS NULL OR p_window IS NULL THEN
        RETURN FALSE;
    END IF;
    INSERT INTO public.rate_limit_buckets (bucket_key, count, window_start)
    VALUES (p_bucket_key, 1, p_window)
    ON CONFLICT (bucket_key) DO UPDATE
    SET count = CASE
            WHEN rate_limit_buckets.window_start <> EXCLUDED.window_start THEN 1
            ELSE LEAST(rate_limit_buckets.count + 1, p_limit + 1)
        END,
        window_start = EXCLUDED.window_start,
        updated_at = NOW()
    RETURNING count INTO current_count;
    RETURN current_count <= p_limit;
END; $$;

-- No browser/anon access to messages, queue jobs, rate buckets, or idempotency.
ALTER TABLE public.ig_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outbound_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inbox_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.zernio_chat_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rate_limit_buckets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.idempotency_keys ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE
    public.ig_accounts, public.outbound_queue, public.inbox_messages,
    public.zernio_chat_history, public.rate_limit_buckets, public.idempotency_keys
FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    public.ig_accounts, public.outbound_queue, public.inbox_messages,
    public.zernio_chat_history, public.rate_limit_buckets, public.idempotency_keys
TO service_role;

REVOKE ALL ON SEQUENCE
    public.ig_accounts_id_seq, public.outbound_queue_id_seq,
    public.inbox_messages_id_seq, public.zernio_chat_history_id_seq
FROM PUBLIC, anon, authenticated;
GRANT USAGE, SELECT ON SEQUENCE
    public.ig_accounts_id_seq, public.outbound_queue_id_seq,
    public.inbox_messages_id_seq, public.zernio_chat_history_id_seq
TO service_role;

REVOKE ALL ON FUNCTION public.claim_next_outbound_job(TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.try_acquire_rate_slot(TEXT, TIMESTAMPTZ, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_next_outbound_job(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.try_acquire_rate_slot(TEXT, TIMESTAMPTZ, INT) TO service_role;
