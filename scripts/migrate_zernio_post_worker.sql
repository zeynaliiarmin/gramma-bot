-- Additive post-only worker functions for the already-migrated outbound_queue.
-- Apply in one transaction to the existing, permitted Supabase project.
-- The existing generic claim/ChatbotX/legacy chat_history are untouched.

CREATE OR REPLACE FUNCTION public.claim_next_zernio_post_job()
RETURNS public.outbound_queue
LANGUAGE plpgsql
SET search_path = public, pg_temp AS $$
DECLARE claimed public.outbound_queue;
BEGIN
    UPDATE public.outbound_queue
       SET status = 'processing', attempts = attempts + 1, updated_at = NOW()
     WHERE id = (
         SELECT id
           FROM public.outbound_queue
          WHERE provider = 'zernio'
            AND action_type = 'post'
            AND status = 'pending'
            AND scheduled_for <= NOW()
            AND attempts < max_attempts
          ORDER BY priority DESC, scheduled_for ASC, id ASC
          LIMIT 1 FOR UPDATE SKIP LOCKED
     )
    RETURNING * INTO claimed;
    RETURN claimed;
END; $$;

-- Reserve a daily attempt AND a rolling minimum spacing atomically. A reserved
-- slot remains spent if the function crashes before recording the provider
-- result: conservative limits are safer than a duplicate public post.
CREATE OR REPLACE FUNCTION public.reserve_zernio_post_slot(
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
        RAISE EXCEPTION 'invalid Zernio post rate configuration';
    END IF;

    -- Serialized only for this page, not globally. One transaction checks and
    -- commits BOTH counters. These keys cannot collide with the reply buckets.
    PERFORM pg_advisory_xact_lock(hashtextextended('zernio:post:' || p_page_key, 0));
    v_now := clock_timestamp();
    v_day_start := (((v_now AT TIME ZONE 'Asia/Tehran')::DATE)::TIMESTAMP
                    AT TIME ZONE 'Asia/Tehran');
    v_next_day := ((((v_now AT TIME ZONE 'Asia/Tehran')::DATE + 1)::TIMESTAMP)
                   AT TIME ZONE 'Asia/Tehran');
    v_day_key := 'zernio:post:day:' || p_page_key;
    v_gap_key := 'zernio:post:gap:' || p_page_key;

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

REVOKE ALL ON FUNCTION public.claim_next_zernio_post_job() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.reserve_zernio_post_slot(TEXT, INT, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_next_zernio_post_job() TO service_role;
GRANT EXECUTE ON FUNCTION public.reserve_zernio_post_slot(TEXT, INT, INT) TO service_role;
