from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.ask.contracts import AskRequest, ProfileSnapshot, RetrievalMode
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit


def _profile(identifier: str, _timeout: float) -> ProfileSnapshot:
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

    def resolve_commit(_root: Path, ref: str, timeout: float) -> tuple[str, str]:
        assert ref == "HEAD"
        assert 0 < timeout <= 600
        with calls_lock:
            calls["commit"] += 1
        return "a" * 40, "b" * 40

    def resolve_profile(identifier: str, timeout: float) -> ProfileSnapshot:
        assert 0 < timeout <= 600
        with calls_lock:
            calls[identifier] += 1
        return _profile(identifier, timeout)

    admitted_at = datetime.now(UTC)
    storage = AskRunStorage(
        manager,
        pipeline_name="native-ask",
        pipeline_snapshot={"name": "native-ask", "version": 1},
        commit_resolver=resolve_commit,
        profile_resolver=resolve_profile,
        now=lambda: admitted_at,
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
    assert record.binding.deadline_at == admitted_at + timedelta(minutes=10)
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
        record.binding.deadline_at.isoformat().replace("+00:00", "Z")
    )
    assert json.loads(persisted.definition_json or "{}") == {"name": "native-ask", "version": 1}
    assert record.investigator.content_hash is not None
    snapshot_pointer = {
        "kind": "snapshot",
        "project_id": project_id,
        "run_id": record.run_id,
        "sha256": "d" * 64,
        "relative_path": "bodies/snapshot.json",
        "size_bytes": 123,
    }
    attached = storage.attach_snapshot(
        record.run_id,
        inventory_digest="c" * 64,
        snapshot_artifact=snapshot_pointer,
        deadline_at=record.binding.deadline_at,
    )
    assert attached.binding.inventory_digest == "c" * 64
    assert attached.binding.snapshot_artifact == snapshot_pointer
    lifecycle_pointer = {
        "kind": "snapshot-lifecycle",
        "project_id": project_id,
        "run_id": record.run_id,
        "sha256": "f" * 64,
        "relative_path": "bodies/snapshot-lifecycle.json",
        "size_bytes": 456,
    }
    storage.publish_snapshot_generation(
        record.run_id,
        generation=1,
        lifecycle_artifact=lifecycle_pointer,
        expected_previous_generation=None,
        deadline_at=record.binding.deadline_at,
    )
    current = storage.get_snapshot_generation(record.run_id)
    assert current is not None
    assert current.generation == 1
    assert current.lifecycle_artifact == lifecycle_pointer
    with pytest.raises(ValueError, match="immutable"):
        storage.attach_snapshot(
            record.run_id,
            inventory_digest="e" * 64,
            snapshot_artifact=snapshot_pointer,
            deadline_at=record.binding.deadline_at,
        )
    relation = temp_db.fetchone("SELECT to_regclass('public.ask_runs') AS relation")
    assert relation is not None
    assert relation["relation"] is None

    with pytest.raises(ValueError, match="different request"):
        storage.start(
            request.model_copy(update={"question": "A different question"}),
            project_root=tmp_path,
        )


@pytest.mark.parametrize("timeout", [float("inf"), float("-inf"), float("nan")])
def test_ask_request_rejects_nonfinite_timeouts(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        AskRequest(
            question="Where is the source of truth?",
            project_id="project",
            investigator_profile="investigator",
            reviewer_profile="reviewer",
            timeout_seconds=timeout,
        )


def test_start_captures_deadline_before_resolution(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    events: list[str] = []

    def now() -> datetime:
        events.append("admitted")
        return datetime(2026, 9, 8, 12, tzinfo=UTC)

    def resolve_commit(_root: Path, _ref: str, timeout: float) -> tuple[str, str]:
        assert events == ["admitted"]
        assert 0 < timeout <= 600
        events.append("commit")
        return "a" * 40, "b" * 40

    def resolve_profile(identifier: str, timeout: float) -> ProfileSnapshot:
        assert 0 < timeout <= 600
        events.append(identifier)
        return _profile(identifier, timeout)

    storage = AskRunStorage(
        LocalPipelineExecutionManager(temp_db, project_id=project_id),
        commit_resolver=resolve_commit,
        profile_resolver=resolve_profile,
        now=now,
    )
    record = storage.start(
        AskRequest(
            question="Where is the source of truth?",
            project_id=project_id,
            investigator_profile="investigator",
            reviewer_profile="reviewer",
        ),
        tmp_path,
    )

    assert events == ["admitted", "commit", "investigator", "reviewer"]
    assert record.binding.deadline_at == datetime(2026, 9, 8, 12, 10, tzinfo=UTC)
