"""Immutable requirement snapshots and requirement-aware source citations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from gobby.plans.review_evidence_models import (
    ReviewEvidenceError,
    canonical_json_bytes,
    canonical_json_object,
)
from gobby.utils.hashing import is_sha256

REQUEST_ANCHOR_VARIABLE = "plan_review_request_anchor"
_ANCHOR_FIELDS = {
    "version",
    "anchor_id",
    "content",
    "content_sha256",
    "captured_by",
}
_TASK_FIELDS = ("title", "description", "validation_criteria")
_REQUIREMENT_ID_RE = re.compile(r"^req-[0-9a-f]{12}$")
_SPAN_FIELDS = {"line_start", "line_end"}


def build_request_anchor(anchor_id: str, content: str | Sequence[str]) -> dict[str, object]:
    """Build the server-owned initiating-request anchor."""
    if not anchor_id:
        raise ReviewEvidenceError(
            "invalid_request_anchor",
            "request anchor identity must be a non-empty string",
        )
    messages = [content] if isinstance(content, str) else list(content)
    if not messages or any(not isinstance(message, str) or not message for message in messages):
        raise ReviewEvidenceError(
            "missing_request_anchor",
            "plan-mode entry did not carry initiating request content",
        )
    return {
        "version": 2,
        "anchor_id": anchor_id,
        "content": messages,
        "content_sha256": _sha256(_canonical_message_bytes(messages)),
        "captured_by": "plan_mode_observer",
    }


def validate_request_anchor(raw: object) -> dict[str, object]:
    """Validate one durable server-owned request anchor."""
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "missing_request_anchor",
            "taskless plan review requires a server-owned request anchor",
        )
    anchor = canonical_json_object(raw)
    if set(anchor) != _ANCHOR_FIELDS or anchor.get("version") != 2:
        raise ReviewEvidenceError(
            "invalid_request_anchor",
            "request anchor does not match the canonical version-2 schema",
        )
    if anchor.get("captured_by") != "plan_mode_observer":
        raise ReviewEvidenceError(
            "invalid_request_anchor",
            "request anchor is not owned by the plan-mode entry observer",
        )
    anchor_id = anchor.get("anchor_id")
    content = anchor.get("content")
    digest = anchor.get("content_sha256")
    if not isinstance(anchor_id, str) or not anchor_id:
        raise ReviewEvidenceError(
            "invalid_request_anchor",
            "request anchor identity must be a non-empty string",
        )
    if (
        not isinstance(content, list)
        or not content
        or any(not isinstance(message, str) or not message for message in content)
    ):
        raise ReviewEvidenceError(
            "invalid_request_anchor",
            "request anchor content must be a non-empty list of strings",
        )
    if not isinstance(digest, str) or digest != _sha256(_canonical_message_bytes(content)):
        raise ReviewEvidenceError(
            "invalid_request_anchor",
            "request anchor content hash does not match its exact content",
        )
    return anchor


def capture_request_anchor(
    variables: dict[str, object],
    *,
    anchor_id: str,
    content: str | None,
) -> dict[str, object]:
    """Capture observed content, otherwise reuse a valid persisted anchor."""
    if content is not None:
        anchor = build_request_anchor(anchor_id, content)
        variables[REQUEST_ANCHOR_VARIABLE] = anchor
        return anchor
    existing = variables.get(REQUEST_ANCHOR_VARIABLE)
    try:
        anchor = validate_request_anchor(existing)
    except ReviewEvidenceError:
        raise ReviewEvidenceError(
            "missing_request_anchor",
            "plan-mode entry has no request content or persisted request anchor",
        ) from None
    return anchor


def append_request_anchor(
    variables: dict[str, object],
    *,
    content: str,
) -> dict[str, object]:
    """Append one observer-owned message to the current plan-mode span."""
    anchor = validate_request_anchor(variables.get(REQUEST_ANCHOR_VARIABLE))
    messages = cast(list[str], anchor["content"])
    if messages[-1] == content:
        return anchor
    appended = build_request_anchor(
        cast(str, anchor["anchor_id"]),
        [*messages, content],
    )
    variables[REQUEST_ANCHOR_VARIABLE] = appended
    return appended


def parse_requirement_source_paths(plan_snapshot: bytes) -> tuple[str, ...]:
    """Parse the sole requirement-source marker grammar from Constraints."""
    try:
        text = plan_snapshot.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewEvidenceError(
            "invalid_requirement_source",
            "plan snapshot is not valid UTF-8",
        ) from exc

    inside_constraints = False
    fence: str | None = None
    paths: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```"):
            fence = "```"
            continue
        if stripped.startswith("~~~"):
            fence = "~~~"
            continue
        if stripped.startswith("## "):
            inside_constraints = stripped == "## Constraints"
            continue
        marker_line = stripped
        for prefix in ("- ", "* ", "+ "):
            if marker_line.startswith(prefix):
                marker_line = marker_line.removeprefix(prefix).lstrip()
                break
        if not marker_line.startswith("requirement-source:"):
            continue
        if not inside_constraints:
            raise ReviewEvidenceError(
                "invalid_requirement_source",
                "requirement-source marker must appear in ## Constraints",
            )
        raw_path = marker_line.removeprefix("requirement-source:").strip()
        if not raw_path or any(character.isspace() for character in raw_path):
            raise ReviewEvidenceError(
                "invalid_requirement_source",
                f"malformed requirement-source marker: {line}",
            )
        if raw_path not in seen:
            paths.append(raw_path)
            seen.add(raw_path)
    return tuple(paths)


def assemble_requirements_bundle(
    *,
    project_root: Path,
    plan_snapshot: bytes,
    task_id: str | None = None,
    task_fields: Mapping[str, str | None] | None = None,
    request_anchor: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Snapshot the exact authoritative requirement-source universe."""
    task_bound = task_id is not None and task_fields is not None and request_anchor is None
    taskless = task_id is None and task_fields is None
    if not (task_bound or taskless):
        raise ReviewEvidenceError(
            "invalid_requirement_source",
            "requirements require task fields or a taskless request anchor",
        )

    sources: list[dict[str, object]] = []
    if task_bound:
        assert task_id is not None and task_fields is not None
        for field in _TASK_FIELDS:
            content = task_fields.get(field) or ""
            source_ref = f"task:{task_id}:{field}"
            sources.append(
                _requirement_source(
                    source_kind="task_field",
                    source_ref=source_ref,
                    content=content,
                    details={"field": field},
                )
            )
    else:
        anchor = validate_request_anchor(request_anchor)
        anchor_id = cast(str, anchor["anchor_id"])
        anchor_content = _canonical_message_bytes(cast(list[str], anchor["content"])).decode()
        sources.append(
            _requirement_source(
                source_kind="request_anchor",
                source_ref=f"request:{anchor_id}",
                content=anchor_content,
                details={"anchor_id": anchor_id},
            )
        )

    root = project_root.resolve(strict=True)
    for relative in parse_requirement_source_paths(plan_snapshot):
        content = _read_repository_requirement(root, relative)
        sources.append(
            _requirement_source(
                source_kind="repository_document",
                source_ref=f"repository:{relative}",
                content=content,
                details={"path": relative},
            )
        )

    bundle: dict[str, object] = {
        "version": 1,
        "sources": sources,
    }
    bundle["bundle_digest"] = _requirements_bundle_digest(sources)
    return validate_requirements_bundle(bundle)


def validate_requirements_bundle(raw: object) -> dict[str, object]:
    """Validate and canonicalize a requirements bundle from durable evidence."""
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            "requirements bundle must be an object",
        )
    bundle = canonical_json_object(raw)
    if set(bundle) != {"version", "sources", "bundle_digest"} or bundle.get("version") != 1:
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            "requirements bundle does not match the canonical version-1 schema",
        )
    raw_sources = bundle.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            "requirements bundle sources must be a non-empty array",
        )
    sources: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        source = _validate_requirement_source(raw_source, index=index)
        requirement_id = cast(str, source["requirement_id"])
        if requirement_id in seen_ids:
            raise ReviewEvidenceError(
                "invalid_requirements_bundle",
                f"duplicate requirement_id: {requirement_id}",
            )
        seen_ids.add(requirement_id)
        sources.append(source)
    expected_digest = _requirements_bundle_digest(sources)
    if bundle.get("bundle_digest") != expected_digest:
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            "requirements bundle digest mismatch",
        )
    bundle["sources"] = sources
    return bundle


def requirements_bundle_from_context(
    prior_round_context: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Return the validated bound bundle when one is present."""
    if prior_round_context is None:
        return None
    raw = prior_round_context.get("requirements_bundle")
    if raw is None:
        return None
    return validate_requirements_bundle(raw)


def validate_source_citation(
    raw: object,
    *,
    requirements_bundle: Mapping[str, object] | None,
    owner: str = "source citation",
) -> dict[str, object]:
    """Validate the repository/immutable-requirement citation union."""
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "invalid_source_citation",
            f"{owner} must be an object",
        )
    citation = canonical_json_object(raw)
    has_path = "path" in citation or "sha256" in citation
    has_requirement = "requirement_id" in citation or "content_sha256" in citation
    if has_path == has_requirement:
        raise ReviewEvidenceError(
            "invalid_source_citation",
            f"{owner} must match exactly one citation branch",
        )

    if has_path:
        expected = {"path", "sha256"} | _SPAN_FIELDS
        _require_exact_citation_fields(
            citation,
            required={"path", "sha256"},
            allowed=expected,
            owner=owner,
        )
        path = citation["path"]
        digest = citation["sha256"]
        if not isinstance(path, str) or not path:
            raise _invalid_citation(f"{owner}.path must be a non-empty string")
        if not is_sha256(digest):
            raise _invalid_citation(f"{owner}.sha256 must be lowercase hexadecimal SHA-256")
    else:
        expected = {"requirement_id", "content_sha256"} | _SPAN_FIELDS
        _require_exact_citation_fields(
            citation,
            required={"requirement_id", "content_sha256"},
            allowed=expected,
            owner=owner,
        )
        requirement_id = citation["requirement_id"]
        digest = citation["content_sha256"]
        if (
            not isinstance(requirement_id, str)
            or _REQUIREMENT_ID_RE.fullmatch(requirement_id) is None
        ):
            raise _invalid_citation(f"{owner}.requirement_id is invalid")
        if not is_sha256(digest):
            raise _invalid_citation(f"{owner}.content_sha256 must be lowercase hexadecimal SHA-256")
        if requirements_bundle is None:
            raise _invalid_citation(f"{owner} requires a bound requirements bundle")
        bundle = validate_requirements_bundle(requirements_bundle)
        sources = cast(list[dict[str, object]], bundle["sources"])
        source = next(
            (candidate for candidate in sources if candidate["requirement_id"] == requirement_id),
            None,
        )
        if source is None:
            raise _invalid_citation(
                f"{owner}.requirement_id is absent from the bound requirements bundle"
            )
        if source["content_sha256"] != digest:
            raise ReviewEvidenceError(
                "requirement_citation_hash_mismatch",
                f"{owner} requirement citation hash disagrees with the bound bundle",
            )

    _validate_citation_span(citation, owner=owner)
    return citation


def _requirement_source(
    *,
    source_kind: str,
    source_ref: str,
    content: str,
    details: Mapping[str, object],
) -> dict[str, object]:
    return {
        "requirement_id": _requirement_id(source_ref),
        "source_kind": source_kind,
        "source_ref": source_ref,
        **details,
        "content_sha256": _sha256(content.encode()),
        "content": content,
    }


def _validate_requirement_source(raw: object, *, index: int) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} must be an object",
        )
    source = canonical_json_object(raw)
    source_kind = source.get("source_kind")
    branch_fields = {
        "task_field": {"field"},
        "request_anchor": {"anchor_id"},
        "repository_document": {"path"},
    }
    details = branch_fields.get(str(source_kind))
    if details is None:
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} has an invalid source_kind",
        )
    required = {
        "requirement_id",
        "source_kind",
        "source_ref",
        "content_sha256",
        "content",
        *details,
    }
    if set(source) != required:
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} has non-canonical fields",
        )
    source_ref = source.get("source_ref")
    requirement_id = source.get("requirement_id")
    content = source.get("content")
    digest = source.get("content_sha256")
    if not isinstance(source_ref, str) or not source_ref:
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} has an invalid source_ref",
        )
    if requirement_id != _requirement_id(source_ref):
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} has an unstable requirement_id",
        )
    if not isinstance(content, str):
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} content must be a string",
        )
    if not isinstance(digest, str) or digest != _sha256(content.encode()):
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} content hash mismatch",
        )
    detail = next(iter(details))
    if not isinstance(source.get(detail), str) or not source[detail]:
        raise ReviewEvidenceError(
            "invalid_requirements_bundle",
            f"requirements bundle source {index} has an invalid {detail}",
        )
    return source


def _read_repository_requirement(root: Path, relative: str) -> str:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ReviewEvidenceError(
            "invalid_requirement_source",
            f"requirement source must be repository-relative: {relative}",
        )
    try:
        resolved = (root / path).resolve(strict=True)
    except FileNotFoundError:
        raise ReviewEvidenceError(
            "missing_requirement_source",
            f"designated requirement source is missing: {relative}",
        ) from None
    except OSError as exc:
        raise ReviewEvidenceError(
            "unreadable_requirement_source",
            f"designated requirement source is unreadable: {relative}",
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ReviewEvidenceError(
            "invalid_requirement_source",
            f"requirement source escapes the repository: {relative}",
        ) from None
    if not resolved.is_file():
        raise ReviewEvidenceError(
            "invalid_requirement_source",
            f"designated requirement source is not a file: {relative}",
        )
    try:
        return resolved.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewEvidenceError(
            "unreadable_requirement_source",
            f"designated requirement source is unreadable: {relative}",
        ) from exc


def _requirements_bundle_digest(sources: list[dict[str, object]]) -> str:
    descriptors = [
        {key: value for key, value in source.items() if key != "content"} for source in sources
    ]
    return _sha256(canonical_json_bytes({"version": 1, "sources": descriptors}))


def _requirement_id(source_ref: str) -> str:
    return f"req-{_sha256(source_ref.encode())[:12]}"


def _require_exact_citation_fields(
    citation: Mapping[str, object],
    *,
    required: set[str],
    allowed: set[str],
    owner: str,
) -> None:
    missing = sorted(required - set(citation))
    unknown = sorted(set(citation) - allowed)
    if missing:
        raise _invalid_citation(f"{owner}.{missing[0]} is required")
    if unknown:
        raise _invalid_citation(f"{owner} has unknown fields: {', '.join(unknown)}")


def _validate_citation_span(citation: Mapping[str, object], *, owner: str) -> None:
    start = citation.get("line_start")
    end = citation.get("line_end")
    if start is not None and (not isinstance(start, int) or isinstance(start, bool) or start < 1):
        raise _invalid_citation(f"{owner}.line_start must be a positive integer")
    if end is not None and (not isinstance(end, int) or isinstance(end, bool) or end < 1):
        raise _invalid_citation(f"{owner}.line_end must be a positive integer")
    if isinstance(start, int) and isinstance(end, int) and end < start:
        raise _invalid_citation(f"{owner}.line_end precedes line_start")


def _invalid_citation(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_source_citation", message)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_message_bytes(messages: Sequence[str]) -> bytes:
    return json.dumps(
        messages,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
