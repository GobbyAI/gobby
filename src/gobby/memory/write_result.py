"""Typed outcomes for memory primary writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MemoryWriteOutcome = Literal["created", "reactivated", "deduped", "updated", "unchanged"]


@dataclass(frozen=True, slots=True)
class MemoryWriteResult[PayloadT]:
    """A memory payload paired with the primary-write outcome."""

    memory: PayloadT
    outcome: MemoryWriteOutcome
