from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from gobby.ask.agents import AskAgentSpec
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.claims import AnswerDraft, ReviewClaimVerdict, ReviewerResult, canonical_hash
from gobby.ask.contracts import AskRequest, AskRunRecord, ProfileSnapshot
from gobby.ask.errors import AskLifecycleConflict, AskRunNotFound
from gobby.ask.evidence_runtime import AskSnapshotManager, PreparedAskSnapshot
from gobby.ask.permissions import (
    AskAgentStage,
    AskPermissionRuntime,
    AskRuntimeProfile,
)
from gobby.ask.pipeline import ASK_PIPELINE_STEPS, parse_ask_pipeline
from gobby.ask.publication import publish_answer
from gobby.ask.storage import AskRunStorage
from gobby.ask.validation import EvidenceManifest
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.utils.session_context import reset_current_agent_run_id, set_current_agent_run_id
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_state import ExecutionStatus
from gobby.workflows.templates import TemplateEngine
from tests.ask.test_validation import _valid_case

if TYPE_CHECKING:
    from gobby.ask.service import AskService

pytestmark = pytest.mark.unit


def _profile(identifier: str, _timeout: float) -> ProfileSnapshot:
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-09T12:00:00+00:00",
        effective={
            "name": identifier,
            "provider": "claude",
            "model": "claude-test",
            "reasoning_effort": "high",
        },
    )


@dataclass(frozen=True)
class _PreparedSnapshot:
    generation: int
    binding: dict[str, Any]
    inventory: dict[str, Any]
    source_root: Path
    manifest_pointer: dict[str, Any]


class _SnapshotManager:
    def __init__(self, project_id: str, root: Path) -> None:
        self.project_id = project_id
        self.root = root
        self.by_run: dict[str, EvidenceManifest] = {}

    async def prepare_async(
        self,
        *,
        run_id: str,
        repository_root: Path,
        artifacts: AskArtifactStore,
    ) -> _PreparedSnapshot:
        del repository_root, artifacts
        _draft, evidence, blobs, _review = _valid_case(run_id=run_id, project_id=self.project_id)
        self.by_run[run_id] = evidence
        source_root = self.root / run_id / "source"
        source_root.mkdir(parents=True)
        for (relative, _blob_oid), content in blobs.items():
            path = source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return _PreparedSnapshot(
            generation=1,
            binding=evidence.snapshot_binding.model_dump(mode="json"),
            inventory=evidence.inventory.model_dump(mode="json"),
            source_root=source_root,
            manifest_pointer={
                "kind": "snapshot-lifecycle",
                "project_id": self.project_id,
                "run_id": run_id,
                "relative_path": "bodies/snapshot-lifecycle.json",
                "sha256": "f" * 64,
                "size_bytes": 1,
            },
        )

    async def recover_async(self, *, run_id: str, artifacts: AskArtifactStore) -> _PreparedSnapshot:
        del artifacts
        evidence = self.by_run[run_id]
        return _PreparedSnapshot(
            generation=2,
            binding=evidence.snapshot_binding.model_dump(mode="json"),
            inventory=evidence.inventory.model_dump(mode="json"),
            source_root=self.root / run_id / "source",
            manifest_pointer={
                "kind": "snapshot-lifecycle",
                "project_id": self.project_id,
                "run_id": run_id,
                "relative_path": "bodies/snapshot-lifecycle-recovered.json",
                "sha256": "e" * 64,
                "size_bytes": 1,
            },
        )

    def release(self, snapshot: _PreparedSnapshot, *, artifacts: AskArtifactStore) -> None:
        del snapshot, artifacts


class _EvidenceAdmission:
    def __init__(self, evidence: EvidenceManifest) -> None:
        self.evidence = evidence
        self.queries: list[tuple[str, Mapping[str, Any]]] = []

    def manifest(self) -> EvidenceManifest:
        record = self.evidence.records[0]
        return self.evidence.model_copy(
            update={
                "records": tuple(
                    record.model_copy(update={"invocation_id": f"invocation-{index}"})
                    for index, _query in enumerate(self.queries, start=1)
                )
            }
        )

    async def query(
        self,
        operation: str,
        selector: Mapping[str, Any],
        *,
        continuation: str | None = None,
    ) -> dict[str, Any]:
        assert continuation is None
        self.queries.append((operation, selector))
        return self.evidence.records[0].response.model_dump(mode="json", by_alias=True)


class _PermissionStore:
    def __init__(self, events: list[str], project_id: str) -> None:
        self.events = events
        self.project_id = project_id
        self.principals: dict[str, SimpleNamespace] = {}

    def activate(self, **arguments: Any) -> SimpleNamespace:
        agent_run_id = str(arguments["agent_run_id"])
        principal = SimpleNamespace(
            ask_run_id=str(arguments["ask_run_id"]),
            agent_run_id=agent_run_id,
            stage=arguments["stage"],
            attempt=int(arguments["attempt"]),
            project_id=self.project_id,
        )
        self.principals[agent_run_id] = principal
        self.events.append(f"authority:{agent_run_id}")
        return principal

    def replace_for_resume(self, **arguments: Any) -> SimpleNamespace:
        original = str(arguments["original_agent_run_id"])
        successor = str(arguments["successor_agent_run_id"])
        principal = self.principals.pop(original)
        principal.agent_run_id = successor
        self.principals[successor] = principal
        return principal

    def authorize(
        self,
        agent_run_id: str,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        **_keywords: object,
    ) -> SimpleNamespace:
        principal = self.principals[agent_run_id]
        assert server_name == "gobby-ask"
        assert tool_name in {"query_evidence", "submit_answer", "submit_review"}
        assert arguments["run_id"] == principal.ask_run_id
        if "attempt" in arguments:
            assert arguments["attempt"] == principal.attempt
        return principal

    def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
        self.events.append(f"revoke:{ask_run_id}:{reason}")
        return 1


class _NativeAgents:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.service: Any = None
        self.specs: dict[str, Any] = {}
        self.statuses: dict[str, str] = {}
        self.launches: list[str] = []
        self.answer_evidence_hashes: list[str] = []
        self.preflights: list[dict[AskAgentStage, ProfileSnapshot]] = []

    def preflight(self, profiles: Mapping[AskAgentStage, ProfileSnapshot]) -> None:
        assert set(profiles) == {AskAgentStage.INVESTIGATOR, AskAgentStage.REVIEWER}
        self.preflights.append(dict(profiles))
        self.events.append("preflight")

    async def launch(
        self,
        spec: AskAgentSpec,
        bind_authority: Callable[[str, AskRuntimeProfile], None],
    ) -> str:
        run_id = f"{spec.stage.value}-{spec.attempt}-{len(self.launches) + 1}"
        self.launches.append(run_id)
        self.specs[run_id] = spec
        self.statuses[run_id] = "running"
        bind_authority(run_id, cast(AskRuntimeProfile, object()))
        self.events.append(f"launch:{run_id}")
        return run_id

    def status(self, agent_run_id: str) -> str | None:
        return self.statuses.get(agent_run_id)

    async def wait(self, agent_run_id: str, *, timeout: float) -> str:
        assert timeout > 0
        spec = self.specs[agent_run_id]
        if spec.stage in {AskAgentStage.INVESTIGATOR, AskAgentStage.REPAIR}:
            template, _evidence, _blobs, _review = _valid_case(
                run_id=spec.run_id, project_id=spec.project_id
            )
            body = template.model_dump(mode="json")
            body["investigator_run_id"] = agent_run_id
            draft = AnswerDraft.model_validate(body)
            token = set_current_agent_run_id(agent_run_id)
            try:
                query_result = await self.service.query_evidence(
                    run_id=spec.run_id,
                    operation="read",
                    selector={
                        "kind": "range",
                        "path": "src/app.py",
                        "start_line": 2,
                        "end_line": 2,
                    },
                )
                evidence_manifest_hash = str(query_result["evidence_manifest_hash"])
                self.answer_evidence_hashes.append(evidence_manifest_hash)
                await self.service.submit_answer(
                    run_id=spec.run_id,
                    attempt=spec.attempt,
                    draft=draft.model_dump(mode="json"),
                    draft_hash=draft.content_hash,
                    evidence_manifest_hash=evidence_manifest_hash,
                )
            finally:
                reset_current_agent_run_id(token)
        else:
            draft = AnswerDraft.model_validate(spec.draft)
            accepted = spec.attempt > 0
            review = ReviewerResult(
                run_id=spec.run_id,
                reviewer_run_id=agent_run_id,
                draft_hash=draft.content_hash,
                evidence_manifest_hash=spec.evidence_manifest_hash,
                claim_verdicts=tuple(
                    ReviewClaimVerdict(
                        claim_id=claim.id,
                        accepted=accepted,
                        classification_supported=accepted,
                        rationale="accepted" if accepted else "repair required",
                    )
                    for claim in draft.claims
                ),
                missing_question_parts=() if accepted else (draft.question_parts[0].id,),
                rationale="independent review",
            )
            token = set_current_agent_run_id(agent_run_id)
            try:
                await self.service.submit_review(
                    run_id=spec.run_id,
                    attempt=spec.attempt,
                    review=review.model_dump(mode="json"),
                    review_hash=canonical_hash(review.model_dump(mode="json")),
                    draft_hash=draft.content_hash,
                    evidence_manifest_hash=spec.evidence_manifest_hash,
                )
            finally:
                reset_current_agent_run_id(token)
        self.statuses[agent_run_id] = "success"
        self.events.append(f"complete:{agent_run_id}")
        return "success"

    async def cancel(self, agent_run_id: str) -> None:
        self.events.append(f"cleanup:{agent_run_id}")
        self.statuses[agent_run_id] = "cancelled"


class _AskToolProxy:
    def __init__(self, project_root: Path) -> None:
        self.service: AskService | None = None
        self.project_root = project_root

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        assert server == "gobby-ask"
        assert session_id == "caller-session"
        assert enforce_workflow is False
        if self.service is None:
            raise RuntimeError("Ask service is not attached")
        run_id = str(arguments["run_id"])
        project_id = str(arguments["project_id"])
        if tool == "prepare":
            return await self.service.prepare(
                run_id=run_id,
                project_id=project_id,
                project_root=self.project_root,
            )
        if tool == "seed":
            return await self.service.seed(run_id=run_id, project_id=project_id)
        if tool == "spawn":
            return await self.service.spawn(
                run_id=run_id,
                project_id=project_id,
                stage=AskAgentStage(str(arguments["stage"])),
                attempt=int(arguments["attempt"]),
                caller_session_id=session_id,
            )
        if tool == "validate":
            return await self.service.validate(
                run_id=run_id,
                project_id=project_id,
                stage=AskAgentStage(str(arguments["stage"])),
                attempt=int(arguments["attempt"]),
            )
        if tool == "admit_repair":
            return await self.service.admit_repair(run_id=run_id, project_id=project_id)
        if tool == "publish":
            return await self.service.publish(run_id=run_id, project_id=project_id)
        raise ValueError(f"unexpected Ask pipeline tool: {tool}")


@pytest.mark.asyncio
async def test_native_investigation_review_and_single_repair(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    try:
        from gobby.ask.service import AskFaultInjected, AskService
        from gobby.ask.stages import AskStageStore
    except ModuleNotFoundError:
        raise NotImplementedError("native Ask orchestration is not implemented") from None

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    pipeline_path = (
        Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    pipeline = parse_ask_pipeline(yaml.safe_load(pipeline_path.read_text()))
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=pipeline.model_dump(mode="json"),
    )
    events: list[str] = []
    snapshots = _SnapshotManager(project_id, tmp_path)
    admissions: list[_EvidenceAdmission] = []

    def evidence_factory(
        record: AskRunRecord,
        _snapshot: PreparedAskSnapshot,
        _artifacts: AskArtifactStore,
    ) -> _EvidenceAdmission:
        _draft, evidence, _blobs, _review = _valid_case(
            run_id=record.run_id,
            project_id=project_id,
        )
        admission = _EvidenceAdmission(evidence)
        admissions.append(admission)
        return admission

    def manifest_factory(
        _record: AskRunRecord,
        _snapshot: PreparedAskSnapshot,
        _artifacts: AskArtifactStore,
    ) -> EvidenceManifest:
        return admissions[-1].manifest()

    agents = _NativeAgents(events)
    permissions = _PermissionStore(events, project_id)
    proxy = _AskToolProxy(tmp_path)
    injected_boundaries = {"investigator:0:submitted", "publish"}

    def inject_fault(boundary_id: str) -> None:
        fault_key = "publish" if boundary_id.startswith("publish:") else boundary_id
        if fault_key in injected_boundaries:
            injected_boundaries.remove(fault_key)
            raise AskFaultInjected(boundary_id)

    executor = PipelineExecutor(
        db=temp_db,
        execution_manager=manager,
        llm_service=object(),
        template_engine=TemplateEngine(),
        tool_proxy_getter=lambda: proxy,
    )
    service = AskService(
        storage=storage,
        stages=AskStageStore(manager),
        snapshot_manager=cast("AskSnapshotManager", snapshots),
        agents=agents,
        permissions=cast("AskPermissionRuntime", permissions),
        pipeline_executor=executor,
        state_root=tmp_path / "state",
        evidence_factory=evidence_factory,
        evidence_manifest_factory=manifest_factory,
        fault_injector=inject_fault,
    )
    proxy.service = service
    agents.service = service
    request = AskRequest(
        question="What does alpha return?",
        project_id=project_id,
        investigator_profile="ask-investigator",
        reviewer_profile="ask-reviewer",
    )

    started = await service.start(
        request,
        project_root=tmp_path,
        caller_session_id="caller-session",
    )
    interrupted = await service.wait(started.run_id, project_id=project_id, timeout=10)
    assert interrupted.status == "failed"
    assert agents.launches == ["investigator-0-1"]
    before_resume = manager.get_steps_for_execution(started.run_id)
    preserved_outputs = {
        step.step_id: step.output_json for step in before_resume if step.status.value == "completed"
    }

    await service.resume(
        started.run_id,
        project_id=project_id,
        caller_session_id="resume-operator",
    )
    publication_interrupted = await service.wait(
        started.run_id,
        project_id=project_id,
        timeout=10,
    )
    assert publication_interrupted.status == "failed"
    assert publication_interrupted.artifact_manifest is not None
    publication_root = Path(publication_interrupted.artifact_manifest["root"])
    manifest_before_resume = (publication_root / "manifest.json").read_bytes()

    await service.resume(
        started.run_id,
        project_id=project_id,
        caller_session_id="second-resume-operator",
    )
    result = await service.wait(started.run_id, project_id=project_id, timeout=10)

    assert result.status == "completed", result.model_dump_json(indent=2)
    assert len(agents.preflights) == 3
    assert result.current_stage == "publish"
    assert result.answer_outcome == "complete"
    assert result.typed_error is None
    assert result.artifact_manifest is not None
    assert Path(result.artifact_manifest["root"]).is_dir()
    assert len(set(agents.launches)) == 4
    assert [agents.specs[run_id].stage for run_id in agents.launches] == [
        AskAgentStage.INVESTIGATOR,
        AskAgentStage.REVIEWER,
        AskAgentStage.REPAIR,
        AskAgentStage.REVIEWER,
    ]
    assert [agents.specs[run_id].attempt for run_id in agents.launches] == [0, 0, 1, 1]
    assert result.attempt_count == 4
    assert result.repair_count == 1
    admission = admissions[-1]
    assert admission.queries
    reviewer_specs = [
        agents.specs[run_id]
        for run_id in agents.launches
        if agents.specs[run_id].stage is AskAgentStage.REVIEWER
    ]
    assert [spec.evidence_manifest_hash for spec in reviewer_specs] == (
        agents.answer_evidence_hashes
    )
    assert all("tool_chat" not in identity for identity in result.tool_identities)
    assert all("task" not in identity for identity in result.tool_identities)

    execution = manager.get_execution(result.run_id)
    assert execution is not None
    assert execution.id == result.run_id
    assert execution.status.value == "completed"
    execution_count = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM pipeline_executions WHERE id = %s",
        (result.run_id,),
    )
    assert execution_count is not None
    assert execution_count["count"] == 1
    steps = manager.get_steps_for_execution(result.run_id)
    assert tuple(item.step_id for item in steps) == tuple(ASK_PIPELINE_STEPS)
    assert all(item.status.value == "completed" for item in steps)
    assert {
        step.step_id: step.output_json for step in steps if step.step_id in preserved_outputs
    } == preserved_outputs

    publication_root = Path(result.artifact_manifest["root"])
    assert (publication_root / "manifest.json").read_bytes() == manifest_before_resume
    published_evidence = json.loads((publication_root / "evidence-manifest.json").read_text())
    assert len(published_evidence["records"]) == len(admission.queries)
    assert published_evidence["records"][-1]["invocation_id"] == (
        f"invocation-{len(admission.queries)}"
    )


@pytest.mark.parametrize(
    ("termination", "stall_point"),
    [
        pytest.param("cancel", "write", id="cancel-during-write"),
        pytest.param("cancel", "rename", id="cancel-after-final-guard"),
        pytest.param("deadline", "write", id="deadline-during-write"),
    ],
)
@pytest.mark.asyncio
async def test_publication_termination_cannot_expose_completed_answer(
    termination: str,
    stall_point: str,
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ask import publication as publication_module
    from gobby.ask import service as service_module
    from gobby.ask import stage_runtime as stage_runtime_module
    from gobby.ask.service import AskService
    from gobby.ask.stages import AskStageStore

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    pipeline_path = (
        Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    pipeline = parse_ask_pipeline(yaml.safe_load(pipeline_path.read_text()))
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=pipeline.model_dump(mode="json"),
    )
    events: list[str] = []
    snapshots = _SnapshotManager(project_id, tmp_path)
    admissions: list[_EvidenceAdmission] = []

    def evidence_factory(
        record: AskRunRecord,
        _snapshot: PreparedAskSnapshot,
        _artifacts: AskArtifactStore,
    ) -> _EvidenceAdmission:
        _draft, evidence, _blobs, _review = _valid_case(
            run_id=record.run_id,
            project_id=project_id,
        )
        admission = _EvidenceAdmission(evidence)
        admissions.append(admission)
        return admission

    agents = _NativeAgents(events)
    permissions = _PermissionStore(events, project_id)
    proxy = _AskToolProxy(tmp_path)
    executor = PipelineExecutor(
        db=temp_db,
        execution_manager=manager,
        llm_service=object(),
        template_engine=TemplateEngine(),
        tool_proxy_getter=lambda: proxy,
    )
    service = AskService(
        storage=storage,
        stages=AskStageStore(manager),
        snapshot_manager=cast("AskSnapshotManager", snapshots),
        agents=agents,
        permissions=cast("AskPermissionRuntime", permissions),
        pipeline_executor=executor,
        state_root=tmp_path / "state",
        evidence_factory=evidence_factory,
        evidence_manifest_factory=lambda _record, _snapshot, _artifacts: admissions[-1].manifest(),
    )
    proxy.service = service
    agents.service = service
    write_started = threading.Event()
    release_write = threading.Event()
    producer_finished = threading.Event()
    original_write = publication_module._write_file
    original_rename = os.rename
    original_publish = publish_answer

    def stalled_write(path: Path, payload: bytes) -> None:
        if stall_point == "write" and not write_started.is_set():
            write_started.set()
            assert release_write.wait(timeout=10)
        original_write(path, payload)

    def stalled_rename(source: Path, target: Path) -> None:
        if stall_point == "rename" and not write_started.is_set():
            write_started.set()
            assert release_write.wait(timeout=10)
        original_rename(source, target)

    def tracked_publish(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_publish(*args, **kwargs)
        finally:
            producer_finished.set()

    monkeypatch.setattr(publication_module, "_write_file", stalled_write)
    monkeypatch.setattr("gobby.ask.publication.os.rename", stalled_rename)
    monkeypatch.setattr("gobby.ask.stage_runtime.publish_answer", tracked_publish)
    if termination == "deadline":
        monkeypatch.setattr(service_module, "_REVIEW_RESERVE_SECONDS", 0)

        async def skip_seed(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(stage_runtime_module.AskStageRuntime, "_seed", skip_seed)
    started = await service.start(
        AskRequest(
            question="What does alpha return?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
            timeout_seconds=3 if termination == "deadline" else 600,
        ),
        project_root=tmp_path,
        caller_session_id="caller-session",
    )
    assert await asyncio.to_thread(write_started.wait, 10)
    producer = service._tasks[started.run_id]
    if termination == "cancel":
        cancel_task = asyncio.create_task(
            service.cancel(
                started.run_id,
                project_id=project_id,
                caller_session_id="caller-session",
            )
        )
        await asyncio.sleep(0)
        cancelled_execution = manager.get_execution(started.run_id)
        assert cancelled_execution is not None
        assert cancelled_execution.status is ExecutionStatus.CANCELLED
        release_write.set()
        result = await asyncio.wait_for(cancel_task, timeout=10)
        assert result.status == ExecutionStatus.CANCELLED.value
        assert producer_finished.is_set()
    else:
        started_waiting = time.monotonic()
        try:
            result = await service.wait(started.run_id, project_id=project_id, timeout=5)
        finally:
            release_write.set()
        assert time.monotonic() - started_waiting < 4
        assert result.status == ExecutionStatus.FAILED.value
        assert result.typed_error is not None
        assert result.typed_error["code"] == "deadline_exceeded"
        await service.stage_runtime.wait_for_publication_cleanup(started.run_id)
        assert producer_finished.is_set()

    assert producer.done()
    assert service.stage_runtime.resources == {}
    with pytest.raises(AskLifecycleConflict, match="no completed publication"):
        service.publication_root(started.run_id, project_id=project_id)
    with pytest.raises(AskRunNotFound, match="Ask run not found"):
        service.get(started.run_id, project_id="foreign-project")
    publication_root = (
        AskArtifactStore(tmp_path / "state", project_id, started.run_id).run_root / "publication"
    )
    assert not publication_root.exists()
