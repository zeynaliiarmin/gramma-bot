-- Scoped, additive indexes for Phase 3. No cron, status, provider data or
-- worker is changed by this migration. Re-running is idempotent.
-- Run only against the authorized gramma-bot database.

-- Existing ix_outbound_pending(status, scheduled_for, priority DESC, id)
-- already supports generic pending jobs; these narrower indexes align with
-- the two protected Zernio claim functions without scanning ChatbotX rows.
CREATE INDEX IF NOT EXISTS ix_zernio_post_due_v3
    ON public.outbound_queue (scheduled_for, id)
    WHERE provider = 'zernio' AND action_type = 'post'
      AND status = 'pending' AND attempts < max_attempts;
CREATE INDEX IF NOT EXISTS ix_zernio_story_due_v3
    ON public.outbound_queue (scheduled_for, id)
    WHERE provider = 'zernio' AND action_type = 'story'
      AND status = 'pending' AND attempts < max_attempts
      AND payload->>'story_released' = 'true';
-- The admin calendar filters owner and status then orders by due time.
CREATE INDEX IF NOT EXISTS ix_zernio_post_calendar_v3
    ON public.outbound_queue ((payload->>'owner_id'), scheduled_for, id)
    WHERE provider = 'zernio' AND action_type = 'post' AND status = 'pending';
-- The legacy scheduler also claims by status + time.
CREATE INDEX IF NOT EXISTS ix_posts_due_v3
    ON public.posts (scheduled_at, id) WHERE status = 'scheduled';
-- Avoid scanning all historical album sequences/slides on each new fragment.
CREATE INDEX IF NOT EXISTS ix_story_invalid_group_v3
    ON public.telegram_story_sequences (chat_id, invalid_media_group_id)
    WHERE status = 'invalid' AND invalid_media_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_story_media_group_v3
    ON public.telegram_story_slides (chat_id, media_group_id)
    WHERE media_group_id IS NOT NULL;
