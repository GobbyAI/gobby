-- Preserve ParadeDB parallel scans for project-scoped managed principals.
-- The function is STABLE and performs only a read of the principal binding;
-- marking it parallel-safe prevents its RLS policy from disabling parallel BM25.

ALTER FUNCTION gobby_agent_auth.current_project_id() PARALLEL SAFE;
