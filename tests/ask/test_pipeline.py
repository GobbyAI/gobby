from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.ask.claims import AnswerDraft, ReviewClaimVerdict, ReviewerResult, canonical_hash
from gobby.ask.contracts import AskRequest, ProfileSnapshot
from gobby.ask.permissions import AskAgentStage
from gobby.ask.storage import AskRunStorage
from gobby.ask.validation import EvidenceManifest
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.utils.session_context import reset_current_agent_run_id, set_current_agent_run_id
from tests.ask.test_validation import _valid_case

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


def _replace_identity(value: object, *, run_id: str, project_id: str) -> object:
    if isinstance(value, dict):
        return {
            key: _replace_identity(child, run_id=run_id, project_id=project_id)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_identity(child, run_id=run_id, project_id=project_id)
            for child in value
        ]
    if value == "run-1":
        return run_id
    if value == "project":
        return project_id
    return value


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
        artifacts: object,
    ) -> _PreparedSnapshot:
        del repository_root, artifacts
        _draft, evidence, blobs, _review = _valid_case()
        evidence = EvidenceManifest.model_validate(
            _replace_identity(
                evidence.model_dump(mode="json", by_alias=True),
                run_id=run_id,
                project_id=self.project_id,
            )
        )
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

    async def recover_async(self, *, run_id: str, artifacts: object) -> _PreparedSnapshot:
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

    def release(self, snapshot: _PreparedSnapshot, *, artifacts: object) -> None:
        del snapshot, artifacts


class _EvidenceAdmission:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Mapping[str, Any]]] = []

    async def query(
        self,
        operation: str,
        selector: Mapping[str, Any],
        *,
        continuation: str | None = None,
    ) -> dict[str, Any]:
        assert continuation is None
        self.queries.append((operation, selector))
        return {"items": [], "complete": True}


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
        assert tool_name in {"submit_answer", "submit_review"}
        assert arguments["run_id"] == principal.ask_run_id
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

    async def launch(self, spec: Any, bind_authority: Callable[[str, object], None]) -> str:
        run_id = f"{spec.stage.value}-{spec.attempt}-{len(self.launches) + 1}"
        self.launches.append(run_id)
        self.specs[run_id] = spec
        self.statuses[run_id] = "running"
        bind_authority(run_id, object())
        self.events.append(f"launch:{run_id}")
        return run_id

    def status(self, agent_run_id: str) -> str | None:
        return self.statuses.get(agent_run_id)

    async def wait(self, agent_run_id: str, *, timeout: float) -> str:
        assert timeout > 0
        spec = self.specs[agent_run_id]
        if spec.stage in {AskAgentStage.INVESTIGATOR, AskAgentStage.REPAIR}:
            template, _evidence, _blobs, _review = _valid_case()
            body = _replace_identity(
                template.model_dump(mode="json"),
                run_id=spec.run_id,
                project_id=spec.project_id,
            )
            assert isinstance(body, dict)
            body["investigator_run_id"] = agent_run_id
            draft = AnswerDraft.model_validate(body)
            token = set_current_agent_run_id(agent_run_id)
            try:
                await self.service.submit_answer(
                    run_id=spec.run_id,
                    attempt=spec.attempt,
                    draft=draft.model_dump(mode="json"),
                    draft_hash=draft.content_hash,
                    evidence_manifest_hash=spec.evidence_manifest_hash,
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


@pytest.mark.asyncio
async def test_native_investigation_review_and_single_repair(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    try:
        from gobby.ask.service import AskService
        from gobby.ask.stages import AskStageStore
    except ModuleNotFoundError:
        raise NotImplementedError("native Ask orchestration is not implemented") from None

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
    )
    events: list[str] = []
    snapshots = _SnapshotManager(project_id, tmp_path)
    admission = _EvidenceAdmission()
    agents = _NativeAgents(events)
    permissions = _PermissionStore(events, project_id)
    service = AskService(
        storage=storage,
        stages=AskStageStore(manager),
        snapshot_manager=snapshots,
        agents=agents,
        permissions=permissions,
        state_root=tmp_path / "state",
        evidence_factory=lambda _record, _snapshot, _artifacts: admission,
        evidence_manifest_factory=lambda record, _snapshot, _artifacts: snapshots.by_run[
            record.run_id
        ],
    )
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
    result = await service.wait(started.run_id, project_id=project_id, timeout=10)

    assert result.status == "completed", result.model_dump_json(indent=2)
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
    assert admission.queries
    assert all("tool_chat" not in identity for identity in result.tool_identities)
    assert all("task" not in identity for identity in result.tool_identities)

    execution = manager.get_execution(result.run_id)
    assert execution is not None
    assert execution.status.value == "completed"
    assert execution.outputs_json is not None
    assert execution.outputs_json.count('"publication"') == 1
