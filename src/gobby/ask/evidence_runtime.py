"""Active evidence admission and immutable manifest snapshots for native Ask."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import AskRunRecord, EvidenceReference
from gobby.ask.permissions import AskAgentStage
from gobby.ask.snapshots import SnapshotDriftError
from gobby.ask.stages import AskAttemptStatus, AskStage, AskStageStore
from gobby.ask.storage import AskRunStorage
from gobby.ask.validation_models import (
    EvidenceManifest,
    GraphEvidenceItem,
    RecordedEvidence,
    SourceEvidence,
)


class PreparedAskSnapshot(Protocol):
    binding: dict[str, Any]
    source_root: Path


class AskSnapshotManager(Protocol):
    async def prepare_async(
        self,
        *,
        run_id: str,
        repository_root: Path,
        artifacts: AskArtifactStore,
    ) -> PreparedAskSnapshot: ...

    async def recover_async(
        self,
        *,
        run_id: str,
        artifacts: AskArtifactStore,
    ) -> PreparedAskSnapshot: ...

    def release(
        self,
        snapshot: PreparedAskSnapshot,
        *,
        artifacts: AskArtifactStore,
    ) -> None: ...


class AskEvidenceSession(Protocol):
    async def query(
        self,
        operation: str,
        selector: Mapping[str, Any],
        *,
        continuation: str | None = None,
    ) -> dict[str, Any]: ...


EvidenceFactory = Callable[
    [AskRunRecord, PreparedAskSnapshot, AskArtifactStore], AskEvidenceSession
]
EvidenceManifestFactory = Callable[
    [AskRunRecord, PreparedAskSnapshot, AskArtifactStore], EvidenceManifest
]
FaultInjector = Callable[[str], None]


@dataclass(slots=True)
class _ActiveEvidence:
    record: AskRunRecord
    snapshot: PreparedAskSnapshot
    artifacts: AskArtifactStore
    admission: AskEvidenceSession
    lock: asyncio.Lock


class AskEvidenceRuntime:
    """Serialize agent evidence retrieval with accepted manifest checkpoints."""

    def __init__(
        self,
        *,
        stages: AskStageStore,
        manifest_factory: EvidenceManifestFactory,
        remaining_seconds: Callable[[Any], float],
        fault_injector: FaultInjector,
    ) -> None:
        self.stages = stages
        self.manifest_factory = manifest_factory
        self.remaining_seconds = remaining_seconds
        self.fault_injector = fault_injector
        self._active: dict[str, _ActiveEvidence] = {}

    def activate(
        self,
        record: AskRunRecord,
        snapshot: PreparedAskSnapshot,
        artifacts: AskArtifactStore,
        admission: AskEvidenceSession,
    ) -> None:
        self._active[record.run_id] = _ActiveEvidence(
            record=record,
            snapshot=snapshot,
            artifacts=artifacts,
            admission=admission,
            lock=asyncio.Lock(),
        )

    def deactivate(self, run_id: str) -> None:
        self._active.pop(run_id, None)

    def admission(self, run_id: str) -> AskEvidenceSession:
        return self._required(run_id).admission

    @asynccontextmanager
    async def submission_guard(self, run_id: str) -> AsyncIterator[None]:
        async with self._required(run_id).lock:
            yield

    async def persist_manifest(
        self,
        run_id: str,
        *,
        stage: AskStage,
    ) -> EvidenceManifest:
        active = self._required(run_id)
        async with active.lock:
            return await self._persist(active, stage=stage)

    async def query(
        self,
        *,
        run_id: str,
        stage: AskAgentStage,
        attempt: int,
        agent_run_id: str,
        operation: str,
        selector: Mapping[str, Any],
        continuation: str | None,
    ) -> dict[str, Any]:
        active = self._required(run_id)
        async with active.lock:
            self._assert_active_attempt(
                run_id,
                stage=stage,
                attempt=attempt,
                agent_run_id=agent_run_id,
            )
            response = await active.admission.query(
                operation,
                selector,
                continuation=continuation,
            )
            manifest = await self._persist(
                active,
                stage=(AskStage.REPAIR if stage is AskAgentStage.REPAIR else AskStage.INVESTIGATOR),
            )
            if "evidence_manifest_hash" in response:
                raise RuntimeError("evidence response used a reserved Ask field")
            return {**response, "evidence_manifest_hash": manifest.content_hash}

    async def load_current(
        self,
        run_id: str,
        *,
        expected_hash: str | None = None,
    ) -> EvidenceManifest:
        active = self._required(run_id)
        state = await asyncio.to_thread(self.stages.get, run_id)
        if state is None or state.evidence_manifest_artifact is None:
            raise RuntimeError("Ask evidence manifest is not available")
        manifest = EvidenceManifest.model_validate(
            await asyncio.to_thread(
                active.artifacts.read_body,
                state.evidence_manifest_artifact,
            )
        )
        if manifest.content_hash != state.evidence_manifest_hash:
            raise RuntimeError("Ask evidence manifest checkpoint hash mismatch")
        if expected_hash is not None and manifest.content_hash != expected_hash:
            raise RuntimeError("Ask submission references a superseded evidence manifest")
        return manifest

    async def _persist(
        self,
        active: _ActiveEvidence,
        *,
        stage: AskStage,
    ) -> EvidenceManifest:
        manifest = await asyncio.to_thread(
            self.manifest_factory,
            active.record,
            active.snapshot,
            active.artifacts,
        )
        pointer = await asyncio.to_thread(
            active.artifacts.write_body,
            "evidence-manifest",
            manifest.model_dump(mode="json", by_alias=True),
            timeout_seconds=self.remaining_seconds(active.record.binding.deadline_at),
        )
        boundary_id = f"evidence:{manifest.content_hash}"
        await asyncio.to_thread(
            self.stages.checkpoint,
            active.record.run_id,
            stage=stage,
            boundary_id=boundary_id,
            evidence_manifest_artifact=pointer,
            evidence_manifest_hash=manifest.content_hash,
        )
        self.fault_injector(boundary_id)
        return manifest

    def _assert_active_attempt(
        self,
        run_id: str,
        *,
        stage: AskAgentStage,
        attempt: int,
        agent_run_id: str,
    ) -> None:
        state = self.stages.get(run_id)
        if state is None:
            raise PermissionError("Ask orchestration state is unavailable")
        for checkpoint in state.attempts:
            if checkpoint.stage is stage and checkpoint.attempt == attempt:
                if (
                    checkpoint.status is not AskAttemptStatus.RUNNING
                    or checkpoint.agent_run_id != agent_run_id
                ):
                    raise PermissionError("Ask attempt is not accepting evidence queries")
                return
        raise PermissionError("Ask evidence query has no active stage attempt")

    def _required(self, run_id: str) -> _ActiveEvidence:
        active = self._active.get(run_id)
        if active is None:
            raise RuntimeError("Ask evidence runtime is not active")
        return active


def evidence_references(storage: AskRunStorage, run_id: str) -> list[EvidenceReference]:
    return storage.evidence_references(run_id)


def build_evidence_manifest(
    storage: AskRunStorage,
    record: AskRunRecord,
    snapshot: PreparedAskSnapshot,
    artifacts: AskArtifactStore,
) -> EvidenceManifest:
    records: list[RecordedEvidence] = []
    for reference in evidence_references(storage, record.run_id):
        if reference.status != "succeeded" or reference.response_hash is None:
            continue
        result = artifacts.read_body(reference.result_artifact)
        response = result.get("response")
        if not isinstance(response, dict):
            raise RuntimeError("successful Ask evidence artifact omitted its response")
        records.append(
            RecordedEvidence.model_validate(
                {
                    "run_id": record.run_id,
                    "invocation_id": reference.invocation_id,
                    "binding_digest": reference.binding_digest,
                    "request_hash": reference.request_hash,
                    "response_hash": reference.response_hash,
                    "response": response,
                }
            )
        )
    return EvidenceManifest.model_validate(
        {
            "run_id": record.run_id,
            "project_id": record.binding.project_id,
            "repository_binding": snapshot.binding,
            "records": records,
        }
    )


def pinned_blobs(
    snapshot: PreparedAskSnapshot,
    evidence: EvidenceManifest,
) -> dict[tuple[str, str], bytes]:
    source_root = snapshot.source_root.resolve()
    blobs: dict[tuple[str, str], bytes] = {}
    for record in evidence.records:
        for item in record.response.items:
            source = item.source if isinstance(item, GraphEvidenceItem) else item
            if not isinstance(source, SourceEvidence):
                continue
            path = (source_root / source.path).resolve()
            if not path.is_relative_to(source_root):
                raise SnapshotDriftError("Ask evidence path escaped the repository")
            with path.open("rb") as stream:
                content = stream.read(10 * 1024 * 1024 + 1)
            if len(content) > 10 * 1024 * 1024:
                raise SnapshotDriftError("Ask evidence source exceeds its byte bound")
            blobs[(source.path, source.content_hash)] = content
    return blobs
