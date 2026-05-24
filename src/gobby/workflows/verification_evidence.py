"""Helpers for verification evidence session variables."""

from __future__ import annotations

import logging
from typing import Any

MAX_VERIFICATION_EVIDENCE_ITEMS = 50
logger = logging.getLogger(__name__)


def append_verification_evidence(
    existing: list[Any] | None,
    evidence: dict[str, Any],
    *,
    session_id: str | None = None,
) -> list[Any]:
    """Append one evidence item, retaining only the newest entries."""
    if isinstance(existing, list):
        items = existing
    elif existing is None:
        items = []
    else:
        logger.warning(
            "Ignoring malformed verification_evidence value",
            extra={"stored_type": type(existing).__name__, "session_id": session_id},
        )
        items = []
    return [*items, evidence][-MAX_VERIFICATION_EVIDENCE_ITEMS:]
