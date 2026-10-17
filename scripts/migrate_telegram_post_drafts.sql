-- Additive Phase 2 ONLY: admin-private Telegram album and media drafts.
-- This table never publishes; only explicit user confirmation can create a
-- post in the existing public.outbound_queue. Other queues are untouched.
-- Run inside one transaction against the permitted Supabase project.

CREATE TABLE IF NOT EXISTS public.telegram_post_drafts (
    id BIGSERIAL PRIMARY KEY,
    source_key VARCHAR(180) NOT NULL UNIQUE,
    owner_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    local_account_id BIGINT NOT NULL,
    expected_username VARCHAR(64) NOT NULL,
    media_group_id VARCHAR(255),
    media JSONB NOT NULL DEFAULT '[]'::jsonb,
    caption TEXT NOT NULL DEFAULT '',
    caption_from_reply BOOLEAN NOT NULL DEFAULT FALSE,
    options JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'collecting',
    queue_id BIGINT,
    panel_message_id BIGINT,
    prompt_message_id BIGINT,
    prompt_kind VARCHAR(24),
    prompt_slide INTEGER,
    notify_attempts INTEGER NOT NULL DEFAULT 0,
    last_prompt_at TIMESTAMPTZ,
    error_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '7 days'),
    CONSTRAINT uq_telegram_post_drafts_album UNIQUE (chat_id, media_group_id),
    CONSTRAINT ck_telegram_post_drafts_status CHECK
        (status IN ('collecting','ready','queued','cancelled','invalid','expired')),
    CONSTRAINT ck_telegram_post_drafts_private CHECK (chat_id = owner_id),
    CONSTRAINT ck_telegram_post_drafts_media CHECK
        (jsonb_typeof(media) = 'array' AND jsonb_array_length(media) <= 10),
    CONSTRAINT ck_telegram_post_drafts_options CHECK (jsonb_typeof(options) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_telegram_post_drafts_due
    ON public.telegram_post_drafts (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_telegram_post_drafts_owner
    ON public.telegram_post_drafts (owner_id, created_at);

ALTER TABLE public.telegram_post_drafts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.telegram_post_drafts FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.telegram_post_drafts TO service_role;
REVOKE ALL ON SEQUENCE public.telegram_post_drafts_id_seq FROM PUBLIC, anon, authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.telegram_post_drafts_id_seq TO service_role;
