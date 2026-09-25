ALTER TABLE plan_review_evidence
    ADD COLUMN static_writer_session_id uuid,
    ADD COLUMN static_coordinator_session_id uuid;

ALTER TABLE plan_review_evidence
    DROP CONSTRAINT plan_review_evidence_bound_lease_cleared,
    ADD CONSTRAINT plan_review_evidence_bound_lease_cleared
        CHECK ((dispatch_run_id IS NULL AND static_writer_session_id IS NULL)
               OR lease_expires_at IS NULL),
    ADD CONSTRAINT plan_review_evidence_static_binding
        CHECK ((static_writer_session_id IS NULL AND static_coordinator_session_id IS NULL)
               OR (static_writer_session_id IS NOT NULL
                   AND static_coordinator_session_id IS NOT NULL
                   AND session_id IS NOT NULL
                   AND dispatch_run_id IS NULL));
