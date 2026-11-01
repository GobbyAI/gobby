"""LLM confirmation of found-work permission-deferral candidates."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_SHIRK_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "block": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["block", "reason"],
    "additionalProperties": False,
}
_SHIRK_SYSTEM_PROMPT = """You classify one coding-agent stop attempt against the found-work ladder.
Return block=true only when the assistant describes an actionable defect it could address and
ends by asking the user for permission instead of following the supplied Found Work ladder.
Return block=false for destructive-action confirmations, user-reserved decisions, explicit user
deferral instructions, assessment-only deliverables, or enhancement ideas with nothing broken.
Use only the supplied text and evidence. Return JSON matching the schema."""


async def confirm_shirk(
    message: str,
    user_prompt: str,
    *,
    llm_service: Any | None,
    daemon_config: Any | None,
) -> bool | None:
    """LLM verdict on a fast-path candidate.

    ``True`` confirms the shirk, ``False`` clears it, and ``None`` means no
    confirmation was available (no service, validation disabled, timeout,
    malformed payload) — the caller then alerts on the fast-path verdict.
    """
    if llm_service is None or daemon_config is None:
        return None
    try:
        validation = daemon_config.get_gobby_tasks_config().validation
        if not validation.enabled:
            return None
        prompt = (
            f"USER INSTRUCTION:\n{user_prompt or '(unavailable)'}\n\n"
            f"FINAL ASSISTANT MESSAGE:\n{message}"
        )
        payload = await llm_service.call_json_feature(
            validation,
            prompt,
            system_prompt=_SHIRK_SYSTEM_PROMPT,
            json_schema=_SHIRK_CONFIRM_SCHEMA,
            caller="workflows.found_work_gate",
            total_timeout_seconds=min(
                float(validation.confirmation_timeout_seconds),
                8.0,
            ),
        )
    except Exception:
        logger.debug(
            "Found-work shirk confirmation unavailable; using fast-path verdict", exc_info=True
        )
        return None
    if isinstance(payload, Mapping) and isinstance(payload.get("block"), bool):
        return payload["block"] is True
    return None
