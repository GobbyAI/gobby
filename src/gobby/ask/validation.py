"""Deterministic validation for Ask claims, gcode evidence, and reviews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from gobby.ask.claims import (
    AnswerDraft,
    AssertionKind,
    Claim,
    ClaimClassification,
    GitMetadataCitation,
    GraphCitation,
    ReviewerResult,
    SourceCitation,
    canonical_hash,
)
from gobby.ask.validation_models import (
    ClaimValidationReport,
    ClaimValidationResult,
    CommitMetadataEvidenceItem,
    EvidenceItem,
    EvidenceManifest,
    GcodeEvidenceResponse,
    GraphEvidenceItem,
    RecordedEvidence,
    RepositoryBinding,
    ReviewValidationReport,
    SourceEvidence,
    SourceEvidenceItem,
    ValidationDiagnostic,
)


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


def _line_range(content: bytes, start: int, end: int) -> tuple[int, int]:
    start_line = content[:start].count(b"\n") + 1
    end_line = content[: max(start, end - 1)].count(b"\n") + 1
    return start_line, end_line


def _source_identity(binding: RepositoryBinding, source: SourceEvidence) -> str:
    identity = [
        binding.model_dump(mode="json"),
        source.path,
        source.byte_start,
        source.byte_end,
        source.content_hash,
        source.excerpt_hash,
        source.qualified_name,
    ]
    return f"src:{_rust_json_hash(identity)}"


def _validate_source(
    source: SourceEvidence,
    binding: RepositoryBinding,
    pinned_blobs: Mapping[tuple[str, str], bytes],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    if not _safe_path(source.path):
        diagnostics.append(
            _diagnostic("invalid_evidence_path", "source evidence path is not canonical")
        )
        return diagnostics
    content = pinned_blobs.get((source.path, source.content_hash))
    if content is None:
        diagnostics.append(
            _diagnostic("source_content_missing", "the exact indexed source was not supplied")
        )
        return diagnostics
    if hashlib.sha256(content).hexdigest() != source.content_hash:
        diagnostics.append(_diagnostic("content_hash_mismatch", "pinned source hash is invalid"))
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


def _validate_git_metadata(
    item: CommitMetadataEvidenceItem,
    response: GcodeEvidenceResponse,
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    selector = response.request.get("read", {})
    requested_commit = selector.get("commit_oid") if isinstance(selector, dict) else None
    expected_commit = requested_commit or response.binding.commit_oid
    if item.commit_oid != expected_commit:
        diagnostics.append(
            _diagnostic("git_metadata_mismatch", "commit differs from the evidence request")
        )
    if item.parent_oids:
        if (
            item.comparison_kind != "first_parent"
            or item.comparison_parent_oid != item.parent_oids[0]
        ):
            diagnostics.append(
                _diagnostic(
                    "git_comparison_mismatch",
                    "commit metadata is not bound to the exact first parent",
                )
            )
    elif item.comparison_kind != "empty_tree" or not item.comparison_parent_oid:
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
    elif item.changed_path is None:
        diagnostics.append(
            _diagnostic("git_metadata_mismatch", "nonempty comparison is missing a changed path")
        )
    record_body = None if item.changed_path is None else item.changed_path.model_dump(mode="json")
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
    binding: RepositoryBinding,
    pinned_blobs: Mapping[tuple[str, str], bytes],
) -> list[ValidationDiagnostic]:
    diagnostics = _validate_source(item.source, binding, pinned_blobs)
    if (
        not _safe_path(item.owner.path)
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


def _record_supports_exhaustive_scope(record: RecordedEvidence) -> bool:
    response = record.response
    return response.complete and (response.completeness in {"complete", "complete_empty"})


def _validate_evidence_manifest(
    manifest: EvidenceManifest,
    pinned_blobs: Mapping[tuple[str, str], bytes],
    referenced_ids: set[str],
) -> tuple[list[ValidationDiagnostic], dict[str, EvidenceItem], dict[str, list[RecordedEvidence]]]:
    diagnostics: list[ValidationDiagnostic] = []
    add = diagnostics.append
    binding = manifest.repository_binding
    items: dict[str, EvidenceItem] = {}
    item_records: dict[str, list[RecordedEvidence]] = {}
    invocation_ids: set[str] = set()
    for record in manifest.records:
        response = record.response
        if record.invocation_id in invocation_ids:
            add(_diagnostic("duplicate_invocation_id", "invocation id occurs more than once"))
        invocation_ids.add(record.invocation_id)
        if record.run_id != manifest.run_id:
            diagnostics.append(_diagnostic("cross_run_evidence", "evidence record has another run"))
        if record.binding_digest != canonical_hash(binding.model_dump(mode="json")):
            add(
                _diagnostic(
                    "repository_binding_mismatch", "evidence has another repository binding"
                )
            )
        response_body = _response_body(response)
        if canonical_hash(response_body) != record.response_hash:
            add(_diagnostic("response_hash_mismatch", "evidence response hash is invalid"))
        if canonical_hash(response.request) != record.request_hash or not (
            len(response.request_fingerprint) == 64
            and all(character in "0123456789abcdef" for character in response.request_fingerprint)
        ):
            add(_diagnostic("request_hash_mismatch", "gcode request fingerprint is invalid"))
        request_binding = response.request.get("binding")
        binding_body = binding.model_dump(mode="json")
        if response.binding != binding or request_binding != binding_body:
            add(
                _diagnostic(
                    "repository_binding_mismatch", "gcode response has another repository binding"
                )
            )
        if (
            response.contract.name != "gcode-evidence"
            or response.contract.schema_version != 1
            or response.contract.tool != "gobby-code"
        ):
            add(
                _diagnostic(
                    "unsupported_evidence_contract", "gcode contract identity is unsupported"
                )
            )
        if response.bounds.returned_items != len(
            response.items
        ) or response.bounds.total_items < len(response.items):
            add(_diagnostic("evidence_bounds_mismatch", "gcode response bounds are inconsistent"))
        if response.complete:
            if response.completeness not in {"complete", "complete_empty", "excluded_scope"}:
                add(
                    _diagnostic(
                        "evidence_completeness_mismatch", "complete response is inconsistent"
                    )
                )
        elif response.completeness not in {
            "paginated",
            "truncated_index",
            "truncated_traversal",
        }:
            add(_diagnostic("evidence_completeness_mismatch", "partial response is inconsistent"))
        record_item_ids: set[str] = set()
        for item in response.items:
            prior = items.get(item.evidence_id)
            if item.evidence_id in record_item_ids:
                diagnostics.append(
                    _diagnostic(
                        "duplicate_evidence_id",
                        "evidence id occurs more than once in one response",
                        evidence_id=item.evidence_id,
                    )
                )
                continue
            record_item_ids.add(item.evidence_id)
            if prior is not None:
                if prior != item:
                    diagnostics.append(
                        _diagnostic(
                            "conflicting_evidence_id",
                            "one evidence id identifies conflicting canonical content",
                            evidence_id=item.evidence_id,
                        )
                    )
                else:
                    item_records[item.evidence_id].append(record)
                continue
            items[item.evidence_id] = item
            item_records[item.evidence_id] = [record]
            if item.evidence_id not in referenced_ids and isinstance(
                item, (SourceEvidenceItem, GraphEvidenceItem)
            ):
                # The immutable response remains checked above. A past, unused
                # retrieval does not require its working-tree file to stay fresh.
                continue
            if isinstance(item, SourceEvidenceItem):
                item_diagnostics = _validate_source(item, binding, pinned_blobs)
            elif isinstance(item, GraphEvidenceItem):
                item_diagnostics = _validate_graph(item, binding, pinned_blobs)
            else:
                item_diagnostics = _validate_git_metadata(item, response)
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


def referenced_evidence_ids(draft: AnswerDraft, evidence: EvidenceManifest) -> set[str]:
    """Include citations and every item in an explicitly claimed query scope."""
    evidence_ids = {citation.evidence_id for claim in draft.claims for citation in claim.citations}
    invocation_ids: set[str] = set()
    for claim in draft.claims:
        if claim.evidence_scope is not None:
            evidence_ids.update(claim.evidence_scope.evidence_ids)
            invocation_ids.update(claim.evidence_scope.invocation_ids)
    for record in evidence.records:
        if record.invocation_id in invocation_ids:
            evidence_ids.update(item.evidence_id for item in record.response.items)
    return evidence_ids


def validate_claims(
    draft: AnswerDraft,
    evidence: EvidenceManifest,
    *,
    pinned_blobs: Mapping[tuple[str, str], bytes],
) -> ClaimValidationReport:
    """Validate an immutable draft against recorded gcode evidence and pinned blobs."""
    global_diagnostics, items, item_records = _validate_evidence_manifest(
        evidence, pinned_blobs, referenced_evidence_ids(draft, evidence)
    )
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
                evidence_record_groups = [
                    item_records[evidence_id] for evidence_id in claim.evidence_scope.evidence_ids
                ]
                scoped_invocations = [
                    invocation_records[invocation_id]
                    for invocation_id in claim.evidence_scope.invocation_ids
                ]
                if any(
                    not any(map(_record_supports_exhaustive_scope, records))
                    for records in evidence_record_groups
                ) or any(
                    not _record_supports_exhaustive_scope(record) for record in scoped_invocations
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
