"""Task records and the ref-keyed store that plan coverage evaluates against."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gobby.plans._task_record_state import coerce_task_state
from gobby.plans._task_refs import normalize_task_ref
from gobby.tasks.state_semantics import projected_task_state

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import Task


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


def load_task_records(
    project_id: str,
    *,
    db: HubDatabase,
) -> tuple[TaskRecord, ...]:
    from gobby.storage.tasks import LocalTaskManager

    manager = LocalTaskManager(db)
    tasks = []
    limit = 1000
    offset = 0
    while True:
        # sort_by="created_at" pages in SQL (LIMIT/OFFSET). The default
        # hierarchy sort materializes and re-sorts the entire project task set
        # for every page — O(pages x project) CPU inside the daemon, which is
        # enough to starve the event loop on large projects (#19878). Coverage
        # evaluation is order-independent, so take the cheap deterministic order.
        page = manager.list_tasks(
            project_id=project_id,
            limit=limit,
            offset=offset,
            sort_by="created_at",
        )
        tasks.extend(page)
        if len(page) < limit:
            break
        offset += limit
    task_ref_by_id = {task.id: _live_task_ref(task) for task in tasks}
    return tuple(_live_task_record(task, task_ref_by_id) for task in tasks)


def _live_task_record(task: Task, task_ref_by_id: Mapping[str, str]) -> TaskRecord:
    labels = task.labels or ()
    dependencies = tuple(
        sorted(
            task_ref_by_id[depends_on]
            for depends_on in task.blocked_by
            if depends_on in task_ref_by_id
        )
    )
    parent_ref = (
        task_ref_by_id.get(task.parent_task_id) if task.parent_task_id is not None else None
    )
    return TaskRecord(
        ref=_live_task_ref(task),
        labels=tuple(str(label) for label in labels),
        validation_criteria=task.validation_criteria or task.description or "",
        state=projected_task_state(task),
        parent_ref=parent_ref,
        path_cache=task.path_cache,
        dependencies=dependencies,
        closed_reason=_optional_string(task.closed_reason),
    )


def _live_task_ref(task: Task) -> str:
    seq_num = task.seq_num
    if isinstance(seq_num, int):
        return f"#{seq_num}"
    return str(task.id)


def coerce_task_record(raw: Mapping[str, object]) -> TaskRecord:
    ref = _task_ref(raw)
    return TaskRecord(
        ref=ref,
        labels=_labels(raw.get("labels")),
        validation_criteria=_first_string(raw, "validation_criteria", "validation", "description"),
        state=coerce_task_state(raw.get("state")),
        parent_ref=_optional_ref(
            _first_string(raw, "parent_ref", "parent_task_ref", "parent", default="")
        ),
        path_cache=_optional_string(raw.get("path_cache")),
        dependencies=_dependency_refs(raw.get("dependencies")),
        closed_reason=_optional_string(raw.get("closed_reason")),
    )


def _task_ref(raw: Mapping[str, object]) -> str:
    for key in ("ref", "task_ref", "id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return normalize_task_ref(value)
    seq_num = raw.get("seq_num")
    if isinstance(seq_num, int):
        return f"#{seq_num}"
    return ""


def _labels(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(label.strip() for label in raw.split(",") if label.strip())
    if isinstance(raw, Sequence):
        return tuple(str(label) for label in raw)
    return ()


def _dependency_refs(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    values: list[str] = []
    if isinstance(raw, Mapping):
        for key in ("blocked_by", "depends_on", "dependencies"):
            values.extend(_dependency_refs(raw.get(key)))
        return tuple(values)
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        for item in raw:
            if isinstance(item, str):
                values.append(normalize_task_ref(item))
            elif isinstance(item, Mapping):
                ref = item.get("ref") or item.get("depends_on") or item.get("task_ref")
                if isinstance(ref, str):
                    values.append(normalize_task_ref(ref))
        return tuple(values)
    if isinstance(raw, str):
        return (normalize_task_ref(raw),)
    return ()


def _first_string(raw: Mapping[str, object], *keys: str, default: str = "") -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return default


def _optional_ref(raw: str) -> str | None:
    return normalize_task_ref(raw) if raw else None


def _optional_string(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw)
    return value if value else None


__all__ = ["TaskRecord", "TaskRecordStore", "coerce_task_record", "load_task_records"]
