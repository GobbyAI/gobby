-- gobby:destructive

-- Evidence block: tasks.assignee
-- Read-only hub catalog check at schema version 355 (2026-07-31): present as TEXT.
-- Pre-implementation token sweep: no runtime or canonical-schema references;
-- migration 356 and its contract assertion are the only source mentions.
ALTER TABLE tasks DROP COLUMN IF EXISTS assignee;

-- Evidence block: task_artifacts.last_reviewed_plan_hash
-- Read-only hub catalog check (2026-07-31): present as TEXT.
-- Pre-implementation token sweep across src/ and tests/: zero references.
ALTER TABLE task_artifacts DROP COLUMN IF EXISTS last_reviewed_plan_hash;

-- Evidence block: task_artifacts.plan_review_attempts
-- Read-only hub catalog check (2026-07-31): present as INTEGER.
-- Pre-implementation token sweep across src/ and tests/: zero references.
ALTER TABLE task_artifacts DROP COLUMN IF EXISTS plan_review_attempts;

-- Evidence block: task_artifacts.qa_attempts
-- Read-only hub catalog check (2026-07-31): present as INTEGER.
-- Pre-implementation token sweep across src/ and tests/: zero references.
ALTER TABLE task_artifacts DROP COLUMN IF EXISTS qa_attempts;

-- Evidence block: task_artifacts.epic_qa_attempts
-- Read-only hub catalog check (2026-07-31): present as INTEGER.
-- Pre-implementation token sweep across src/ and tests/: zero references.
ALTER TABLE task_artifacts DROP COLUMN IF EXISTS epic_qa_attempts;

-- Evidence block: task_artifacts.merge_attempts
-- Read-only hub catalog check (2026-07-31): present as INTEGER.
-- Pre-implementation token sweep across src/ and tests/: zero references.
ALTER TABLE task_artifacts DROP COLUMN IF EXISTS merge_attempts;

-- Evidence block: inter_session_messages.read_at
-- Read-only hub catalog check at schema version 355 (2026-07-31): present as TIMESTAMPTZ.
-- Pre-implementation token sweep: no runtime or canonical-schema references;
-- migration 356 and its contract assertion are the only source mentions.
ALTER TABLE inter_session_messages DROP COLUMN IF EXISTS read_at;

-- Evidence block: idx_token_events_event_at
-- Read-only pg_stat_user_indexes check (2026-07-31): idx_scan=0; 22,970,368 bytes.
-- Reader sweep: event_at appears in aggregate time-window predicates; observed hub
-- plans have never scanned this plain btree.
DROP INDEX IF EXISTS idx_token_events_event_at;

-- Evidence block: idx_token_events_model_family
-- Read-only pg_stat_user_indexes check (2026-07-31): idx_scan=0; 32,325,632 bytes.
-- Reader sweep: model_family is grouped and ordered, never used as a lookup predicate;
-- observed hub plans have never scanned this plain btree.
DROP INDEX IF EXISTS idx_token_events_model_family;

-- Evidence block: idx_token_events_project_event
-- Read-only pg_stat_user_indexes check (2026-07-31): idx_scan=0; 35,512,320 bytes.
-- Reader sweep: project_id and event_at are optional aggregate filters; observed hub
-- plans have never scanned this plain btree. Retained session and dedup indexes cover
-- the session lookup/order and message idempotency query shapes.
DROP INDEX IF EXISTS idx_token_events_project_event;
