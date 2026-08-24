-- Leave interactive principals out of the generic rotation sweep.
--
-- principals_due_for_rotation feeds ManagedCredentialManager.rotate_due, which
-- rotates through rotate_principal. That function copies owner_kind, session,
-- project, overlay and machine forward but not deployment_token, and derives a
-- gobby_agent_<execution>_<generation> role name.
--
-- Interactive principals rotate through rotate_interactive_principal instead:
-- it marks the predecessor draining before creating the successor, keeps the
-- deployment token, derives gobby_ix_<token-slug>_<generation>, and stores the
-- password through replace_interactive_credential_material -- a table whose
-- deployment_token is NOT NULL. Rotating one generically therefore produced an
-- interactive binding with no token, no retrievable credential material, and a
-- role name the gobby_ix_* reapers do not match.
--
-- It also failed outright. uq_interactive_principal_active covers live
-- non-draining interactive rows keyed on
-- (deployment_token, issuing_machine_id, project_id, code_overlay_project_id)
-- NULLS NOT DISTINCT, so the token-less successor landed on the predecessor's
-- key and every daemon start logged a unique violation out of
-- _reconcile_agent_runs_after_restart.
--
-- Scoping the sweep is the fix rather than reordering rotate_principal: the
-- ordering is only reachable for a principal the sweep should never have
-- claimed, and reordering would leave the wrong role name, the dropped token,
-- and the missing credential material in place.

CREATE OR REPLACE FUNCTION gobby_agent_auth.principals_due_for_rotation(
    p_machine_id UUID
)
RETURNS TABLE(
    managed_execution_id UUID,
    role_name NAME,
    credential_generation INTEGER
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, gobby_agent_auth
AS $function$
    WITH single_active AS (
        SELECT pb.managed_execution_id
        FROM gobby_agent_auth.principal_bindings AS pb
        WHERE pb.issuing_machine_id = p_machine_id
          AND pb.revoked_at IS NULL
          AND pb.expires_at > clock_timestamp()
        GROUP BY pb.managed_execution_id
        HAVING count(*) = 1
    )
    SELECT pb.managed_execution_id, pb.role_name, pb.credential_generation
    FROM gobby_agent_auth.principal_bindings AS pb
    JOIN single_active AS active USING (managed_execution_id)
    WHERE pb.revoked_at IS NULL
      AND pb.revocation_requested_at IS NULL
      AND pb.owner_kind <> 'interactive'
      AND pb.issued_at <= clock_timestamp() - INTERVAL '45 minutes'
      AND pb.expires_at > clock_timestamp()
    ORDER BY pb.managed_execution_id
$function$;
