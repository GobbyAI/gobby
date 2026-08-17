-- Migration 387 created gobby_agent_auth.interactive_role_name owned by the
-- migration runner, and the baseline default-privilege hardening revokes
-- EXECUTE FROM PUBLIC on new functions. The SECURITY DEFINER issuance
-- functions run as gobby_agent_issuer, so without an explicit grant every
-- interactive issue/rotate fails with "permission denied for function
-- interactive_role_name".
GRANT EXECUTE ON FUNCTION gobby_agent_auth.interactive_role_name(TEXT, UUID, UUID, INTEGER)
    TO gobby_agent_issuer;
