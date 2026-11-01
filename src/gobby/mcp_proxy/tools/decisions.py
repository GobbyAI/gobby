"""Agent-facing ``gobby-decisions`` tool over the shared decision service."""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from gobby.ai.decisions import (
    BoxAnswer,
    BoxImage,
    BoxQuestion,
    ChoiceQuestion,
    DecisionResult,
    DecisionService,
    DecisionsUnavailable,
    ScoreQuestion,
    get_decision_service,
)
from gobby.config.app import DaemonConfig
from gobby.llm.image_payloads import MAX_IMAGE_BYTES
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

CONSUMER = "mcp_evaluate"
_KEY_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"
_KEY = re.compile(_KEY_PATTERN)
_MAX_QUESTIONS = 64
_MIN_OPTIONS = 2
_MAX_OPTIONS = 255
_MIN_LEVELS = 2
_MAX_LEVELS = 10
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"


def _shape(kind: str, **fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"const": kind},
            "instructions": {"type": "string", "minLength": 1},
            **fields,
        },
        "required": ["type", "instructions", *fields],
        "additionalProperties": False,
    }


_CHOICE_SHAPE = _shape(
    "choice",
    criteria={
        "type": "object",
        "minProperties": _MIN_OPTIONS,
        "maxProperties": _MAX_OPTIONS,
        "additionalProperties": {"type": "string", "minLength": 1},
    },
)
_NOUL_SHAPE = _shape("noul")
_SCORE_SHAPE = _shape(
    "score",
    levels={
        "type": "array",
        "minItems": _MIN_LEVELS,
        "maxItems": _MAX_LEVELS,
        "items": {"type": "string", "minLength": 1},
    },
)
_BOX_SHAPE = _shape("box", image_path={"type": "string", "minLength": 1})

EVALUATE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "object"},
        "questions": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": _MAX_QUESTIONS,
            "propertyNames": {"pattern": _KEY_PATTERN},
            "additionalProperties": {
                "oneOf": [_CHOICE_SHAPE, _NOUL_SHAPE, _SCORE_SHAPE, _BOX_SHAPE]
            },
        },
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    },
    "required": ["state", "questions"],
    "additionalProperties": False,
}

_SHAPE_FIELDS: dict[str, frozenset[str]] = {
    "choice": frozenset({"type", "instructions", "criteria"}),
    "noul": frozenset({"type", "instructions"}),
    "score": frozenset({"type", "instructions", "levels"}),
    "box": frozenset({"type", "instructions", "image_path"}),
}


class _InvalidRequest(ValueError):
    """A caller request that breaks the public contract; the detail names the rule only."""


@dataclass(frozen=True)
class _Choice:
    questions: dict[str, ChoiceQuestion]


@dataclass(frozen=True)
class _Noul:
    propositions: dict[str, str]


@dataclass(frozen=True)
class _Score:
    questions: dict[str, ScoreQuestion]


@dataclass(frozen=True)
class _Box:
    questions: dict[str, BoxQuestion]


_Parsed = _Choice | _Noul | _Score | _Box


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timeout(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _InvalidRequest("timeout_seconds must be a number")
    if not math.isfinite(value) or value <= 0:
        raise _InvalidRequest("timeout_seconds must be finite and above 0")
    return float(value)


def _instructions(question: dict[str, Any]) -> str:
    text = question.get("instructions")
    if not isinstance(text, str) or not text.strip():
        raise _InvalidRequest("instructions must be non-empty text")
    return text


def _question_type(key: object, question: object) -> str:
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise _InvalidRequest("question keys must match [A-Za-z0-9_.-]{1,100}")
    if not isinstance(question, dict):
        raise _InvalidRequest("each question must be an object")
    kind = question.get("type")
    if not isinstance(kind, str) or kind not in _SHAPE_FIELDS:
        raise _InvalidRequest("question type must be choice, noul, score, or box")
    if set(question) - _SHAPE_FIELDS[kind]:
        raise _InvalidRequest("question carries a key its type does not allow")
    return kind


def _choice(question: dict[str, Any]) -> ChoiceQuestion:
    criteria = question.get("criteria")
    if not isinstance(criteria, dict) or not _MIN_OPTIONS <= len(criteria) <= _MAX_OPTIONS:
        raise _InvalidRequest("choice criteria must be an object of 2 to 255 options")
    if not all(_non_empty(key) and _non_empty(text) for key, text in criteria.items()):
        raise _InvalidRequest("choice options need non-empty keys and text")
    return ChoiceQuestion(instructions=_instructions(question), criteria=dict(criteria))


def _score(question: dict[str, Any]) -> ScoreQuestion:
    levels = question.get("levels")
    if (
        not isinstance(levels, list)
        or not _MIN_LEVELS <= len(levels) <= _MAX_LEVELS
        or not all(_non_empty(level) for level in levels)
    ):
        raise _InvalidRequest("score levels must be 2 to 10 non-empty strings")
    return ScoreQuestion(instructions=_instructions(question), levels=tuple(levels))


def _box(question: dict[str, Any]) -> BoxQuestion:
    path_text = question.get("image_path")
    if not isinstance(path_text, str) or not Path(path_text).is_absolute():
        raise _InvalidRequest("image_path must be an absolute path")
    try:
        with Path(path_text).open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError as error:
        raise _InvalidRequest("image_path must name a readable file") from error
    if len(data) > MAX_IMAGE_BYTES:
        raise _InvalidRequest("image exceeds byte limit")
    mime_type: Literal["image/png", "image/jpeg"]
    if data.startswith(_PNG_SIGNATURE):
        mime_type = "image/png"
    elif data.startswith(_JPEG_SIGNATURE):
        mime_type = "image/jpeg"
    else:
        raise _InvalidRequest("image must be a PNG or JPEG file")
    image = BoxImage(mime_type=mime_type, data_base64=base64.b64encode(data).decode("ascii"))
    return BoxQuestion(instructions=_instructions(question), image=image)


def _parse(
    state: object, questions: object, timeout_seconds: object
) -> tuple[dict[str, Any], _Parsed, float | None]:
    if not isinstance(state, dict):
        raise _InvalidRequest("state must be an object")
    timeout = _timeout(timeout_seconds)
    if not isinstance(questions, dict) or not 1 <= len(questions) <= _MAX_QUESTIONS:
        raise _InvalidRequest("questions must hold 1 to 64 entries")
    kinds = {_question_type(key, question) for key, question in questions.items()}
    if len(kinds) != 1:
        raise _InvalidRequest("every question in one call must have the same type")
    match kinds.pop():
        case "choice":
            parsed: _Parsed = _Choice({k: _choice(q) for k, q in questions.items()})
        case "noul":
            parsed = _Noul({k: _instructions(q) for k, q in questions.items()})
        case "score":
            parsed = _Score({k: _score(q) for k, q in questions.items()})
        case _:
            parsed = _Box({k: _box(q) for k, q in questions.items()})
    return state, parsed, timeout


def _failure(reason: str, detail: str) -> dict[str, Any]:
    return {"success": False, "reason": reason, "detail": detail, "error": f"{reason}: {detail}"}


def _render(answer: Any) -> dict[str, Any]:
    if isinstance(answer, BoxAnswer):
        box = {
            "top": answer.top,
            "bottom": answer.bottom,
            "left": answer.left,
            "right": answer.right,
        }
        return {"box": box, "confidence": answer.confidence}
    return asdict(answer)


def _answered(kind: str, result: DecisionResult[Any]) -> dict[str, Any]:
    return {
        "success": True,
        "type": kind,
        "answers": {key: _render(answer) for key, answer in result.answers.items()},
        "response_model": result.response_model,
        "backend_identity": result.backend_identity,
    }


async def _decide(
    service: DecisionService, state: dict[str, Any], parsed: _Parsed, timeout: float | None
) -> dict[str, Any]:
    if isinstance(parsed, _Choice):
        choice = await service.choose(CONSUMER, state, parsed.questions, timeout_seconds=timeout)
        return _answered("choice", choice)
    if isinstance(parsed, _Noul):
        noul = await service.noul(CONSUMER, state, parsed.propositions, timeout_seconds=timeout)
        return _answered("noul", noul)
    if isinstance(parsed, _Score):
        score = await service.score(CONSUMER, state, parsed.questions, timeout_seconds=timeout)
        return _answered("score", score)
    box = await service.box(CONSUMER, state, parsed.questions, timeout_seconds=timeout)
    return _answered("box", box)


def create_decisions_registry(
    config_resolver: Callable[[], DaemonConfig | None],
) -> InternalToolRegistry:
    registry = InternalToolRegistry(
        name="gobby-decisions",
        description="Decision classifier over the configured decision wire",
    )

    async def evaluate(
        state: object,
        questions: object,
        timeout_seconds: object = None,
    ) -> dict[str, Any]:
        try:
            state_map, parsed, timeout = _parse(state, questions, timeout_seconds)
        except _InvalidRequest as error:
            return _failure("invalid_request", str(error))
        config = config_resolver()
        decisions = None if config is None else config.ai.decisions
        if decisions is None or decisions.api_base is None or decisions.model is None:
            return _failure("unconfigured", "decisions api_base is unset")
        service = get_decision_service(decisions)
        try:
            return await _decide(service, state_map, parsed, timeout)
        except DecisionsUnavailable as error:
            return _failure(error.reason, error.detail)

    registry.register(
        name="evaluate",
        description=(
            "Answer decision questions through the configured decision service. "
            "Every question in one call shares a type: choice, noul, score, or box."
        ),
        input_schema=EVALUATE_INPUT_SCHEMA,
        func=evaluate,
        read_only=True,
    )
    return registry
