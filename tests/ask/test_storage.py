from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.ask.contracts import AskRequest, ProfileSnapshot, RetrievalMode
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit


def _profile(identifier: str) -> ProfileSnapshot:
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-08T12:00:00+00:00",
        effective={"name": identifier, "provider": "codex", "model": "gpt-test"},
    )


def test_pinned_idempotent_run_persistence(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    calls = {"commit": 0, "investigator": 0, "reviewer": 0}
    calls_lock = threading.Lock()

    def resolve_commit(_root: Path, ref: str) -> tuple[str, str]:
        assert ref == "HEAD"
        with calls_lock:
            calls["commit"] += 1
        return "a" * 40, "b" * 40

    def resolve_profile(identifier: str) -> ProfileSnapshot:
        with calls_lock:
            calls[identifier] += 1
        return _profile(identifier)

    storage = AskRunStorage(
        manager,
        pipeline_name="native-ask",
        pipeline_snapshot={"name": "native-ask", "version": 1},
        commit_resolver=resolve_commit,
        profile_resolver=resolve_profile,
        now=lambda: datetime(2026, 9, 8, 12, tzinfo=UTC),
    )
    request = AskRequest(
        question="Where is the source of truth?",
        project_id=project_id,
        investigator_profile="investigator",
        reviewer_profile="reviewer",
        idempotency_key="same-request",
    )
    assert request.commit_ref == "HEAD"
    assert request.timeout_seconds == 600
    assert request.retrieval_mode is RetrievalMode.DETERMINISTIC

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: storage.start(request, project_root=tmp_path), range(4)))

    assert len({record.run_id for record in records}) == 1
    record = records[0]
    assert record.binding.commit_oid == "a" * 40
    assert record.binding.tree_oid == "b" * 40
    assert record.binding.deadline_at == datetime(2026, 9, 8, 12, 10, tzinfo=UTC)
    assert record.investigator.identifier == "investigator"
    assert record.reviewer.identifier == "reviewer"
    assert calls == {"commit": 1, "investigator": 1, "reviewer": 1}

    independent = storage.start(request.model_copy(update={"idempotency_key": None}), tmp_path)
    another = storage.start(request.model_copy(update={"idempotency_key": None}), tmp_path)
    assert independent.run_id != another.run_id != record.run_id
    assert manager.count_executions(pipeline_name="native-ask") == 3

    persisted = manager.get_execution(record.run_id)
    assert persisted is not None
    assert json.loads(persisted.inputs_json or "{}")["ask"]["binding"]["deadline_at"] == (
        "2026-09-08T12:10:00Z"
    )
    assert json.loads(persisted.definition_json or "{}") == {"name": "native-ask", "version": 1}
    assert record.investigator.content_hash is not None
    snapshot_pointer = {
        "kind": "snapshot",
        "sha256": "d" * 64,
        "relative_path": "bodies/snapshot.json",
        "size_bytes": 123,
    }
    attached = storage.attach_snapshot(
        record.run_id,
        inventory_digest="c" * 64,
        snapshot_artifact=snapshot_pointer,
    )
    assert attached.binding.inventory_digest == "c" * 64
    assert attached.binding.snapshot_artifact == snapshot_pointer
    with pytest.raises(ValueError, match="immutable"):
        storage.attach_snapshot(
            record.run_id,
            inventory_digest="e" * 64,
            snapshot_artifact=snapshot_pointer,
        )
    relation = temp_db.fetchone("SELECT to_regclass('public.ask_runs') AS relation")
    assert relation is not None
    assert relation["relation"] is None

    with pytest.raises(ValueError, match="different request"):
        storage.start(
            request.model_copy(update={"question": "A different question"}),
            project_root=tmp_path,
        )
