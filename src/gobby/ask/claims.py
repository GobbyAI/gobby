"""Typed immutable claim, citation, and independent-review contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GitObjectId = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ChangeStatus = Literal["added", "copied", "deleted", "modified", "renamed", "type_changed"]
ComparisonKind = Literal["first_parent", "empty_tree"]
GraphDirection = Literal["incoming", "outgoing", "both"]
GraphProvenance = Literal["extracted", "inferred", "unresolved"]
GraphRelation = Literal["call", "import", "inheritance", "usage"]


def canonical_json(value: object) -> bytes:
    """Encode a portable deterministic JSON value."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class ClaimClassification(StrEnum):
    DIRECT = "direct"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class AssertionKind(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    EXHAUSTIVE = "exhaustive"


class ChangedPathSelector(BaseModel):
    """Exact gcode commit-metadata changed-path record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ChangeStatus
    similarity: int | None = Field(default=None, ge=0, le=100)
    old_path: str | None = None
    new_path: str | None = None
    old_exclusion: str | None = None
    new_exclusion: str | None = None
    old_mode: str | None = None
    new_mode: str | None = None
    old_blob_oid: GitObjectId | None = None
    new_blob_oid: GitObjectId | None = None


class SourceCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    citation_type: Literal["source"] = "source"
    run_id: str
    evidence_id: str
    path: str
    content_hash: Sha256Digest
    excerpt_hash: Sha256Digest
    qualified_name: str | None = None
    line_start: int = Field(gt=0)
    line_end: int = Field(gt=0)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(gt=0)


class GraphCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    citation_type: Literal["graph"] = "graph"
    run_id: str
    evidence_id: str
    source_evidence_id: str
    relation: GraphRelation
    direction: GraphDirection
    from_id: str
    to_id: str
    owner_path: str
    owner_content_hash: Sha256Digest
    provenance: GraphProvenance


class GitMetadataCitation(BaseModel):
    """Commit metadata selector; source line ranges are deliberately impossible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    citation_type: Literal["git_metadata"] = "git_metadata"
    run_id: str
    evidence_id: str
    commit_oid: GitObjectId
    parent_oids: tuple[GitObjectId, ...]
    comparison_parent_oid: GitObjectId
    comparison_kind: ComparisonKind
    changed_paths_digest: Sha256Digest
    changed_path_count: int = Field(ge=0)
    changed_path: ChangedPathSelector | None = None
    record_hash: Sha256Digest


Citation = Annotated[
    SourceCitation | GraphCitation | GitMetadataCitation,
    Field(discriminator="citation_type"),
]


class EvidenceScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    evidence_ids: tuple[str, ...] = ()
    invocation_ids: tuple[str, ...] = ()

    @field_validator("description")
    @classmethod
    def _description_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence scope description must not be empty")
        return value

    @model_validator(mode="after")
    def _has_recorded_scope(self) -> EvidenceScope:
        if not self.evidence_ids and not self.invocation_ids:
            raise ValueError("evidence scope requires evidence or invocation ids")
        return self


class QuestionPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    classification: ClaimClassification
    statement: str
    citations: tuple[Citation, ...] = ()
    premise_claim_ids: tuple[str, ...] = ()
    rationale: str | None = None
    question_part_ids: tuple[str, ...] = ()
    assertion_kind: AssertionKind = AssertionKind.POSITIVE
    evidence_scope: EvidenceScope | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> Claim:
        if not self.id.strip() or not self.statement.strip():
            raise ValueError("claim id and statement must not be empty")
        if self.classification in {ClaimClassification.DIRECT, ClaimClassification.INFERRED}:
            if not self.citations:
                raise ValueError("direct and inferred claims require citations")
        elif self.citations or self.premise_claim_ids:
            raise ValueError("unknown claims cannot assert citations or premises")
        if (
            self.classification is ClaimClassification.INFERRED
            and not (self.rationale or "").strip()
        ):
            raise ValueError("inferred claims require rationale")
        if (
            self.classification is ClaimClassification.UNKNOWN
            and not (self.rationale or "").strip()
        ):
            raise ValueError("unknown claims require explicit evidence limits")
        if self.assertion_kind in {AssertionKind.NEGATIVE, AssertionKind.EXHAUSTIVE}:
            if self.evidence_scope is None:
                raise ValueError(
                    "negative and exhaustive claims require an explicit evidence scope"
                )
        return self


class AnswerSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    claim_ids: tuple[str, ...]


class AnswerDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str
    investigator_run_id: str
    question: str
    question_parts: tuple[QuestionPart, ...]
    claims: tuple[Claim, ...]
    sections: tuple[AnswerSection, ...]

    @model_validator(mode="after")
    def _validate_references(self) -> AnswerDraft:
        claim_ids = [claim.id for claim in self.claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("claim ids must be unique")
        part_ids = [part.id for part in self.question_parts]
        if len(set(part_ids)) != len(part_ids):
            raise ValueError("question part ids must be unique")
        section_ids = [section.id for section in self.sections]
        if len(set(section_ids)) != len(section_ids):
            raise ValueError("section ids must be unique")
        known_claims = set(claim_ids)
        ordered = [claim_id for section in self.sections for claim_id in section.claim_ids]
        if len(ordered) != len(set(ordered)) or set(ordered) != known_claims:
            raise ValueError("sections must reference every claim exactly once")
        known_parts = set(part_ids)
        if any(set(claim.question_part_ids) - known_parts for claim in self.claims):
            raise ValueError("claim references an unknown question part")
        return self

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class ReviewClaimVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    accepted: bool
    classification_supported: bool
    support_diagnostics: tuple[str, ...] = ()
    rationale: str

    @field_validator("rationale")
    @classmethod
    def _rationale_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review rationale must not be empty")
        return value


class ReviewerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str
    reviewer_run_id: str
    draft_hash: Sha256Digest
    evidence_manifest_hash: Sha256Digest
    claim_verdicts: tuple[ReviewClaimVerdict, ...]
    missing_question_parts: tuple[str, ...] = ()
    rationale: str

    @model_validator(mode="after")
    def _unique_verdicts(self) -> ReviewerResult:
        claim_ids = [verdict.claim_id for verdict in self.claim_verdicts]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("review claim verdict ids must be unique")
        if not self.rationale.strip():
            raise ValueError("review rationale must not be empty")
        return self
