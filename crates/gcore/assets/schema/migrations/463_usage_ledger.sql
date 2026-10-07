-- Usage ledger: per-call api_calls, provider-reported run totals, task claim
-- intervals, and quota observation details.

-- NULL means the provider does not state a call count.
ALTER TABLE token_events ADD COLUMN api_calls integer CHECK (api_calls >= 0);

CREATE TABLE session_reported_usage (
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_key text NOT NULL,
    source text NOT NULL,
    cost_unit text CHECK (cost_unit IN ('usd', 'factory_credits')),
    cost_amount numeric,
    cost_complete boolean NOT NULL DEFAULT true,
    api_duration_ms bigint,
    wall_duration_ms bigint,
    started_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    model_usage jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, run_key),
    CHECK ((cost_unit IS NULL) = (cost_amount IS NULL)),
    CHECK (observed_at >= started_at)
);

-- Trigger inserts run as the invoking role, so the key needs no sequence grant.
CREATE TABLE task_claim_intervals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    claimed_at timestamptz NOT NULL,
    released_at timestamptz,
    CHECK (released_at >= claimed_at)
);
CREATE UNIQUE INDEX idx_task_claim_intervals_open
    ON task_claim_intervals (task_id) WHERE released_at IS NULL;
CREATE INDEX idx_task_claim_intervals_session ON task_claim_intervals (session_id, claimed_at);
CREATE INDEX idx_task_claim_intervals_task ON task_claim_intervals (task_id, claimed_at);

-- The holder is the claimant of an open task. Interval edges use the database clock.
CREATE FUNCTION sync_task_claim_interval(p_task_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_holder uuid;
BEGIN
    SELECT CASE WHEN closed_at IS NULL THEN claimed_by_session_id END
      INTO v_holder
      FROM tasks
     WHERE id = p_task_id;

    IF v_holder IS NOT NULL AND EXISTS (
        SELECT 1
          FROM task_claim_intervals
         WHERE task_id = p_task_id AND session_id = v_holder AND released_at IS NULL
    ) THEN
        RETURN;
    END IF;

    UPDATE task_claim_intervals
       SET released_at = greatest(clock_timestamp(), claimed_at)
     WHERE task_id = p_task_id AND released_at IS NULL;

    IF v_holder IS NOT NULL THEN
        INSERT INTO task_claim_intervals (task_id, session_id, claimed_at)
        VALUES (p_task_id, v_holder, clock_timestamp());
    END IF;
END;
$$;

CREATE FUNCTION sync_task_claim_interval_from_task() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM sync_task_claim_interval(NEW.id);
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_claim_interval
    AFTER INSERT OR UPDATE OF claimed_by_session_id, closed_at ON tasks
    FOR EACH ROW EXECUTE FUNCTION sync_task_claim_interval_from_task();

-- Seed current holders only; earlier claims have no recorded history.
SELECT sync_task_claim_interval(id)
  FROM tasks
 WHERE closed_at IS NULL AND claimed_by_session_id IS NOT NULL;

ALTER TABLE provider_capacity_snapshots ADD COLUMN details jsonb NOT NULL DEFAULT '{}';

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE session_reported_usage, task_claim_intervals
    TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION sync_task_claim_interval(p_task_id uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sync_task_claim_interval(p_task_id uuid) TO gobby_daemon_runtime;
REVOKE ALL ON FUNCTION sync_task_claim_interval_from_task() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sync_task_claim_interval_from_task() TO gobby_daemon_runtime;
