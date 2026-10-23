"""Reset must revalidate generated work after acquiring its deletion locks."""

from collections.abc import Mapping, Sequence
from threading import Event, Thread
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg import sql

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.postgres_pool import _PostgresTransaction
from gobby.storage.hub.protocol import Cursor, HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit

COMMIT = "51151d39d1f7d0a882e08eec2a936052f03f901d"


@pytest.mark.parametrize(
    ("protection", "reason"),
    [
        ("claimed", "is claimed"),
        ("closed", "is closed"),
        ("escalated", "is escalated"),
        ("commits", "linked commits"),
        ("artifacts", "isolation artifacts"),
    ],
)
def test_reset_preserves_protection_committed_while_waiting_for_task_lock(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    protection: str,
    reason: str,
) -> None:
    assert isinstance(temp_db, PostgresHubDatabase)
    manager = LocalTaskManager(temp_db)
    runs = LocalExpansionRunManager(temp_db)
    parent = manager.create_task(
        sample_project["id"], "Reset parent", task_type="epic", validation_criteria="Expanded."
    )
    leaf = manager.create_task(
        sample_project["id"],
        "Original leaf",
        parent_task_id=parent.id,
        validation_criteria="Work remains intact.",
    )
    session = SessionManager(temp_db).register(
        external_id="reset-concurrent-owner",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    run = runs.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    temp_db.execute("UPDATE expansion_runs SET status = 'applying' WHERE id = %s", (run.id,))
    runs.save_apply_result(run.id, task_id_map={"leaf": leaf.id}, created_task_ids=[leaf.id])
    writer_db = PostgresHubDatabase(temp_db.conninfo)
    writer_db.open()
    reached_task_lock = Event()
    finished = Event()
    errors: list[BaseException] = []
    execute = _PostgresTransaction.execute

    def observe_task_lock(
        txn: _PostgresTransaction,
        query: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        text = query.lower()
        if (
            isinstance(params, tuple)
            and leaf.id in params
            and (("from tasks" in text and "for update" in text) or "delete from tasks" in text)
        ):
            reached_task_lock.set()
        return execute(txn, query, params)

    monkeypatch.setattr(_PostgresTransaction, "execute", observe_task_lock)

    def reset() -> None:
        try:
            service = ExpansionService(
                task_manager=LocalTaskManager(writer_db),
                llm_service=MagicMock(),
                run_manager=LocalExpansionRunManager(writer_db),
            )
            service.reset_expansion_output(parent.id, run_id=run.id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = Thread(target=reset, daemon=True)
    schema = temp_db.fetchone("SELECT current_schema() AS schema")
    assert schema is not None
    try:
        with psycopg.connect(temp_db.conninfo) as holder:
            holder.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema["schema"]))
            )
            holder.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (leaf.id,))
            worker.start()
            assert reached_task_lock.wait(timeout=5)
            if protection == "claimed":
                holder.execute(
                    "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s",
                    (session.id, leaf.id),
                )
            elif protection == "closed":
                holder.execute(
                    "UPDATE tasks SET closed_at = now(), closed_reason = 'done' WHERE id = %s",
                    (leaf.id,),
                )
            elif protection == "escalated":
                holder.execute("UPDATE tasks SET escalated_at = now() WHERE id = %s", (leaf.id,))
            elif protection == "commits":
                holder.execute(
                    "UPDATE tasks SET commits = %s WHERE id = %s", ('["' + COMMIT + '"]', leaf.id)
                )
            else:
                holder.execute(
                    """INSERT INTO task_artifacts
                       (task_id, worktree_path, worktree_id, base_commit_sha)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (task_id) DO UPDATE
                       SET worktree_path = EXCLUDED.worktree_path,
                           worktree_id = EXCLUDED.worktree_id,
                           base_commit_sha = EXCLUDED.base_commit_sha""",
                    (
                        leaf.id,
                        "/tmp/reset-concurrent-worktree",
                        "12345678-1234-4abc-8abc-123456789abc",
                        COMMIT,
                    ),
                )
            holder.commit()
        assert finished.wait(timeout=5)
        worker.join(timeout=1)
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert reason in str(errors[0])
        assert manager.get_task(leaf.id).title == "Original leaf"
    finally:
        worker.join(timeout=5)
        writer_db.close()
