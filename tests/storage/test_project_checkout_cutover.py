from __future__ import annotations

import ast
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

import gobby.storage.project_checkout_cutover as cutover
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.project_checkout_cutover import preflight_project_checkout_cutover
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import (
    PERSONAL_PROJECT_ID,
    IsolatedAgentProjectPathError,
    LocalProjectManager,
)
from gobby.storage.schema_contract import apply_schema
from gobby.storage.sessions import SessionManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.checkout_root import validate_checkout_root
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    patch_local_machine_id,
    write_project_marker,
)
from tests.fixtures.postgres import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    TEST_USER_NAME,
    TEST_USER_PASSWORD_HASH,
    isolated_test_schema,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The receipt the live hub holds today (a v375 in-place text edit) and the
# pre-epic merge-base baseline; both must admit the cutover.
_LIVE_PREDECESSOR_CHECKSUM = "ece3754752dbc72aaff4bbd3ebaa91a41305e4899e180012f8429c4f7467b1bf"
_MERGE_BASE_PREDECESSOR_CHECKSUM = (
    "84eb875cb839f6f61219f3f3fd54a5befc3abf38f01461d96780e956dc1864d8"
)


def _install_predecessor_resolver(connection: psycopg.Connection[dict[str, object]]) -> None:
    schema_row = connection.execute("SELECT current_schema() AS schema_name").fetchone()
    assert schema_row is not None
    schema_name = str(schema_row["schema_name"])
    connection.execute("DROP FUNCTION IF EXISTS gobby_agent_auth.resolve_tool_session(UUID)")
    connection.execute(
        sql.SQL(
            """
            CREATE FUNCTION gobby_agent_auth.resolve_tool_session(p_session_id UUID)
            RETURNS TABLE(session_id UUID, project_id UUID, root_path TEXT)
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            SELECT session.id, session.project_id, project.repo_path
            FROM {}.sessions AS session
            JOIN {}.projects AS project ON project.id = session.project_id
            WHERE session.id = p_session_id
              AND COALESCE(session.status, 'active') NOT IN ('expired', 'deleted')
            $function$
            """
        ).format(sql.Identifier(schema_name), sql.Identifier(schema_name))
    )
    connection.execute(
        "REVOKE ALL ON FUNCTION gobby_agent_auth.resolve_tool_session(UUID) FROM PUBLIC"
    )
    connection.execute(
        "GRANT EXECUTE ON FUNCTION gobby_agent_auth.resolve_tool_session(UUID) "
        "TO gobby_daemon_runtime"
    )


def _admit_batch(
    connection: psycopg.Connection[dict[str, object]],
    *,
    epoch_id: uuid.UUID,
    batch_id: uuid.UUID,
) -> None:
    with connection.transaction():
        cutover.admit_project_checkout_campaign(connection)
        connection.execute(
            """
            INSERT INTO maintenance_epochs(id, campaign, opened_by, scope_note)
            VALUES (%s, %s, %s, %s)
            """,
            (epoch_id, cutover.PROJECT_CHECKOUT_CUTOVER_CAMPAIGN, "test", "test"),
        )
        connection.execute(
            """
            INSERT INTO destructive_batches(id, maintenance_epoch_id, campaign)
            VALUES (%s, %s, %s)
            """,
            (batch_id, epoch_id, cutover.PROJECT_CHECKOUT_CUTOVER_CAMPAIGN),
        )


@pytest.fixture
def predecessor_database(postgres_database_url: str) -> Iterator[str]:
    with isolated_test_schema(postgres_database_url, "checkoutcutover") as schema_name:
        apply_schema(postgres_database_url, schema=schema_name)
        parts = urlsplit(postgres_database_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={schema_name}"
        scoped_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
        with psycopg.connect(scoped_url, autocommit=True) as connection:
            # Keep the full migration ledger: a real hub carries every
            # numbered receipt alongside baseline@375.
            connection.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE version = 375",
                (_LIVE_PREDECESSOR_CHECKSUM,),
            )
            connection.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS repo_path text")
            connection.execute(
                """
                INSERT INTO users(id, email, name, password_hash)
                VALUES (%s, %s, %s, %s)
                """,
                (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
            )
        yield scoped_url


def test_preflight_records_no_candidate_machine_without_filesystem_access(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    root = tmp_path / "ordinary-project"
    write_project_marker(root, project_id=str(project_id), name="ordinary-project")
    with psycopg.connect(predecessor_database, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "ordinary-project", str(root)),
        )

    monkeypatch.setattr(
        "gobby.storage.project_checkout_cutover.validate_campaign_checkout_root",
        lambda *_args, **_kwargs: pytest.fail("filesystem validation must not run"),
    )

    preflight = preflight_project_checkout_cutover(predecessor_database)

    assert preflight.expected_inserts == ()
    assert len(preflight.projects) == 1
    evidence = preflight.projects[0]
    assert evidence.project_id == project_id
    assert evidence.candidate_machine_ids == ()
    assert evidence.existing_checkouts == ()
    assert evidence.resolution_status == "no_candidate_machine"
    assert preflight.blocked


def test_preflight_authorizes_machine_before_validating_candidate_root(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    root = tmp_path / "ordinary-project"
    write_project_marker(root, project_id=str(project_id), name="ordinary-project")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_id = uuid.UUID(insert_isolated_machine(db))
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "ordinary-project", str(root)),
        )
        db.execute(
            """
            INSERT INTO sessions(id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (uuid.uuid4(), "candidate-session", machine_id, "codex", project_id),
        )
    finally:
        db.close()

    calls: list[str] = []

    def require_local(provided: str | None, *, resource_kind: str, resource_id: str) -> str:
        calls.append("require_local_machine_id")
        assert provided == str(machine_id)
        assert resource_kind == "project_checkout"
        assert resource_id == str(project_id)
        return str(machine_id)

    def validate_root(*_args: object, **kwargs: object) -> str:
        calls.append("validate_campaign_checkout_root")
        assert kwargs["machine_id"] == str(machine_id)
        return str(root)

    monkeypatch.setattr(cutover, "require_local_machine_id", require_local)
    monkeypatch.setattr(cutover, "validate_campaign_checkout_root", validate_root)

    preflight = preflight_project_checkout_cutover(predecessor_database)

    assert calls == ["require_local_machine_id", "validate_campaign_checkout_root"]
    assert preflight.expected_inserts == (
        cutover.ProjectCheckoutInsert(machine_id, project_id, str(root)),
    )
    assert preflight.projects[0].resolution_status == "ready"
    assert not preflight.blocked


def test_admission_bootstraps_complete_checkout_shape_without_refreshing_receipt(
    predecessor_database: str,
) -> None:
    with psycopg.connect(
        predecessor_database,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        receipt_before = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 375"
        ).fetchone()
        assert receipt_before is not None
        connection.execute("DROP FUNCTION IF EXISTS gobby_agent_auth.resolve_tool_session(UUID)")
        connection.execute("DROP TABLE project_checkouts")

        with connection.transaction():
            cutover.admit_project_checkout_campaign(connection)

        receipt_after = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 375"
        ).fetchone()
        assert receipt_after == receipt_before
        relation = connection.execute(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE oid = 'project_checkouts'::regclass
            """
        ).fetchone()
        assert relation == {"relrowsecurity": True, "relforcerowsecurity": True}
        constraints = {
            row["conname"]
            for row in connection.execute(
                """
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'project_checkouts'::regclass
                """
            ).fetchall()
        }
        assert constraints == {
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
        policies = {
            row["policyname"]
            for row in connection.execute(
                "SELECT policyname FROM pg_policies WHERE tablename = 'project_checkouts'"
            ).fetchall()
        }
        assert policies == {
            "gobby_daemon_runtime_access",
            "gobby_migration_owner_access",
            "gobby_gcode_project_read",
            "gobby_gcode_project_update",
        }
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
        assert privileges == {
            "daemon_dml": True,
            "gcode_select": True,
            "gcode_update": True,
        }


def test_apply_registers_exact_checkout_then_replaces_legacy_identity(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    session_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    target_checksum = "b" * 64
    root = tmp_path / "ordinary-project"
    write_project_marker(root, project_id=str(project_id), name="ordinary-project")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_id = uuid.UUID(insert_isolated_machine(db))
    finally:
        db.close()
    patch_local_machine_id(monkeypatch, str(machine_id))
    with psycopg.connect(
        predecessor_database,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        connection.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "ordinary-project", str(root)),
        )
        connection.execute(
            """
            INSERT INTO sessions(id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session_id, "candidate-session", machine_id, "codex", project_id),
        )
        # A merge-base hub holds the pre-epic baseline receipt rather than
        # the live in-place edit; the cutover must CAS from whatever it sees.
        connection.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE version = 375",
            (_MERGE_BASE_PREDECESSOR_CHECKSUM,),
        )
        _install_predecessor_resolver(connection)
        _admit_batch(connection, epoch_id=epoch_id, batch_id=batch_id)

    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)
    evidence = cutover.apply_project_checkout_cutover(
        predecessor_database,
        epoch_id=epoch_id,
        batch_id=batch_id,
        preflight=preflight,
        target_checksum=target_checksum,
    )

    assert evidence.expected_inserts == preflight.expected_inserts
    assert evidence.predecessor_checksum == _MERGE_BASE_PREDECESSOR_CHECKSUM
    with psycopg.connect(
        predecessor_database,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        assert connection.execute(
            """
            SELECT machine_id, project_id, root_path
            FROM project_checkouts
            WHERE machine_id = %s AND project_id = %s
            """,
            (machine_id, project_id),
        ).fetchone() == {
            "machine_id": machine_id,
            "project_id": project_id,
            "root_path": str(root),
        }
        assert (
            connection.execute(
                """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'projects'
              AND column_name = 'repo_path'
            """
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 375"
        ).fetchone() == {"checksum": target_checksum}
        assert connection.execute(
            "SELECT * FROM gobby_agent_auth.resolve_tool_session(%s)",
            (session_id,),
        ).fetchone() == {
            "session_id": session_id,
            "project_id": project_id,
            "machine_id": machine_id,
            "root_path": str(root),
        }
        batch = connection.execute(
            "SELECT intent FROM destructive_batches WHERE id = %s",
            (batch_id,),
        ).fetchone()
        assert batch is not None
        assert (
            batch["intent"]["project_checkout_cutover"]["predecessor_checksum"]
            == _MERGE_BASE_PREDECESSOR_CHECKSUM
        )
    assert cutover.project_checkout_cutover_already_applied(
        predecessor_database,
        batch_id=batch_id,
        target_checksum=target_checksum,
    )
    cutover.verify_project_checkout_cutover(
        predecessor_database,
        batch_id=batch_id,
        target_checksum=target_checksum,
    )


def test_preflight_records_foreign_machine_refusal_before_filesystem_access(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    root = tmp_path / "foreign-project"
    write_project_marker(root, project_id=str(project_id), name="foreign-project")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_id = uuid.UUID(insert_isolated_machine(db))
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "foreign-project", str(root)),
        )
        db.execute(
            """
            INSERT INTO sessions(id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (uuid.uuid4(), "foreign-session", machine_id, "codex", project_id),
        )
    finally:
        db.close()

    def reject_foreign(
        provided: str | None,
        *,
        resource_kind: str,
        resource_id: str,
    ) -> str:
        assert provided == str(machine_id)
        raise MachineOwnershipMismatchError(
            resource_kind=resource_kind,
            resource_id=resource_id,
            owner_machine_id=str(machine_id),
            current_machine_id=str(uuid.uuid4()),
        )

    monkeypatch.setattr(cutover, "require_local_machine_id", reject_foreign)
    monkeypatch.setattr(
        cutover,
        "validate_campaign_checkout_root",
        lambda *_args, **_kwargs: pytest.fail("filesystem validation must not run"),
    )

    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)

    assert preflight.blocked
    assert preflight.projects[0].resolution_status == "foreign_machine"
    reason = preflight.projects[0].resolution_reason
    assert reason is not None
    assert "belongs to machine" in reason


def test_two_authoritative_checkouts_require_no_insert_and_preserve_index_state(
    predecessor_database: str,
    tmp_path: Path,
) -> None:
    project_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    roots = (tmp_path / "machine-one", tmp_path / "machine-two")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_ids = tuple(uuid.UUID(insert_isolated_machine(db)) for _ in roots)
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "covered-project", str(roots[0])),
        )
        db.execute("INSERT INTO code_indexed_projects(id) VALUES (%s)", (project_id,))
        manager = LocalProjectCheckoutManager(db)
        for ordinal, (machine_id, root) in enumerate(zip(machine_ids, roots, strict=True), 1):
            write_project_marker(root, project_id=str(project_id), name="covered-project")
            manager.register(str(machine_id), str(project_id), str(root))
            db.execute(
                """
                INSERT INTO code_indexed_project_states(
                    machine_id, project_id, root_path, total_files, total_symbols
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (machine_id, project_id, str(root), ordinal, ordinal * 10),
            )
    finally:
        db.close()
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        _install_predecessor_resolver(connection)
        _admit_batch(connection, epoch_id=epoch_id, batch_id=batch_id)

    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)

    assert preflight.expected_inserts == ()
    assert preflight.projects[0].resolution_status == "covered"
    cutover.apply_project_checkout_cutover(
        predecessor_database,
        epoch_id=epoch_id,
        batch_id=batch_id,
        preflight=preflight,
        target_checksum="c" * 64,
    )
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        assert connection.execute(
            """
            SELECT machine_id, root_path, total_files, total_symbols
            FROM code_indexed_project_states
            WHERE project_id = %s ORDER BY root_path
            """,
            (project_id,),
        ).fetchall() == [
            {
                "machine_id": machine_id,
                "root_path": str(root),
                "total_files": ordinal,
                "total_symbols": ordinal * 10,
            }
            for ordinal, (machine_id, root) in enumerate(zip(machine_ids, roots, strict=True), 1)
        ]


def test_multiple_unresolved_candidates_block_without_filesystem_access(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    root = tmp_path / "ambiguous-project"
    write_project_marker(root, project_id=str(project_id), name="ambiguous-project")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_ids = tuple(uuid.UUID(insert_isolated_machine(db)) for _ in range(2))
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "ambiguous-project", str(root)),
        )
        for ordinal, machine_id in enumerate(machine_ids):
            db.execute(
                """
                INSERT INTO sessions(id, external_id, machine_id, source, project_id)
                VALUES (%s, %s, %s, 'codex', %s)
                """,
                (uuid.uuid4(), f"candidate-{ordinal}", machine_id, project_id),
            )
    finally:
        db.close()
    monkeypatch.setattr(
        cutover,
        "validate_campaign_checkout_root",
        lambda *_args, **_kwargs: pytest.fail("ambiguous candidates touched the filesystem"),
    )

    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)

    assert preflight.expected_inserts == ()
    assert preflight.projects[0].candidate_machine_ids == tuple(sorted(machine_ids))
    assert preflight.projects[0].resolution_status == "multiple_candidate_machines"
    assert preflight.blocked


@pytest.mark.parametrize("foreign", [False, True], ids=["local", "foreign"])
def test_abort_rebind_then_rerun_uses_authoritative_checkout(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign: bool,
) -> None:
    project_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    root = tmp_path / ("foreign" if foreign else "local")
    write_project_marker(root, project_id=str(project_id), name="rebound-project")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_id = uuid.UUID(insert_isolated_machine(db))
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "rebound-project", str(root)),
        )
        first = cutover.preflight_project_checkout_cutover(predecessor_database)
        assert first.projects[0].resolution_status == "no_candidate_machine"
        assert first.blocked
        LocalProjectCheckoutManager(db).rebind(str(machine_id), str(project_id), str(root))
    finally:
        db.close()
    if foreign:
        monkeypatch.setattr(
            cutover,
            "require_local_machine_id",
            lambda *_args, **_kwargs: pytest.fail(
                "authoritative checkout required local authorization"
            ),
        )
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        _install_predecessor_resolver(connection)
        _admit_batch(connection, epoch_id=epoch_id, batch_id=batch_id)

    rebound = cutover.preflight_project_checkout_cutover(predecessor_database)

    assert rebound.expected_inserts == ()
    assert rebound.projects[0].resolution_status == "covered"
    cutover.apply_project_checkout_cutover(
        predecessor_database,
        epoch_id=epoch_id,
        batch_id=batch_id,
        preflight=rebound,
        target_checksum="d" * 64,
    )


def test_resolver_recreation_failure_rolls_back_every_cutover_write(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    root = tmp_path / "rollback-project"
    write_project_marker(root, project_id=str(project_id), name="rollback-project")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_id = uuid.UUID(insert_isolated_machine(db))
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "rollback-project", str(root)),
        )
        db.execute(
            """
            INSERT INTO sessions(id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, 'codex', %s)
            """,
            (uuid.uuid4(), "rollback-candidate", machine_id, project_id),
        )
    finally:
        db.close()
    patch_local_machine_id(monkeypatch, str(machine_id))
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        _install_predecessor_resolver(connection)
        _admit_batch(connection, epoch_id=epoch_id, batch_id=batch_id)
    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)

    def fail_recreation(_connection: object) -> None:
        raise RuntimeError("recreation failed")

    monkeypatch.setattr(cutover, "_replace_resolve_tool_session", fail_recreation)

    with pytest.raises(RuntimeError, match="recreation failed"):
        cutover.apply_project_checkout_cutover(
            predecessor_database,
            epoch_id=epoch_id,
            batch_id=batch_id,
            preflight=preflight,
            target_checksum="e" * 64,
        )

    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        assert connection.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'projects' AND column_name = 'repo_path'
            """
        ).fetchone() == {"?column?": 1}
        assert (
            connection.execute(
                "SELECT 1 FROM project_checkouts WHERE project_id = %s",
                (project_id,),
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 375"
        ).fetchone() == {"checksum": _LIVE_PREDECESSOR_CHECKSUM}


def test_soft_deleted_project_and_sentinel_have_explicit_preflight_outcomes(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    orphan_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    root = tmp_path / "deleted-project"
    write_project_marker(root, project_id=str(project_id), name="deleted-project")
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_id = uuid.UUID(insert_isolated_machine(db))
        db.execute(
            """
            INSERT INTO projects(id, name, repo_path, deleted_at)
            VALUES (%s, %s, %s, NOW())
            """,
            (project_id, "deleted-project", str(root)),
        )
        LocalProjectCheckoutManager(db).rebind(str(machine_id), str(project_id), str(root))
        # Soft-deleted, a candidate machine, no checkout row, and a root that
        # no longer exists: excluded without touching the filesystem.
        db.execute(
            """
            INSERT INTO projects(id, name, repo_path, deleted_at)
            VALUES (%s, %s, %s, NOW())
            """,
            (orphan_id, "orphan-deleted", str(tmp_path / "gone")),
        )
        db.execute(
            """
            INSERT INTO sessions(id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, 'codex', %s)
            """,
            (uuid.uuid4(), "orphan-session", machine_id, orphan_id),
        )
        db.execute(
            "UPDATE projects SET repo_path = '/sentinel/legacy' WHERE id = %s",
            (PERSONAL_PROJECT_ID,),
        )
    finally:
        db.close()
    monkeypatch.setattr(
        cutover,
        "validate_campaign_checkout_root",
        lambda *_args, **_kwargs: pytest.fail(
            "soft-deleted projects must not touch the filesystem"
        ),
    )

    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)

    by_id = {project.project_id: project for project in preflight.projects}
    assert by_id[project_id].resolution_status == "covered"
    assert by_id[orphan_id].resolution_status == "excluded"
    assert by_id[orphan_id].exclusion_reason == "soft_deleted"
    assert by_id[orphan_id].candidate_machine_ids == (machine_id,)
    personal_id = uuid.UUID(PERSONAL_PROJECT_ID)
    assert by_id[personal_id].resolution_status == "excluded"
    assert by_id[personal_id].exclusion_reason == "checkout_free_sentinel"
    assert preflight.expected_inserts == ()
    assert not preflight.blocked
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        _install_predecessor_resolver(connection)
        _admit_batch(connection, epoch_id=epoch_id, batch_id=batch_id)

    cutover.apply_project_checkout_cutover(
        predecessor_database,
        epoch_id=epoch_id,
        batch_id=batch_id,
        preflight=preflight,
        target_checksum="f" * 64,
    )
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT deleted_at IS NOT NULL AS preserved FROM projects WHERE id = %s",
            (project_id,),
        ).fetchone() == {"preserved": True}
        assert connection.execute(
            "SELECT deleted_at IS NOT NULL AS preserved FROM projects WHERE id = %s",
            (orphan_id,),
        ).fetchone() == {"preserved": True}
        assert (
            connection.execute(
                "SELECT 1 FROM project_checkouts WHERE project_id IN (%s, %s)",
                (PERSONAL_PROJECT_ID, orphan_id),
            ).fetchone()
            is None
        )


def test_receipt_checksum_reads_baseline_row_from_full_ledger(
    predecessor_database: str,
) -> None:
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        ledger = connection.execute("SELECT count(*) AS receipts FROM schema_migrations").fetchone()
        assert ledger is not None
        assert ledger["receipts"] > 1
        assert cutover._receipt_checksum(connection) == _LIVE_PREDECESSOR_CHECKSUM
    assert cutover.read_baseline_receipt_checksum(predecessor_database) == (
        _LIVE_PREDECESSOR_CHECKSUM
    )


def test_every_predecessor_receipt_admits_and_reports_not_applied(
    predecessor_database: str,
) -> None:
    assert {_LIVE_PREDECESSOR_CHECKSUM, _MERGE_BASE_PREDECESSOR_CHECKSUM} <= (
        cutover.PROJECT_CHECKOUT_PREDECESSOR_CHECKSUMS
    )
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        for checksum in sorted(cutover.PROJECT_CHECKOUT_PREDECESSOR_CHECKSUMS):
            connection.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE version = 375",
                (checksum,),
            )
            with connection.transaction():
                cutover.admit_project_checkout_campaign(connection)
            assert cutover._receipt_checksum(connection) == checksum
            assert not cutover.project_checkout_cutover_already_applied(
                predecessor_database,
                batch_id=uuid.uuid4(),
                target_checksum="c" * 64,
            )
        connection.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE version = 375",
            ("d" * 64,),
        )
        with (
            pytest.raises(cutover.ProjectCheckoutCutoverError, match="not a known predecessor"),
            connection.transaction(),
        ):
            cutover.admit_project_checkout_campaign(connection)


def test_preflight_classifies_primary_checkout_despite_ambient_isolated_session(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    root = tmp_path / "ordinary-project"
    write_project_marker(root, project_id=str(project_id), name="ordinary-project")
    worktree_root = tmp_path / "agent-worktree"
    worktree_root.mkdir()
    db = PostgresHubDatabase(predecessor_database)
    try:
        # Agent-run and worktree writers resolve the real local machine id
        # through their own import-time bindings, so register that id here.
        local_machine_id = get_machine_id()
        assert local_machine_id is not None
        machine_id = insert_isolated_machine(db, local_machine_id)
        patch_local_machine_id(monkeypatch, machine_id)
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "ordinary-project", str(root)),
        )
        sessions = SessionManager(db)
        parent = sessions.register(
            external_id="operator-terminal",
            machine_id=machine_id,
            source="test",
            project_id=str(project_id),
        )
        child = sessions.register(
            external_id="isolated-agent",
            machine_id=machine_id,
            source="codex",
            project_id=str(project_id),
            parent_session_id=parent.id,
        )
        runs = LocalAgentRunManager(db)
        run = runs.create(
            parent_session_id=parent.id,
            child_session_id=child.id,
            provider="codex",
            prompt="work in isolation",
        )
        worktree = LocalWorktreeManager(db).create(
            project_id=str(project_id),
            branch_name="task-worktree",
            worktree_path=str(worktree_root),
            agent_session_id=child.id,
        )
        runs.update_runtime(run.id, worktree_id=worktree.id)
        # The operator's terminal exports the isolated child's session id.
        monkeypatch.setenv("GOBBY_SESSION_ID", child.id)
        assert LocalProjectManager(db)._is_isolated_agent_session()
        with pytest.raises(IsolatedAgentProjectPathError):
            validate_checkout_root(
                db,
                project_id=str(project_id),
                machine_id=machine_id,
                candidate_path=str(root),
                expected_marker_id=str(project_id),
            )
    finally:
        db.close()

    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)

    by_id = {project.project_id: project for project in preflight.projects}
    assert by_id[project_id].resolution_status == "ready"
    assert preflight.expected_inserts == (
        cutover.ProjectCheckoutInsert(uuid.UUID(machine_id), project_id, str(root)),
    )
    assert not preflight.blocked


def test_preflight_treats_missing_checkout_table_as_empty_and_names_remedies(
    predecessor_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    db = PostgresHubDatabase(predecessor_database)
    try:
        machine_id = uuid.UUID(insert_isolated_machine(db))
        db.execute(
            "INSERT INTO projects(id, name, repo_path) VALUES (%s, %s, %s)",
            (project_id, "vanished-project", str(tmp_path / "vanished")),
        )
        db.execute(
            """
            INSERT INTO sessions(id, external_id, machine_id, source, project_id)
            VALUES (%s, %s, %s, 'codex', %s)
            """,
            (uuid.uuid4(), "vanished-session", machine_id, project_id),
        )
    finally:
        db.close()
    patch_local_machine_id(monkeypatch, str(machine_id))
    with psycopg.connect(predecessor_database, autocommit=True, row_factory=dict_row) as connection:
        _install_predecessor_resolver(connection)
        connection.execute("DROP TABLE project_checkouts")

    preflight = cutover.preflight_project_checkout_cutover(predecessor_database)

    by_id = {project.project_id: project for project in preflight.projects}
    assert by_id[project_id].resolution_status == "invalid_checkout_root"
    assert by_id[project_id].existing_checkouts == ()
    assert preflight.blocked
    message = cutover.blocked_preflight_message(preflight)
    assert f"vanished-project ({project_id}) invalid_checkout_root at" in message
    assert "restore the directory" in message
    assert "gobby projects delete <name> --confirm <name>" in message
    assert "gobby hub-maintenance preflight project-checkout-cutover" in message


def test_target_schema_assets_are_checkout_only() -> None:
    baseline = (_REPO_ROOT / "crates/gcore/assets/schema/baseline.sql").read_text()
    migration = (
        _REPO_ROOT / "crates/gcore/assets/schema/migrations/418_project_checkouts.sql"
    ).read_text()
    privileges = json.loads(
        (_REPO_ROOT / "crates/gcode/security/managed_postgres_privileges.json").read_text()
    )

    assert "CREATE TABLE project_checkouts (" in baseline
    assert "repo_path text" not in baseline
    assert "projects.repo_path" not in baseline
    assert "SELECT(repo_path) ON TABLE projects" not in baseline
    assert "'project-checkout-cutover'::text" in baseline
    assert "LEFT JOIN public.project_checkouts AS checkout" in baseline
    assert "SELECT (repo_path) ON TABLE projects" not in migration
    projects = next(item for item in privileges["relations"] if item["relation"] == "projects")
    assert projects["columns"] == ["id", "name", "deleted_at"]


def test_agent_spawn_resolves_machine_checkout_instead_of_logical_project_path() -> None:
    source = (Path(__file__).parents[2] / "src/gobby/servers/routes/agent_spawn.py").read_text()

    assert "project.repo_path" not in source
    assert "require_root(task_manager.db, effective_project_id, require_machine_id())" in source


def test_identity_repo_path_residue_allowlist() -> None:
    """Pin the exact gcode residue queries and their narrow historical allowlist."""
    source_roots = (
        "src/gobby",
        "crates/gcore/src",
        "crates/gcore/tests",
        "crates/gcode/src",
        "tests",
    )
    source_suffixes = {".json", ".py", ".rs", ".sql"}
    allowed_qualified_column_paths = {
        "crates/gcore/tests/schema_contract.rs",
        "src/gobby/storage/project_checkout_cutover.py",
        "tests/storage/test_project_checkout_cutover.py",
    }
    literal_queries: tuple[tuple[str, str, set[str]], ...] = (
        (
            "gcode grep -F 'projects.repo_path' src/gobby crates/gcore/src "
            "crates/gcore/tests crates/gcode/src tests -m 500",
            "projects.repo_path",
            allowed_qualified_column_paths,
        ),
        (
            "gcode grep -F 'Project.repo_path' src/gobby crates/gcore/src "
            "crates/gcore/tests crates/gcode/src tests -m 500",
            "Project.repo_path",
            set(),
        ),
        (
            'gcode grep -F "project\'s repo_path" src/gobby -m 50',
            "project's repo_path",
            set(),
        ),
        (
            "gcode grep -F 'Project repo_path is required' src/gobby -m 50",
            "Project repo_path is required",
            set(),
        ),
        (
            "gcode grep -F 'canonical repo_path' src/gobby -m 50",
            "canonical repo_path",
            set(),
        ),
    )
    violations: dict[str, list[str]] = {}
    residue_test_path = "tests/storage/test_project_checkout_cutover.py"
    for command, literal, allowed_paths in literal_queries:
        for root_name in source_roots:
            root = _REPO_ROOT / root_name
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in source_suffixes:
                    continue
                relative = path.relative_to(_REPO_ROOT).as_posix()
                if (
                    literal in path.read_text(errors="replace")
                    and relative not in allowed_paths
                    and relative != residue_test_path
                ):
                    violations.setdefault(command, []).append(relative)

    fixture_paths = (
        "crates/gcode/src/config/tests.rs",
        "tests/e2e/conftest.py",
        "tests/e2e/test_worktrees_e2e.py",
        "tests/integration/test_edit_history.py",
        "tests/integration/test_hub_query.py",
        "tests/mcp_proxy/test_metrics_manager.py",
        "tests/mcp_proxy/test_metrics_store.py",
        "tests/mcp_proxy/test_registries.py",
        "tests/mcp_proxy/tools/test_apply_persona.py",
        "tests/mcp_proxy/tools/test_hub.py",
        "tests/plans/test_plan_coverage_ci.py",
        "tests/sessions/test_e2e_session_tracking.py",
        "tests/sessions/test_token_usage.py",
        "tests/storage/test_checkpoints.py",
        "tests/storage/test_manager_surface_parity.py",
        "tests/storage/test_postgres_agent_authorization.py",
        "tests/storage/test_project_manager.py",
        "tests/storage/test_project_repo_path_isolation.py",
        "tests/sync/test_github_issue_sync.py",
        "tests/workflows/test_pipeline_heartbeat.py",
    )
    json_query = (
        "gcode grep -F '\"repo_path\":' crates/gcode/src/config/tests.rs "
        + " ".join(fixture_paths[1:])
        + " -m 500"
    )
    for relative in fixture_paths:
        if '"repo_path":' in (_REPO_ROOT / relative).read_text(errors="replace"):
            violations.setdefault(json_query, []).append(relative)

    positional_query = (
        "gcode grep '\\.(create|ensure_exists|update)\\(' "
        + " ".join(path for path in fixture_paths if path.endswith(".py"))
        + " -m 500"
    )
    positional_limits = {"create": 2, "ensure_exists": 3, "update": 2}
    manager_names = {"pm", "project_manager", "projects"}
    for relative in (path for path in fixture_paths if path.endswith(".py")):
        tree = ast.parse((_REPO_ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    if value.func.id == "LocalProjectManager":
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        manager_names.update(
                            target.id for target in targets if isinstance(target, ast.Name)
                        )
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            limit = positional_limits.get(node.func.attr)
            if limit is None or len(node.args) < limit:
                continue
            owner = node.func.value
            direct_manager = (
                isinstance(owner, ast.Call)
                and isinstance(owner.func, ast.Name)
                and owner.func.id == "LocalProjectManager"
            )
            named_manager = isinstance(owner, ast.Name) and owner.id in manager_names
            if direct_manager or named_manager:
                violations.setdefault(positional_query, []).append(f"{relative}:{node.lineno}")

    privileges = json.loads(
        (_REPO_ROOT / "crates/gcode/security/managed_postgres_privileges.json").read_text()
    )
    projects = next(item for item in privileges["relations"] if item["relation"] == "projects")
    if projects["columns"] != ["id", "name", "deleted_at"]:
        violations.setdefault(
            'gcode grep -F \'"relation": "projects"\' '
            "crates/gcode/security/managed_postgres_privileges.json -A 12 -m 20",
            [],
        ).append(str(projects["columns"]))

    assert not violations, json.dumps(violations, indent=2, sort_keys=True)
