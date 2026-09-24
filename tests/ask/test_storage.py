from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from gobby.ask.contracts import AskRequest, EvidenceReference, ProfileSnapshot, RetrievalMode
from gobby.ask.pipeline import parse_ask_pipeline
from gobby.ask.stages import AskStage, AskStageStore
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_state import ExecutionStatus
from tests.ask.service_support import build_ask_service

pytestmark = pytest.mark.unit


def _profile(identifier: str, _timeout: float) -> ProfileSnapshot:
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-08T12:00:00+00:00",
        effective={"name": identifier, "provider": "codex", "model": "gpt-test"},
    )


def test_orchestration_checkpoints_do_not_write_executor_step_outputs(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    pipeline_path = (
        Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=parse_ask_pipeline(yaml.safe_load(pipeline_path.read_text())).model_dump(
            mode="json"
        ),
    )
    record = storage.start(
        AskRequest(
            question="Who owns declared pipeline step output?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)

    stages.initialize(record)
    stages.checkpoint(
        record.run_id,
        stage=AskStage.SEED_QUERIES,
        boundary_id="seed:complete",
    )

    step_rows = manager.get_steps_for_execution(record.run_id)
    assert step_rows == []
    execution = manager.get_execution(record.run_id)
    assert execution is not None
    inputs = json.loads(execution.inputs_json or "{}")
    assert inputs["ask"]["runtime"]["orchestration"]["run_id"] == record.run_id
    assert stages.step_output(record.run_id, "seed")["run_id"] == record.run_id


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
    current_record = storage.get(record.run_id)
    assert current_record is not None
    current = current_record.generation
    assert current is not None
    assert current.generation == 1
    assert current.lifecycle_artifact == lifecycle_pointer
    assert current_record.binding.repository_root == str(tmp_path.resolve())
    with pytest.raises(ValueError, match="generation changed"):
        storage.publish_snapshot_generation(
            record.run_id,
            generation=2,
            lifecycle_artifact=lifecycle_pointer,
            expected_previous_generation=None,
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


def test_runtime_metadata_survives_executor_outputs_and_concurrent_evidence(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
    )
    record = storage.start(
        AskRequest(
            question="Does executor output replacement erase Ask runtime state?",
            project_id=project_id,
            investigator_profile="investigator",
            reviewer_profile="reviewer",
        ),
        tmp_path,
    )
    inputs = storage.bind_execution_context(
        record.run_id,
        project_root=tmp_path,
        caller_session_id="caller-session",
    )
    assert inputs["run_id"] == record.run_id
    assert storage.execution_inputs(record.run_id) == inputs

    lifecycle = {
        "kind": "snapshot-lifecycle",
        "project_id": project_id,
        "run_id": record.run_id,
        "sha256": "f" * 64,
    }
    storage.publish_snapshot_generation(
        record.run_id,
        generation=1,
        lifecycle_artifact=lifecycle,
        expected_previous_generation=None,
        deadline_at=record.binding.deadline_at,
    )

    def append(index: int) -> None:
        storage.append_evidence_reference(
            record.run_id,
            EvidenceReference(
                invocation_id=f"invocation-{index}",
                operation="read",
                status="succeeded",
                invocation_artifact={"kind": "invocation", "index": index},
                result_artifact={"kind": "result", "index": index},
                request_hash=f"{index:064x}",
                response_hash=f"{index + 10:064x}",
                binding_digest="c" * 64,
            ),
            deadline_at=record.binding.deadline_at,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(append, (1, 2)))

    manager.update_execution_status(
        execution_id=record.run_id,
        status=ExecutionStatus.COMPLETED,
        outputs_json=json.dumps({"result": {"status": "completed"}}),
    )

    refreshed = storage.get(record.run_id)
    assert refreshed is not None
    generation = refreshed.generation
    assert generation is not None and generation.lifecycle_artifact == lifecycle
    assert {item.invocation_id for item in storage.evidence_references(record.run_id)} == {
        "invocation-1",
        "invocation-2",
    }
    execution = manager.get_execution(record.run_id)
    assert execution is not None
    assert json.loads(execution.outputs_json or "{}") == {"result": {"status": "completed"}}


async def test_service_start_resolves_the_commit_off_the_event_loop(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #22829: _run_git is synchronous; the daemon reaches it only through
    # AskService.start, which runs AskRunStorage.start in a worker thread.
    project_id = str(sample_project["id"])
    harness = build_ask_service(temp_db, project_id=project_id, state_root=tmp_path / "state")
    harness.storage.commit_resolver = None
    loop_running: list[bool] = []

    def run_git(_root: Path, *arguments: str, timeout: float) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop_running.append(False)
        else:
            loop_running.append(True)
        return "b" * 40 if arguments[-1].endswith("^{tree}") else "a" * 40

    monkeypatch.setattr("gobby.ask.storage._run_git", run_git)
    await harness.service.start(
        AskRequest(
            question="Where is the source of truth?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        project_root=tmp_path,
        caller_session_id="22222222-2222-4222-8222-222222222222",
    )
    await harness.service.stop()

    assert loop_running == [False, False]
