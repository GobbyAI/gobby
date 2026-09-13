"""Database durability, integrity, ownership, and retention of Ask results."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.retention import cleanup_expired
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager


def _store(db: HubDatabase, project: dict[str, object], root: Path) -> AskArtifactStore:
    project_id = str(project["id"])
    manager = LocalPipelineExecutionManager(db, project_id=project_id)
    execution = manager.create_execution(pipeline_name="native-ask", project_id=project_id)
    return AskArtifactStore(root, project_id, execution.id, db=db)


def test_database_artifacts_survive_restart_and_concurrent_writes(
    temp_db: HubDatabase, sample_project: dict[str, object], tmp_path: Path
) -> None:
    existing_paths = set(tmp_path.iterdir())
    store = _store(temp_db, sample_project, tmp_path)
    body: dict[str, Any] = {"schema_version": 1, "payload": "durable π"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pointers = list(pool.map(lambda _: store.write_body("evidence", body), range(8)))
    assert all(pointer == pointers[0] for pointer in pointers)
    reopened = AskArtifactStore(tmp_path, store.project_id, store.run_id, db=temp_db)
    assert reopened.read_body(pointers[0]) == body
    reopened.verify_manifest()
    assert set(tmp_path.iterdir()) == existing_paths
    with pytest.raises(RuntimeError, match="size mismatch"):
        reopened.read_body({**pointers[0], "size_bytes": 0})
    with pytest.raises(ValueError, match="does not belong"):
        reopened.read_body({**pointers[0], "project_id": "another-project"})
    other = _store(temp_db, sample_project, tmp_path)
    with pytest.raises(ValueError, match="does not belong"):
        other.read_body(pointers[0])
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE ask_artifacts SET body = %s WHERE execution_id = %s",
            ('{"payload":"tampered"}', store.run_id),
        )
    with pytest.raises(RuntimeError, match="hash mismatch"):
        reopened.read_body(pointers[0])


def test_artifact_rollback_leaves_no_body(
    temp_db: HubDatabase, sample_project: dict[str, object], tmp_path: Path
) -> None:
    store = _store(temp_db, sample_project, tmp_path)
    with pytest.raises(RuntimeError, match="abort publication"):
        with temp_db.transaction():
            store.write_body("publication", {"answer": "uncommitted"})
            raise RuntimeError("abort publication")
    assert (
        temp_db.fetchone("SELECT 1 FROM ask_artifacts WHERE execution_id = %s", (store.run_id,))
        is None
    )


def test_retention_is_bounded_repeatable_and_preserves_recoverable_work(
    temp_db: HubDatabase, sample_project: dict[str, object], tmp_path: Path
) -> None:
    expired = [_store(temp_db, sample_project, tmp_path) for _ in range(2)]
    running = _store(temp_db, sample_project, tmp_path)
    resumable = _store(temp_db, sample_project, tmp_path)
    recent = _store(temp_db, sample_project, tmp_path)
    for store in expired + [running, resumable, recent]:
        store.write_body("evidence", {"run": store.run_id})
    with temp_db.transaction() as conn:
        for store in expired:
            conn.execute(
                "UPDATE pipeline_executions SET status = 'completed', completed_at = NOW() - INTERVAL '8 days' WHERE id = %s",
                (store.run_id,),
            )
        conn.execute(
            "UPDATE pipeline_executions SET status = 'running', completed_at = NULL WHERE id = %s",
            (running.run_id,),
        )
        conn.execute(
            "UPDATE pipeline_executions SET status = 'failed', completed_at = NOW() - INTERVAL '8 days', inputs_json = jsonb_build_object('ask', jsonb_build_object('binding', jsonb_build_object('deadline_at', NOW() + INTERVAL '1 day'))) WHERE id = %s",
            (resumable.run_id,),
        )
        conn.execute(
            "UPDATE pipeline_executions SET status = 'cancelled', completed_at = NOW() WHERE id = %s",
            (recent.run_id,),
        )
    assert cleanup_expired(temp_db, days=7, limit=1) == 1
    assert cleanup_expired(temp_db, days=7, limit=1) == 1
    assert cleanup_expired(temp_db, days=7, limit=1) == 0
    for store in expired:
        assert (
            temp_db.fetchone("SELECT 1 FROM ask_artifacts WHERE execution_id = %s", (store.run_id,))
            is None
        )
    for store in (running, resumable, recent):
        assert (
            temp_db.fetchone("SELECT 1 FROM ask_artifacts WHERE execution_id = %s", (store.run_id,))
            is not None
        )
