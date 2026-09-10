"""Task records and the ref-keyed store that plan coverage evaluates against."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from gobby.plans._task_refs import normalize_task_ref


@dataclass(frozen=True)
class TaskRecord:
    ref: str
    labels: tuple[str, ...]
    validation_criteria: str
    state: str
    parent_ref: str | None = None
    path_cache: str | None = None
    dependencies: tuple[str, ...] = ()
    # Deferral validation reads this to tell a target that delivered its
    # obligation from one that was abandoned; both serialize as state "closed".
    closed_reason: str | None = None


class TaskRecordStore:
    def __init__(self, records: Sequence[TaskRecord]) -> None:
        self._by_ref = {record.ref: record for record in records}

    def get_task(self, task_ref: str) -> dict[str, object] | None:
        record = self._by_ref.get(normalize_task_ref(task_ref))
        if record is None:
            return None
        return {
            "state": record.state,
            "closed_reason": record.closed_reason,
            "validation_criteria": record.validation_criteria,
            "labels": list(record.labels),
            "dependencies": list(record.dependencies),
        }

    def get_task_labels(self, task_ref: str) -> list[str]:
        record = self._by_ref.get(normalize_task_ref(task_ref))
        return list(record.labels) if record is not None else []

    def get_task_dependencies(self, task_ref: str) -> list[str]:
        record = self._by_ref.get(normalize_task_ref(task_ref))
        return list(record.dependencies) if record is not None else []

    def find_task_ref_by_label(self, label: str) -> str | None:
        """Return the one task carrying ``label``; ``None`` when absent or ambiguous."""
        matches = [record.ref for record in self._by_ref.values() if label in record.labels]
        if len(matches) != 1:
            return None
        return matches[0]


__all__ = ["TaskRecord", "TaskRecordStore"]
