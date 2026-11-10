"""Typed-input latch records: the pending cells one typed write may have reached (#25856)."""

from __future__ import annotations

import re
from typing import Any, NamedTuple

_SCRIPT_FAILED = "Script failed"


class TypedLatch(NamedTuple):
    """One typed call's latch: the session it typed into and the cells it marked."""

    session_id: str | None
    outer_call_ids: tuple[str, ...]


def _output_texts(output: Any) -> list[str]:
    if isinstance(output, str):
        return [output]
    if isinstance(output, list):
        return [
            block["text"]
            for block in output
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
    return []


def failed_typed_write(output: Any, session_id: str | None) -> bool:
    """Whether a typed cell failed structurally: its script failed on an unknown session.

    A failed script never reached a shell, so its latch clears. The "Unknown process id"
    naming the same session confirms the failure. Any other typed output keeps its latch.
    """
    texts = _output_texts(output)
    if session_id is None or not texts or not texts[0].startswith(_SCRIPT_FAILED):
        return False
    unknown = rf"Unknown process id {re.escape(session_id)}\b"
    return re.search(unknown, "\n".join(texts)) is not None
