"""Repository source-citation validation for plan-review lanes and findings.

Reviewers cite the repository files they actually read (path + sha256 +
optional line span); coverage validation rehashes every cited path against
the working tree and refuses drifted or out-of-tree citations.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from gobby.plans.review_evidence_models import ReviewEvidenceError, canonical_json_object
from gobby.utils.hashing import is_sha256

_REQUIRED_CITATION_FIELDS = ("path", "sha256")
_SPAN_FIELDS = ("line_start", "line_end")
_CITATION_FIELD_SHAPES: dict[str, dict[str, object]] = {
    "path": {
        "type": "string",
        "description": "Repository-relative path of a file this lane actually read.",
    },
    "sha256": {
        "type": "string",
        "description": "Lowercase hexadecimal SHA-256 of the cited file's current bytes.",
    },
    "line_start": {
        "type": "integer",
        "minimum": 1,
        "description": "Optional span start.",
    },
    "line_end": {
        "type": "integer",
        "minimum": 1,
        "description": "Optional span end; must not precede line_start.",
    },
}
_CITATION_FIELDS = _REQUIRED_CITATION_FIELDS + _SPAN_FIELDS


def source_citation_schema() -> dict[str, object]:
    """Describe a citation exactly as :func:`validate_source_citation` accepts it."""
    return {
        "type": "object",
        "description": (
            "A repository file this lane read; coverage rehashes every cited path "
            "against the working tree."
        ),
        "properties": {
            field: deepcopy(_CITATION_FIELD_SHAPES[field]) for field in _CITATION_FIELDS
        },
        "required": list(_REQUIRED_CITATION_FIELDS),
    }


def validate_source_citation(
    raw: object,
    *,
    owner: str = "source citation",
) -> dict[str, object]:
    """Validate a repository citation: path, sha256, and an optional span."""
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "invalid_source_citation",
            f"{owner} must be an object",
        )
    citation = canonical_json_object(raw)
    _require_exact_citation_fields(
        citation,
        required=set(_REQUIRED_CITATION_FIELDS),
        allowed=set(_CITATION_FIELDS),
        owner=owner,
    )
    path = citation["path"]
    digest = citation["sha256"]
    if not isinstance(path, str) or not path:
        raise _invalid_citation(f"{owner}.path must be a non-empty string")
    if not is_sha256(digest):
        raise _invalid_citation(f"{owner}.sha256 must be lowercase hexadecimal SHA-256")
    _validate_citation_span(citation, owner=owner)
    return citation


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
