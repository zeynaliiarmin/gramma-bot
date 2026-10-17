-- Phase 4 activation: permit a recoverable pre-send inference lease.
-- A claimed message is 'generating' until a quota slot and send intent are
-- committed together; an expired generation lease can be claimed again.
-- Any row with a committed auto_reply_slot is NEVER automatically replayed.
-- Preserve all existing inbox rows, RLS grants, quotas and unrelated crons.

ALTER TABLE public.inbox_messages
    ADD COLUMN IF NOT EXISTS claim_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE public.inbox_messages
    DROP CONSTRAINT IF EXISTS inbox_messages_state_check;
ALTER TABLE public.inbox_messages
    ADD CONSTRAINT inbox_messages_state_check CHECK (
        state IN ('legacy_processing', 'legacy_sent', 'legacy_no_reply',
                  'legacy_quota', 'legacy_unknown', 'legacy_failed',
                  'unmapped', 'pending', 'generating', 'processing', 'sent',
                  'no_reply', 'quota_expired', 'unknown', 'failed')
    );
