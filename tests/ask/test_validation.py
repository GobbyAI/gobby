from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _json_hash(value: object, *, sort_keys: bool = True) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=sort_keys,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _blob_oid(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def _valid_case(
    *, run_id: str = "run-1", project_id: str = "project"
) -> tuple[Any, Any, dict[tuple[str, str], bytes], Any]:
    from gobby.ask.claims import (
        AnswerDraft,
        AnswerSection,
        Claim,
        ClaimClassification,
        QuestionPart,
        ReviewClaimVerdict,
        ReviewerResult,
        SourceCitation,
    )
    from gobby.ask.validation_models import EvidenceManifest

    content = b"def alpha():\n    return 1\n"
    excerpt = b"    return 1\n"
    content_hash = hashlib.sha256(content).hexdigest()
    excerpt_hash = hashlib.sha256(excerpt).hexdigest()
    binding = {"project_id": project_id, "commit_oid": "a" * 40, "tree_oid": "d" * 40}
    source_identity = [
        binding,
        "src/app.py",
        len(b"def alpha():\n"),
        len(content),
        content_hash,
        excerpt_hash,
        None,
    ]
    source_id = f"src:{_json_hash(source_identity, sort_keys=False)}"
    source_item = {
        "item_type": "source",
        "evidence_id": source_id,
        "path": "src/app.py",
        "content_hash": content_hash,
        "excerpt_hash": excerpt_hash,
        "line_start": 2,
        "line_end": 2,
        "byte_start": len(b"def alpha():\n"),
        "byte_end": len(content),
        "excerpt": excerpt.decode(),
        "numbered_excerpt": "2|     return 1\n",
    }
    request = {
        "schema_version": 1,
        "binding": binding,
        "operation": "read",
        "read": {
            "kind": "range",
            "path": "src/app.py",
            "start_line": 2,
            "end_line": 2,
        },
        "max_bytes": 262_144,
    }
    response = {
        "request": request,
        "request_fingerprint": _json_hash(request, sort_keys=False),
        "binding": binding,
        "contract": {
            "name": "gcode-evidence",
            "schema_version": 1,
            "tool": "gobby-code",
            "tool_version": "0.5.0",
            "lane": "read",
        },
        "items": [source_item],
        "complete": True,
        "completeness": "complete",
        "bounds": {
            "max_bytes": 262_144,
            "serialized_item_bytes": len(json.dumps(source_item, separators=(",", ":")).encode()),
            "returned_items": 1,
            "total_items": 1,
            "result_limit": 1,
        },
        "warnings": [],
    }
    evidence = EvidenceManifest.model_validate(
        {
            "run_id": run_id,
            "repository_binding": binding,
            "project_id": project_id,
            "records": [
                {
                    "run_id": run_id,
                    "invocation_id": "invocation-1",
                    "binding_digest": _json_hash(binding),
                    "request_hash": _json_hash(request),
                    "response_hash": _json_hash(response),
                    "response": response,
                }
            ],
        }
    )
    citation = SourceCitation(
        run_id=run_id,
        evidence_id=source_id,
        path="src/app.py",
        content_hash=content_hash,
        excerpt_hash=excerpt_hash,
        line_start=2,
        line_end=2,
        byte_start=len(b"def alpha():\n"),
        byte_end=len(content),
    )
    claims = (
        Claim(
            id="claim-return",
            classification=ClaimClassification.DIRECT,
            statement="alpha returns 1.",
            citations=(citation,),
            question_part_ids=("behavior",),
        ),
        Claim(
            id="claim-stable",
            classification=ClaimClassification.INFERRED,
            statement="The return value is fixed by the pinned implementation.",
            citations=(citation,),
            premise_claim_ids=("claim-return",),
            question_part_ids=("behavior",),
            rationale="The pinned function returns the same literal value.",
        ),
    )
    draft = AnswerDraft(
        run_id=run_id,
        investigator_run_id="investigator-1",
        question="What does alpha return?",
        question_parts=(QuestionPart(id="behavior", text="Return behavior"),),
        claims=claims,
        sections=(
            AnswerSection(
                id="answer",
                title="Answer",
                claim_ids=("claim-return", "claim-stable"),
            ),
        ),
    )
    review = ReviewerResult(
        run_id=run_id,
        reviewer_run_id="reviewer-1",
        draft_hash=draft.content_hash,
        evidence_manifest_hash=evidence.content_hash,
        claim_verdicts=tuple(
            ReviewClaimVerdict(
                claim_id=claim.id,
                accepted=True,
                classification_supported=True,
                rationale="Supported by the pinned source and stated premise.",
            )
            for claim in claims
        ),
        rationale="All question parts are supported.",
    )
    return draft, evidence, {("src/app.py", content_hash): content}, review


def test_repeated_canonical_evidence_retains_complete_invocation_provenance() -> None:
    from gobby.ask.claims import AssertionKind, EvidenceScope
    from gobby.ask.validation import _response_body, validate_claims

    draft, evidence, blobs, _review = _valid_case()
    source_id = evidence.records[0].response.items[0].evidence_id
    scoped_claim = draft.claims[0].model_copy(
        update={
            "assertion_kind": AssertionKind.NEGATIVE,
            "evidence_scope": EvidenceScope(
                description="Every recorded return-value query.",
                evidence_ids=(source_id,),
            ),
        }
    )
    draft = draft.model_copy(update={"claims": (scoped_claim, draft.claims[1])})
    complete = evidence.records[0].model_copy(update={"invocation_id": "invocation-complete"})
    partial_response = complete.response.model_copy(
        update={"complete": False, "completeness": "truncated_index"}
    )
    partial = complete.model_copy(
        update={
            "invocation_id": "invocation-partial",
            "response": partial_response,
            "response_hash": _json_hash(_response_body(partial_response)),
        }
    )

    for records in ((partial, complete), (complete, partial)):
        repeated = evidence.model_copy(update={"records": records})
        report = validate_claims(draft, repeated, pinned_blobs=blobs)
        assert report.accepted_claim_ids == ("claim-return", "claim-stable")
        assert "duplicate_evidence_id" not in report.diagnostic_codes

    explicitly_partial = scoped_claim.model_copy(
        update={
            "evidence_scope": EvidenceScope(
                description="The explicitly selected partial invocation.",
                evidence_ids=(source_id,),
                invocation_ids=(partial.invocation_id,),
            )
        }
    )
    explicit_report = validate_claims(
        draft.model_copy(update={"claims": (explicitly_partial, draft.claims[1])}),
        evidence.model_copy(update={"records": (complete, partial)}),
        pinned_blobs=blobs,
    )
    assert "incomplete_exhaustive_scope" in explicit_report.diagnostic_codes

    source = complete.response.items[0]
    conflicting_source = source.model_copy(update={"excerpt": "    return 2\n"})
    conflicting_response = complete.response.model_copy(update={"items": (conflicting_source,)})
    conflicting = complete.model_copy(
        update={
            "invocation_id": "invocation-conflict",
            "response": conflicting_response,
            "response_hash": _json_hash(_response_body(conflicting_response)),
        }
    )
    conflict_report = validate_claims(
        draft,
        evidence.model_copy(update={"records": (complete, conflicting)}),
        pinned_blobs=blobs,
    )
    assert "conflicting_evidence_id" in conflict_report.diagnostic_codes


def test_numbered_excerpt_must_render_the_verified_excerpt() -> None:
    from gobby.ask.validation import _response_body, validate_claims

    draft, evidence, blobs, _review = _valid_case()
    assert "numbered_excerpt_mismatch" not in (
        validate_claims(draft, evidence, pinned_blobs=blobs).diagnostic_codes
    )
    record = evidence.records[0]
    renumbered = record.response.items[0].model_copy(
        update={"numbered_excerpt": "1|     return 1\n"}
    )
    response = record.response.model_copy(update={"items": (renumbered,)})
    tampered = record.model_copy(
        update={"response": response, "response_hash": _json_hash(_response_body(response))}
    )
    report = validate_claims(
        draft,
        evidence.model_copy(update={"records": (tampered, *evidence.records[1:])}),
        pinned_blobs=blobs,
    )
    assert "numbered_excerpt_mismatch" in report.diagnostic_codes


def test_claim_validation_and_review_gates() -> None:
    from gobby.ask.claims import (
        ClaimClassification,
        GitMetadataCitation,
        GraphCitation,
        QuestionPart,
    )
    from gobby.ask.validation import validate_claims, validate_review

    draft, evidence, blobs, review = _valid_case()
    valid = validate_claims(draft, evidence, pinned_blobs=blobs)
    assert valid.accepted_claim_ids == ("claim-return", "claim-stable")
    reviewed = validate_review(draft, evidence, valid, review)
    assert reviewed.accepted_claim_ids == ("claim-return", "claim-stable")

    semantic_gap = review.model_copy(
        update={"missing_question_parts": (draft.question_parts[0].id,)}
    )
    partial = validate_review(draft, evidence, valid, semantic_gap)
    assert partial.accepted_claim_ids == reviewed.accepted_claim_ids
    assert partial.missing_question_parts == semantic_gap.missing_question_parts
    assert not partial.diagnostics

    citation = draft.claims[0].citations[0]
    cross_run = draft.model_copy(
        update={
            "claims": (
                draft.claims[0].model_copy(
                    update={"citations": (citation.model_copy(update={"run_id": "run-2"}),)}
                ),
                draft.claims[1],
            )
        }
    )
    assert (
        "cross_run_citation"
        in validate_claims(cross_run, evidence, pinned_blobs=blobs).diagnostic_codes
    )

    stale_range = draft.model_copy(
        update={
            "claims": (
                draft.claims[0].model_copy(
                    update={"citations": (citation.model_copy(update={"byte_start": 0}),)}
                ),
                draft.claims[1],
            )
        }
    )
    assert (
        "citation_selector_mismatch"
        in validate_claims(stale_range, evidence, pinned_blobs=blobs).diagnostic_codes
    )

    cycled = draft.model_copy(
        update={
            "claims": (
                draft.claims[0].model_copy(
                    update={
                        "classification": ClaimClassification.INFERRED,
                        "premise_claim_ids": ("claim-stable",),
                        "rationale": "cycle",
                    }
                ),
                draft.claims[1],
            )
        }
    )
    assert "premise_cycle" in validate_claims(cycled, evidence, pinned_blobs=blobs).diagnostic_codes

    source = evidence.records[0].response.items[0]
    graph_citation = GraphCitation(
        run_id="run-1",
        evidence_id="graph:unrecorded",
        source_evidence_id=source.evidence_id,
        relation="call",
        direction="outgoing",
        from_id="alpha",
        to_id="beta",
        owner_path="src/app.py",
        owner_content_hash=source.content_hash,
        provenance="extracted",
    )
    graph_direct = draft.model_copy(
        update={
            "claims": (
                draft.claims[0].model_copy(update={"citations": (graph_citation,)}),
                draft.claims[1],
            )
        }
    )
    graph_report = validate_claims(graph_direct, evidence, pinned_blobs=blobs)
    assert "unsupported_direct_evidence" in graph_report.diagnostic_codes
    assert "fabricated_citation" in graph_report.diagnostic_codes
    untyped_diagram = draft.model_copy(
        update={
            "claims": (
                draft.claims[0].model_copy(update={"statement": "```mermaid\nA-->B\n```"}),
                draft.claims[1],
            )
        }
    )
    assert (
        "untyped_mermaid"
        in validate_claims(untyped_diagram, evidence, pinned_blobs=blobs).diagnostic_codes
    )

    stale_review = review.model_copy(update={"draft_hash": "0" * 64})
    assert "stale_review" in validate_review(draft, evidence, valid, stale_review).diagnostic_codes
    same_agent = review.model_copy(update={"reviewer_run_id": "investigator-1"})
    assert (
        "reviewer_not_independent"
        in validate_review(draft, evidence, valid, same_agent).diagnostic_codes
    )
    uncovered = draft.model_copy(
        update={
            "question_parts": draft.question_parts
            + (QuestionPart(id="limits", text="Known limitations"),)
        }
    )
    incomplete_coverage = review.model_copy(update={"draft_hash": uncovered.content_hash})
    uncovered_validation = validate_claims(uncovered, evidence, pinned_blobs=blobs)
    assert (
        "invalid_question_coverage"
        in validate_review(
            uncovered,
            evidence,
            uncovered_validation,
            incomplete_coverage,
        ).diagnostic_codes
    )

    with pytest.raises(ValidationError):
        GitMetadataCitation.model_validate(
            {
                "citation_type": "git_metadata",
                "run_id": "run-1",
                "evidence_id": "commit:fake",
                "commit_oid": "a" * 40,
                "parent_oids": ["b" * 40],
                "comparison_parent_oid": "b" * 40,
                "comparison_kind": "first_parent",
                "changed_paths_digest": "c" * 64,
                "changed_path_count": 1,
                "record_hash": "d" * 64,
                "line_start": 1,
                "line_end": 2,
            }
        )


@pytest.mark.parametrize("requested_commit", [None, "9" * 40])
def test_typed_git_metadata_citation_binds_canonical_comparison(
    requested_commit: str | None,
) -> None:
    from gobby.ask.claims import (
        AnswerDraft,
        AnswerSection,
        Claim,
        ClaimClassification,
        GitMetadataCitation,
        QuestionPart,
    )
    from gobby.ask.validation import validate_claims
    from gobby.ask.validation_models import EvidenceManifest

    draft, evidence, blobs, _review = _valid_case()
    body = evidence.model_dump(mode="json")
    binding = body["repository_binding"]
    commit_oid = requested_commit or binding["commit_oid"]
    changed_path = {
        "status": "modified",
        "similarity": None,
        "old_path": "src/app.py",
        "new_path": "src/app.py",
        "old_exclusion": None,
        "new_exclusion": None,
        "old_mode": "100644",
        "new_mode": "100644",
        "old_blob_oid": "c" * 40,
        "new_blob_oid": "f" * 40,
    }
    parent_oids = ["b" * 40]
    commit = {
        "parent_oids": parent_oids,
        "comparison_parent_oid": "b" * 40,
        "comparison_kind": "first_parent",
        "changed_paths_digest": _json_hash([changed_path], sort_keys=False),
    }
    patch = "@@ -1 +1 @@\n-old value\n+new value\n"
    record_hash = _json_hash([changed_path, patch], sort_keys=False)
    evidence_id = "commit:" + _json_hash(
        [
            commit_oid,
            commit["parent_oids"],
            commit["comparison_parent_oid"],
            commit["comparison_kind"],
            commit["changed_paths_digest"],
            record_hash,
        ],
        sort_keys=False,
    )
    metadata = {
        "item_type": "commit_metadata",
        "evidence_id": evidence_id,
        "commit_oid": commit_oid,
        "parent_oids": commit["parent_oids"],
        "comparison_parent_oid": commit["comparison_parent_oid"],
        "comparison_kind": commit["comparison_kind"],
        "changed_paths_digest": commit["changed_paths_digest"],
        "changed_path_count": 1,
        "changed_path": changed_path,
        "patch": patch,
        "record_hash": record_hash,
    }
    response = body["records"][0]["response"]
    response["request"]["read"] = {"kind": "commit_metadata"}
    if requested_commit is not None:
        response["request"]["read"]["commit_oid"] = requested_commit
    response["request_fingerprint"] = _json_hash(response["request"])
    body["records"][0]["request_hash"] = _json_hash(response["request"])
    response["items"].append(metadata)
    response["bounds"]["returned_items"] = 2
    response["bounds"]["total_items"] = 2
    response["bounds"]["serialized_item_bytes"] = sum(
        len(json.dumps(item, separators=(",", ":")).encode()) for item in response["items"]
    )
    response["items"][0].pop("qualified_name")
    response["contract"].pop("hybrid")
    response["bounds"].pop("graph_depth")
    response.pop("continuation")
    body["records"][0]["response_hash"] = _json_hash(response)
    evidence = EvidenceManifest.model_validate(body)
    citation = GitMetadataCitation(
        run_id="run-1",
        evidence_id=evidence_id,
        commit_oid=metadata["commit_oid"],
        parent_oids=tuple(parent_oids),
        comparison_parent_oid=metadata["comparison_parent_oid"],
        comparison_kind=metadata["comparison_kind"],
        changed_paths_digest=metadata["changed_paths_digest"],
        changed_path_count=1,
        changed_path=metadata["changed_path"],
        record_hash=record_hash,
    )
    metadata_draft = AnswerDraft(
        run_id="run-1",
        investigator_run_id="investigator-1",
        question="What changed?",
        question_parts=(QuestionPart(id="change", text="Changed paths"),),
        claims=(
            Claim(
                id="claim-change",
                classification=ClaimClassification.DIRECT,
                statement="src/app.py changed relative to the first parent.",
                citations=(citation,),
                question_part_ids=("change",),
            ),
        ),
        sections=(AnswerSection(id="answer", title="Answer", claim_ids=("claim-change",)),),
    )
    assert validate_claims(metadata_draft, evidence, pinned_blobs=blobs).accepted_claim_ids == (
        "claim-change",
    )

    altered = metadata_draft.model_copy(
        update={
            "claims": (
                metadata_draft.claims[0].model_copy(
                    update={
                        "citations": (
                            citation.model_copy(update={"comparison_parent_oid": "e" * 40}),
                        )
                    }
                ),
            )
        }
    )
    report = validate_claims(altered, evidence, pinned_blobs=blobs)
    assert "git_metadata_mismatch" in report.diagnostic_codes

    response["items"][-1]["patch"] += "tampered\n"
    response["bounds"]["serialized_item_bytes"] = sum(
        len(json.dumps(item, separators=(",", ":")).encode()) for item in response["items"]
    )
    body["records"][0]["response_hash"] = _json_hash(response)
    tampered = EvidenceManifest.model_validate(body)
    assert (
        "git_record_hash_mismatch"
        in validate_claims(metadata_draft, tampered, pinned_blobs=blobs).diagnostic_codes
    )


def test_negative_scope_requires_complete_evidence() -> None:
    from gobby.ask.claims import AssertionKind, EvidenceScope
    from gobby.ask.validation import validate_claims
    from gobby.ask.validation_models import EvidenceManifest

    draft, evidence, blobs, _review = _valid_case()
    scoped = draft.model_copy(
        update={
            "claims": (
                draft.claims[0].model_copy(
                    update={
                        "assertion_kind": AssertionKind.NEGATIVE,
                        "evidence_scope": EvidenceScope(
                            description="All tracked source in the pinned inventory",
                            evidence_ids=(draft.claims[0].citations[0].evidence_id,),
                            invocation_ids=("invocation-1",),
                        ),
                    }
                ),
                draft.claims[1],
            )
        }
    )
    assert validate_claims(scoped, evidence, pinned_blobs=blobs).is_valid

    partial_body = deepcopy(evidence.model_dump(mode="json"))
    response = partial_body["records"][0]["response"]
    response["complete"] = False
    response["completeness"] = "truncated_index"
    partial_body["records"][0]["response_hash"] = _json_hash(response)
    partial = EvidenceManifest.model_validate(partial_body)
    assert (
        "incomplete_exhaustive_scope"
        in validate_claims(scoped, partial, pinned_blobs=blobs).diagnostic_codes
    )

    excluded_body = deepcopy(evidence.model_dump(mode="json"))
    excluded_response = excluded_body["records"][0]["response"]
    excluded_response["complete"] = True
    excluded_response["completeness"] = "excluded_scope"
    excluded_body["records"][0]["response_hash"] = _json_hash(excluded_response)
    excluded = EvidenceManifest.model_validate(excluded_body)
    assert (
        "incomplete_exhaustive_scope"
        in validate_claims(scoped, excluded, pinned_blobs=blobs).diagnostic_codes
    )


def test_graph_citation_validates_canonical_owner_and_requires_inference() -> None:
    from gobby.ask.claims import (
        AnswerDraft,
        AnswerSection,
        Claim,
        ClaimClassification,
        GraphCitation,
        QuestionPart,
    )
    from gobby.ask.validation import validate_claims
    from gobby.ask.validation_models import EvidenceManifest

    _draft, evidence, blobs, _review = _valid_case()
    body = evidence.model_dump(mode="json")
    response = body["records"][0]["response"]
    source = response["items"][0]
    source.pop("item_type")
    source.pop("qualified_name")
    endpoint_from = {
        "id": "alpha",
        "name": "alpha",
        "kind": "function",
        "path": "src/app.py",
    }
    endpoint_to = {
        "id": "beta",
        "name": "beta",
        "kind": "function",
        "path": "src/app.py",
    }
    owner = {"path": "src/app.py", "content_hash": source["content_hash"]}
    graph_id = "graph:" + _json_hash(
        [
            body["repository_binding"],
            source["evidence_id"],
            "call",
            "outgoing",
            endpoint_from,
            endpoint_to,
            owner,
            "extracted",
        ],
        sort_keys=False,
    )
    graph_item = {
        "item_type": "graph",
        "evidence_id": graph_id,
        "source": source,
        "relation": "call",
        "direction": "outgoing",
        "from": endpoint_from,
        "to": endpoint_to,
        "owner": owner,
        "provenance": "extracted",
    }
    response["items"] = [graph_item]
    response["bounds"]["serialized_item_bytes"] = len(
        json.dumps(graph_item, separators=(",", ":")).encode()
    )
    response["contract"].pop("hybrid")
    response["bounds"].pop("graph_depth")
    response.pop("continuation")
    body["records"][0]["response_hash"] = _json_hash(response)
    evidence = EvidenceManifest.model_validate(body)
    citation = GraphCitation(
        run_id="run-1",
        evidence_id=graph_id,
        source_evidence_id=source["evidence_id"],
        relation="call",
        direction="outgoing",
        from_id="alpha",
        to_id="beta",
        owner_path="src/app.py",
        owner_content_hash=source["content_hash"],
        provenance="extracted",
    )
    draft = AnswerDraft(
        run_id="run-1",
        investigator_run_id="investigator-1",
        question="What calls beta?",
        question_parts=(QuestionPart(id="calls", text="Call relationship"),),
        claims=(
            Claim(
                id="claim-call",
                classification=ClaimClassification.INFERRED,
                statement="Recorded graph facts associate alpha with a call to beta.",
                citations=(citation,),
                rationale="The extracted graph edge is owned by the cited pinned source.",
                question_part_ids=("calls",),
            ),
        ),
        sections=(AnswerSection(id="answer", title="Answer", claim_ids=("claim-call",)),),
    )
    assert validate_claims(draft, evidence, pinned_blobs=blobs).accepted_claim_ids == (
        "claim-call",
    )

    corrupt = deepcopy(evidence.model_dump(mode="json"))
    corrupt_response = corrupt["records"][0]["response"]
    corrupt_response["items"][0]["owner"]["content_hash"] = "0" * 64
    corrupt_response["contract"].pop("hybrid")
    corrupt_response["bounds"].pop("graph_depth")
    corrupt_response.pop("continuation")
    corrupt_response["items"][0]["source"].pop("qualified_name")
    corrupt["records"][0]["response_hash"] = _json_hash(corrupt_response)
    corrupt_evidence = EvidenceManifest.model_validate(corrupt)
    assert (
        "graph_owner_mismatch"
        in validate_claims(draft, corrupt_evidence, pinned_blobs=blobs).diagnostic_codes
    )


@pytest.mark.parametrize("same_path", [False, True])
def test_live_source_freshness_follows_claim_references(tmp_path: Path, same_path: bool) -> None:
    from dataclasses import dataclass

    from gobby.ask.claims import EvidenceScope
    from gobby.ask.evidence_runtime import pinned_blobs
    from gobby.ask.validation import _response_body, _source_identity, validate_claims
    from gobby.ask.validation_models import SourceEvidenceItem

    draft, evidence, blobs, _review = _valid_case()
    record = evidence.records[0]
    source = record.response.items[0]
    assert isinstance(source, SourceEvidenceItem)
    old_content = b"def alpha():\n    return 0\n"
    old_excerpt = "    return 0\n"
    old_source = source.model_copy(
        update={
            "path": source.path if same_path else "src/deleted.py",
            "content_hash": hashlib.sha256(old_content).hexdigest(),
            "excerpt": old_excerpt,
            "numbered_excerpt": "2|     return 0\n",
            "excerpt_hash": hashlib.sha256(old_excerpt.encode()).hexdigest(),
        }
    )
    old_source = old_source.model_copy(
        update={
            "evidence_id": _source_identity(evidence.repository_binding, old_source),
        }
    )
    response = record.response.model_copy(update={"items": (old_source,)})
    previous = record.model_copy(
        update={
            "invocation_id": "previous-query",
            "response": response,
            "response_hash": _json_hash(_response_body(response)),
        }
    )
    manifest = evidence.model_copy(update={"records": (previous, record)})

    # Repair can cite refreshed evidence while retaining the original query log.
    report = validate_claims(draft, manifest, pinned_blobs=blobs)
    assert report.accepted_claim_ids == ("claim-return", "claim-stable")
    path = tmp_path / source.path
    path.parent.mkdir(parents=True)
    path.write_bytes(blobs[(source.path, source.content_hash)])

    @dataclass
    class SourceSnapshot:
        binding: dict[str, object]
        source_root: Path

    snapshot = SourceSnapshot(evidence.repository_binding.model_dump(mode="json"), tmp_path)
    assert pinned_blobs(snapshot, manifest, draft) == blobs

    # All manifest records retain integrity checks, even those not cited.
    tampered = previous.model_copy(update={"response_hash": "0" * 64})
    invalid = validate_claims(
        draft,
        manifest.model_copy(update={"records": (tampered, record)}),
        pinned_blobs=blobs,
    )
    assert "response_hash_mismatch" in invalid.diagnostic_codes

    # Explicit evidence and invocation scopes depend on their full source sets.
    for scope in (
        EvidenceScope(description="Earlier evidence", evidence_ids=(old_source.evidence_id,)),
        EvidenceScope(description="Earlier query", invocation_ids=(previous.invocation_id,)),
    ):
        scoped = draft.model_copy(
            update={
                "claims": (
                    draft.claims[0].model_copy(update={"evidence_scope": scope}),
                    draft.claims[1],
                )
            }
        )
        stale = validate_claims(scoped, manifest, pinned_blobs=blobs)
        assert "source_content_missing" in stale.diagnostic_codes


def _community_case() -> tuple[Any, Any, dict[tuple[str, str], bytes], str]:
    """The valid case plus a second record holding one `communities` list item."""
    from gobby.ask.validation_models import EvidenceManifest

    draft, evidence, blobs, _review = _valid_case()
    body = evidence.model_dump(mode="json")
    binding = body["repository_binding"]
    community_id = "com:" + "c" * 64
    community_item = {
        "item_type": "community",
        "evidence_id": community_id,
        "community_id": 1,
        "label": "src",
        "label_source": "deterministic",
        "label_confidence": None,
        "label_stale": False,
        "size": 2,
        "cohesion": 0.5,
        "internal_edges": 1,
        "member_signature": "e" * 64,
        "members": [],
        "members_truncated": True,
        "representatives": ["src/app.py"],
        "boundary": [{"other_community_id": 2, "label": "tests", "import_count": 3}],
    }
    request = {
        "schema_version": 1,
        "binding": binding,
        "operation": "communities",
        "communities": {"min_size": 2, "max_members": 50},
        "max_bytes": 262_144,
    }
    response = {
        "request": request,
        "request_fingerprint": _json_hash(request, sort_keys=False),
        "binding": binding,
        "contract": {
            "name": "gcode-evidence",
            "schema_version": 1,
            "tool": "gobby-code",
            "tool_version": "0.5.0",
            "lane": "communities",
        },
        "items": [community_item],
        "complete": True,
        "completeness": "complete",
        "bounds": {
            "max_bytes": 262_144,
            "serialized_item_bytes": len(
                json.dumps(community_item, separators=(",", ":")).encode()
            ),
            "returned_items": 1,
            "total_items": 1,
            "result_limit": 1000,
        },
        "warnings": [],
    }
    body["records"].append(
        {
            "run_id": body["run_id"],
            "invocation_id": "invocation-2",
            "binding_digest": _json_hash(binding),
            "request_hash": _json_hash(request),
            "response_hash": _json_hash(response),
            "response": response,
        }
    )
    return draft, EvidenceManifest.model_validate(body), blobs, community_id


def test_manifest_accepts_community_item() -> None:
    from gobby.ask.validation import validate_claims

    draft, evidence, blobs, community_id = _community_case()
    community = evidence.records[1].response.items[0]
    assert (community.item_type, community.evidence_id) == ("community", community_id)

    report = validate_claims(draft, evidence, pinned_blobs=blobs)
    assert report.diagnostic_codes == ()
    assert report.accepted_claim_ids == ("claim-return", "claim-stable")


def test_citation_of_community_item_is_rejected() -> None:
    from gobby.ask.validation import validate_claims

    draft, evidence, blobs, community_id = _community_case()
    claim = draft.claims[0]
    citing_community = claim.model_copy(
        update={"citations": (claim.citations[0].model_copy(update={"evidence_id": community_id}),)}
    )
    draft = draft.model_copy(update={"claims": (citing_community, draft.claims[1])})

    report = validate_claims(draft, evidence, pinned_blobs=blobs)
    rejected = report.results[0]
    assert (rejected.claim_id, rejected.accepted) == ("claim-return", False)
    assert [(item.code, item.evidence_id) for item in rejected.diagnostics] == [
        ("community_not_citable", community_id)
    ]
    assert "communities" in rejected.diagnostics[0].message


def test_selector_rejects_multiple_community_keys() -> None:
    from gobby.ask.errors import EvidenceAdmissionError
    from gobby.ask.evidence import EvidenceAdmission

    assert EvidenceAdmission._normalize_selector(
        "communities", {"label": "Ask pipeline", "community_id": None}
    ) == {"communities": {"label": "Ask pipeline", "min_size": 2, "max_members": 50}}
    with pytest.raises(EvidenceAdmissionError, match="at most one of community_id, label, or path"):
        EvidenceAdmission._normalize_selector(
            "communities", {"community_id": 3, "path": "src/app.py"}
        )
