-- Memories record what an agent chose to remember but never why. Recall
-- audits show junk rows (frozen review-run logs with hex IDs, one-time
-- status snapshots) being re-served across unrelated sessions, and dream's
-- planner must cite a concrete obsolescence signal for every delete yet has
-- no creation claim to judge staleness against. rationale stores the
-- writer's durable-value claim; source_task_id and created_by_agent extend
-- provenance beyond source_type + source_session_id so verdicts can cite
-- which task and agent produced a memory. All three are NULL on
-- pre-existing rows: NULL means "no recorded claim", never an error.
ALTER TABLE memories ADD COLUMN rationale text;
ALTER TABLE memories ADD COLUMN source_task_id uuid;
ALTER TABLE memories ADD COLUMN created_by_agent text;

ALTER TABLE ONLY memories
    ADD CONSTRAINT memories_source_task_id_fkey
    FOREIGN KEY (source_task_id) REFERENCES tasks(id)
    ON DELETE SET NULL DEFERRABLE;

CREATE INDEX idx_memories_source_task ON memories USING btree (source_task_id);
