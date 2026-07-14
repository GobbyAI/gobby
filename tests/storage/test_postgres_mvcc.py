from __future__ import annotations

import os
import queue
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from gobby.storage.hub.protocol import TaskSeqAllocation, Transaction
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions import _field_update as session_field_update
from gobby.storage.task_dependencies import DependencyCycleError, TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, _creation

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class _CounterLock:
    PRIORITY: ClassVar[int] = 950
    name: str


def test_post_phase5_audit_report_frontmatter_and_rows(repo_root: Path) -> None:
    report_path = repo_root / "docs" / "audits" / "postgres-concurrency-audit.md"
    text = report_path.read_text()

    top_frontmatter = _parse_top_frontmatter(text)
    assert top_frontmatter["audit_version"] == "1"
    assert top_frontmatter["phase_baseline"] == "P4"
    assert _HEX40.fullmatch(top_frontmatter["audit_commit"])

    post_frontmatter = _parse_post_phase_frontmatter(text)
    assert post_frontmatter["audit_version"] == "2"
    assert post_frontmatter["phase_baseline"] == "P5"
    assert _HEX40.fullmatch(post_frontmatter["audit_commit"])
    assert _HEX40.fullmatch(post_frontmatter["prior_audit_commit"])
    assert post_frontmatter["prior_audit_commit"] == top_frontmatter["audit_commit"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", post_frontmatter["audited_at"])

    assert "Unresolved High/Medium findings: None." in text
    assert "| Callback Site | Risk Level | Read-Modify-Write Risk |" in text
    for required in (
        "Postgres after-commit callbacks",
        "transaction_immediate",
    ):
        assert required in text


@pytest.mark.integration
def test_after_commit_async_reader_uses_committed_state(postgres_db: Any) -> None:
    table = "mvcc_async_reader"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')

    try:
        results: queue.Queue[object] = queue.Queue()

        def callback() -> None:
            def read_from_pool() -> None:
                try:
                    row = postgres_db.fetchone(f'SELECT value FROM "{table}" WHERE id = %s', (1,))
                    results.put(None if row is None else row["value"])
                except BaseException as exc:  # pragma: no cover - re-raised in main thread
                    results.put(exc)

            thread = threading.Thread(target=read_from_pool)
            thread.start()
            thread.join(timeout=5)
            if thread.is_alive():
                results.put(TimeoutError("after_commit reader did not finish"))

        with postgres_db.transaction() as txn:
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES (%s, %s)', (1, "committed"))
            txn.after_commit(callback)
            assert results.empty()

        result = results.get(timeout=5)
        if isinstance(result, BaseException):
            raise AssertionError("after_commit reader failed") from result
        assert result == "committed"
    finally:
        _drop_table(postgres_db, table)


@pytest.mark.integration
def test_after_commit_reader_respects_long_running_snapshot(
    postgres_db: Any,
    postgres_schema: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    table = "mvcc_snapshot_reader"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')

    held = psycopg.connect(_scoped_dsn(postgres_schema), autocommit=True)
    try:
        held.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        first_count = held.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        assert first_count == 0

        observed: list[int] = []

        def callback() -> None:
            row = held.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            observed.append(int(row[0]))

        with postgres_db.transaction() as txn:
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES (%s, %s)', (1, "committed"))
            txn.after_commit(callback)

        assert observed == [0]
        held.execute("COMMIT")
        fresh_count = held.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        assert fresh_count == 1
    finally:
        try:
            held.execute("ROLLBACK")
        except Exception:
            pass
        held.close()
        _drop_table(postgres_db, table)


@pytest.mark.integration
def test_savepoint_callback_rollback_safe_with_postgres(postgres_db: Any) -> None:
    table = "mvcc_savepoint_reader"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')

    try:
        seen: list[int] = []

        with postgres_db.transaction() as txn:
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES (%s, %s)', (1, "outer"))
            savepoint = txn.savepoint("mvcc_rollback")
            txn.execute(f'INSERT INTO "{table}" (id, value) VALUES (%s, %s)', (2, "rolled-back"))
            savepoint.rollback()
            savepoint.release()
            txn.after_commit(
                lambda: seen.extend(
                    int(row["id"]) for row in postgres_db.fetchall(f'SELECT id FROM "{table}"')
                )
            )

        assert seen == [1]
    finally:
        _drop_table(postgres_db, table)


@pytest.mark.integration
def test_read_modify_write_path_serializes_concurrent_writers(postgres_db: Any) -> None:
    table = "mvcc_counter"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)')
    postgres_db.execute(f'INSERT INTO "{table}" (id, value) VALUES (%s, %s)', (1, 0))

    try:
        errors: queue.Queue[BaseException] = queue.Queue()
        first_writer_read = threading.Event()
        release_first_writer = threading.Event()

        def worker() -> None:
            try:
                with postgres_db.transaction_immediate(_CounterLock(table)) as txn:
                    row = txn.execute(f'SELECT value FROM "{table}" WHERE id = %s', (1,)).fetchone()
                    assert row is not None
                    next_value = int(row["value"]) + 1
                    if not first_writer_read.is_set():
                        first_writer_read.set()
                        assert release_first_writer.wait(timeout=1)
                    txn.execute(f'UPDATE "{table}" SET value = %s WHERE id = %s', (next_value, 1))
            except BaseException as exc:  # pragma: no cover - re-raised in main thread
                errors.put(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        assert first_writer_read.wait(timeout=1)
        release_first_writer.set()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        if not errors.empty():
            raise AssertionError("concurrent writer failed") from errors.get()

        row = postgres_db.fetchone(f'SELECT value FROM "{table}" WHERE id = %s', (1,))
        assert row is not None
        assert row["value"] == 2
    finally:
        _drop_table(postgres_db, table)


def test_dependency_cycle_check_and_insert_are_serialized(postgres_db: Any) -> None:
    project = LocalProjectManager(postgres_db).create(f"dependency-race-{uuid.uuid4()}")
    task_manager = LocalTaskManager(postgres_db)
    first_task = task_manager.create_task(project.id, "first")
    second_task = task_manager.create_task(project.id, "second")
    first_check_started = threading.Event()
    release_first_check = threading.Event()
    second_finished = threading.Event()
    errors: queue.Queue[BaseException] = queue.Queue()

    class PausingDependencyManager(TaskDependencyManager):
        def _would_create_cycle(self, task_id: str, depends_on: str, conn: Transaction) -> bool:
            first_check_started.set()
            assert release_first_check.wait(timeout=5)
            return super()._would_create_cycle(task_id, depends_on, conn)

    def add_first_edge() -> None:
        try:
            PausingDependencyManager(postgres_db).add_dependency(first_task.id, second_task.id)
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.put(exc)

    def add_opposite_edge() -> None:
        try:
            TaskDependencyManager(postgres_db).add_dependency(second_task.id, first_task.id)
        except DependencyCycleError:
            pass
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.put(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=add_first_edge)
    second_thread = threading.Thread(target=add_opposite_edge)
    first_thread.start()
    assert first_check_started.wait(timeout=5)
    second_thread.start()

    second_was_blocked = not second_finished.wait(timeout=0.5)
    release_first_check.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in (first_thread, second_thread))
    if not errors.empty():
        raise AssertionError("concurrent dependency addition failed") from errors.get()

    assert second_was_blocked
    rows = postgres_db.fetchall(
        "SELECT task_id, depends_on FROM task_dependencies WHERE task_id IN (%s, %s)",
        (first_task.id, second_task.id),
    )
    assert [(row["task_id"], row["depends_on"]) for row in rows] == [
        (first_task.id, second_task.id)
    ]


def test_session_parent_cycle_check_and_update_are_serialized(
    postgres_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = LocalProjectManager(postgres_db).create(f"session-lineage-race-{uuid.uuid4()}")
    session_manager = SessionManager(postgres_db)
    first = session_manager.register(
        external_id=f"first-{uuid.uuid4()}",
        machine_id="session-lineage-race",
        source="codex",
        project_id=project.id,
    )
    second = session_manager.register(
        external_id=f"second-{uuid.uuid4()}",
        machine_id="session-lineage-race",
        source="codex",
        project_id=project.id,
    )
    first_check_started = threading.Event()
    release_first_check = threading.Event()
    second_finished = threading.Event()
    errors: queue.Queue[BaseException] = queue.Queue()
    original_sanitize = session_field_update.sanitize_parent_session_id

    def pausing_sanitize(*args: Any, **kwargs: Any) -> str | None:
        if kwargs["child_session_id"] == first.id:
            first_check_started.set()
            assert release_first_check.wait(timeout=5)
        return original_sanitize(*args, **kwargs)

    monkeypatch.setattr(session_field_update, "sanitize_parent_session_id", pausing_sanitize)

    def update_first_parent() -> None:
        try:
            session_manager.update_parent_session_id(first.id, second.id)
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.put(exc)

    def update_second_parent() -> None:
        try:
            session_manager.update_parent_session_id(second.id, first.id)
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.put(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=update_first_parent)
    second_thread = threading.Thread(target=update_second_parent)
    first_thread.start()
    assert first_check_started.wait(timeout=5)
    second_thread.start()

    second_was_blocked = not second_finished.wait(timeout=0.5)
    release_first_check.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in (first_thread, second_thread))
    if not errors.empty():
        raise AssertionError("concurrent session parent update failed") from errors.get()

    assert second_was_blocked
    updated_first = session_manager.get(first.id)
    updated_second = session_manager.get(second.id)
    assert updated_first is not None
    assert updated_second is not None
    assert updated_first.parent_session_id == second.id
    assert updated_second.parent_session_id is None


def test_task_seq_allocation_serializes_across_project_visibility(postgres_db: Any) -> None:
    project_id = str(uuid.uuid4())
    errors: queue.Queue[BaseException] = queue.Queue()
    project_inserted = threading.Event()
    release_project = threading.Event()
    project_committed = threading.Event()
    first_lock_acquired = threading.Event()
    allow_first_task = threading.Event()
    first_task_created = threading.Event()
    release_first_allocator = threading.Event()
    second_attempted = threading.Event()
    second_lock_acquired = threading.Event()
    allow_second_task = threading.Event()
    second_task_created = threading.Event()

    def create_project() -> None:
        try:
            with postgres_db.transaction() as txn:
                txn.execute(
                    "INSERT INTO projects (id, name) VALUES (%s, %s)",
                    (project_id, "visibility-race"),
                )
                project_inserted.set()
                assert release_project.wait(timeout=5)
            project_committed.set()
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.put(exc)
            project_inserted.set()
            project_committed.set()

    def create_first_task() -> None:
        try:
            assert project_inserted.wait(timeout=5)
            with postgres_db.transaction_immediate(TaskSeqAllocation(project_id)) as txn:
                first_lock_acquired.set()
                assert project_committed.wait(timeout=5)
                assert allow_first_task.wait(timeout=5)
                _creation._create_task_in_transaction(
                    postgres_db,
                    txn,
                    project_id=project_id,
                    title="first",
                )
                first_task_created.set()
                assert release_first_allocator.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.put(exc)
            first_lock_acquired.set()
            first_task_created.set()

    def create_second_task() -> None:
        try:
            assert project_committed.wait(timeout=5)
            second_attempted.set()
            with postgres_db.transaction_immediate(TaskSeqAllocation(project_id)) as txn:
                second_lock_acquired.set()
                assert allow_second_task.wait(timeout=5)
                _creation._create_task_in_transaction(
                    postgres_db,
                    txn,
                    project_id=project_id,
                    title="second",
                )
                second_task_created.set()
        except BaseException as exc:  # pragma: no cover - re-raised in main thread
            errors.put(exc)
            second_lock_acquired.set()
            second_task_created.set()

    project_thread = threading.Thread(target=create_project)
    first_thread = threading.Thread(target=create_first_task)
    second_thread = threading.Thread(target=create_second_task)
    threads = [project_thread, first_thread, second_thread]

    project_thread.start()
    assert project_inserted.wait(timeout=5)
    first_thread.start()
    assert first_lock_acquired.wait(timeout=5)
    release_project.set()
    assert project_committed.wait(timeout=5)

    second_thread.start()
    assert second_attempted.wait(timeout=5)
    second_was_blocked = not second_lock_acquired.wait(timeout=0.5)

    if second_was_blocked:
        allow_first_task.set()
        assert first_task_created.wait(timeout=5)
        release_first_allocator.set()
        assert second_lock_acquired.wait(timeout=5)
        allow_second_task.set()
    else:
        allow_second_task.set()
        assert second_task_created.wait(timeout=5)
        allow_first_task.set()
        assert first_task_created.wait(timeout=5)
        release_first_allocator.set()

    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    if not errors.empty():
        raise AssertionError("concurrent task creation failed") from errors.get()

    rows = postgres_db.fetchall(
        "SELECT seq_num FROM tasks WHERE project_id = %s ORDER BY seq_num",
        (project_id,),
    )
    assert second_was_blocked
    assert [row["seq_num"] for row in rows] == [1, 2]


@pytest.mark.integration
def test_deferrable_constraint_is_forced_before_marker(postgres_schema: str) -> None:
    psycopg = pytest.importorskip("psycopg")
    parent = "mvcc_parent"
    child = "mvcc_child"

    with psycopg.connect(_scoped_dsn(postgres_schema), autocommit=True) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{child}"')
        conn.execute(f'DROP TABLE IF EXISTS "{parent}"')
        conn.execute(f'CREATE TABLE "{parent}" (id INTEGER PRIMARY KEY)')
        conn.execute(
            f'CREATE TABLE "{child}" ('
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES "
            f'"{parent}"(id) DEFERRABLE INITIALLY IMMEDIATE)'
        )
        try:
            with conn.transaction():
                conn.execute("SET CONSTRAINTS ALL DEFERRED")
                conn.execute(f'INSERT INTO "{child}" (id, parent_id) VALUES (1, 7)')
                conn.execute(f'INSERT INTO "{parent}" (id) VALUES (7)')
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")

            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute("SET CONSTRAINTS ALL DEFERRED")
                    conn.execute(f'INSERT INTO "{child}" (id, parent_id) VALUES (2, 404)')
                    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")

            row = conn.execute(f'SELECT COUNT(*) FROM "{child}" WHERE id = 2').fetchone()
            assert row[0] == 0
        finally:
            conn.execute(f'DROP TABLE IF EXISTS "{child}"')
            conn.execute(f'DROP TABLE IF EXISTS "{parent}"')


def _parse_top_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(?P<body>.*?)\n---", text, flags=re.DOTALL)
    assert match is not None
    return _parse_key_values(match.group("body"))


def _parse_post_phase_frontmatter(text: str) -> dict[str, str]:
    _before, _heading, after = text.partition("## Post-Phase-5 re-audit")
    assert after
    match = re.search(r"```yaml\n(?P<body>.*?)\n```", after, flags=re.DOTALL)
    assert match is not None
    parsed = _parse_key_values(match.group("body"))
    for key in {
        "audit_version",
        "phase_baseline",
        "audit_commit",
        "prior_audit_commit",
        "audited_at",
    }:
        assert key in parsed
    return parsed


def _parse_key_values(block: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _scoped_dsn(postgres_schema: str) -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for PostgreSQL MVCC tests")
    return dsn + f"?options=-csearch_path%3D{postgres_schema}"


def _drop_table(postgres_db: Any, table: str) -> None:
    postgres_db.execute(f'DROP TABLE IF EXISTS "{table}"')
