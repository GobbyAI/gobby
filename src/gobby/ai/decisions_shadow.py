"""Shadow capture for decision consumers (plan 2.1).

Each shadow run appends one JSON line to ``~/.gobby/decisions/shadow/<consumer>.jsonl``.
Writes are best effort and never raise into the consumer.

Stub: behavior lands with the Developer. The contract is pinned by
``tests/ai/test_decisions_shadow.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def record(consumer: str, record: Mapping[str, Any]) -> None:
    """Append one shadow record for ``consumer``; never raises into the caller."""
    raise NotImplementedError
