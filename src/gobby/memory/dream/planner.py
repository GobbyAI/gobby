"""Dream plan construction."""

from __future__ import annotations

import json
import logging
from typing import Any

from gobby.ai.text_generation import FeatureGenerationUnavailableError
from gobby.llm.base import LLMProviderCancellation
from gobby.memory.dream.models import DreamCandidate
from gobby.memory.generation_schemas import DREAM_ACTIONS_SCHEMA
from gobby.prompts.loader import PromptLoader
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)
DEFAULT_MIN_ACTION_CONFIDENCE = 0.72
DEFAULT_MIN_DELETE_CONFIDENCE = 0.85
DEFAULT_MIN_PROMOTE_CONFIDENCE = 0.85
DEFAULT_PLANNER_BATCH_SIZE = 25
DEFAULT_PLANNER_BATCH_MAX_CHARS = 100_000
# The in-process Dream path enforces no provider-fallback deadline of its own
# (LLMService never sets total_timeout_seconds), so the planner request must
# carry the overall deadline explicitly for the work-unit ceiling to be sound.
PLANNER_TOTAL_DEADLINE_SECONDS = 1200.0
_EXPECTED_PLANNER_ERRORS = (
    json.JSONDecodeError,
    ValueError,
    TypeError,
    LLMProviderCancellation,
    FeatureGenerationUnavailableError,
    OSError,
    TimeoutError,
    ConnectionError,
)


async def build_raw_plan(
    *,
    candidates: list[DreamCandidate],
    dream_config: Any,
    llm_service: Any | None,
    db: HubDatabase,
    project_id: str | None,
    skip_consolidation: bool,
    truth_digest: str = "",
) -> dict[str, Any]:
    """Build raw planner JSON from paged LLM output.

    The planner runs over bounded pages of candidates so each LLM call carries a
    small prompt. A single oversized prompt pushed spawn-cold providers past the
    per-candidate timeout and made JSON-mode providers return empty output, which
    failed the whole run. A failure on one page is isolated so the remaining
    pages still contribute actions.
    """
    planner_errors: list[str] = []
    actions: list[dict[str, Any]] = []

    if llm_service is not None and candidates and not skip_consolidation:
        batch_size = _positive_int(
            getattr(dream_config, "planner_batch_size", DEFAULT_PLANNER_BATCH_SIZE),
            DEFAULT_PLANNER_BATCH_SIZE,
        )
        batch_max_chars = _positive_int(
            getattr(
                dream_config,
                "planner_batch_max_chars",
                DEFAULT_PLANNER_BATCH_MAX_CHARS,
            ),
            DEFAULT_PLANNER_BATCH_MAX_CHARS,
        )
        planner_pages = [
            split_page
            for page in _chunk(candidates, batch_size)
            for split_page in _split_oversized_planner_page(page, batch_max_chars)
        ]
        # Pages run serially: Dream may hold at most one of the host-wide
        # spawn-cold generation slots, leaving the rest for unrelated callers.
        for page in planner_pages:
            page_actions, error = await _run_planner_page(
                page=page,
                dream_config=dream_config,
                llm_service=llm_service,
                db=db,
                project_id=project_id,
                truth_digest=truth_digest,
            )
            actions.extend(page_actions)
            if error is not None:
                planner_errors.append(error)

    return {"actions": actions, "planner_errors": planner_errors}


async def _run_planner_page(
    *,
    page: list[DreamCandidate],
    dream_config: Any,
    llm_service: Any,
    db: HubDatabase,
    project_id: str | None,
    truth_digest: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    """Plan one page of candidates, isolating expected planner failures.

    Returns ``(actions, error)``; ``error`` is set when the page failed so the
    caller can record it without losing the other pages' actions.
    """
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


def _render_candidates_json(candidates: list[DreamCandidate]) -> str:
    """Render planner candidates once with the prompt's stable JSON format."""
    return json.dumps(
        [candidate.to_prompt_dict() for candidate in candidates],
        indent=2,
        sort_keys=True,
    )


def _split_oversized_planner_page(
    page: list[DreamCandidate],
    max_chars: int,
) -> list[list[DreamCandidate]]:
    """Recursively split a page until each batch fits the soft character limit."""
    rendered_size = len(_render_candidates_json(page))
    if rendered_size <= max_chars:
        return [page]
    if len(page) == 1:
        logger.warning(
            "Memory dream planner candidate %s renders to %d chars, exceeding "
            "planner_batch_max_chars=%d; dispatching intact",
            page[0].id,
            rendered_size,
            max_chars,
        )
        return [page]

    midpoint = len(page) // 2
    return _split_oversized_planner_page(
        page[:midpoint], max_chars
    ) + _split_oversized_planner_page(page[midpoint:], max_chars)


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
    db: HubDatabase,
    project_id: str | None,
    truth_digest: str = "",
) -> dict[str, Any]:
    loader = PromptLoader(db=db, project_id=project_id)
    prompt = loader.render(
        getattr(dream_config, "prompt_path", "memory/dream"),
        {
            "candidates": _render_candidates_json(candidates),
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
        json_schema=DREAM_ACTIONS_SCHEMA,
        max_tokens=getattr(dream_config, "max_tokens", None),
        caller="memory.dream",
        total_timeout_seconds=PLANNER_TOTAL_DEADLINE_SECONDS,
    )
    if not isinstance(response, dict):
        raise TypeError(f"memory.dream expected dict, got {type(response).__name__}")
    return response
