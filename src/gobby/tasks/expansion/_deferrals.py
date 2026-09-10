"""Deferral task creation during contract-plan expansion apply."""

from __future__ import annotations

from typing import Any

from gobby.plans._task_refs import is_placeholder_task_ref
from gobby.storage.hub.protocol import Transaction
from gobby.storage.tasks import Task
from gobby.storage.tasks._creation import _create_task_in_transaction


def create_placeholder_deferral_tasks(
    self: Any,
    conn: Transaction,
    *,
    spec: dict[str, Any],
    parent: Task,
    session_id: str | None,
    provenance_label: str,
    plan_ref_block: str,
) -> dict[str, str]:
    """Create the task each placeholder ``kind: deferred`` section still lacks.

    The Plan-Coverage Contract creates deferral tasks at expansion: a section whose
    ``task_ref`` is not a real ``#N`` reference gets one task under the root epic,
    carrying ``deferred-from:<plan-id>:<section-id>`` provenance, validation criteria
    that duplicate its original acceptance items, and a ``blocked_by`` edge from the
    root epic so it sits in the epic's dependency closure. A numeric ``task_ref`` is
    left alone; expansion-qa reports a dangling one as ``task_missing``.

    Returns ``{section_id: created_task_id}``.
    """
    plan_id = spec.get("plan_id")
    if not plan_id:
        return {}

    created: dict[str, str] = {}
    for deferral in spec.get("deferrals") or []:
        task_ref = str(deferral.get("task_ref") or "")
        if not is_placeholder_task_ref(task_ref):
            continue
        section_id = str(deferral["section_id"])
        item_lines = "\n".join(
            f"- {item['item_id']}: {item.get('prose') or item['item_id']}"
            for item in deferral.get("original_acceptance_items") or []
        )
        description = (
            f"{plan_ref_block}"
            f"Deferred from plan `{plan_id}` section {section_id}; "
            "created at expansion apply.\n\n"
            f"Reason: {deferral.get('reason', '')}\n"
            f"Owner: {deferral.get('owner', '')}\n\n"
            f"Original acceptance items:\n{item_lines}"
        )
        validation = (
            f"Deliver the acceptance items deferred from plan `{plan_id}` "
            f"section {section_id}:\n{item_lines}"
        )
        created_id = _create_task_in_transaction(
            self.db,
            conn,
            project_id=parent.project_id,
            title=str(deferral.get("title") or f"Deferred work from section {section_id}"),
            description=description,
            parent_task_id=parent.id,
            created_in_session_id=session_id,
            category="planning",
            validation_criteria=validation,
            labels=[f"deferred-from:{plan_id}:{section_id}", provenance_label],
        )
        self._add_dependency(parent.id, created_id)
        created[section_id] = created_id
    return created


__all__ = ["create_placeholder_deferral_tasks"]
