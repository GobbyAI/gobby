"""Agent-facing ``gobby-decisions`` tool over the shared decision service."""

from __future__ import annotations

import base64
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from gobby.ai.decisions import (
    BoxAnswer,
    BoxImage,
    BoxQuestion,
    ChoiceQuestion,
    DecisionQuestion,
    DecisionService,
    DecisionsUnavailable,
    QuestionOutcome,
    ScoreQuestion,
    get_decision_service,
)
from gobby.config.ai import DECISION_KINDS
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


def _read_regular_file(path_text: str) -> bytes:
    """Read at most MAX_IMAGE_BYTES + 1 bytes from a regular file.

    O_NONBLOCK keeps a FIFO or device open from waiting for a writer. fstat on the
    open descriptor then rejects anything that is not a regular file.
    """
    try:
        fd = os.open(path_text, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as error:
        raise _InvalidRequest("image_path must name a readable file") from error
    try:
        is_regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError as error:
        os.close(fd)
        raise _InvalidRequest("image_path must name a readable file") from error
    if not is_regular:
        os.close(fd)
        raise _InvalidRequest("image_path must name a regular file")
    with os.fdopen(fd, "rb") as handle:
        try:
            return handle.read(MAX_IMAGE_BYTES + 1)
        except OSError as error:
            raise _InvalidRequest("image_path must name a readable file") from error


def _box(question: dict[str, Any]) -> BoxQuestion:
    path_text = question.get("image_path")
    if not isinstance(path_text, str) or not Path(path_text).is_absolute():
        raise _InvalidRequest("image_path must be an absolute path")
    data = _read_regular_file(path_text)
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


def _question(key: str, question: Any) -> DecisionQuestion:
    match _question_type(key, question):
        case "choice":
            return _choice(question)
        case "noul":
            return _instructions(question)
        case "score":
            return _score(question)
        case _:
            return _box(question)


def _parse(
    state: object, questions: object, timeout_seconds: object
) -> tuple[dict[str, Any], dict[str, DecisionQuestion], float | None]:
    if not isinstance(state, dict):
        raise _InvalidRequest("state must be an object")
    timeout = _timeout(timeout_seconds)
    if not isinstance(questions, dict) or not 1 <= len(questions) <= _MAX_QUESTIONS:
        raise _InvalidRequest("questions must hold 1 to 64 entries")
    return state, {key: _question(key, question) for key, question in questions.items()}, timeout


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


def _outcome(outcome: QuestionOutcome) -> dict[str, Any]:
    if outcome.error is not None:
        return {
            "type": outcome.kind,
            "success": False,
            "reason": outcome.error.reason,
            "detail": outcome.error.detail,
        }
    return {
        "type": outcome.kind,
        "success": True,
        "answer": _render(outcome.answer),
        "response_model": outcome.response_model,
        "backend_identity": outcome.backend_identity,
    }


async def _decide(
    service: DecisionService,
    state: dict[str, Any],
    questions: dict[str, DecisionQuestion],
    timeout: float | None,
) -> dict[str, Any]:
    outcomes = await service.evaluate(CONSUMER, state, questions, timeout_seconds=timeout)
    answers = {key: _outcome(o) for key, o in outcomes.items()}
    # A call fails as a whole only when every question failed; a partial failure stays on
    # its own questions, and the successful answers are still returned.
    errors = [o.error for o in outcomes.values() if o.error is not None]
    if len(errors) == len(outcomes):
        return {**_failure(errors[0].reason, errors[0].detail), "answers": answers}
    return {"success": True, "answers": answers}


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
            state_map, question_map, timeout = _parse(state, questions, timeout_seconds)
        except _InvalidRequest as error:
            return _failure("invalid_request", str(error))
        config = config_resolver()
        decisions = None if config is None else config.ai.decisions
        if decisions is None or all(
            decisions.endpoint_for(kind) is None for kind in DECISION_KINDS
        ):
            return _failure("unconfigured", "no decisions endpoint is configured for any type")
        service = get_decision_service(decisions)
        try:
            return await _decide(service, state_map, question_map, timeout)
        except DecisionsUnavailable as error:
            return _failure(error.reason, error.detail)

    registry.register(
        name="evaluate",
        description=(
            "Answer decision questions through the configured decision service. Each question "
            "runs on the endpoint configured for its type: choice, noul, score, or box."
        ),
        input_schema=EVALUATE_INPUT_SCHEMA,
        func=evaluate,
        read_only=True,
    )
    return registry
