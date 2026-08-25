-- Let interactive principal bindings persist without a session id.
--
-- #20899 made the whole issuance chain treat session_id as audit-only and
-- nullable: gcore sends "session_id":null when GOBBY_SESSION_ID is absent or
-- not a UUID, the operator handshake route accepts the missing id, and
-- issue_interactive/rotate_interactive pass None through to SQL. The column
-- still carried NOT NULL from the baseline, so the first sessionless
-- handshake died at INSERT with a NotNullViolation. Interactive principals
-- are keyed on token+machine+project(+overlay); session_id never identifies
-- them.

ALTER TABLE gobby_agent_auth.principal_bindings
    ALTER COLUMN session_id DROP NOT NULL;
