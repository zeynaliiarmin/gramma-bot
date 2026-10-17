-- Phase 3 additive, admin-private Mini-App composer. No provider publication,
-- cron, existing queue row or ChatbotX table is modified by this migration.
-- The earlier phase3_performance migration is independent and idempotent.

CREATE TABLE IF NOT EXISTS public.miniapp_publish_drafts (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    local_account_id BIGINT NOT NULL,
    expected_username VARCHAR(64) NOT NULL,
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('post','story')),
    status VARCHAR(12) NOT NULL DEFAULT 'collecting'
        CHECK (status IN ('collecting','queued','cancelled','expired')),
    queue_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '7 days')
);
CREATE INDEX IF NOT EXISTS ix_miniapp_drafts_owner
    ON public.miniapp_publish_drafts (owner_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_miniapp_active_kind
    ON public.miniapp_publish_drafts (owner_id, kind)
    WHERE status = 'collecting';

CREATE TABLE IF NOT EXISTS public.miniapp_publish_media (
    id BIGSERIAL PRIMARY KEY,
    draft_id BIGINT NOT NULL REFERENCES public.miniapp_publish_drafts(id),
    position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 29),
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('image','video')),
    ext VARCHAR(5) NOT NULL CHECK (ext IN ('jpg','png','mp4','mov')),
    mime_type VARCHAR(24) NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes BETWEEN 1 AND 52428800),
    width INTEGER NOT NULL CHECK (width BETWEEN 1 AND 10000),
    height INTEGER NOT NULL CHECK (height BETWEEN 1 AND 10000),
    duration_seconds DOUBLE PRECISION,
    storage_path TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'uploading'
        CHECK (status IN ('uploading','ready')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_miniapp_media_position UNIQUE (draft_id, position)
);
CREATE INDEX IF NOT EXISTS ix_miniapp_media_draft
    ON public.miniapp_publish_media (draft_id, position);

-- Remove the retired location control from unfinished Telegram drafts. Do not
-- silently rewrite already queued/scheduled jobs or historical records.
UPDATE public.telegram_post_drafts
   SET options = options - 'locationId',
       prompt_kind = CASE WHEN prompt_kind = 'location' THEN NULL ELSE prompt_kind END,
       prompt_message_id = CASE WHEN prompt_kind = 'location' THEN NULL ELSE prompt_message_id END,
       prompt_slide = CASE WHEN prompt_kind = 'location' THEN NULL ELSE prompt_slide END,
       updated_at = NOW()
 WHERE status IN ('collecting','ready')
   AND (options ? 'locationId' OR prompt_kind = 'location');

ALTER TABLE public.miniapp_publish_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.miniapp_publish_media ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.miniapp_publish_drafts, public.miniapp_publish_media FROM PUBLIC;
REVOKE ALL ON SEQUENCE public.miniapp_publish_drafts_id_seq,
    public.miniapp_publish_media_id_seq FROM PUBLIC;
DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON public.miniapp_publish_drafts, public.miniapp_publish_media FROM anon;
        REVOKE ALL ON SEQUENCE public.miniapp_publish_drafts_id_seq,
            public.miniapp_publish_media_id_seq FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON public.miniapp_publish_drafts, public.miniapp_publish_media FROM authenticated;
        REVOKE ALL ON SEQUENCE public.miniapp_publish_drafts_id_seq,
            public.miniapp_publish_media_id_seq FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.miniapp_publish_drafts,
            public.miniapp_publish_media TO service_role;
        GRANT USAGE, SELECT ON SEQUENCE public.miniapp_publish_drafts_id_seq,
            public.miniapp_publish_media_id_seq TO service_role;
    END IF;
END $roles$;
