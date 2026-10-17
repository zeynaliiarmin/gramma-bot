-- Additive correction to the existing private Telegram post drafts.
-- Apply only to the authorized Supabase project, after Phase 2 is present.
-- No queue, publishing job, ChatbotX table, or existing draft is deleted.

ALTER TABLE public.telegram_post_drafts
    ADD COLUMN IF NOT EXISTS media_intent VARCHAR(8) NOT NULL DEFAULT 'auto',
    ADD COLUMN IF NOT EXISTS caption_moved_to_comment BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS visible_caption TEXT;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_telegram_draft_media_intent'
           AND conrelid = 'public.telegram_post_drafts'::regclass
    ) THEN
        ALTER TABLE public.telegram_post_drafts
            ADD CONSTRAINT ck_telegram_draft_media_intent
            CHECK (media_intent IN ('auto', 'post', 'reel'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_telegram_draft_caption_move'
           AND conrelid = 'public.telegram_post_drafts'::regclass
    ) THEN
        ALTER TABLE public.telegram_post_drafts
            ADD CONSTRAINT ck_telegram_draft_caption_move
            CHECK (
                NOT caption_moved_to_comment OR (
                    visible_caption IS NOT NULL
                    AND char_length(visible_caption) <= 150
                    AND char_length(btrim(caption)) BETWEEN 1 AND 2200
                    AND options->>'firstComment' = btrim(caption)
                )
            );
    END IF;
END $constraints$;

CREATE TABLE IF NOT EXISTS public.telegram_media_intents (
    chat_id BIGINT PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    mode VARCHAR(8) NOT NULL,
    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_telegram_media_intents_private CHECK (chat_id = owner_id),
    CONSTRAINT ck_telegram_media_intents_mode CHECK (mode IN ('post', 'reel'))
);

ALTER TABLE public.telegram_media_intents ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.telegram_media_intents FROM PUBLIC;
-- Supabase has these roles; plain local PostgreSQL may not. Keep the same
-- private access policy on either backend without requiring Supabase roles.
DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON public.telegram_media_intents FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON public.telegram_media_intents FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON public.telegram_media_intents TO service_role;
    END IF;
END $roles$;
