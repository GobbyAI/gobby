"""Deterministic validation for Ask claims, gcode evidence, and reviews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gobby.ask.claims import (
    AnswerDraft,
    AssertionKind,
    ChangedPathSelector,
    Claim,
    ClaimClassification,
    ComparisonKind,
    GitMetadataCitation,
    GitObjectId,
    GraphCitation,
    GraphDirection,
    GraphProvenance,
    GraphRelation,
    ReviewerResult,
    Sha256Digest,
    SourceCitation,
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


class CommitBinding(_FrozenModel):
    parent_oids: tuple[GitObjectId, ...]
    comparison_parent_oid: GitObjectId
    comparison_kind: ComparisonKind
    changed_paths_digest: Sha256Digest
    changed_paths: tuple[ChangedPathSelector, ...]


class SnapshotBinding(_FrozenModel):
    project_id: str
    commit_oid: GitObjectId
    tree_oid: GitObjectId
    inventory_digest: Sha256Digest
    commit: CommitBinding


class InventoryEntry(_FrozenModel):
    path: str
    mode: str
    kind: str
    object_oid: GitObjectId
    blob_oid: GitObjectId | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    content_hash: Sha256Digest | None = None
    language: str | None = None
    exclusion: str | None = None


class SnapshotInventory(_FrozenModel):
    schema_version: Literal[1]
    complete: bool
    digest: Sha256Digest
    entries: tuple[InventoryEntry, ...]


class SourceEvidence(_FrozenModel):
    evidence_id: str
    path: str
    blob_oid: GitObjectId
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
    binding: SnapshotBinding
    contract: ContractIdentity
    items: tuple[EvidenceItem, ...]
    complete: bool
    completeness: str
    bounds: AppliedBounds
    exclusions: tuple[InventoryEntry, ...]
    warnings: tuple[EvidenceWarning, ...]
    continuation: str | None = None


class RecordedEvidence(_FrozenModel):
    run_id: str
    invocation_id: str
    snapshot_inventory_digest: Sha256Digest
    request_hash: Sha256Digest
    response_hash: Sha256Digest
    response: GcodeEvidenceResponse


class EvidenceManifest(_FrozenModel):
    schema_version: Literal[1] = 1
    run_id: str
    snapshot_binding: SnapshotBinding
    inventory: SnapshotInventory
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


class ClaimValidationReport(_FrozenModel):
    draft_hash: Sha256Digest
    evidence_manifest_hash: Sha256Digest
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

    @property
    def is_valid(self) -> bool:
        return not self.diagnostics and all(result.accepted for result in self.results)


class ReviewValidationReport(_FrozenModel):
    draft_hash: Sha256Digest
    evidence_manifest_hash: Sha256Digest
    reviewer_run_id: str
    missing_question_parts: tuple[str, ...]
    results: tuple[ClaimValidationResult, ...]
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    review: ReviewerResult

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

    @property
    def is_valid(self) -> bool:
        return not self.diagnostics


def _rust_json_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _response_body(response: GcodeEvidenceResponse) -> dict[str, Any]:
    """Recreate gcode's field-specific serde omission rules."""
    body = response.model_dump(mode="json", by_alias=True)
    if body["contract"]["hybrid"] is None:
        body["contract"].pop("hybrid")
    if body["bounds"]["graph_depth"] is None:
        body["bounds"].pop("graph_depth")
    if body["continuation"] is None:
        body.pop("continuation")
    for warning in body["warnings"]:
        if warning["path"] is None:
            warning.pop("path")
    for item in body["items"]:
        source = item if item["item_type"] == "source" else item.get("source")
        if source is not None and source["qualified_name"] is None:
            source.pop("qualified_name")
        if item["item_type"] == "commit_metadata" and item["changed_path"] is None:
            item.pop("changed_path")
    return body


def _diagnostic(
    code: str,
    message: str,
    *,
    claim_id: str | None = None,
    evidence_id: str | None = None,
) -> ValidationDiagnostic:
    return ValidationDiagnostic(
        code=code,
        message=message,
        claim_id=claim_id,
        evidence_id=evidence_id,
    )


def _safe_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        bool(path) and not parsed.is_absolute() and ".." not in parsed.parts and str(parsed) == path
    )


def _git_blob_oid(content: bytes, expected: str) -> str:
    algorithm = "sha256" if len(expected) == 64 else "sha1"
    header = b"blob " + str(len(content)).encode() + b"\0"
    return hashlib.new(algorithm, header + content).hexdigest()


def _line_range(content: bytes, start: int, end: int) -> tuple[int, int]:
    start_line = content[:start].count(b"\n") + 1
    end_line = content[: max(start, end - 1)].count(b"\n") + 1
    return start_line, end_line


def _source_identity(binding: SnapshotBinding, source: SourceEvidence) -> str:
    identity = [
        binding.model_dump(mode="json"),
        source.path,
        source.blob_oid,
        source.byte_start,
        source.byte_end,
        source.content_hash,
        source.excerpt_hash,
        source.qualified_name,
    ]
    return f"src:{_rust_json_hash(identity)}"


def _validate_source(
    source: SourceEvidence,
    binding: SnapshotBinding,
    inventory: Mapping[str, InventoryEntry],
    pinned_blobs: Mapping[tuple[str, str], bytes],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    if not _safe_path(source.path):
        diagnostics.append(
            _diagnostic("invalid_evidence_path", "source evidence path is not canonical")
        )
        return diagnostics
    entry = inventory.get(source.path)
    if entry is None or entry.exclusion is not None:
        diagnostics.append(
            _diagnostic("path_not_in_snapshot", "source path is not citeable in the inventory")
        )
        return diagnostics
    if entry.blob_oid != source.blob_oid or entry.content_hash != source.content_hash:
        diagnostics.append(
            _diagnostic("source_inventory_mismatch", "source identity differs from the inventory")
        )
    content = pinned_blobs.get((source.path, source.blob_oid))
    if content is None:
        diagnostics.append(
            _diagnostic("pinned_blob_missing", "the exact cited blob was not supplied")
        )
        return diagnostics
    if _git_blob_oid(content, source.blob_oid) != source.blob_oid:
        diagnostics.append(_diagnostic("blob_hash_mismatch", "pinned blob oid is invalid"))
    if hashlib.sha256(content).hexdigest() != source.content_hash:
        diagnostics.append(_diagnostic("content_hash_mismatch", "pinned source hash is invalid"))
    if entry.size_bytes != len(content):
        diagnostics.append(_diagnostic("source_size_mismatch", "pinned source size is invalid"))
    if not 0 <= source.byte_start < source.byte_end <= len(content):
        diagnostics.append(_diagnostic("stale_source_range", "source byte range is invalid"))
        return diagnostics
    excerpt_bytes = content[source.byte_start : source.byte_end]
    try:
        excerpt = excerpt_bytes.decode("utf-8")
    except UnicodeDecodeError:
        diagnostics.append(_diagnostic("stale_source_range", "source range splits UTF-8"))
        return diagnostics
    if excerpt != source.excerpt:
        diagnostics.append(
            _diagnostic("stale_source_range", "source excerpt does not match the blob")
        )
    if hashlib.sha256(excerpt_bytes).hexdigest() != source.excerpt_hash:
        diagnostics.append(_diagnostic("excerpt_hash_mismatch", "source excerpt hash is invalid"))
    if _line_range(content, source.byte_start, source.byte_end) != (
        source.line_start,
        source.line_end,
    ):
        diagnostics.append(_diagnostic("stale_source_range", "source line range is invalid"))
    if _source_identity(binding, source) != source.evidence_id:
        diagnostics.append(_diagnostic("fabricated_evidence_id", "source evidence id is invalid"))
    return diagnostics


def _changed_path_body(value: ChangedPathSelector) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _validate_git_metadata(
    item: CommitMetadataEvidenceItem,
    binding: SnapshotBinding,
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    commit = binding.commit
    changed_paths = [_changed_path_body(path) for path in commit.changed_paths]
    changed_paths_digest = _rust_json_hash(changed_paths)
    if changed_paths_digest != commit.changed_paths_digest:
        diagnostics.append(
            _diagnostic("changed_paths_hash_mismatch", "binding changed-path digest is invalid")
        )
    expected = (
        binding.commit_oid,
        commit.parent_oids,
        commit.comparison_parent_oid,
        commit.comparison_kind,
        commit.changed_paths_digest,
        len(commit.changed_paths),
    )
    actual = (
        item.commit_oid,
        item.parent_oids,
        item.comparison_parent_oid,
        item.comparison_kind,
        item.changed_paths_digest,
        item.changed_path_count,
    )
    if actual != expected:
        diagnostics.append(
            _diagnostic("git_metadata_mismatch", "commit comparison differs from the snapshot")
        )
    if commit.parent_oids:
        if (
            commit.comparison_kind != "first_parent"
            or commit.comparison_parent_oid != commit.parent_oids[0]
        ):
            diagnostics.append(
                _diagnostic(
                    "git_comparison_mismatch",
                    "commit metadata is not bound to the exact first parent",
                )
            )
    elif commit.comparison_kind != "empty_tree" or not commit.comparison_parent_oid:
        diagnostics.append(
            _diagnostic(
                "git_comparison_mismatch",
                "root commit metadata is not bound to an empty-tree comparison",
            )
        )
    if item.changed_path_count == 0:
        if item.changed_path is not None:
            diagnostics.append(
                _diagnostic("git_metadata_mismatch", "empty comparison includes a changed path")
            )
    elif item.changed_path is None or item.changed_path not in commit.changed_paths:
        diagnostics.append(
            _diagnostic("git_metadata_mismatch", "changed path is not in the canonical comparison")
        )
    record_body = None if item.changed_path is None else _changed_path_body(item.changed_path)
    expected_record_hash = _rust_json_hash(record_body)
    if item.record_hash != expected_record_hash:
        diagnostics.append(
            _diagnostic("git_record_hash_mismatch", "commit metadata record hash is invalid")
        )
    expected_id = "commit:" + _rust_json_hash(
        [
            item.commit_oid,
            list(item.parent_oids),
            item.comparison_parent_oid,
            item.comparison_kind,
            item.changed_paths_digest,
            item.record_hash,
        ]
    )
    if item.evidence_id != expected_id:
        diagnostics.append(
            _diagnostic("fabricated_evidence_id", "commit metadata evidence id is invalid")
        )
    return diagnostics


def _validate_graph(
    item: GraphEvidenceItem,
    binding: SnapshotBinding,
    inventory: Mapping[str, InventoryEntry],
    pinned_blobs: Mapping[tuple[str, str], bytes],
) -> list[ValidationDiagnostic]:
    diagnostics = _validate_source(item.source, binding, inventory, pinned_blobs)
    owner = inventory.get(item.owner.path)
    if (
        not _safe_path(item.owner.path)
        or owner is None
        or owner.content_hash != item.owner.content_hash
        or item.owner.path != item.source.path
        or item.owner.content_hash != item.source.content_hash
    ):
        diagnostics.append(
            _diagnostic("graph_owner_mismatch", "graph fact owner is not its pinned source")
        )
    expected_id = "graph:" + _rust_json_hash(
        [
            binding.model_dump(mode="json"),
            item.source.evidence_id,
            item.relation,
            item.direction,
            item.from_.model_dump(mode="json"),
            item.to.model_dump(mode="json"),
            item.owner.model_dump(mode="json"),
            item.provenance,
        ]
    )
    if item.evidence_id != expected_id:
        diagnostics.append(_diagnostic("fabricated_evidence_id", "graph evidence id is invalid"))
    return diagnostics


def _validate_evidence_manifest(
    manifest: EvidenceManifest,
    pinned_blobs: Mapping[tuple[str, str], bytes],
) -> tuple[
    list[ValidationDiagnostic],
    dict[str, EvidenceItem],
    dict[str, RecordedEvidence],
]:
    diagnostics: list[ValidationDiagnostic] = []
    binding = manifest.snapshot_binding
    entries = [entry.model_dump(mode="json") for entry in manifest.inventory.entries]
    if (
        not manifest.inventory.complete
        or manifest.inventory.digest != binding.inventory_digest
        or _rust_json_hash(entries) != manifest.inventory.digest
    ):
        diagnostics.append(
            _diagnostic("snapshot_inventory_mismatch", "snapshot inventory identity is invalid")
        )
    inventory = {entry.path: entry for entry in manifest.inventory.entries}
    if len(inventory) != len(manifest.inventory.entries):
        diagnostics.append(
            _diagnostic("snapshot_inventory_mismatch", "snapshot inventory paths are duplicated")
        )
    items: dict[str, EvidenceItem] = {}
    item_records: dict[str, RecordedEvidence] = {}
    for record in manifest.records:
        response = record.response
        if record.run_id != manifest.run_id:
            diagnostics.append(_diagnostic("cross_run_evidence", "evidence record has another run"))
        if record.snapshot_inventory_digest != binding.inventory_digest:
            diagnostics.append(
                _diagnostic("snapshot_inventory_mismatch", "evidence record has another snapshot")
            )
        response_body = _response_body(response)
        if canonical_hash(response_body) != record.response_hash:
            diagnostics.append(
                _diagnostic("response_hash_mismatch", "recorded evidence response hash is invalid")
            )
        if canonical_hash(response.request) != record.request_hash or not (
            len(response.request_fingerprint) == 64
            and all(character in "0123456789abcdef" for character in response.request_fingerprint)
        ):
            diagnostics.append(
                _diagnostic("request_hash_mismatch", "gcode request fingerprint is invalid")
            )
        request_binding = response.request.get("binding")
        binding_body = binding.model_dump(mode="json")
        if response.binding != binding or request_binding != binding_body:
            diagnostics.append(
                _diagnostic("snapshot_binding_mismatch", "gcode response has another snapshot")
            )
        if (
            response.contract.name != "gcode-evidence"
            or response.contract.schema_version != 1
            or response.contract.tool != "gobby-code"
        ):
            diagnostics.append(
                _diagnostic(
                    "unsupported_evidence_contract", "gcode contract identity is unsupported"
                )
            )
        if response.bounds.returned_items != len(
            response.items
        ) or response.bounds.total_items < len(response.items):
            diagnostics.append(
                _diagnostic("evidence_bounds_mismatch", "gcode response bounds are inconsistent")
            )
        if response.complete:
            if response.completeness not in {"complete", "complete_empty", "excluded_scope"}:
                diagnostics.append(
                    _diagnostic(
                        "evidence_completeness_mismatch", "complete response is inconsistent"
                    )
                )
        elif response.completeness not in {
            "paginated",
            "truncated_index",
            "truncated_traversal",
        }:
            diagnostics.append(
                _diagnostic("evidence_completeness_mismatch", "partial response is inconsistent")
            )
        for item in response.items:
            if item.evidence_id in items:
                diagnostics.append(
                    _diagnostic(
                        "duplicate_evidence_id",
                        "evidence id occurs more than once",
                        evidence_id=item.evidence_id,
                    )
                )
                continue
            items[item.evidence_id] = item
            item_records[item.evidence_id] = record
            if isinstance(item, SourceEvidenceItem):
                item_diagnostics = _validate_source(item, binding, inventory, pinned_blobs)
            elif isinstance(item, GraphEvidenceItem):
                item_diagnostics = _validate_graph(item, binding, inventory, pinned_blobs)
            else:
                item_diagnostics = _validate_git_metadata(item, binding)
            diagnostics.extend(
                diagnostic.model_copy(update={"evidence_id": item.evidence_id})
                for diagnostic in item_diagnostics
            )
    return diagnostics, items, item_records


def _claim_shape_diagnostics(
    claim: Claim,
    known_claim_ids: set[str],
    known_part_ids: set[str],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    if not claim.id.strip() or not claim.statement.strip():
        diagnostics.append(_diagnostic("invalid_claim_schema", "claim text is empty"))
    if "mermaid" in claim.statement.casefold():
        diagnostics.append(
            _diagnostic("untyped_mermaid", "claim text cannot supply an untyped Mermaid diagram")
        )
    if claim.classification in {ClaimClassification.DIRECT, ClaimClassification.INFERRED}:
        if not claim.citations:
            diagnostics.append(_diagnostic("missing_citation", "supported claim has no citation"))
    elif claim.citations or claim.premise_claim_ids or not (claim.rationale or "").strip():
        diagnostics.append(
            _diagnostic("invalid_unknown_claim", "unknown claim must state only evidence limits")
        )
    if claim.classification is ClaimClassification.INFERRED and not (claim.rationale or "").strip():
        diagnostics.append(_diagnostic("missing_rationale", "inferred claim lacks rationale"))
    if set(claim.premise_claim_ids) - known_claim_ids or claim.id in claim.premise_claim_ids:
        diagnostics.append(
            _diagnostic("invalid_premise_reference", "claim premise reference is invalid")
        )
    if len(set(claim.premise_claim_ids)) != len(claim.premise_claim_ids):
        diagnostics.append(
            _diagnostic("invalid_premise_reference", "claim premises are duplicated")
        )
    if set(claim.question_part_ids) - known_part_ids:
        diagnostics.append(
            _diagnostic("invalid_question_part", "claim question coverage is invalid")
        )
    if claim.assertion_kind in {AssertionKind.NEGATIVE, AssertionKind.EXHAUSTIVE} and (
        claim.evidence_scope is None
    ):
        diagnostics.append(
            _diagnostic("missing_evidence_scope", "negative or exhaustive claim lacks scope")
        )
    if claim.classification is ClaimClassification.DIRECT and any(
        isinstance(citation, GraphCitation) for citation in claim.citations
    ):
        diagnostics.append(
            _diagnostic(
                "unsupported_direct_evidence",
                "graph associations require an inferred claim and rationale",
            )
        )
    return [item.model_copy(update={"claim_id": claim.id}) for item in diagnostics]


def _premise_cycle_claims(claims: tuple[Claim, ...]) -> set[str]:
    graph = {claim.id: claim.premise_claim_ids for claim in claims}
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()
    cyclic: set[str] = set()

    def visit(claim_id: str) -> None:
        if claim_id in active_set:
            cyclic.update(active[active.index(claim_id) :])
            return
        if claim_id in visited:
            return
        visited.add(claim_id)
        active.append(claim_id)
        active_set.add(claim_id)
        for premise_id in graph.get(claim_id, ()):
            if premise_id in graph:
                visit(premise_id)
        active.pop()
        active_set.remove(claim_id)

    for claim_id in graph:
        visit(claim_id)
    return cyclic


def _citation_diagnostics(
    claim: Claim,
    citation: SourceCitation | GraphCitation | GitMetadataCitation,
    manifest: EvidenceManifest,
    items: Mapping[str, EvidenceItem],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    if citation.run_id != manifest.run_id:
        diagnostics.append(_diagnostic("cross_run_citation", "citation belongs to another Ask run"))
    item = items.get(citation.evidence_id)
    if item is None:
        diagnostics.append(
            _diagnostic("fabricated_citation", "citation evidence id was not recorded")
        )
        return diagnostics
    if isinstance(citation, SourceCitation):
        if not isinstance(item, SourceEvidenceItem):
            diagnostics.append(_diagnostic("citation_type_mismatch", "citation type is wrong"))
        else:
            source_selector = (
                citation.path,
                citation.blob_oid,
                citation.content_hash,
                citation.excerpt_hash,
                citation.qualified_name,
                citation.line_start,
                citation.line_end,
                citation.byte_start,
                citation.byte_end,
            )
            recorded_source = (
                item.path,
                item.blob_oid,
                item.content_hash,
                item.excerpt_hash,
                item.qualified_name,
                item.line_start,
                item.line_end,
                item.byte_start,
                item.byte_end,
            )
            if source_selector != recorded_source:
                diagnostics.append(
                    _diagnostic(
                        "citation_selector_mismatch", "source citation range or hash is stale"
                    )
                )
    elif isinstance(citation, GraphCitation):
        if not isinstance(item, GraphEvidenceItem):
            diagnostics.append(_diagnostic("citation_type_mismatch", "citation type is wrong"))
        else:
            graph_selector = (
                citation.source_evidence_id,
                citation.relation,
                citation.direction,
                citation.from_id,
                citation.to_id,
                citation.owner_path,
                citation.owner_content_hash,
                citation.provenance,
            )
            recorded_graph = (
                item.source.evidence_id,
                item.relation,
                item.direction,
                item.from_.id,
                item.to.id,
                item.owner.path,
                item.owner.content_hash,
                item.provenance,
            )
            if graph_selector != recorded_graph:
                diagnostics.append(
                    _diagnostic("graph_selector_mismatch", "graph citation selector is stale")
                )
    elif not isinstance(item, CommitMetadataEvidenceItem):
        diagnostics.append(_diagnostic("citation_type_mismatch", "citation type is wrong"))
    else:
        git_selector = citation.model_dump(mode="json", exclude={"citation_type", "run_id"})
        recorded_git = item.model_dump(mode="json", exclude={"item_type"})
        if git_selector != recorded_git:
            diagnostics.append(
                _diagnostic("git_metadata_mismatch", "Git metadata citation selector is stale")
            )
    return [
        item.model_copy(update={"claim_id": claim.id, "evidence_id": citation.evidence_id})
        for item in diagnostics
    ]


def validate_claims(
    draft: AnswerDraft,
    evidence: EvidenceManifest,
    *,
    pinned_blobs: Mapping[tuple[str, str], bytes],
) -> ClaimValidationReport:
    """Validate an immutable draft against recorded gcode evidence and pinned blobs."""
    global_diagnostics, items, item_records = _validate_evidence_manifest(evidence, pinned_blobs)
    invocation_records = {record.invocation_id: record for record in evidence.records}
    if draft.run_id != evidence.run_id:
        global_diagnostics.append(
            _diagnostic("cross_run_draft", "draft and evidence belong to different runs")
        )
    claim_ids = [claim.id for claim in draft.claims]
    part_ids = [part.id for part in draft.question_parts]
    if len(set(claim_ids)) != len(claim_ids):
        global_diagnostics.append(_diagnostic("duplicate_claim_id", "claim ids are duplicated"))
    if len(set(part_ids)) != len(part_ids):
        global_diagnostics.append(
            _diagnostic("duplicate_question_part", "question part ids are duplicated")
        )
    ordered = [claim_id for section in draft.sections for claim_id in section.claim_ids]
    if len(ordered) != len(set(ordered)) or set(ordered) != set(claim_ids):
        global_diagnostics.append(
            _diagnostic("invalid_section_graph", "sections must order every claim exactly once")
        )
    cyclic = _premise_cycle_claims(draft.claims)
    results: list[ClaimValidationResult] = []
    for claim in draft.claims:
        diagnostics = _claim_shape_diagnostics(claim, set(claim_ids), set(part_ids))
        if claim.id in cyclic:
            diagnostics.append(
                _diagnostic(
                    "premise_cycle",
                    "claim participates in a premise cycle",
                    claim_id=claim.id,
                )
            )
        for citation in claim.citations:
            diagnostics.extend(_citation_diagnostics(claim, citation, evidence, items))
        if claim.evidence_scope is not None:
            missing = set(claim.evidence_scope.evidence_ids) - set(items)
            missing_invocations = set(claim.evidence_scope.invocation_ids) - set(invocation_records)
            if missing or missing_invocations:
                diagnostics.append(
                    _diagnostic(
                        "invalid_evidence_scope",
                        "claim scope references unrecorded evidence",
                        claim_id=claim.id,
                    )
                )
            elif claim.assertion_kind in {AssertionKind.NEGATIVE, AssertionKind.EXHAUSTIVE}:
                scoped_records = [
                    item_records[evidence_id] for evidence_id in claim.evidence_scope.evidence_ids
                ] + [
                    invocation_records[invocation_id]
                    for invocation_id in claim.evidence_scope.invocation_ids
                ]
                if any(
                    not record.response.complete
                    or record.response.completeness not in {"complete", "complete_empty"}
                    or bool(record.response.exclusions)
                    for record in scoped_records
                ):
                    diagnostics.append(
                        _diagnostic(
                            "incomplete_exhaustive_scope",
                            "negative or exhaustive claim relies on incomplete evidence",
                            claim_id=claim.id,
                        )
                    )
        results.append(
            ClaimValidationResult(
                claim_id=claim.id,
                accepted=not global_diagnostics and not diagnostics,
                diagnostics=tuple(diagnostics),
            )
        )
    return ClaimValidationReport(
        draft_hash=draft.content_hash,
        evidence_manifest_hash=evidence.content_hash,
        results=tuple(results),
        diagnostics=tuple(global_diagnostics),
    )


def validate_review(
    draft: AnswerDraft,
    evidence: EvidenceManifest,
    deterministic: ClaimValidationReport,
    review: ReviewerResult,
) -> ReviewValidationReport:
    """Validate review identity and derive reviewer-accepted premise closure."""
    diagnostics: list[ValidationDiagnostic] = []
    if (
        review.run_id != draft.run_id
        or review.draft_hash != draft.content_hash
        or review.evidence_manifest_hash != evidence.content_hash
        or deterministic.draft_hash != draft.content_hash
        or deterministic.evidence_manifest_hash != evidence.content_hash
    ):
        diagnostics.append(
            _diagnostic("stale_review", "review does not bind this draft and evidence")
        )
    if review.reviewer_run_id == draft.investigator_run_id:
        diagnostics.append(
            _diagnostic("reviewer_not_independent", "investigator cannot review its own draft")
        )
    claim_ids = {claim.id for claim in draft.claims}
    verdicts = {verdict.claim_id: verdict for verdict in review.claim_verdicts}
    if set(verdicts) != claim_ids:
        diagnostics.append(
            _diagnostic("incomplete_review", "review must include exactly one verdict per claim")
        )
    part_ids = {part.id for part in draft.question_parts}
    if set(review.missing_question_parts) - part_ids:
        diagnostics.append(
            _diagnostic("invalid_question_coverage", "review names an unknown question part")
        )
    deterministic_ids = set(deterministic.accepted_claim_ids)
    candidates: set[str] = set()
    result_diagnostics: dict[str, list[ValidationDiagnostic]] = {}
    for claim in draft.claims:
        claim_diagnostics: list[ValidationDiagnostic] = []
        verdict = verdicts.get(claim.id)
        if claim.id not in deterministic_ids:
            claim_diagnostics.append(
                _diagnostic(
                    "deterministic_validation_failed",
                    "claim failed deterministic validation",
                    claim_id=claim.id,
                )
            )
        elif verdict is None:
            claim_diagnostics.append(
                _diagnostic("missing_claim_review", "claim has no review", claim_id=claim.id)
            )
        elif not verdict.accepted:
            claim_diagnostics.append(
                _diagnostic("review_rejected", verdict.rationale, claim_id=claim.id)
            )
        elif not verdict.classification_supported or verdict.support_diagnostics:
            claim_diagnostics.append(
                _diagnostic(
                    "review_support_failed",
                    "review diagnostics do not support this classification",
                    claim_id=claim.id,
                )
            )
        else:
            candidates.add(claim.id)
        result_diagnostics[claim.id] = claim_diagnostics

    claim_map = {claim.id: claim for claim in draft.claims}
    changed = True
    while changed:
        changed = False
        for claim_id in tuple(candidates):
            claim = claim_map[claim_id]
            if claim.classification is ClaimClassification.INFERRED and any(
                premise_id not in candidates for premise_id in claim.premise_claim_ids
            ):
                candidates.remove(claim_id)
                result_diagnostics[claim_id].append(
                    _diagnostic(
                        "unaccepted_premise",
                        "inferred claim lacks reviewer-accepted premise closure",
                        claim_id=claim_id,
                    )
                )
                changed = True
    reviewed_parts = {
        part_id for claim_id in candidates for part_id in claim_map[claim_id].question_part_ids
    }
    if set(review.missing_question_parts) != part_ids - reviewed_parts:
        diagnostics.append(
            _diagnostic(
                "invalid_question_coverage",
                "review missing-part diagnostics do not match accepted claim coverage",
            )
        )
    if diagnostics:
        candidates.clear()
    results = tuple(
        ClaimValidationResult(
            claim_id=claim.id,
            accepted=claim.id in candidates,
            diagnostics=tuple(result_diagnostics[claim.id]),
        )
        for claim in draft.claims
    )
    return ReviewValidationReport(
        draft_hash=draft.content_hash,
        evidence_manifest_hash=evidence.content_hash,
        reviewer_run_id=review.reviewer_run_id,
        missing_question_parts=review.missing_question_parts,
        results=results,
        diagnostics=tuple(diagnostics),
        review=review,
    )
