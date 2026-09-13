"""Atomic, reviewed-only Ask answer publication and model-free replay."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.claims import (
    AnswerDraft,
    ClaimClassification,
    GitMetadataCitation,
    GraphCitation,
    SourceCitation,
    canonical_hash,
    canonical_json,
)
from gobby.ask.validation import validate_review
from gobby.ask.validation_models import (
    ClaimValidationReport,
    EvidenceItem,
    EvidenceManifest,
    GraphEvidenceItem,
    ReviewValidationReport,
    SourceEvidence,
    SourceEvidenceItem,
)


class PublicationError(RuntimeError):
    """The immutable reviewed publication cannot be created or replayed."""


@dataclass(frozen=True)
class PublishedAnswer:
    root: Path
    manifest_sha256: str
    outcome: str
    claim_ids: tuple[str, ...]
    answer: dict[str, Any]
    markdown: str


@dataclass(frozen=True)
class ReplayResult:
    answer_json: bytes
    answer_markdown: bytes
    manifest_sha256: str


def _artifact_name(evidence_id: str, suffix: str) -> str:
    return f"{hashlib.sha256(evidence_id.encode()).hexdigest()}{suffix}"


def _source_artifact_path(source: SourceEvidence) -> str:
    return f"evidence/{_artifact_name(source.evidence_id, '.txt')}"


def _record_artifact_path(evidence_id: str) -> str:
    return f"evidence/{_artifact_name(evidence_id, '.json')}"


def _evidence_record_body(item: EvidenceItem) -> dict[str, Any]:
    body = item.model_dump(mode="json", by_alias=True)
    source = body if body["item_type"] == "source" else body.get("source")
    if source is not None and source["qualified_name"] is None:
        source.pop("qualified_name")
    if body["item_type"] == "commit_metadata" and body["changed_path"] is None:
        body.pop("changed_path")
    return body


def _evidence_index(evidence: EvidenceManifest) -> dict[str, EvidenceItem]:
    return {item.evidence_id: item for record in evidence.records for item in record.response.items}


def _citation_body(
    citation: SourceCitation | GraphCitation | GitMetadataCitation,
    item: EvidenceItem,
) -> dict[str, Any]:
    body = citation.model_dump(mode="json")
    body["record_path"] = _record_artifact_path(citation.evidence_id)
    if isinstance(citation, SourceCitation) and isinstance(item, SourceEvidenceItem):
        body["artifact_path"] = _source_artifact_path(item)
    elif isinstance(citation, GraphCitation) and isinstance(item, GraphEvidenceItem):
        body["artifact_path"] = _source_artifact_path(item.source)
        body["source_path"] = item.source.path
        body["line_start"] = item.source.line_start
        body["line_end"] = item.source.line_end
    return body


def _unknown_id(part_id: str, occupied: set[str]) -> str:
    slug = "".join(
        character if character.isalnum() or character == "-" else "-" for character in part_id
    ).strip("-")
    stem = f"unknown-{slug or hashlib.sha256(part_id.encode()).hexdigest()[:12]}"
    if stem not in occupied:
        return stem
    return f"{stem}-{hashlib.sha256(part_id.encode()).hexdigest()[:12]}"


def _published_answer_body(
    draft: AnswerDraft,
    evidence: EvidenceManifest,
    review: ReviewValidationReport,
) -> tuple[dict[str, Any], tuple[str, ...], dict[str, EvidenceItem]]:
    accepted_ids = set(review.accepted_claim_ids)
    claims = {claim.id: claim for claim in draft.claims}
    items = _evidence_index(evidence)
    published_claims: list[dict[str, Any]] = []
    ordered_claim_ids: list[str] = []
    published_sections: list[dict[str, Any]] = []
    represented_parts: set[str] = set()
    supported_parts: set[str] = set()

    for section in draft.sections:
        section_ids: list[str] = []
        for claim_id in section.claim_ids:
            if claim_id not in accepted_ids:
                continue
            claim = claims[claim_id]
            claim_body = claim.model_dump(mode="json")
            claim_body["citations"] = [
                _citation_body(citation, items[citation.evidence_id])
                for citation in claim.citations
            ]
            published_claims.append(claim_body)
            ordered_claim_ids.append(claim.id)
            section_ids.append(claim.id)
            represented_parts.update(claim.question_part_ids)
            if claim.classification in {
                ClaimClassification.DIRECT,
                ClaimClassification.INFERRED,
            }:
                supported_parts.update(claim.question_part_ids)
        if section_ids:
            published_sections.append(
                {"id": section.id, "title": "Reviewed claims", "claim_ids": section_ids}
            )

    explicitly_missing = set(review.missing_question_parts)
    unresolved = [
        part
        for part in draft.question_parts
        if part.id in explicitly_missing or part.id not in supported_parts
    ]
    unrepresented = [part for part in unresolved if part.id not in represented_parts]
    if unrepresented:
        unknown_ids: list[str] = []
        occupied = set(claims)
        part_ordinals = {part.id: index for index, part in enumerate(draft.question_parts, start=1)}
        for part in unrepresented:
            claim_id = _unknown_id(part.id, occupied)
            occupied.add(claim_id)
            unknown_ids.append(claim_id)
            published_claims.append(
                {
                    "id": claim_id,
                    "classification": ClaimClassification.UNKNOWN.value,
                    "statement": (
                        f"Unknown: question part {part_ordinals[part.id]} remains unresolved."
                    ),
                    "citations": [],
                    "premise_claim_ids": [],
                    "rationale": (
                        "Recorded evidence and independent review did not resolve this question part."
                    ),
                    "question_part_ids": [part.id],
                    "assertion_kind": "positive",
                    "evidence_scope": None,
                }
            )
        published_sections.append({"id": "unknowns", "title": "Unknowns", "claim_ids": unknown_ids})

    has_supported_claim = any(
        claim_id in accepted_ids
        and claims[claim_id].classification
        in {
            ClaimClassification.DIRECT,
            ClaimClassification.INFERRED,
        }
        for claim_id in claims
    )
    if unresolved:
        outcome = "partial" if supported_parts else "unknown"
    else:
        outcome = "complete" if has_supported_claim else "unknown"
    answer = {
        "schema_version": 1,
        "run_id": draft.run_id,
        "draft_hash": draft.content_hash,
        "evidence_manifest_hash": evidence.content_hash,
        "reviewer_run_id": review.reviewer_run_id,
        "outcome": outcome,
        "accepted_claim_ids": ordered_claim_ids,
        "question": draft.question,
        "sections": published_sections,
        "claims": published_claims,
    }
    return answer, tuple(ordered_claim_ids), items


def _citation_markdown(citation: Mapping[str, Any]) -> str:
    citation_type = citation["citation_type"]
    if citation_type == "source":
        label = f"{citation['path']}:{citation['line_start']}-{citation['line_end']}"
        return f"[{label}]({citation['artifact_path']})"
    if citation_type == "graph":
        label = (
            f"{citation['source_path']}:{citation['line_start']}-{citation['line_end']} "
            f"{citation['relation']}"
        )
        return f"[{label}]({citation['artifact_path']})"
    path = citation.get("changed_path") or {}
    changed_path = path.get("new_path") or path.get("old_path") or "no changed path"
    commit = str(citation["commit_oid"])
    parent = str(citation["comparison_parent_oid"])
    return f"commit `{commit}` vs `{parent}` ({changed_path})"


def render_markdown(answer: Mapping[str, Any]) -> str:
    """Render only the accepted claims and deterministic unknown entries."""
    claim_map = {claim["id"]: claim for claim in answer["claims"]}
    lines = [f"# {answer['question']}", "", f"Outcome: **{answer['outcome']}**", ""]
    for section in answer["sections"]:
        lines.extend((f"## {section['title']}", ""))
        for claim_id in section["claim_ids"]:
            claim = claim_map[claim_id]
            line = f"- {claim['statement']}"
            citations = claim.get("citations", [])
            if citations:
                line += " " + "; ".join(_citation_markdown(citation) for citation in citations)
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


def _write_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(f"failed to write Ask publication file {path}")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_files(root: Path, manifest: Mapping[str, Any]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PublicationError("invalid publication manifest")
    expected_paths: set[str] = set()
    for descriptor in files:
        if not isinstance(descriptor, dict):
            raise PublicationError("invalid publication manifest entry")
        relative = descriptor.get("path")
        if not isinstance(relative, str):
            raise PublicationError("invalid publication path")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or str(path) != relative:
            raise PublicationError("publication path escapes bundle")
        if relative in expected_paths:
            raise PublicationError("publication manifest path is duplicated")
        expected_paths.add(relative)
        file_path = root / path
        if file_path.is_symlink():
            raise PublicationError(f"publication file is a symlink: {relative}")
        try:
            payload = file_path.read_bytes()
        except FileNotFoundError as error:
            raise PublicationError(f"publication file is missing: {relative}") from error
        if len(payload) != descriptor.get("size_bytes") or hashlib.sha256(payload).hexdigest() != (
            descriptor.get("sha256")
        ):
            raise PublicationError(f"publication hash mismatch for {relative}")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root) != Path("manifest.json")
    }
    if actual_paths != expected_paths:
        raise PublicationError("publication contains unmanifested or missing files")


def _read_manifest(root: Path) -> tuple[dict[str, Any], bytes]:
    if (root / "manifest.json").is_symlink():
        raise PublicationError("publication manifest must not be a symlink")
    try:
        payload = (root / "manifest.json").read_bytes()
        manifest = json.loads(payload)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise PublicationError("publication manifest is missing or invalid") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PublicationError("publication manifest is unsupported")
    return manifest, payload


def replay_publication(root: Path) -> ReplayResult:
    """Verify stored hashes and re-render the answer without any model call."""
    manifest, manifest_bytes = _read_manifest(root)
    _verify_files(root, manifest)
    answer_json = (root / "answer.json").read_bytes()
    try:
        answer = json.loads(answer_json)
    except json.JSONDecodeError as error:
        raise PublicationError("published answer JSON is invalid") from error
    if not isinstance(answer, dict):
        raise PublicationError("published answer JSON must be an object")
    rerendered = render_markdown(answer).encode()
    stored_markdown = (root / "answer.md").read_bytes()
    if rerendered != stored_markdown:
        raise PublicationError("published Markdown is not a byte-stable render")
    return ReplayResult(
        answer_json=answer_json,
        answer_markdown=rerendered,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _existing_publication(root: Path, expected_manifest: bytes) -> PublishedAnswer:
    manifest, manifest_bytes = _read_manifest(root)
    if manifest_bytes != expected_manifest:
        raise PublicationError("immutable publication collision")
    replay = replay_publication(root)
    answer = json.loads(replay.answer_json)
    return PublishedAnswer(
        root=root,
        manifest_sha256=replay.manifest_sha256,
        outcome=answer["outcome"],
        claim_ids=tuple(answer["accepted_claim_ids"]),
        answer=answer,
        markdown=replay.answer_markdown.decode(),
    )


def publish_answer(
    store: AskArtifactStore,
    draft: AnswerDraft,
    evidence: EvidenceManifest,
    deterministic: ClaimValidationReport,
    review: ReviewValidationReport | None,
    *,
    request: Mapping[str, Any],
    binding: Mapping[str, Any],
    profiles: Mapping[str, Any],
    tool_identities: Sequence[str],
    attempt_history: Sequence[Mapping[str, Any]],
    deadline_check: Callable[[], None] | None = None,
) -> PublishedAnswer:
    """Atomically publish the reviewed subset of one immutable draft version."""

    def check_deadline() -> None:
        if deadline_check is not None:
            deadline_check()

    check_deadline()
    if (
        store.run_id != draft.run_id
        or store.project_id != evidence.project_id
        or draft.run_id != evidence.run_id
        or dict(binding) != evidence.repository_binding.model_dump(mode="json")
        or request.get("question") != draft.question
        or not profiles
        or not tool_identities
        or not attempt_history
    ):
        raise PublicationError("publication run identity or provenance is inconsistent")
    if review is None:
        raise PublicationError("mandatory review is missing; publication withheld")
    if review.diagnostics:
        raise PublicationError("mandatory review identity is invalid; publication withheld")
    if (
        deterministic.draft_hash != draft.content_hash
        or deterministic.evidence_manifest_hash != evidence.content_hash
        or review.draft_hash != draft.content_hash
        or review.evidence_manifest_hash != evidence.content_hash
    ):
        raise PublicationError("validation or review is stale; publication withheld")
    expected_review = validate_review(draft, evidence, deterministic, review.review)
    if expected_review != review:
        raise PublicationError("reviewed claims do not preserve accepted premise closure")
    answer, claim_ids, items = _published_answer_body(draft, evidence, review)
    files: dict[str, bytes] = {
        "answer.json": canonical_json(answer),
        "answer.md": render_markdown(answer).encode(),
        "evidence-manifest.json": canonical_json(evidence.model_dump(mode="json", by_alias=True)),
    }
    cited_ids = {
        citation["evidence_id"]
        for claim in answer["claims"]
        for citation in claim.get("citations", [])
    }
    for evidence_id in sorted(cited_ids):
        item = items[evidence_id]
        files[_record_artifact_path(evidence_id)] = canonical_json(_evidence_record_body(item))
        source: SourceEvidence | None = None
        if isinstance(item, SourceEvidenceItem):
            source = item
        elif isinstance(item, GraphEvidenceItem):
            source = item.source
        if source is not None:
            files[_source_artifact_path(source)] = source.excerpt.encode()
    file_manifest = [
        {
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for path, payload in sorted(files.items())
    ]
    manifest = {
        "schema_version": 1,
        "run_id": draft.run_id,
        "publication_id": canonical_hash(answer),
        "files": file_manifest,
        "provenance": {
            "request": dict(request),
            "binding": dict(binding),
            "profiles": dict(profiles),
            "tool_identities": list(tool_identities),
            "draft": draft.model_dump(mode="json"),
            "evidence_manifest_hash": evidence.content_hash,
            "deterministic_validation": deterministic.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "attempt_history": list(attempt_history),
        },
    }
    manifest_bytes = canonical_json(manifest)
    target = store.run_root / "publication"
    if target.exists():
        check_deadline()
        return _existing_publication(target, manifest_bytes)

    temporary = Path(tempfile.mkdtemp(prefix=".publication-", suffix=".tmp", dir=store.run_root))
    try:
        os.chmod(temporary, 0o700)
        (temporary / "evidence").mkdir(mode=0o700)
        for relative, payload in files.items():
            _write_file(temporary / relative, payload)
            check_deadline()
        _write_file(temporary / "manifest.json", manifest_bytes)
        check_deadline()
        _fsync_directory(temporary / "evidence")
        _fsync_directory(temporary)
        check_deadline()
        try:
            os.rename(temporary, target)
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY} and not target.exists():
                raise
            return _existing_publication(target, manifest_bytes)
        try:
            check_deadline()
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            _fsync_directory(store.run_root)
            raise
        _fsync_directory(store.run_root)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    replay = replay_publication(target)
    return PublishedAnswer(
        root=target,
        manifest_sha256=replay.manifest_sha256,
        outcome=answer["outcome"],
        claim_ids=claim_ids,
        answer=answer,
        markdown=replay.answer_markdown.decode(),
    )
