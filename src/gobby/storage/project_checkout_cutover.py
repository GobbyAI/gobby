"""Receipted migration from legacy project paths to machine-owned checkouts."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gobby.storage.account_identity_cutover import admit_cutover_campaign
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.utils.checkout_root import validate_campaign_checkout_root

PROJECT_CHECKOUT_CUTOVER_CAMPAIGN: Literal["project-checkout-cutover"] = "project-checkout-cutover"
# Every baseline@375 receipt a pre-#19651 hub may hold: the pre-epic baseline
# and its in-place text edits. gcore's PROJECT_CHECKOUT_PREDECESSOR_CHECKSUMS
# is kept identical by a runner test that parses this block.
PROJECT_CHECKOUT_PREDECESSOR_CHECKSUMS: frozenset[str] = frozenset(
    {
        "84eb875cb839f6f61219f3f3fd54a5befc3abf38f01461d96780e956dc1864d8",
        "ec222a7f8b3c486abfff05eda4ed02995d272a132ad2fdadb1dd90edbccb2ce1",
        "ece3754752dbc72aaff4bbd3ebaa91a41305e4899e180012f8429c4f7467b1bf",
        "8467fc42e29fec1f58986e7ac141c3cdcf8c6a417c61c73ff3cca63241e2a2cf",
    }
)
_RESOLVED_STATUSES = frozenset({"covered", "excluded", "ready"})
_PREFLIGHT_REMEDIES: dict[str, str] = {
    "invalid_checkout_root": (
        "restore the directory and its .gobby/project.json marker at the legacy root, or "
        "soft-delete the project (`gobby projects delete <name> --confirm <name>`) so the "
        "cutover excludes it"
    ),
    "no_candidate_machine": (
        "soft-delete the project (`gobby projects delete <name> --confirm <name>`), or run "
        "`gobby projects rebind <name> <path>` on the owning machine after the cutover"
    ),
    "foreign_machine": (
        "run the campaign from the owning machine, or run `gobby projects rebind <name> "
        "<path>` there after the cutover"
    ),
    "multiple_candidate_machines": (
        "run the campaign from the owning machine, or run `gobby projects rebind <name> "
        "<path>` on each machine after the cutover"
    ),
}


class ProjectCheckoutCutoverError(RuntimeError):
    """The checkout cutover cannot safely continue."""


@dataclass(frozen=True)
class ExistingCheckoutEvidence:
    machine_id: uuid.UUID
    root_path: str


@dataclass(frozen=True)
class CandidateMachineEvidence:
    machine_id: uuid.UUID
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ProjectCheckoutInsert:
    machine_id: uuid.UUID
    project_id: uuid.UUID
    root_path: str


@dataclass(frozen=True)
class ProjectCheckoutCutoverProjectEvidence:
    project_id: uuid.UUID
    name: str
    legacy_root: str
    candidate_machine_ids: tuple[uuid.UUID, ...]
    evidence_sources: tuple[CandidateMachineEvidence, ...]
    existing_checkouts: tuple[ExistingCheckoutEvidence, ...]
    exclusion_reason: str | None
    resolution_status: str
    resolution_reason: str | None


@dataclass(frozen=True)
class ProjectCheckoutCutoverPreflight:
    projects: tuple[ProjectCheckoutCutoverProjectEvidence, ...]
    expected_inserts: tuple[ProjectCheckoutInsert, ...]

    @property
    def blocked(self) -> bool:
        return any(project.resolution_status not in _RESOLVED_STATUSES for project in self.projects)

    def to_json(self) -> dict[str, object]:
        return _preflight_json(self)


@dataclass(frozen=True)
class ProjectCheckoutCutoverEvidence:
    predecessor_checksum: str
    target_checksum: str
    projects: tuple[ProjectCheckoutCutoverProjectEvidence, ...]
    expected_inserts: tuple[ProjectCheckoutInsert, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "predecessor_checksum": self.predecessor_checksum,
            "target_checksum": self.target_checksum,
            **_preflight_json(_evidence_preflight(self)),
        }


def _preflight_json(preflight: ProjectCheckoutCutoverPreflight) -> dict[str, object]:
    return {
        "projects": [
            {
                "project_id": str(project.project_id),
                "name": project.name,
                "legacy_root": project.legacy_root,
                "candidate_machine_ids": [
                    str(machine_id) for machine_id in project.candidate_machine_ids
                ],
                "evidence_sources": [
                    {
                        "machine_id": str(candidate.machine_id),
                        "sources": list(candidate.sources),
                    }
                    for candidate in project.evidence_sources
                ],
                "existing_checkouts": [
                    {
                        "machine_id": str(checkout.machine_id),
                        "root_path": checkout.root_path,
                    }
                    for checkout in project.existing_checkouts
                ],
                "exclusion_reason": project.exclusion_reason,
                "resolution_status": project.resolution_status,
                "resolution_reason": project.resolution_reason,
            }
            for project in preflight.projects
        ],
        "expected_inserts": [
            {
                "machine_id": str(insert.machine_id),
                "project_id": str(insert.project_id),
                "root_path": insert.root_path,
            }
            for insert in preflight.expected_inserts
        ],
    }


def admit_project_checkout_campaign(connection: psycopg.Connection[Any]) -> None:
    """Admit the campaign and bootstrap checkout storage without changing the receipt."""
    admit_cutover_campaign(
        connection,
        predecessor_checksum=_require_predecessor_receipt(connection),
    )
    _bootstrap_project_checkouts(connection)
    _verify_project_checkouts_shape(connection)


def _bootstrap_project_checkouts(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_checkouts (
            machine_id uuid NOT NULL,
            project_id uuid NOT NULL,
            root_path text NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            updated_at timestamp with time zone DEFAULT now() NOT NULL
        );

        ALTER TABLE ONLY project_checkouts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE ONLY project_checkouts FORCE ROW LEVEL SECURITY;

        DO $constraints$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'project_checkouts_pkey'
                  AND conrelid = 'project_checkouts'::regclass
            ) THEN
                ALTER TABLE ONLY project_checkouts
                ADD CONSTRAINT project_checkouts_pkey PRIMARY KEY (machine_id, project_id);
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'project_checkouts_machine_id_root_path_key'
                  AND conrelid = 'project_checkouts'::regclass
            ) THEN
                ALTER TABLE ONLY project_checkouts
                ADD CONSTRAINT project_checkouts_machine_id_root_path_key
                UNIQUE (machine_id, root_path);
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'project_checkouts_machine_id_fkey'
                  AND conrelid = 'project_checkouts'::regclass
            ) THEN
                ALTER TABLE ONLY project_checkouts
                ADD CONSTRAINT project_checkouts_machine_id_fkey
                FOREIGN KEY (machine_id) REFERENCES machines(id) ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'project_checkouts_project_id_fkey'
                  AND conrelid = 'project_checkouts'::regclass
            ) THEN
                ALTER TABLE ONLY project_checkouts
                ADD CONSTRAINT project_checkouts_project_id_fkey
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;
            END IF;
        END
        $constraints$;

        DROP POLICY IF EXISTS gobby_agent_project_scope ON project_checkouts;
        DROP POLICY IF EXISTS gobby_daemon_runtime_access ON project_checkouts;
        CREATE POLICY gobby_daemon_runtime_access ON project_checkouts
        TO gobby_daemon_runtime USING (TRUE) WITH CHECK (TRUE);

        DROP POLICY IF EXISTS gobby_migration_owner_access ON project_checkouts;
        CREATE POLICY gobby_migration_owner_access ON project_checkouts
        TO CURRENT_USER USING (TRUE) WITH CHECK (TRUE);

        DROP POLICY IF EXISTS gobby_gcode_project_read ON project_checkouts;
        CREATE POLICY gobby_gcode_project_read ON project_checkouts
        FOR SELECT TO gobby_gcode_capability
        USING (
            project_id = gobby_agent_auth.current_project_id()
            AND machine_id = gobby_agent_auth.current_machine_id()
        );

        DROP POLICY IF EXISTS gobby_gcode_project_update ON project_checkouts;
        CREATE POLICY gobby_gcode_project_update ON project_checkouts
        FOR UPDATE TO gobby_gcode_capability
        USING (
            project_id = gobby_agent_auth.current_project_id()
            AND machine_id = gobby_agent_auth.current_machine_id()
        )
        WITH CHECK (false);

        GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE project_checkouts
        TO gobby_daemon_runtime;
        GRANT SELECT (deleted_at) ON TABLE projects TO gobby_gcode_capability;
        GRANT SELECT (repo_path) ON TABLE projects TO gobby_gcode_capability;
        GRANT SELECT (machine_id, project_id, root_path),
              UPDATE (machine_id, project_id, root_path)
        ON TABLE project_checkouts TO gobby_gcode_capability;
        """
    )


def _verify_project_checkouts_shape(connection: Any) -> None:
    relation = connection.execute(
        """
        SELECT relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE oid = to_regclass('project_checkouts')
        """
    ).fetchone()
    if relation is None or not relation["relrowsecurity"] or not relation["relforcerowsecurity"]:
        raise ProjectCheckoutCutoverError("project_checkouts must enforce row-level security")
    columns = connection.execute(
        """
        SELECT attname, format_type(atttypid, atttypmod) AS data_type, attnotnull
        FROM pg_attribute
        WHERE attrelid = 'project_checkouts'::regclass
          AND attnum > 0
          AND NOT attisdropped
        ORDER BY attnum
        """
    ).fetchall()
    observed_columns = tuple(
        (str(row["attname"]), str(row["data_type"]), bool(row["attnotnull"])) for row in columns
    )
    expected_columns = (
        ("machine_id", "uuid", True),
        ("project_id", "uuid", True),
        ("root_path", "text", True),
        ("created_at", "timestamp with time zone", True),
        ("updated_at", "timestamp with time zone", True),
    )
    if observed_columns != expected_columns:
        raise ProjectCheckoutCutoverError(
            f"Unexpected project_checkouts columns: {observed_columns!r}"
        )
    constraints = {
        str(row["conname"]): str(row["definition"])
        for row in connection.execute(
            """
            SELECT conname, pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid = 'project_checkouts'::regclass
            """
        ).fetchall()
    }
    expected_constraints = {
        "project_checkouts_pkey",
        "project_checkouts_machine_id_root_path_key",
        "project_checkouts_machine_id_fkey",
        "project_checkouts_project_id_fkey",
        "project_checkouts_machine_id_not_null",
        "project_checkouts_project_id_not_null",
        "project_checkouts_root_path_not_null",
        "project_checkouts_created_at_not_null",
        "project_checkouts_updated_at_not_null",
    }
    if set(constraints) != expected_constraints:
        raise ProjectCheckoutCutoverError(
            f"Unexpected project_checkouts constraints: {sorted(constraints)}"
        )
    policies = {
        str(row["policyname"])
        for row in connection.execute(
            "SELECT policyname FROM pg_policies WHERE tablename = 'project_checkouts'"
        ).fetchall()
    }
    expected_policies = {
        "gobby_daemon_runtime_access",
        "gobby_migration_owner_access",
        "gobby_gcode_project_read",
        "gobby_gcode_project_update",
    }
    if policies != expected_policies:
        raise ProjectCheckoutCutoverError(
            f"Unexpected project_checkouts policies: {sorted(policies)}"
        )
    privileges = connection.execute(
        """
        SELECT
            has_table_privilege(
                'gobby_daemon_runtime', 'project_checkouts',
                'SELECT, INSERT, DELETE, UPDATE'
            ) AS daemon_dml,
            has_column_privilege(
                'gobby_gcode_capability', 'project_checkouts', 'root_path', 'SELECT'
            ) AS gcode_select,
            has_column_privilege(
                'gobby_gcode_capability', 'project_checkouts', 'root_path', 'UPDATE'
            ) AS gcode_update
        """
    ).fetchone()
    if privileges is None or not all(bool(value) for value in privileges.values()):
        raise ProjectCheckoutCutoverError("project_checkouts grants are incomplete")


def preflight_project_checkout_cutover(database_url: str) -> ProjectCheckoutCutoverPreflight:
    """Collect deterministic legacy-project and candidate-machine evidence."""
    db = PostgresHubDatabase(database_url)
    try:
        return _collect_preflight(db)
    finally:
        db.close()


def _collect_preflight(db: PostgresHubDatabase) -> ProjectCheckoutCutoverPreflight:
    with db.transaction() as connection:
        projects = connection.execute(
            """
            SELECT id, name, repo_path, deleted_at
            FROM projects
            WHERE repo_path IS NOT NULL
              AND BTRIM(repo_path) <> ''
            ORDER BY id
            """
        ).fetchall()
        candidates = connection.execute(
            """
            SELECT machine_id, project_id, source
            FROM (
                SELECT machine_id, project_id, 'sessions'::text AS source FROM sessions
                UNION ALL
                SELECT machine_id, project_id, 'code_indexed_project_states'::text
                FROM code_indexed_project_states
                UNION ALL
                SELECT machine_id, project_id, 'worktrees'::text FROM worktrees
                UNION ALL
                SELECT machine_id, project_id, 'clones'::text FROM clones
            ) AS evidence
            ORDER BY project_id, machine_id, source
            """
        ).fetchall()
        checkouts = _existing_checkout_rows(connection)
    return _build_preflight(db, projects, candidates, checkouts)


def _existing_checkout_rows(connection: Any) -> list[Mapping[str, Any]]:
    """Read project_checkouts, treating the not-yet-bootstrapped table as empty."""
    relation = connection.execute(
        "SELECT pg_catalog.to_regclass('project_checkouts') AS relation"
    ).fetchone()
    if relation is None or relation["relation"] is None:
        return []
    rows: list[Mapping[str, Any]] = connection.execute(
        """
        SELECT machine_id, project_id, root_path
        FROM project_checkouts
        ORDER BY project_id, machine_id
        """
    ).fetchall()
    return rows


def _build_preflight(
    db: PostgresHubDatabase,
    projects: list[Mapping[str, Any]],
    candidates: list[Mapping[str, Any]],
    checkouts: list[Mapping[str, Any]],
) -> ProjectCheckoutCutoverPreflight:
    candidate_sources: dict[uuid.UUID, dict[uuid.UUID, set[str]]] = {}
    for row in candidates:
        project_id = uuid.UUID(str(row["project_id"]))
        machine_id = uuid.UUID(str(row["machine_id"]))
        candidate_sources.setdefault(project_id, {}).setdefault(machine_id, set()).add(
            str(row["source"])
        )
    checkout_rows: dict[uuid.UUID, list[ExistingCheckoutEvidence]] = {}
    for row in checkouts:
        project_id = uuid.UUID(str(row["project_id"]))
        checkout_rows.setdefault(project_id, []).append(
            ExistingCheckoutEvidence(
                machine_id=uuid.UUID(str(row["machine_id"])),
                root_path=str(row["root_path"]),
            )
        )

    evidence: list[ProjectCheckoutCutoverProjectEvidence] = []
    expected_inserts: list[ProjectCheckoutInsert] = []
    for row in projects:
        project_id = uuid.UUID(str(row["id"]))
        project_candidates = candidate_sources.get(project_id, {})
        existing = tuple(checkout_rows.get(project_id, ()))
        excluded = str(project_id) in CHECKOUT_FREE_PROJECT_IDS
        unresolved = tuple(
            machine_id
            for machine_id in sorted(project_candidates)
            if machine_id not in {checkout.machine_id for checkout in existing}
        )
        resolution_reason: str | None = None
        exclusion_reason: str | None = None
        if excluded:
            status = "excluded"
            exclusion_reason = "checkout_free_sentinel"
        elif not unresolved and existing:
            status = "covered"
        elif row.get("deleted_at") is not None:
            # A soft-deleted project without an authoritative checkout never
            # needs one: no insert, no filesystem access, nothing to block on.
            status = "excluded"
            exclusion_reason = "soft_deleted"
        elif not project_candidates:
            status = "no_candidate_machine"
            resolution_reason = "No machine-owned evidence; run `gobby projects rebind`"
        elif len(unresolved) > 1:
            status = "multiple_candidate_machines"
            resolution_reason = (
                f"Multiple unresolved candidate machines: {', '.join(map(str, unresolved))}"
            )
        else:
            machine_id = unresolved[0]
            try:
                local_machine_id = require_local_machine_id(
                    str(machine_id),
                    resource_kind="project_checkout",
                    resource_id=str(project_id),
                )
                root_path = validate_campaign_checkout_root(
                    db,
                    project_id=str(project_id),
                    machine_id=local_machine_id,
                    candidate_path=str(row["repo_path"]),
                    expected_marker_id=str(project_id),
                )
            except MachineOwnershipMismatchError as exc:
                status = "foreign_machine"
                resolution_reason = str(exc)
            except ValueError as exc:
                status = "invalid_checkout_root"
                resolution_reason = str(exc)
            else:
                expected_inserts.append(ProjectCheckoutInsert(machine_id, project_id, root_path))
                status = "ready"
        evidence.append(
            ProjectCheckoutCutoverProjectEvidence(
                project_id=project_id,
                name=str(row["name"]),
                legacy_root=str(row["repo_path"]),
                candidate_machine_ids=tuple(sorted(project_candidates)),
                evidence_sources=tuple(
                    CandidateMachineEvidence(machine_id, tuple(sorted(sources)))
                    for machine_id, sources in sorted(project_candidates.items())
                ),
                existing_checkouts=existing,
                exclusion_reason=exclusion_reason,
                resolution_status=status,
                resolution_reason=resolution_reason,
            )
        )
    return ProjectCheckoutCutoverPreflight(
        projects=tuple(evidence),
        expected_inserts=tuple(expected_inserts),
    )


def apply_project_checkout_cutover(
    database_url: str,
    *,
    epoch_id: uuid.UUID,
    batch_id: uuid.UUID,
    preflight: ProjectCheckoutCutoverPreflight,
    target_checksum: str,
) -> ProjectCheckoutCutoverEvidence:
    """Apply the checkout identity transition in one prompt-free transaction."""
    if preflight.blocked:
        raise ProjectCheckoutCutoverError(blocked_preflight_message(preflight))
    db = PostgresHubDatabase(database_url)
    try:
        with db.transaction() as connection:
            connection.execute(
                """
                LOCK TABLE projects, project_checkouts, sessions,
                           code_indexed_project_states, worktrees, clones,
                           destructive_batches, schema_migrations
                IN SHARE ROW EXCLUSIVE MODE
                """
            )
            predecessor_checksum = _require_predecessor_receipt(connection)
            evidence = ProjectCheckoutCutoverEvidence(
                predecessor_checksum=predecessor_checksum,
                target_checksum=target_checksum,
                projects=preflight.projects,
                expected_inserts=preflight.expected_inserts,
            )
            _verify_project_checkouts_shape(connection)
            current = _collect_preflight(db)
            if current != preflight:
                raise ProjectCheckoutCutoverError(
                    "Datastore changed after project checkout preflight; restart the campaign"
                )
            manager = LocalProjectCheckoutManager(db)
            created: list[ProjectCheckoutInsert] = []
            for expected in preflight.expected_inserts:
                checkout, was_created = manager.register(
                    str(expected.machine_id),
                    str(expected.project_id),
                    expected.root_path,
                )
                observed = ProjectCheckoutInsert(
                    uuid.UUID(checkout.machine_id),
                    uuid.UUID(checkout.project_id),
                    checkout.root_path,
                )
                if observed != expected or not was_created:
                    raise ProjectCheckoutCutoverError(
                        "LocalProjectCheckoutManager.register result did not match "
                        f"the recorded insert {expected!r}"
                    )
                created.append(observed)
            if tuple(created) != preflight.expected_inserts:
                raise ProjectCheckoutCutoverError("Recorded and created checkout sets differ")
            _verify_checkout_coverage(connection, preflight)
            connection.execute("ALTER TABLE projects DROP COLUMN repo_path")
            _replace_resolve_tool_session(connection)
            connection.execute(
                """
                GRANT SELECT (id, name, deleted_at)
                ON TABLE projects TO gobby_gcode_capability
                """
            )
            batch_row = connection.execute(
                """
                UPDATE destructive_batches
                SET intent = intent || %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND maintenance_epoch_id = %s
                  AND campaign = %s
                RETURNING id
                """,
                (
                    Jsonb({"project_checkout_cutover": evidence.to_json()}),
                    batch_id,
                    epoch_id,
                    PROJECT_CHECKOUT_CUTOVER_CAMPAIGN,
                ),
            ).fetchone()
            if batch_row is None:
                raise ProjectCheckoutCutoverError(
                    f"Cutover batch {batch_id} is missing or belongs to another epoch"
                )
            updated = connection.execute(
                """
                UPDATE schema_migrations
                SET checksum = %s,
                    applied_at = NOW()
                WHERE version = 375
                  AND filename = 'baseline@375'
                  AND checksum = %s
                RETURNING version
                """,
                (target_checksum, predecessor_checksum),
            ).fetchone()
            if updated is None:
                raise ProjectCheckoutCutoverError("Predecessor receipt changed during cutover")
    finally:
        db.close()
    return evidence


def record_project_checkout_preflight(
    database_url: str,
    *,
    epoch_id: uuid.UUID,
    batch_id: uuid.UUID,
    preflight: ProjectCheckoutCutoverPreflight,
) -> None:
    """Persist rehearsal or refusal evidence before the destructive transaction."""
    db = PostgresHubDatabase(database_url)
    try:
        with db.transaction() as connection:
            row = connection.execute(
                """
                UPDATE destructive_batches
                SET intent = intent || %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND maintenance_epoch_id = %s
                  AND campaign = %s
                RETURNING id
                """,
                (
                    Jsonb({"project_checkout_cutover_preflight": preflight.to_json()}),
                    batch_id,
                    epoch_id,
                    PROJECT_CHECKOUT_CUTOVER_CAMPAIGN,
                ),
            ).fetchone()
            if row is None:
                raise ProjectCheckoutCutoverError(
                    f"Cutover batch {batch_id} is missing or belongs to another epoch"
                )
    finally:
        db.close()


def project_checkout_cutover_already_applied(
    database_url: str,
    *,
    batch_id: uuid.UUID,
    target_checksum: str,
) -> bool:
    """Return whether a committed cutover can resume without rerunning preflight."""
    db = PostgresHubDatabase(database_url)
    try:
        with db.transaction() as connection:
            checksum = _receipt_checksum(connection)
            if checksum in PROJECT_CHECKOUT_PREDECESSOR_CHECKSUMS:
                return False
            if checksum != target_checksum:
                raise ProjectCheckoutCutoverError(
                    f"Unexpected baseline receipt during resume: {checksum or 'none'}"
                )
            evidence = _load_evidence(connection, batch_id)
            if evidence.target_checksum != target_checksum:
                raise ProjectCheckoutCutoverError("Durable cutover evidence targets another schema")
            return True
    finally:
        db.close()


def verify_project_checkout_cutover(
    database_url: str,
    *,
    batch_id: uuid.UUID,
    target_checksum: str,
) -> ProjectCheckoutCutoverEvidence:
    """Verify the committed checkout identity and authorization contract."""
    db = PostgresHubDatabase(database_url)
    try:
        with db.transaction() as connection:
            if _receipt_checksum(connection) != target_checksum:
                raise ProjectCheckoutCutoverError("Target baseline receipt is not installed")
            evidence = _load_evidence(connection, batch_id)
            if evidence.target_checksum != target_checksum:
                raise ProjectCheckoutCutoverError("Durable cutover evidence has the wrong target")
            legacy_column = connection.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'projects'
                  AND column_name = 'repo_path'
                """
            ).fetchone()
            if legacy_column is not None:
                raise ProjectCheckoutCutoverError("projects.repo_path still exists")
            _verify_checkout_coverage(connection, _evidence_preflight(evidence))
            resolver = connection.execute(
                """
                SELECT
                    pg_get_function_result(candidate.oid) AS result_type,
                    candidate.prosecdef,
                    candidate.proconfig = ARRAY['search_path=pg_catalog'] AS safe_search_path,
                    has_function_privilege(
                        'gobby_daemon_runtime', candidate.oid, 'EXECUTE'
                    ) AS daemon_execute
                FROM pg_proc AS candidate
                JOIN pg_namespace AS namespace ON namespace.oid = candidate.pronamespace
                WHERE namespace.nspname = 'gobby_agent_auth'
                  AND candidate.proname = 'resolve_tool_session'
                  AND candidate.proargtypes = '2950'::oidvector
                """
            ).fetchone()
            if resolver is None:
                raise ProjectCheckoutCutoverError("resolve_tool_session(UUID) is missing")
            result_type = " ".join(str(resolver["result_type"]).lower().split())
            expected_result = (
                "table(session_id uuid, project_id uuid, machine_id uuid, root_path text)"
            )
            if result_type != expected_result:
                raise ProjectCheckoutCutoverError(
                    f"resolve_tool_session has unexpected result type {result_type!r}"
                )
            if not resolver["prosecdef"] or not resolver["daemon_execute"]:
                raise ProjectCheckoutCutoverError("resolve_tool_session security is incomplete")
            if not resolver["safe_search_path"]:
                raise ProjectCheckoutCutoverError("resolve_tool_session search_path is unsafe")
            grants = connection.execute(
                """
                SELECT
                    has_column_privilege(
                        'gobby_gcode_capability', 'projects', 'id', 'SELECT'
                    ) AS id_select,
                    has_column_privilege(
                        'gobby_gcode_capability', 'projects', 'name', 'SELECT'
                    ) AS name_select,
                    has_column_privilege(
                        'gobby_gcode_capability', 'projects', 'deleted_at', 'SELECT'
                    ) AS deleted_at_select
                """
            ).fetchone()
            if grants is None or not all(bool(value) for value in grants.values()):
                raise ProjectCheckoutCutoverError("gcode project-column grants are incomplete")
            return evidence
    finally:
        db.close()


def _load_evidence(
    connection: Any,
    batch_id: uuid.UUID,
) -> ProjectCheckoutCutoverEvidence:
    row = connection.execute(
        """
        SELECT intent
        FROM destructive_batches
        WHERE id = %s
          AND campaign = %s
        """,
        (batch_id, PROJECT_CHECKOUT_CUTOVER_CAMPAIGN),
    ).fetchone()
    if row is None:
        raise ProjectCheckoutCutoverError(f"Cutover batch {batch_id} does not exist")
    intent = row["intent"]
    if isinstance(intent, str):
        try:
            intent = json.loads(intent)
        except json.JSONDecodeError as exc:
            raise ProjectCheckoutCutoverError("Cutover batch intent is invalid JSON") from exc
    raw = intent.get("project_checkout_cutover") if isinstance(intent, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ProjectCheckoutCutoverError("Cutover batch has no durable checkout evidence")
    try:
        projects = tuple(
            ProjectCheckoutCutoverProjectEvidence(
                project_id=uuid.UUID(str(project["project_id"])),
                name=str(project["name"]),
                legacy_root=str(project["legacy_root"]),
                candidate_machine_ids=tuple(
                    uuid.UUID(str(machine_id)) for machine_id in project["candidate_machine_ids"]
                ),
                evidence_sources=tuple(
                    CandidateMachineEvidence(
                        machine_id=uuid.UUID(str(candidate["machine_id"])),
                        sources=tuple(str(source) for source in candidate["sources"]),
                    )
                    for candidate in project["evidence_sources"]
                ),
                existing_checkouts=tuple(
                    ExistingCheckoutEvidence(
                        machine_id=uuid.UUID(str(checkout["machine_id"])),
                        root_path=str(checkout["root_path"]),
                    )
                    for checkout in project["existing_checkouts"]
                ),
                exclusion_reason=(
                    None
                    if project["exclusion_reason"] is None
                    else str(project["exclusion_reason"])
                ),
                resolution_status=str(project["resolution_status"]),
                resolution_reason=(
                    None
                    if project["resolution_reason"] is None
                    else str(project["resolution_reason"])
                ),
            )
            for project in raw["projects"]
        )
        expected_inserts = tuple(
            ProjectCheckoutInsert(
                machine_id=uuid.UUID(str(insert["machine_id"])),
                project_id=uuid.UUID(str(insert["project_id"])),
                root_path=str(insert["root_path"]),
            )
            for insert in raw["expected_inserts"]
        )
        return ProjectCheckoutCutoverEvidence(
            predecessor_checksum=str(raw["predecessor_checksum"]),
            target_checksum=str(raw["target_checksum"]),
            projects=projects,
            expected_inserts=expected_inserts,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectCheckoutCutoverError("Cutover checkout evidence is invalid") from exc


def _evidence_preflight(
    evidence: ProjectCheckoutCutoverEvidence,
) -> ProjectCheckoutCutoverPreflight:
    return ProjectCheckoutCutoverPreflight(
        projects=evidence.projects,
        expected_inserts=evidence.expected_inserts,
    )


def _verify_checkout_coverage(
    connection: Any,
    preflight: ProjectCheckoutCutoverPreflight,
) -> None:
    expected = {
        (insert.machine_id, insert.project_id, insert.root_path)
        for insert in preflight.expected_inserts
    }
    authoritative = {
        (checkout.machine_id, project.project_id, checkout.root_path)
        for project in preflight.projects
        for checkout in project.existing_checkouts
    }
    rows = connection.execute(
        "SELECT machine_id, project_id, root_path FROM project_checkouts"
    ).fetchall()
    observed = {
        (
            uuid.UUID(str(row["machine_id"])),
            uuid.UUID(str(row["project_id"])),
            str(row["root_path"]),
        )
        for row in rows
    }
    missing = (expected | authoritative) - observed
    if missing:
        raise ProjectCheckoutCutoverError(f"Checkout coverage is incomplete: {sorted(missing)!r}")


def _replace_resolve_tool_session(connection: Any) -> None:
    schema_row = connection.execute("SELECT current_schema() AS schema_name").fetchone()
    if schema_row is None:
        raise ProjectCheckoutCutoverError("Could not resolve the cutover schema")
    schema_name = str(schema_row["schema_name"])
    connection.execute("DROP FUNCTION gobby_agent_auth.resolve_tool_session(UUID)")
    connection.execute(
        sql.SQL(
            """
            CREATE FUNCTION gobby_agent_auth.resolve_tool_session(p_session_id UUID)
            RETURNS TABLE(
                session_id UUID,
                project_id UUID,
                machine_id UUID,
                root_path TEXT
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            SELECT session.id, session.project_id, session.machine_id, checkout.root_path
            FROM {}.sessions AS session
            LEFT JOIN {}.project_checkouts AS checkout
              ON checkout.machine_id = session.machine_id
             AND checkout.project_id = session.project_id
            WHERE session.id = p_session_id
              AND COALESCE(session.status, 'active') NOT IN ('expired', 'deleted')
            $function$
            """
        ).format(sql.Identifier(schema_name), sql.Identifier(schema_name))
    )
    connection.execute(
        "ALTER FUNCTION gobby_agent_auth.resolve_tool_session(UUID) OWNER TO CURRENT_USER"
    )
    connection.execute(
        "REVOKE ALL ON FUNCTION gobby_agent_auth.resolve_tool_session(UUID) FROM PUBLIC"
    )
    connection.execute(
        "GRANT EXECUTE ON FUNCTION gobby_agent_auth.resolve_tool_session(UUID) "
        "TO gobby_daemon_runtime"
    )


def _require_predecessor_receipt(connection: Any) -> str:
    """Return the observed baseline receipt, which must be a known predecessor."""
    checksum = _receipt_checksum(connection)
    if checksum is None:
        raise ProjectCheckoutCutoverError("Baseline receipt baseline@375 is missing")
    if checksum not in PROJECT_CHECKOUT_PREDECESSOR_CHECKSUMS:
        raise ProjectCheckoutCutoverError(
            f"Project checkout cutover receipt mismatch: {checksum} is not a known predecessor"
        )
    return checksum


def _receipt_checksum(connection: Any) -> str | None:
    row = connection.execute(
        """
        SELECT checksum
        FROM schema_migrations
        WHERE version = 375
          AND filename = 'baseline@375'
        """
    ).fetchone()
    return None if row is None else str(row["checksum"])


def read_baseline_receipt_checksum(database_url: str) -> str | None:
    """Read the baseline@375 receipt over a plain connection; touches nothing else."""
    with psycopg.connect(
        database_url,
        autocommit=True,
        connect_timeout=5,
        row_factory=dict_row,
        application_name="gobby-hub-maintenance-receipt",
    ) as connection:
        return _receipt_checksum(connection)


def blocked_preflight_message(preflight: ProjectCheckoutCutoverPreflight) -> str:
    """Name every unresolved project together with the remedy its status calls for."""
    blocked = [
        f"{project.name} ({project.project_id}) {project.resolution_status} at "
        f"{project.legacy_root}: "
        + _PREFLIGHT_REMEDIES.get(project.resolution_status, "resolve it before rerunning")
        for project in preflight.projects
        if project.resolution_status not in _RESOLVED_STATUSES
    ]
    return (
        "Project checkout cutover preflight is unresolved: "
        + "; ".join(blocked)
        + ". Re-check read-only with `gobby hub-maintenance preflight "
        f"{PROJECT_CHECKOUT_CUTOVER_CAMPAIGN}`."
    )
