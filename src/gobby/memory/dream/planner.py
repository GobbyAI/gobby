"""Dream plan construction."""

from __future__ import annotations

import json
import logging
from typing import Any

from gobby.memory.dream.models import DreamCandidate, DuplicateGroup

logger = logging.getLogger(__name__)


async def build_raw_plan(
    *,
    candidates: list[DreamCandidate],
    duplicate_groups: list[DuplicateGroup],
    dream_config: Any,
    llm_service: Any | None,
    db: Any | None,
    project_id: str | None,
    skip_consolidation: bool,
) -> dict[str, Any]:
    """Build raw planner JSON from LLM output plus deterministic duplicate actions."""
    planner_errors: list[str] = []
    actions: list[dict[str, Any]] = []

    if llm_service is not None and candidates and not skip_consolidation:
        try:
            response = await _call_llm_planner(
                candidates=candidates,
                duplicate_groups=duplicate_groups,
                dream_config=dream_config,
                llm_service=llm_service,
                db=db,
                project_id=project_id,
            )
            raw_actions = response.get("actions", []) if isinstance(response, dict) else []
            if isinstance(raw_actions, list):
                actions.extend(item for item in raw_actions if isinstance(item, dict))
        except Exception as exc:  # noqa: BLE001 - invalid planner output becomes review
            planner_errors.append(str(exc))
            logger.warning("Memory dream planner unavailable: %s", exc)

    if not skip_consolidation:
        actions.extend(_duplicate_merge_actions(duplicate_groups, actions))

    return {"actions": actions, "planner_errors": planner_errors}


async def _call_llm_planner(
    *,
    candidates: list[DreamCandidate],
    duplicate_groups: list[DuplicateGroup],
    dream_config: Any,
    llm_service: Any,
    db: Any | None,
    project_id: str | None,
) -> dict[str, Any]:
    from gobby.prompts.loader import PromptLoader

    loader = PromptLoader(db=db, project_id=project_id)
    prompt = loader.render(
        getattr(dream_config, "prompt_path", "memory/dream"),
        {
            "candidates": json.dumps(
                [candidate.to_prompt_dict() for candidate in candidates],
                indent=2,
                sort_keys=True,
            ),
            "duplicate_groups": json.dumps(
                [group.to_prompt_dict() for group in duplicate_groups],
                indent=2,
                sort_keys=True,
            ),
            "min_action_confidence": getattr(dream_config, "min_action_confidence", 0.72),
            "min_delete_confidence": getattr(dream_config, "min_delete_confidence", 0.85),
        },
    )
    response = await llm_service.call_json_feature(
        dream_config,
        prompt,
        caller="memory.dream",
    )
    if not isinstance(response, dict):
        raise TypeError(f"memory.dream expected dict, got {type(response).__name__}")
    return response


def _duplicate_merge_actions(
    duplicate_groups: list[DuplicateGroup],
    existing_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    referenced = _referenced_ids(existing_actions)
    actions: list[dict[str, Any]] = []
    for group in duplicate_groups:
        if referenced.intersection(group.memory_ids):
            continue
        actions.append(
            {
                "action": "merge",
                "memory_ids": group.memory_ids,
                "canonical_content": group.canonical_content,
                "confidence": 1.0,
                "reason": group.reason,
            }
        )
    return actions


def _referenced_ids(actions: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for action in actions:
        memory_id = action.get("memory_id", action.get("id"))
        if memory_id:
            ids.add(str(memory_id))
        memory_ids = action.get("memory_ids")
        if isinstance(memory_ids, list):
            ids.update(str(item) for item in memory_ids if item)
    return ids
