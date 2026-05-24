"""Helpers for verification evidence session variables."""

from __future__ import annotations

import logging
from typing import Any

MAX_VERIFICATION_EVIDENCE_ITEMS = 50
logger = logging.getLogger(__name__)


def append_verification_evidence(existing: Any, evidence: dict[str, Any]) -> list[Any]:
    """Append one evidence item, retaining only the newest entries."""
    if isinstance(existing, list):
        items = existing
    else:
        logger.warning(
            "Ignoring malformed verification_evidence value",
            extra={"stored_type": type(existing).__name__},
        )
        items = []
    return [*items, evidence][-MAX_VERIFICATION_EVIDENCE_ITEMS:]
