-- updated_at carries row-modified semantics, so lifecycle writes (bulk expiry,
-- pause sweeps, backfills) destroy the "when was this session last genuinely
-- active" signal the moment they run — the 2026-08-13 bulk expiry stamped
-- ~3,900 historical sessions in one pass. sessions.last_activity is bumped
-- only by confirmed agent/user activity (hook-confirmed status updates,
-- web-chat turns, compact revival, transcript growth), never by lifecycle
-- status writes, so idle-timeout decisions and activity queries have a
-- trustworthy column. Existing rows seed from updated_at: the best proxy
-- available, and exactly what idle decisions keyed on before this column.
ALTER TABLE sessions
    ADD COLUMN last_activity TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();

UPDATE sessions SET last_activity = updated_at;

-- The database owns creation timestamps: application INSERTs stop passing
-- created_at/updated_at, so every table they touch needs the now() default
-- the rest of the schema already has. Values are unchanged for existing rows.
ALTER TABLE cron_jobs ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE cron_jobs ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE cron_runs ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE expansion_runs ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE expansion_runs ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE memories ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE memories ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE plans ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE plans ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE project_lifecycle_events ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE prompts ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE prompts ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE recall_gate_runs ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE recall_gate_runs ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE recall_injection_outcomes ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE recall_shadow_audit_verdicts ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE recall_shadow_judge_state ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE recall_shadow_prompt_snapshot ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE recall_signal_hits ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE recall_signal_requests ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE secret_key_material ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE secret_key_material ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE secrets ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE secrets ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE session_tasks ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE skill_files ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE skill_files ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE skills ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE skills ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE task_dependencies ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE tasks ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE tasks ALTER COLUMN updated_at SET DEFAULT now();
