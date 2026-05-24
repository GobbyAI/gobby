"""Helpers for verification evidence session variables."""

from __future__ import annotations

from typing import Any

MAX_VERIFICATION_EVIDENCE_ITEMS = 50


def append_verification_evidence(existing: Any, evidence: dict[str, Any]) -> list[Any]:
    """Append one evidence item, retaining only the newest entries."""
    items = existing if isinstance(existing, list) else []
    return [*items, evidence][-MAX_VERIFICATION_EVIDENCE_ITEMS:]
