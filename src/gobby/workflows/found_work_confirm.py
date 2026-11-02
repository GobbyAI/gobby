"""Found-work shirk confirmation: a classifier settles confident cases, the LLM decides the rest."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gobby.ai import decisions_shadow
from gobby.ai.decisions import DecisionsUnavailable, get_decision_service

logger = logging.getLogger(__name__)

_CONSUMER = "found_work"
_PROPOSITION = (
    "The final assistant message defers, declines, or hands back work the user instruction "
    "asked for, without doing it."
)
_MAX_CONFIRMATION_SECONDS = 8.0
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


@dataclass(frozen=True)
class _Outcome:
    """One classifier answer, or the reason it produced none."""

    status: str
    probability: float | None = None
    backend_identity: str | None = None
    reason: str | None = None


async def confirm_shirk(
    message: str,
    user_prompt: str,
    *,
    llm_service: Any | None,
    daemon_config: Any | None,
) -> bool | None:
    """Confirm a fast-path candidate: ``True`` confirms, ``False`` clears, ``None`` is unavailable.

    ``found_work.mode`` picks the path. ``off`` asks the LLM alone. ``shadow`` runs the
    classifier beside the LLM and returns the LLM verdict. ``enforce`` settles a
    confident classifier answer without the LLM and escalates everything else. All
    calls share one budget of ``min(confirmation_timeout_seconds, 8.0)`` seconds.
    """
    if llm_service is None or daemon_config is None:
        return None
    try:
        validation = daemon_config.get_gobby_tasks_config().validation
        if not validation.enabled:
            return None
        decisions = daemon_config.ai.decisions
        cap = min(float(validation.confirmation_timeout_seconds), _MAX_CONFIRMATION_SECONDS)
    except Exception:
        logger.debug(
            "Found-work shirk confirmation unavailable; using fast-path verdict", exc_info=True
        )
        return None
    policy = decisions.found_work
    if policy.mode == "off":
        return await _llm_verdict(llm_service, validation, message, user_prompt, cap)
    deadline = asyncio.get_running_loop().time() + cap
    if policy.mode == "enforce" and policy.evaluated_model == decisions.model:
        return await _enforce(llm_service, validation, decisions, message, user_prompt, deadline)
    return await _shadow(llm_service, validation, decisions, message, user_prompt, cap)


async def _enforce(
    llm_service: Any,
    validation: Any,
    decisions: Any,
    message: str,
    user_prompt: str,
    deadline: float,
) -> bool | None:
    policy = decisions.found_work
    outcome = await _classify(decisions, message, user_prompt, _remaining(deadline))
    if outcome.status == "ok" and outcome.backend_identity != policy.evaluated_backend:
        logger.warning("Found-work classifier backend is not the evaluated backend; using LLM")
        verdict = await _llm_verdict(
            llm_service, validation, message, user_prompt, _remaining(deadline)
        )
        _record_shadow(decisions, message, user_prompt, outcome, verdict)
        return verdict
    if outcome.status == "ok":
        settled = _settle(outcome.probability, policy)
        if settled is not None:
            return settled
    return await _llm_verdict(llm_service, validation, message, user_prompt, _remaining(deadline))


async def _shadow(
    llm_service: Any,
    validation: Any,
    decisions: Any,
    message: str,
    user_prompt: str,
    cap: float,
) -> bool | None:
    # Each child is bounded by cap; an outer timeout would drop a finished LLM verdict.
    async with asyncio.TaskGroup() as group:
        classified = group.create_task(_classify(decisions, message, user_prompt, cap))
        verdict = group.create_task(
            _llm_verdict(llm_service, validation, message, user_prompt, cap)
        )
    _record_shadow(decisions, message, user_prompt, classified.result(), verdict.result())
    return verdict.result()


async def _classify(
    decisions: Any,
    message: str,
    user_prompt: str,
    timeout: float,
) -> _Outcome:
    """Ask the classifier for the found-work probability; every failure is unavailable."""
    state = {"final_assistant_message": message, "user_instruction": user_prompt}
    try:
        service = get_decision_service(decisions)
        async with asyncio.timeout(timeout):
            result = await service.noul(
                _CONSUMER,
                state,
                {_CONSUMER: _PROPOSITION},
                timeout_seconds=timeout,
            )
        answer = result.answers[_CONSUMER]
    except DecisionsUnavailable as error:
        return _Outcome("unavailable", reason=error.reason)
    except TimeoutError:
        return _Outcome("unavailable", reason="timeout")
    except Exception:
        logger.warning("Found-work classifier failed", exc_info=True)
        return _Outcome("unavailable", reason="error")
    return _Outcome("ok", probability=answer.probability, backend_identity=result.backend_identity)


def _settle(probability: float | None, policy: Any) -> bool | None:
    if probability is None:
        return None
    if probability >= policy.accept_above:
        return True
    if probability <= policy.accept_below:
        return False
    return None


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - asyncio.get_running_loop().time())


async def _llm_verdict(
    llm_service: Any,
    validation: Any,
    message: str,
    user_prompt: str,
    timeout: float,
) -> bool | None:
    """LLM verdict on a fast-path candidate; ``None`` when no verdict arrives in time."""
    if timeout <= 0:
        return None
    prompt = (
        f"USER INSTRUCTION:\n{user_prompt or '(unavailable)'}\n\n"
        f"FINAL ASSISTANT MESSAGE:\n{message}"
    )
    try:
        async with asyncio.timeout(timeout):
            payload = await llm_service.call_json_feature(
                validation,
                prompt,
                system_prompt=_SHIRK_SYSTEM_PROMPT,
                json_schema=_SHIRK_CONFIRM_SCHEMA,
                caller="workflows.found_work_gate",
                total_timeout_seconds=timeout,
            )
    except Exception:
        logger.debug(
            "Found-work shirk confirmation unavailable; using fast-path verdict", exc_info=True
        )
        return None
    if isinstance(payload, Mapping) and isinstance(payload.get("block"), bool):
        return payload["block"] is True
    return None


def _record_shadow(
    decisions: Any,
    message: str,
    user_prompt: str,
    outcome: _Outcome,
    verdict: bool | None,
) -> None:
    decisions_shadow.record(
        _CONSUMER,
        {
            "model": decisions.model,
            "state": {"final_assistant_message": message, "user_instruction": user_prompt},
            "proposition": _PROPOSITION,
            "classifier": {
                "status": outcome.status,
                "reason": outcome.reason,
                "probability": outcome.probability,
            },
            "incumbent": {
                "status": "ok" if verdict is not None else "unavailable",
                "verdict": verdict,
            },
            "backend_identity": outcome.backend_identity,
            "wire_api": decisions.wire_api,
        },
    )
