"""Repository source-citation validation for plan-review lanes and findings.

Reviewers cite the repository files they actually read (path + sha256 +
optional line span); coverage validation rehashes every cited path against
the working tree and refuses drifted or out-of-tree citations.
"""

from __future__ import annotations

from collections.abc import Mapping

from gobby.plans.review_evidence_models import ReviewEvidenceError, canonical_json_object
from gobby.utils.hashing import is_sha256

_SPAN_FIELDS = {"line_start", "line_end"}


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
        required={"path", "sha256"},
        allowed={"path", "sha256"} | _SPAN_FIELDS,
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
