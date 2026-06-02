"""Stable identities for review-learning findings."""

from __future__ import annotations

import hashlib
import re
from typing import Any

_SPACE_RE = re.compile(r"\s+")


def normalize_identity_text(value: Any) -> str:
    """Normalize text for stable identity derivation."""
    text = "" if value is None else str(value)
    return _SPACE_RE.sub(" ", text.strip().lower())


def short_hash(value: str, length: int = 12) -> str:
    """Return a deterministic short hash for bounded tags and labels."""
    if length < 4:
        raise ValueError("short_hash length must be at least 4")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def derive_finding_fingerprint(finding: dict[str, Any]) -> str:
    """Return native finding fingerprint or derive a line-agnostic one."""
    native = finding.get("finding_fingerprint")
    if native:
        return str(native)

    identity_parts = [
        finding.get("rule_id"),
        finding.get("principle"),
        finding.get("title") or finding.get("message"),
        finding.get("path"),
        finding.get("symbol"),
        finding.get("diagnostic_format"),
    ]
    normalized = "|".join(normalize_identity_text(part) for part in identity_parts if part)
    if not normalized:
        normalized = normalize_identity_text(finding)
    return f"derived:{short_hash(normalized, 16)}"


def build_occurrence_key(source_review: str, finding_fingerprint: str) -> str:
    """Build the dedupe/promote occurrence identity."""
    return f"{source_review}:{finding_fingerprint}"


def occurrence_tag(occurrence_key: str) -> str:
    """Build the bounded occurrence tag."""
    return f"occurrence:{short_hash(occurrence_key)}"


def fingerprint_tag(finding_fingerprint: str) -> str:
    """Build the bounded finding-fingerprint tag."""
    return f"fingerprint:{short_hash(finding_fingerprint)}"
