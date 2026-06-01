from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import (
    DispatchMutexRow,
    HubDatabase,
    LockAcquisitionOrderError,
    SessionRecoveryByProject,
    SessionRegistration,
    SystemSessionBootstrap,
    TaskSubtreeCascade,
    WebChatSessionBootstrap,
)
from gobby.storage.tasks._crud import cascade_build_state_to_subtree

pytestmark = pytest.mark.unit


def _scoped_postgres_url(database_url: str, schema: str) -> str:
    return database_url + f"?options=-csearch_path%3D{schema}"


@pytest.fixture
def postgres_db_factory(
    postgres_db: HubDatabase,
    postgres_database_url: str,
    postgres_schema: str,
) -> Callable[[], PostgresHubDatabase]:
    def create_db() -> PostgresHubDatabase:
        return PostgresHubDatabase(_scoped_postgres_url(postgres_database_url, postgres_schema))

    return create_db


def test_local_task_manager_dual_backend(hub_db: HubDatabase) -> None:
    db = hub_db
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS manager_surface_items (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.execute("DELETE FROM manager_surface_items")

    insert_cursor = db.execute(
        "INSERT INTO manager_surface_items (id, name, count) VALUES (%s, %s, %s)",
        (1, "one", 1),
    )
    assert insert_cursor.rowcount == 1
    assert db.fetchone("SELECT name FROM manager_surface_items WHERE id = %s", (1,)) == {
        "name": "one"
    }

    update_cursor = db.execute(
        "UPDATE manager_surface_items SET count = count + %s WHERE id = %s",
        (2, 1),
    )
    assert update_cursor.rowcount == 1

    db.safe_update("manager_surface_items", {"count": 7}, "id = %s", (1,))
    assert db.fetchall("SELECT count FROM manager_surface_items WHERE id = %s", (1,)) == [
        {"count": 7}
    ]

    with db.transaction_immediate(DispatchMutexRow(task_id="manager-surface")) as txn:
        txn.execute(
            "UPDATE manager_surface_items SET count = count + %s WHERE id = %s",
            (1, 1),
        )

    assert db.fetchone("SELECT count FROM manager_surface_items WHERE id = %s", (1,)) == {
        "count": 8
    }


def test_ambient_transaction_groups_convenience_calls(hub_db: HubDatabase) -> None:
    db = hub_db
    db.execute("CREATE TABLE IF NOT EXISTS ambient_items (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("DELETE FROM ambient_items")

    with pytest.raises(RuntimeError, match="rollback"):
        with db.transaction():
            db.execute("INSERT INTO ambient_items (id, name) VALUES (%s, %s)", (1, "one"))
            db.execute("INSERT INTO ambient_items (id, name) VALUES (%s, %s)", (2, "two"))
            raise RuntimeError("rollback")

    assert db.fetchall("SELECT id FROM ambient_items ORDER BY id") == []


def test_ambient_transaction_isolated_per_adapter(
    postgres_db_factory: Callable[[], PostgresHubDatabase],
) -> None:
    primary_db = postgres_db_factory()
    other_db = postgres_db_factory()
    try:
        primary_db.execute("CREATE TABLE IF NOT EXISTS ambient_items (id INTEGER PRIMARY KEY)")
        primary_db.execute("DELETE FROM ambient_items")
        other_db.execute("CREATE TABLE IF NOT EXISTS ambient_items (id INTEGER PRIMARY KEY)")

        with pytest.raises(RuntimeError, match="rollback"):
            with primary_db.transaction():
                primary_db.execute("INSERT INTO ambient_items (id) VALUES (%s)", (1,))
                other_db.execute("INSERT INTO ambient_items (id) VALUES (%s)", (2,))
                raise RuntimeError("rollback")

        assert primary_db.fetchall("SELECT id FROM ambient_items") == [{"id": 2}]
        assert other_db.fetchall("SELECT id FROM ambient_items") == [{"id": 2}]
    finally:
        primary_db.close()
        other_db.close()


def test_subtree_cascade_serializes_overlapping_subtrees(
    monkeypatch: pytest.MonkeyPatch,
    postgres_db_factory: Callable[[], PostgresHubDatabase],
) -> None:
    db = postgres_db_factory()
    seen_locks: list[object] = []
    original_transaction_immediate = db.transaction_immediate

    class FakeStageStatesManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def list_for_task(self, _task_id: str) -> list[object]:
            return []

        def initialize_manifest(
            self,
            _task_id: str,
            _specs: list[object],
            *,
            by_session_id: str | None,
        ) -> None:
            pass

    monkeypatch.setattr(
        "gobby.storage.tasks._build_cascade.StageStatesManager",
        FakeStageStatesManager,
    )

    @contextmanager
    def recording_transaction(lock: object) -> Iterator[object]:
        seen_locks.append(lock)
        with original_transaction_immediate(lock) as txn:
            yield txn

    db.transaction_immediate = recording_transaction  # type: ignore[method-assign]
    try:
        db.execute(
            """
            INSERT INTO projects (id, name, repo_path, created_at, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO NOTHING
            """,
            ("project-1", "Project 1", "/tmp/project-1"),
        )
        db.executemany(
            """
            INSERT INTO tasks (
                id, project_id, parent_task_id, title, task_type, closed_at,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                ("root", "project-1", None, "Root", "epic"),
                ("child", "project-1", "root", "Child", "task"),
            ],
        )

        updated = cascade_build_state_to_subtree(
            db,
            "root",
            "worktree",
            unattended=False,
            allow_automation=True,
            parent_manifest_specs=[],
        )

        assert updated == 2
        assert isinstance(seen_locks[0], TaskSubtreeCascade)
        assert seen_locks[0].project_id == "project-1"
    finally:
        db.close()


def test_nested_lock_target_acquires_both_lookup_branch(
    postgres_db_factory: Callable[[], PostgresHubDatabase],
) -> None:
    db = postgres_db_factory()
    web_lock = WebChatSessionBootstrap("ext", "machine", "codex", "project", "web_chat")
    registration_lock = SessionRegistration("ext", "machine", "codex", "project", "web_chat")
    try:
        with db.transaction_immediate(web_lock) as outer:
            with db.transaction_immediate(registration_lock) as inner:
                assert inner is outer
    finally:
        db.close()


def test_nested_lock_target_acquires_both_recovery_branch(
    postgres_db_factory: Callable[[], PostgresHubDatabase],
) -> None:
    db = postgres_db_factory()
    web_lock = WebChatSessionBootstrap("ext", "machine", "codex", "project", "web_chat")
    recovery_lock = SessionRecoveryByProject(project_id="project")
    try:
        with db.transaction_immediate(web_lock) as outer:
            with db.transaction_immediate(recovery_lock) as inner:
                assert inner is outer
    finally:
        db.close()


def test_nested_lock_target_out_of_order_priority_raises(
    postgres_db_factory: Callable[[], PostgresHubDatabase],
) -> None:
    db = postgres_db_factory()
    web_lock = WebChatSessionBootstrap("ext", "machine", "codex", "project", "web_chat")
    try:
        with db.transaction_immediate(web_lock):
            with pytest.raises(LockAcquisitionOrderError) as exc_info:
                with db.transaction_immediate(web_lock):
                    pass
    finally:
        db.close()

    message = str(exc_info.value)
    assert "500" in message
    assert "WebChatSessionBootstrap" in message


def test_transaction_immediate_inside_non_immediate_transaction_raises(
    postgres_db_factory: Callable[[], PostgresHubDatabase],
) -> None:
    db = postgres_db_factory()
    try:
        with db.transaction():
            with pytest.raises(RuntimeError, match="non-immediate"):
                with db.transaction_immediate(SystemSessionBootstrap()):
                    pass
    finally:
        db.close()


def test_storage_runtime_imports_hub_database_not_legacy_protocol(repo_root: Path) -> None:
    offenders: list[str] = []
    for path in _runtime_storage_files(repo_root):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "gobby.storage.database":
                if any(alias.name == "HubDatabase" for alias in node.names):
                    offenders.append(str(path.relative_to(repo_root)))

    assert offenders == []


def test_storage_runtime_has_no_raw_database_escape_hatches(repo_root: Path) -> None:
    pattern = re.compile(r"\bdb\.(?:connection|cursor)\b")
    offenders = [
        str(path.relative_to(repo_root))
        for path in _runtime_storage_files(repo_root)
        if pattern.search(path.read_text())
    ]

    assert offenders == []


def test_storage_runtime_immediate_transactions_pass_lock_targets(repo_root: Path) -> None:
    offenders: list[str] = []
    for path in _runtime_storage_files(repo_root):
        if path.parent.name == "hub":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "transaction_immediate"
                and not node.args
                and not node.keywords
            ):
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert offenders == []


def _runtime_storage_files(repo_root: Path) -> list[Path]:
    storage_root = repo_root / "src" / "gobby" / "storage"
    return [
        path
        for path in storage_root.rglob("*.py")
        if path.name != "database.py" and "__pycache__" not in path.parts
    ]
