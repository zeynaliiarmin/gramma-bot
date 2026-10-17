-- Small additive state for exactly one due-time LLM retry of a held reply.
-- Apply after phase4_knowledge_hold_review, before deploying priority Worker.
-- Existing Hold rows get zero prior due attempts; no message/slot is replayed.
ALTER TABLE public.inbox_messages
    ADD COLUMN IF NOT EXISTS hold_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.inbox_messages
    DROP CONSTRAINT IF EXISTS inbox_messages_hold_attempts_nonnegative;
ALTER TABLE public.inbox_messages
    ADD CONSTRAINT inbox_messages_hold_attempts_nonnegative CHECK (hold_attempts >= 0);
