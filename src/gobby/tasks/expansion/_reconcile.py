"""Match a new expansion spec to leaves a prior run already created."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.plans._task_refs import is_placeholder_task_ref
from gobby.storage.expansion_runs import ExpansionRun
from gobby.tasks.expansion._reset import (
    _has_progressed_stage_state,
    _is_closed_obsolete_task,
    _task_exists,
)


@dataclass(frozen=True)
class SectionBinding:
    """One prior or new leaf named in the reconciliation report."""

    section_id: str
    task_id: str
    reason: str | None = None

    def to_dict(self) -> dict[str, str]:
        row = {"section_id": self.section_id, "task_id": self.task_id}
        if self.reason is not None:
            row["reason"] = self.reason
        return row


@dataclass(frozen=True)
class AdoptedLeaf:
    """A spec entry that keeps an existing task instead of creating one."""

    task_id: str
    action: str
    reason: str | None = None


@dataclass(frozen=True)
class SkippedEdge:
    """A spec edge that would have changed a protected leaf's blockers."""

    section_id: str
    task_id: str
    depends_on: str

    def to_dict(self) -> dict[str, str]:
        return {
            "section_id": self.section_id,
            "task_id": self.task_id,
            "depends_on": self.depends_on,
        }


@dataclass
class ReconciliationPlan:
    """Section matches for one re-expansion apply."""

    prior_by_section: dict[str, str]
    removed: list[SectionBinding]
    preserved: list[SectionBinding]
    reused: list[SectionBinding]
    created: list[SectionBinding]
    untouched: list[SectionBinding]
    skipped_edges: list[SkippedEdge]

    def checkpoint(self) -> dict[str, list[dict[str, str]]]:
        return {
            "preserved": [row.to_dict() for row in self.preserved],
            "reused": [row.to_dict() for row in self.reused],
            "created": [row.to_dict() for row in self.created],
            "removed": [row.to_dict() for row in self.removed],
            "untouched": [row.to_dict() for row in self.untouched],
            "skipped_edges": [row.to_dict() for row in self.skipped_edges],
        }


def can_reconcile(prior: ExpansionRun, spec: dict[str, Any]) -> bool:
    """True when both specs identify every leaf by source section."""
    new_tasks = spec.get("tasks") or []
    old_tasks = (prior.compiled_spec or {}).get("tasks") or []
    if not new_tasks or not old_tasks:
        return False
    return all(_section_id(task) for task in new_tasks) and all(
        _section_id(task) for task in old_tasks
    )


def prepare(self: Any, prior: ExpansionRun, spec: dict[str, Any]) -> ReconciliationPlan:
    """Index live prior leaves and report sections the new spec dropped."""
    mapping = prior.task_id_map or {}
    prior_by_section: dict[str, str] = {}
    for item in (prior.compiled_spec or {}).get("tasks") or []:
        section_id = _section_id(item)
        task_id = mapping.get(item.get("id"))
        if section_id is None or not isinstance(task_id, str):
            continue
        if not _task_exists(self, task_id) or _is_closed_obsolete_task(self, task_id):
            continue
        prior_by_section[section_id] = task_id

    new_sections = {
        section_id
        for item in spec.get("tasks") or []
        if (section_id := _section_id(item)) is not None
    }
    removed = [
        SectionBinding(section_id=section_id, task_id=task_id)
        for section_id, task_id in sorted(prior_by_section.items())
        if section_id not in new_sections
    ]
    kept_placeholders = _placeholder_sections(spec)
    for section_id, task_id in sorted(prior_deferral_map(prior).items()):
        if section_id in kept_placeholders:
            continue
        if not _task_exists(self, task_id) or _is_closed_obsolete_task(self, task_id):
            continue
        removed.append(SectionBinding(section_id=section_id, task_id=task_id))
    return ReconciliationPlan(
        prior_by_section=prior_by_section,
        removed=removed,
        preserved=[],
        reused=[],
        created=[],
        untouched=[],
        skipped_edges=[],
    )


def adopt(
    self: Any,
    conn: Any,
    plan: ReconciliationPlan,
    task_item: dict[str, Any],
) -> AdoptedLeaf | None:
    """Keep a prior leaf for this section, or return None so apply creates one.

    The caller is the apply transaction. The row lock is held until that
    transaction commits, including through ``update_reused_leaf``.
    """
    section_id = _section_id(task_item)
    if section_id is None:
        return None
    task_id = plan.prior_by_section.get(section_id)
    if task_id is None:
        return None
    row = conn.execute(
        """
        SELECT closed_at, closed_reason, claimed_by_session_id
        FROM tasks
        WHERE id = %s
        FOR UPDATE
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    binding = SectionBinding(section_id=section_id, task_id=task_id)
    if row["closed_at"] is not None:
        if row["closed_reason"] == "obsolete":
            return None
        plan.preserved.append(binding)
        return AdoptedLeaf(task_id=task_id, action="preserve")
    if row["claimed_by_session_id"]:
        claimed = SectionBinding(section_id=section_id, task_id=task_id, reason="claimed")
        plan.untouched.append(claimed)
        return AdoptedLeaf(task_id=task_id, action="untouched", reason="claimed")
    if _has_progressed_stage_state(self, task_id):
        active = SectionBinding(section_id=section_id, task_id=task_id, reason="in_progress")
        plan.untouched.append(active)
        return AdoptedLeaf(task_id=task_id, action="untouched", reason="in_progress")
    return AdoptedLeaf(task_id=task_id, action="update")


def record_reused(plan: ReconciliationPlan, task_item: dict[str, Any], task_id: str) -> None:
    section_id = _section_id(task_item)
    if section_id is not None:
        plan.reused.append(SectionBinding(section_id=section_id, task_id=task_id))


def record_created(plan: ReconciliationPlan, task_item: dict[str, Any], task_id: str) -> None:
    section_id = _section_id(task_item)
    if section_id is not None:
        plan.created.append(SectionBinding(section_id=section_id, task_id=task_id))


def record_skipped_edge(
    plan: ReconciliationPlan,
    task_item: dict[str, Any],
    task_id: str,
    depends_on: str,
) -> None:
    section_id = _section_id(task_item)
    if section_id is not None:
        plan.skipped_edges.append(
            SkippedEdge(section_id=section_id, task_id=task_id, depends_on=depends_on)
        )


def prior_deferral_map(prior: ExpansionRun) -> dict[str, str]:
    """Section ids of placeholder holders the prior run recorded."""
    raw = (prior.checkpoints or {}).get("deferral_task_map") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        section_id: task_id
        for section_id, task_id in raw.items()
        if isinstance(section_id, str) and isinstance(task_id, str)
    }


def reusable_phase_id(self: Any, prior: ExpansionRun, phase_id: str) -> str | None:
    """Return the phase epic a prior run created when it still exists."""
    checkpoints = prior.checkpoints or {}
    phase_map = checkpoints.get("phase_parent_map") or {}
    task_id = phase_map.get(phase_id)
    if not isinstance(task_id, str):
        return None
    if not _task_exists(self, task_id) or _is_closed_obsolete_task(self, task_id):
        return None
    return task_id


def update_reused_leaf(
    self: Any,
    *,
    task_id: str,
    task_item: dict[str, Any],
    plan_ref_block: str,
    labels: list[str] | None,
) -> None:
    """Refresh an open unclaimed leaf from the amended section text."""
    raw_description = task_item.get("description") or ""
    description = f"{plan_ref_block}{raw_description}" if plan_ref_block else raw_description
    self.task_manager.update_task(
        task_id,
        title=task_item["title"],
        description=description or None,
        priority=task_item.get("priority", 2),
        task_type=task_item.get("task_type", "task"),
        category=task_item.get("category"),
        validation_criteria=task_item.get("validation"),
        labels=labels,
        assigned_agent=task_item.get("assigned_agent"),
        implementation_domain=task_item.get("implementation_domain"),
        additional_skills=task_item.get("additional_skills"),
    )
    affected_files = task_item.get("affected_files") or []
    if affected_files:
        self.af_manager.set_files(task_id, affected_files, "expansion")


def _placeholder_sections(spec: dict[str, Any]) -> set[str]:
    sections: set[str] = set()
    for deferral in spec.get("deferrals") or []:
        if not isinstance(deferral, dict):
            continue
        task_ref = str(deferral.get("task_ref") or "")
        section_id = deferral.get("section_id")
        if isinstance(section_id, str) and is_placeholder_task_ref(task_ref):
            sections.add(section_id)
    return sections


def _section_id(task_item: dict[str, Any]) -> str | None:
    section_id = task_item.get("source_section_id")
    if not isinstance(section_id, str) or not section_id:
        return None
    return section_id
