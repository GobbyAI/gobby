"""Internal helpers for daemon text generation."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from gobby.ai._text_generation_contracts import ACPStreamEventLike, TextGenerationRequest
from gobby.config.feature_base import parse_feature_candidate

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult


_PROMPT_ECHO_PREFIX_MIN_CHARS = 200


class _InvalidTextGenerationOutputError(RuntimeError):
    """Recoverable invalid output from one text generation candidate."""


class _CandidateTimeoutError(RuntimeError):
    """A single candidate attempt exceeded the per-candidate timeout."""


def _llm_text_result_type() -> type[LLMTextResult]:
    from gobby.llm.base import LLMTextResult

    return LLMTextResult


def _coerce_text_result(result: str | LLMTextResult) -> LLMTextResult:
    text_result_type = _llm_text_result_type()
    if isinstance(result, text_result_type):
        return result
    return text_result_type(text=cast(str, result))


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


_ONE_SHOT_DENIAL_REASON = "Tool use is disabled for one-shot text generation."


async def _deny_one_shot_tool_use(_payload: dict[str, Any]) -> dict[str, Any]:
    """Deny every agent tool request during one-shot text generation."""
    return {"decision": "deny", "reason": _ONE_SHOT_DENIAL_REASON}


def _with_one_shot_directive(request: TextGenerationRequest) -> TextGenerationRequest:
    """Append the one-shot no-tools directive to the request's system prompt."""
    if request.system_prompt:
        return replace(request, system_prompt=f"{request.system_prompt}\n\n{ONE_SHOT_DIRECTIVE}")
    return replace(request, system_prompt=ONE_SHOT_DIRECTIVE)


async def _collect_acp_text(events: AsyncIterator[ACPStreamEventLike]) -> str:
    chunks: list[str] = []
    result_chunks: list[str] = []
    async for event in events:
        if event.event_type == "error":
            raise RuntimeError(f"ACP text generation failed: {_event_error_message(event.data)}")
        text = _stream_event_text(event)
        if not text:
            continue
        if event.event_type == "content_delta":
            chunks.append(text)
        elif event.event_type == "result":
            result_chunks.append(text)

    return "".join(chunks or result_chunks).strip()


def _event_error_message(data: Mapping[str, Any]) -> str:
    for key in ("message", "error", "details", "code"):
        value = data.get(key)
        if value:
            if isinstance(value, str):
                return value
            return json.dumps(value, sort_keys=True, default=str)
    return "unknown error"


def _stream_event_text(event: ACPStreamEventLike) -> str:
    for key in ("content", "output", "text", "message"):
        value = event.data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _codex_event_text(event: dict[str, Any]) -> str:
    delta = event.get("delta")
    if isinstance(delta, str) and delta:
        return delta

    item = event.get("item")
    if not isinstance(item, dict):
        return ""

    text = item.get("text")
    if isinstance(text, str) and text:
        return text

    content = item.get("content")
    if isinstance(content, str) and content:
        return content
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("delta") or ""
            if isinstance(text, str) and text:
                chunks.append(text)
    return "".join(chunks)


def _raise_for_codex_error_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    if event_type not in {"turn/created", "turn/completed"}:
        return

    for payload in _codex_turn_payloads(event):
        error = payload.get("error")
        if error:
            raise RuntimeError(f"Codex text generation failed: {_codex_error_message(error)}")
        status = payload.get("status")
        if isinstance(status, str) and status.lower() in {"error", "failed"}:
            raise RuntimeError(
                f"Codex text generation failed with status {status}: "
                f"{_codex_error_message(payload)}"
            )


def _codex_turn_payloads(event: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    turn = event.get("turn")
    if isinstance(turn, dict):
        return event, turn
    return (event,)


def _codex_error_message(error: object) -> str:
    if isinstance(error, str):
        return error
    if isinstance(error, Mapping):
        return _event_error_message(error)
    return json.dumps(error, sort_keys=True, default=str)


def _codex_completed_item_type(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    return item_type if isinstance(item_type, str) else None


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    return data.decode(errors="replace")
