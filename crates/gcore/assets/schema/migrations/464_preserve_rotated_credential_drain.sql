-- Rotation requests revocation while advertising a predecessor drain deadline.
-- Reconciliation waits for that deadline. Explicit revoke attempts remain immediate.

CREATE OR REPLACE FUNCTION gobby_agent_auth.reconcile_daemon(p_machine_id uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    SET createrole_self_grant TO ''
    AS $_$
DECLARE
    candidate RECORD;
    orphan_role RECORD;
    result_count INTEGER;
    remaining_sessions INTEGER;
    reconciled_count INTEGER := 0;
    retry_pending BOOLEAN := FALSE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('gobby-agent-auth-reconcile', 0));
    IF NOT EXISTS (SELECT 1 FROM public.machines WHERE id = p_machine_id) THEN
        RAISE EXCEPTION 'unknown reconciling machine' USING ERRCODE = '23503';
    END IF;

    FOR candidate IN
        SELECT pb.managed_execution_id, pb.credential_generation
        FROM principal_bindings AS pb
        LEFT JOIN daemon_registry AS daemon
          ON daemon.machine_id = pb.issuing_machine_id
        LEFT JOIN public.agent_runs AS run
          ON run.id = pb.agent_run_id
        WHERE pb.revoked_at IS NULL
          AND (
              pb.issuing_machine_id = p_machine_id
              OR daemon.lease_expires_at IS NULL
              OR daemon.lease_expires_at <= clock_timestamp()
          )
          AND (
              pb.expires_at <= clock_timestamp()
              OR pb.predecessor_drain_deadline <= clock_timestamp()
              OR (
                  pb.revocation_requested_at IS NOT NULL
                  AND (
                      pb.predecessor_drain_deadline IS NULL
                      OR pb.revocation_attempts > 0
                  )
              )
              OR (
                  pb.owner_kind = 'agent_run'
                  AND run.status IN ('success', 'error', 'timeout', 'cancelled')
              )
          )
        ORDER BY pb.managed_execution_id, pb.credential_generation
    LOOP
        result_count := revoke_principal(
            candidate.managed_execution_id,
            candidate.credential_generation
        );
        IF result_count > 0 THEN
            reconciled_count := reconciled_count + result_count;
        ELSIF result_count < 0 THEN
            retry_pending := TRUE;
        END IF;
    END LOOP;

    FOR orphan_role IN
        SELECT roles.rolname
        FROM pg_roles AS roles
        WHERE roles.rolname ~ '^(gobby_agent_[0-9a-f]{32}|gobby_ix_([0-9a-f]{16}|[A-Za-z0-9]{1,8}_[0-9a-f]{8}_[0-9a-f]{8})|gobby_mnt_[0-9a-f]{32})_[1-9][0-9]*$'
          AND NOT EXISTS (
              SELECT 1 FROM principal_bindings AS binding
              WHERE binding.role_name = roles.rolname
          )
        ORDER BY roles.rolname
    LOOP
        EXECUTE format('ALTER ROLE %I NOLOGIN', orphan_role.rolname);
        PERFORM pg_terminate_backend(pid, 5000)
        FROM pg_stat_activity
        WHERE usename = orphan_role.rolname::TEXT
          AND pid <> pg_backend_pid();
        SELECT count(*) INTO remaining_sessions
        FROM pg_stat_activity
        WHERE usename = orphan_role.rolname::TEXT;
        IF remaining_sessions <> 0 THEN
            INSERT INTO orphan_revocation_retries (
                role_name, revocation_attempts, next_retry_at, last_failure, updated_at
            ) VALUES (
                orphan_role.rolname, 1, clock_timestamp() + INTERVAL '15 seconds',
                'active_sessions_remaining', clock_timestamp()
            )
            ON CONFLICT (role_name) DO UPDATE
            SET revocation_attempts = orphan_revocation_retries.revocation_attempts + 1,
                next_retry_at = EXCLUDED.next_retry_at,
                last_failure = EXCLUDED.last_failure,
                updated_at = EXCLUDED.updated_at;
            retry_pending := TRUE;
            CONTINUE;
        END IF;
        EXECUTE format('DROP ROLE %I', orphan_role.rolname);
        DELETE FROM orphan_revocation_retries
        WHERE role_name = orphan_role.rolname;
        reconciled_count := reconciled_count + 1;
    END LOOP;
    IF retry_pending THEN
        RETURN -1;
    END IF;
    RETURN reconciled_count;
END
$_$;
