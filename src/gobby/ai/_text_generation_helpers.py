"""Internal helpers for daemon text generation."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from gobby.ai._text_generation_contracts import TextGenerationRequest
from gobby.config.feature_base import parse_feature_candidate

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult


_PROMPT_ECHO_PREFIX_MIN_CHARS = 200


class _InvalidTextGenerationOutputError(RuntimeError):
    """Recoverable invalid output from one text generation candidate."""


class _CandidateTimeoutError(RuntimeError):
    """A single candidate attempt exceeded the per-candidate timeout."""


class FeatureGenerationUnavailableError(RuntimeError):
    """No text-generation candidate produced a usable result.

    Raised when every candidate failed for infrastructure reasons — timeouts,
    capability unavailability, or provider transport/format failures (including
    all candidates returning malformed JSON for a feature JSON call). This is an
    infrastructure problem, not a domain decision: callers such as task
    validation use it to distinguish "the model never answered" from a genuine
    verdict, so they can back off and retry rather than recording a false result.
    """


def is_feature_generation_infrastructure_error(exc: BaseException | None) -> bool:
    """Return True if ``exc`` (or its cause/context chain) is an infra generation failure.

    Treats :class:`FeatureGenerationUnavailableError`, per-candidate timeouts, and
    capability-unavailable errors as infrastructure failures. A single malformed-JSON
    parse failure is *not* infra on its own (the candidate loop falls through to the
    next candidate); when every candidate fails, the service raises
    :class:`FeatureGenerationUnavailableError`, which this recognizes.
    """
    from gobby.ai.registry import CapabilityUnavailableError

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                FeatureGenerationUnavailableError,
                _CandidateTimeoutError,
                CapabilityUnavailableError,
            ),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _llm_text_result_type() -> type[LLMTextResult]:
    from gobby.llm.base import LLMTextResult

    return LLMTextResult


def _coerce_text_result(
    result: str | LLMTextResult,
    *,
    applied_reasoning_effort: str | None = None,
) -> LLMTextResult:
    text_result_type = _llm_text_result_type()
    if isinstance(result, text_result_type):
        if applied_reasoning_effort is None:
            return result
        if result.applied_reasoning_effort == applied_reasoning_effort:
            return result
        return replace(result, applied_reasoning_effort=applied_reasoning_effort)
    return text_result_type(
        text=cast(str, result),
        applied_reasoning_effort=applied_reasoning_effort,
    )


def _validate_text_generation_output(request: TextGenerationRequest, text: str) -> None:
    normalized_text = _normalize_generation_text(text)
    if not normalized_text:
        raise _InvalidTextGenerationOutputError("text generation returned blank output")

    for prompt in _prompt_echo_targets(request):
        normalized_prompt = _normalize_generation_text(prompt)
        if not normalized_prompt:
            continue
        if normalized_text == normalized_prompt:
            raise _InvalidTextGenerationOutputError(
                "text generation returned the prompt instead of generated text"
            )
        if len(normalized_prompt) >= _PROMPT_ECHO_PREFIX_MIN_CHARS and normalized_text.startswith(
            normalized_prompt
        ):
            raise _InvalidTextGenerationOutputError(
                "text generation output started with the prompt instead of generated text"
            )


def _prompt_echo_targets(request: TextGenerationRequest) -> tuple[str, ...]:
    composed_prompt = _compose_prompt(request)
    if composed_prompt == request.prompt:
        return (request.prompt,)
    return (request.prompt, composed_prompt)


def _normalize_generation_text(text: str) -> str:
    return " ".join(text.strip().split())


def _candidate_debug_label(candidate: TextGenerationRequest) -> str:
    provider = candidate.provider or "<auto>"
    model = candidate.model or "<auto>"
    return f"{provider}/{model}"


def _parse_candidate(candidate: str) -> tuple[str, str]:
    try:
        return parse_feature_candidate(candidate)
    except ValueError as exc:
        raise ValueError(
            f"Feature candidate must use provider/model format: {candidate!r}"
        ) from exc


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _json_request(request: TextGenerationRequest) -> TextGenerationRequest:
    system_prompt = request.system_prompt
    json_instruction = "Respond with a single valid JSON object. Do not include markdown."
    if system_prompt:
        system_prompt = f"{system_prompt}\n\n{json_instruction}"
    else:
        system_prompt = json_instruction
    return replace(request, system_prompt=system_prompt)


def _parse_json_text(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("Generated JSON must be an object")
    return parsed


def _compose_prompt(request: TextGenerationRequest) -> str:
    if not request.system_prompt:
        return request.prompt
    return f"{request.system_prompt}\n\n{request.prompt}"


ONE_SHOT_DIRECTIVE = (
    "Non-interactive one-shot request: answer directly from the information "
    "in this prompt. Do not use tools, run commands, read or write files, or "
    "explore the workspace; any tool request will be denied. Do not narrate "
    "plans or actions. Reply with only the requested output."
)


def _with_one_shot_directive(request: TextGenerationRequest) -> TextGenerationRequest:
    """Append the one-shot no-tools directive to the request's system prompt."""
    if request.system_prompt:
        return replace(request, system_prompt=f"{request.system_prompt}\n\n{ONE_SHOT_DIRECTIVE}")
    return replace(request, system_prompt=ONE_SHOT_DIRECTIVE)


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    return data.decode(errors="replace")
