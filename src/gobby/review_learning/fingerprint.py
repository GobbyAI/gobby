"""Stable identities for review-learning findings."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SPACE_RE = re.compile(r"\s+")


def normalize_identity_text(value: Any) -> str:
    """Normalize text for stable identity derivation."""
    text = "" if value is None else str(value)
    return _SPACE_RE.sub(" ", text.strip().lower())


def short_hash(value: str, length: int = 12) -> str:
    """Return a deterministic short hash of length 4..64 for bounded tags and labels."""
    if length < 4:
        raise ValueError("short_hash length must be at least 4")
    if length > 64:
        raise ValueError("short_hash length must be at most 64")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _encode_parts(*parts: str) -> str:
    """Encode ordered identity parts without delimiter ambiguity."""
    return "".join(f"{len(part)}:{part}" for part in parts)


def derive_finding_fingerprint(finding: dict[str, Any]) -> str:
    """Return native finding fingerprint or derive a line-agnostic one."""
    native = finding.get("finding_fingerprint")
    if native:
        return str(native)

    identity_fields = [
        ("rule_id", finding.get("rule_id")),
        ("principle", finding.get("principle")),
        ("title_or_message", finding.get("title") or finding.get("message")),
        ("path", finding.get("path")),
        ("symbol", finding.get("symbol")),
        ("diagnostic_format", finding.get("diagnostic_format")),
    ]
    normalized_fields = [
        (field_name, normalize_identity_text(value)) for field_name, value in identity_fields
    ]
    if any(value for _, value in normalized_fields):
        normalized = _encode_parts(*(part for field in normalized_fields for part in field))
    else:
        canonical_finding = json.dumps(
            finding,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        normalized = normalize_identity_text(canonical_finding)
    return f"derived:{short_hash(normalized, 16)}"


def build_occurrence_key(source_review: str, finding_fingerprint: str) -> str:
    """Build the dedupe/promote occurrence identity."""
    return _encode_parts(source_review, finding_fingerprint)


def occurrence_tag(occurrence_key: str) -> str:
    """Build the bounded occurrence tag."""
    return f"occurrence:{short_hash(occurrence_key)}"


def fingerprint_tag(finding_fingerprint: str) -> str:
    """Build the bounded finding-fingerprint tag."""
    return f"fingerprint:{short_hash(finding_fingerprint)}"
