"""Dream plan construction."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from gobby.llm.base import LLMProviderCancellation
from gobby.llm.resolver import ProviderError
from gobby.memory.dream.models import DreamCandidate, DuplicateGroup
from gobby.prompts.loader import PromptLoader

logger = logging.getLogger(__name__)
DEFAULT_MIN_ACTION_CONFIDENCE = 0.72
DEFAULT_MIN_DELETE_CONFIDENCE = 0.85
DEFAULT_MIN_PROMOTE_CONFIDENCE = 0.85
DEFAULT_PLANNER_BATCH_SIZE = 25
DEFAULT_PLANNER_MAX_CONCURRENCY = 3
_EXPECTED_PLANNER_ERRORS = (
    json.JSONDecodeError,
    ValueError,
    TypeError,
    LLMProviderCancellation,
    ProviderError,
    OSError,
    TimeoutError,
    ConnectionError,
)


async def build_raw_plan(
    *,
    candidates: list[DreamCandidate],
    duplicate_groups: list[DuplicateGroup],
    dream_config: Any,
    llm_service: Any | None,
    db: Any | None,
    project_id: str | None,
    skip_consolidation: bool,
    truth_digest: str = "",
) -> dict[str, Any]:
    """Build raw planner JSON from LLM output plus deterministic duplicate actions.

    The planner runs over bounded pages of candidates so each LLM call carries a
    small prompt. A single oversized prompt pushed spawn-cold providers past the
    per-candidate timeout and made JSON-mode providers return empty output, which
    failed the whole run. Duplicate-group members are merged deterministically and
    excluded from the LLM batches, and a failure on one page is isolated so the
    remaining pages still contribute actions.
    """
    planner_errors: list[str] = []
    actions: list[dict[str, Any]] = []

    duplicate_ids = {memory_id for group in duplicate_groups for memory_id in group.memory_ids}
    llm_candidates = [candidate for candidate in candidates if candidate.id not in duplicate_ids]

    if llm_service is not None and llm_candidates and not skip_consolidation:
        batch_size = _positive_int(
            getattr(dream_config, "planner_batch_size", DEFAULT_PLANNER_BATCH_SIZE),
            DEFAULT_PLANNER_BATCH_SIZE,
        )
        max_concurrency = _positive_int(
            getattr(
                dream_config,
                "planner_max_concurrency",
                DEFAULT_PLANNER_MAX_CONCURRENCY,
            ),
            DEFAULT_PLANNER_MAX_CONCURRENCY,
        )
        semaphore = asyncio.Semaphore(max_concurrency)
        page_results = await asyncio.gather(
            *(
                _run_planner_page(
                    page=page,
                    dream_config=dream_config,
                    llm_service=llm_service,
                    db=db,
                    project_id=project_id,
                    semaphore=semaphore,
                    truth_digest=truth_digest,
                )
                for page in _chunk(llm_candidates, batch_size)
            )
        )
        for page_actions, error in page_results:
            actions.extend(page_actions)
            if error is not None:
                planner_errors.append(error)

    if not skip_consolidation:
        actions.extend(_duplicate_merge_actions(duplicate_groups, actions))

    return {"actions": actions, "planner_errors": planner_errors}


async def _run_planner_page(
    *,
    page: list[DreamCandidate],
    dream_config: Any,
    llm_service: Any,
    db: Any | None,
    project_id: str | None,
    semaphore: asyncio.Semaphore,
    truth_digest: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    """Plan one page of candidates, isolating expected planner failures.

    Returns ``(actions, error)``; ``error`` is set when the page failed so the
    caller can record it without losing the other pages' actions.
    """
    async with semaphore:
        try:
            response = await _call_llm_planner(
                candidates=page,
                dream_config=dream_config,
                llm_service=llm_service,
                db=db,
                project_id=project_id,
                truth_digest=truth_digest,
            )
        except _EXPECTED_PLANNER_ERRORS as exc:
            logger.warning("Memory dream planner unavailable: %s", exc)
            return [], str(exc)
    return _planner_response_actions(response, page, project_id), None


def _planner_response_actions(
    response: dict[str, Any],
    candidates: list[DreamCandidate],
    project_id: str | None,
) -> list[dict[str, Any]]:
    """Extract valid dict actions from a planner response, logging anomalies."""
    raw_actions = response.get("actions", []) if isinstance(response, dict) else []
    if isinstance(raw_actions, list):
        invalid_actions = [item for item in raw_actions if not isinstance(item, dict)]
        if invalid_actions:
            logger.warning(
                "Memory dream planner returned non-dict actions",
                extra={
                    "invalid_actions": invalid_actions,
                    "project_id": project_id,
                    "candidate_ids": [candidate.id for candidate in candidates],
                },
            )
        return [item for item in raw_actions if isinstance(item, dict)]
    if raw_actions:
        logger.warning(
            "Memory dream planner returned invalid actions payload",
            extra={
                "raw_actions": raw_actions,
                "project_id": project_id,
                "candidate_ids": [candidate.id for candidate in candidates],
            },
        )
    return []


def _chunk(items: list[DreamCandidate], size: int) -> list[list[DreamCandidate]]:
    """Split candidates into bounded pages of at most ``size`` items."""
    return [items[index : index + size] for index in range(0, len(items), size)]


def _positive_int(value: Any, default: int) -> int:
    """Coerce a config value to a positive int, falling back to ``default``."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 1 else default


async def _call_llm_planner(
    *,
    candidates: list[DreamCandidate],
    dream_config: Any,
    llm_service: Any,
    db: Any | None,
    project_id: str | None,
    truth_digest: str = "",
) -> dict[str, Any]:
    loader = PromptLoader(db=db, project_id=project_id)
    prompt = loader.render(
        getattr(dream_config, "prompt_path", "memory/dream"),
        {
            "candidates": json.dumps(
                [candidate.to_prompt_dict() for candidate in candidates],
                indent=2,
                sort_keys=True,
            ),
            "truth_digest": truth_digest or "(no current-truth digest available)",
            "min_action_confidence": getattr(
                dream_config,
                "min_action_confidence",
                DEFAULT_MIN_ACTION_CONFIDENCE,
            ),
            "min_delete_confidence": getattr(
                dream_config,
                "min_delete_confidence",
                DEFAULT_MIN_DELETE_CONFIDENCE,
            ),
            "min_rescope_confidence": getattr(
                dream_config,
                "min_rescope_confidence",
                DEFAULT_MIN_PROMOTE_CONFIDENCE,
            ),
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
