"""Typed evidence manifests and deterministic claim/review validation reports."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gobby.ask.claims import (
    ChangedPathSelector,
    ComparisonKind,
    GitObjectId,
    GraphDirection,
    GraphProvenance,
    GraphRelation,
    ReviewerResult,
    Sha256Digest,
    canonical_hash,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )


class RepositoryBinding(_FrozenModel):
    project_id: str
    commit_oid: GitObjectId
    tree_oid: GitObjectId


class SourceEvidence(_FrozenModel):
    evidence_id: str
    path: str
    content_hash: Sha256Digest
    excerpt_hash: Sha256Digest
    qualified_name: str | None = None
    line_start: int = Field(gt=0)
    line_end: int = Field(gt=0)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    excerpt: str


class SourceEvidenceItem(SourceEvidence):
    item_type: Literal["source"]


class GraphEndpoint(_FrozenModel):
    id: str
    name: str
    kind: str
    path: str


class GraphOwner(_FrozenModel):
    path: str
    content_hash: Sha256Digest


class GraphEvidenceItem(_FrozenModel):
    item_type: Literal["graph"]
    evidence_id: str
    source: SourceEvidence
    relation: GraphRelation
    direction: GraphDirection
    from_: GraphEndpoint = Field(alias="from", serialization_alias="from")
    to: GraphEndpoint
    owner: GraphOwner
    provenance: GraphProvenance


class CommitMetadataEvidenceItem(_FrozenModel):
    item_type: Literal["commit_metadata"]
    evidence_id: str
    commit_oid: GitObjectId
    parent_oids: tuple[GitObjectId, ...]
    comparison_parent_oid: GitObjectId
    comparison_kind: ComparisonKind
    changed_paths_digest: Sha256Digest
    changed_path_count: int = Field(ge=0)
    changed_path: ChangedPathSelector | None = None
    record_hash: Sha256Digest


EvidenceItem = Annotated[
    SourceEvidenceItem | GraphEvidenceItem | CommitMetadataEvidenceItem,
    Field(discriminator="item_type"),
]


class ContractIdentity(_FrozenModel):
    name: str
    schema_version: int
    tool: str
    tool_version: str
    lane: str
    hybrid: dict[str, Any] | None = None


class AppliedBounds(_FrozenModel):
    max_bytes: int = Field(gt=0)
    serialized_item_bytes: int = Field(ge=0)
    returned_items: int = Field(ge=0)
    total_items: int = Field(ge=0)
    result_limit: int = Field(ge=0)
    graph_depth: int | None = Field(default=None, ge=0)


class EvidenceWarning(_FrozenModel):
    code: str
    message: str
    path: str | None = None


class GcodeEvidenceResponse(_FrozenModel):
    request: dict[str, Any]
    request_fingerprint: str
    binding: RepositoryBinding
    contract: ContractIdentity
    items: tuple[EvidenceItem, ...]
    complete: bool
    completeness: str
    bounds: AppliedBounds
    warnings: tuple[EvidenceWarning, ...]
    continuation: str | None = None


class RecordedEvidence(_FrozenModel):
    run_id: str
    invocation_id: str
    binding_digest: Sha256Digest
    request_hash: Sha256Digest
    response_hash: Sha256Digest
    response: GcodeEvidenceResponse


class EvidenceManifest(_FrozenModel):
    schema_version: Literal[1] = 1
    run_id: str
    project_id: str
    repository_binding: RepositoryBinding
    records: tuple[RecordedEvidence, ...]

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json", by_alias=True))


class ValidationDiagnostic(_FrozenModel):
    code: str
    message: str
    claim_id: str | None = None
    evidence_id: str | None = None


class ClaimValidationResult(_FrozenModel):
    claim_id: str
    accepted: bool
    diagnostics: tuple[ValidationDiagnostic, ...] = ()


class _ValidationReport(_FrozenModel):
    results: tuple[ClaimValidationResult, ...]
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    @property
    def accepted_claim_ids(self) -> tuple[str, ...]:
        return tuple(result.claim_id for result in self.results if result.accepted)

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(
            diagnostic.code
            for diagnostic in self.diagnostics
            + tuple(item for result in self.results for item in result.diagnostics)
        )


class ClaimValidationReport(_ValidationReport):
    draft_hash: Sha256Digest
    evidence_manifest_hash: Sha256Digest

    @property
    def is_valid(self) -> bool:
        return not self.diagnostics and all(result.accepted for result in self.results)


class ReviewValidationReport(_ValidationReport):
    draft_hash: Sha256Digest
    evidence_manifest_hash: Sha256Digest
    reviewer_run_id: str
    missing_question_parts: tuple[str, ...]
    review: ReviewerResult

    @property
    def is_valid(self) -> bool:
        return not self.diagnostics
