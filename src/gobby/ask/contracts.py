"""Typed contracts shared by Ask persistence, snapshots, and evidence admission."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RetrievalMode(StrEnum):
    DETERMINISTIC = "deterministic"
    AUDITED_HYBRID = "audited_hybrid"


class AskRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str
    project_id: str
    commit_ref: str = "HEAD"
    timeout_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    retrieval_mode: RetrievalMode = RetrievalMode.DETERMINISTIC
    investigator_profile: str
    reviewer_profile: str
    idempotency_key: str | None = None

    @field_validator(
        "question",
        "project_id",
        "commit_ref",
        "investigator_profile",
        "reviewer_profile",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


class ProfileSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str
    definition_id: str
    definition_updated_at: str
    effective: dict[str, Any]
    content_hash: str | None = None


class AskBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    commit_oid: str
    tree_oid: str
    deadline_at: datetime
    retrieval_mode: RetrievalMode
    inventory_digest: str | None = None
    snapshot_artifact: dict[str, Any] | None = None


class EvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str
    operation: str
    status: str
    invocation_artifact: dict[str, Any]
    result_artifact: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()
    request_hash: str
    response_hash: str | None = None
    snapshot_inventory_digest: str
    contract: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


class AskRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    request: AskRequest
    binding: AskBinding
    investigator: ProfileSnapshot
    reviewer: ProfileSnapshot


class SnapshotGeneration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    generation: int = Field(gt=0)
    lifecycle_artifact: dict[str, Any]


class AskRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: str
    current_stage: str | None = None
    answer_outcome: str | None = None
    typed_error: dict[str, str] | None = None
    deadline_at: datetime
    profile_identities: dict[str, str] = Field(default_factory=dict)
    tool_identities: tuple[str, ...] = ()
    artifact_manifest: dict[str, Any] | None = None
    attempt_count: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0)
    binding: AskBinding
    evidence: tuple[EvidenceReference, ...] = ()
    result_artifact: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
