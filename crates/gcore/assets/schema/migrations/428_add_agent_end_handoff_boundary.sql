ALTER TABLE session_handoff_deliveries
    DROP CONSTRAINT session_handoff_deliveries_boundary_kind_valid;

ALTER TABLE session_handoff_deliveries
    ADD CONSTRAINT session_handoff_deliveries_boundary_kind_valid
    CHECK (boundary_kind = ANY (ARRAY['compact'::text, 'clear'::text, 'agent_end'::text]));
