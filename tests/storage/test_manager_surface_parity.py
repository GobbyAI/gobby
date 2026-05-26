from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import (
    DispatchMutexRow,
    LockAcquisitionOrderError,
    SessionRecoveryByProject,
    SessionRegistration,
    SystemSessionBootstrap,
    TaskSubtreeCascade,
    WebChatSessionBootstrap,
)
from gobby.storage.tasks._crud import cascade_build_state_to_subtree

pytestmark = pytest.mark.unit


def _postgres_hub_or_skip() -> object:
    dsn = os.getenv("GOBBY_POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("PostgreSQL DSN required for hub runtime surface tests")

    from gobby.storage.hub.postgres import PostgresHubDatabase

    return PostgresHubDatabase(dsn)


@pytest.fixture(params=["postgres"])
def hub_db() -> Iterator[object]:
    db = _postgres_hub_or_skip()
    try:
        yield db
    finally:
        db.close()


def test_local_task_manager_dual_backend(hub_db: object) -> None:
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
        "INSERT INTO manager_surface_items (id, name, count) VALUES ($1, $2, $3)",
        (1, "one", 1),
    )
    assert insert_cursor.rowcount == 1
    assert db.fetchone("SELECT name FROM manager_surface_items WHERE id = $1", (1,)) == {
        "name": "one"
    }

    update_cursor = db.execute(
        "UPDATE manager_surface_items SET count = count + $1 WHERE id = $2",
        (2, 1),
    )
    assert update_cursor.rowcount == 1

    db.safe_update("manager_surface_items", {"count": 7}, "id = $1", (1,))
    assert db.fetchall("SELECT count FROM manager_surface_items WHERE id = $1", (1,)) == [
        {"count": 7}
    ]

    with db.transaction_immediate(DispatchMutexRow(task_id="manager-surface")) as txn:
        txn.execute(
            "UPDATE manager_surface_items SET count = count + $1 WHERE id = $2",
            (1, 1),
        )

    assert db.fetchone("SELECT count FROM manager_surface_items WHERE id = $1", (1,)) == {
        "count": 8
    }


def test_ambient_transaction_groups_convenience_calls(hub_db: object) -> None:
    db = hub_db
    db.execute("CREATE TABLE IF NOT EXISTS ambient_items (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("DELETE FROM ambient_items")

    with pytest.raises(RuntimeError, match="rollback"):
        with db.transaction():
            db.execute("INSERT INTO ambient_items (id, name) VALUES ($1, $2)", (1, "one"))
            db.execute("INSERT INTO ambient_items (id, name) VALUES ($1, $2)", (2, "two"))
            raise RuntimeError("rollback")

    assert db.fetchall("SELECT id FROM ambient_items ORDER BY id") == []


def test_ambient_transaction_isolated_per_adapter() -> None:
    primary_db = _postgres_hub_or_skip()
    other_db = _postgres_hub_or_skip()
    try:
        primary_db.execute("CREATE TABLE ambient_items (id INTEGER PRIMARY KEY)")
        other_db.execute("CREATE TABLE ambient_items (id INTEGER PRIMARY KEY)")

        with pytest.raises(RuntimeError, match="rollback"):
            with primary_db.transaction():
                primary_db.execute("INSERT INTO ambient_items (id) VALUES ($1)", (1,))
                other_db.execute("INSERT INTO ambient_items (id) VALUES ($1)", (2,))
                raise RuntimeError("rollback")

        assert primary_db.fetchall("SELECT id FROM ambient_items") == []
        assert other_db.fetchall("SELECT id FROM ambient_items") == [{"id": 2}]
    finally:
        primary_db.close()
        other_db.close()


def test_subtree_cascade_serializes_overlapping_subtrees(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _postgres_hub_or_skip()
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
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                parent_task_id TEXT,
                task_type TEXT NOT NULL,
                closed_at TEXT,
                allow_automation INTEGER,
                unattended INTEGER,
                isolation TEXT,
                updated_at TEXT
            )
            """
        )
        db.executemany(
            """
            INSERT INTO tasks (id, project_id, parent_task_id, task_type, closed_at)
            VALUES ($1, $2, $3, $4, NULL)
            """,
            [("root", "project-1", None, "epic"), ("child", "project-1", "root", "task")],
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


def test_nested_lock_target_acquires_both_lookup_branch() -> None:
    db = _postgres_hub_or_skip()
    web_lock = WebChatSessionBootstrap("ext", "machine", "codex", "project", "web_chat")
    registration_lock = SessionRegistration("ext", "machine", "codex", "project", "web_chat")
    try:
        with db.transaction_immediate(web_lock) as outer:
            with db.transaction_immediate(registration_lock) as inner:
                assert inner is outer
    finally:
        db.close()


def test_nested_lock_target_acquires_both_recovery_branch() -> None:
    db = _postgres_hub_or_skip()
    web_lock = WebChatSessionBootstrap("ext", "machine", "codex", "project", "web_chat")
    recovery_lock = SessionRecoveryByProject(project_id="project")
    try:
        with db.transaction_immediate(web_lock) as outer:
            with db.transaction_immediate(recovery_lock) as inner:
                assert inner is outer
    finally:
        db.close()


def test_nested_lock_target_out_of_order_priority_raises() -> None:
    db = _postgres_hub_or_skip()
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


def test_transaction_immediate_inside_non_immediate_transaction_raises() -> None:
    db = _postgres_hub_or_skip()
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
