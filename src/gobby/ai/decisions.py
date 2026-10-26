"""Strict Choice, Noul, Score and Box decisions over the supported wires."""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import re
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

import httpx

from gobby.config.ai import DecisionsConfig
from gobby.llm.claude_runtime import retry_async
from gobby.llm.image_payloads import MAX_IMAGE_BYTES, MAX_REQUEST_IMAGE_BYTES, MAX_REQUEST_IMAGES

logger = logging.getLogger(__name__)

DecisionsUnavailableReason = Literal[
    "unconfigured",
    "cooldown",
    "oversize",
    "invalid_request",
    "truncated",
    "timeout",
    "transport",
    "http_status",
    "parse",
]

_service_cache: tuple[tuple[object, ...], "DecisionService"] | None = None

_ATTEMPTS = 3
_RETRY_DELAY = 0.1
_MAX_QUESTIONS = 64
_MIN_OPTIONS = 2
_MAX_OPTIONS = 255
_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SUM_TOLERANCE = 1e-3
_TIE_TOLERANCE = 1e-6
_CARD_FIELDS = ("run_path", "base_model", "lora_config", "dtype", "temperature")


def _now() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class ChoiceQuestion:
    instructions: str
    criteria: Mapping[str, str]


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True)
class NoulAnswer:
    probability: float


@dataclass(frozen=True)
class ScoreQuestion:
    instructions: str
    levels: tuple[str, ...]


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class BoxImage:
    mime_type: Literal["image/png", "image/jpeg"]
    data_base64: str


@dataclass(frozen=True)
class BoxQuestion:
    instructions: str
    image: BoxImage


@dataclass(frozen=True)
class BoxAnswer:
    top: float
    bottom: float
    left: float
    right: float
    confidence: float


@dataclass(frozen=True)
class DecisionResult[A]:
    answers: dict[str, A]
    backend_identity: str | None
    response_model: str


class DecisionsUnavailable(Exception):
    def __init__(self, reason: DecisionsUnavailableReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class _HTTPStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def estimate_tokens(body: Mapping[str, Any]) -> int:
    """Estimate prompt tokens at four characters per token."""
    return len(json.dumps(body)) // 4


def get_decision_service(config: DecisionsConfig) -> "DecisionService":
    """Return the shared service for this endpoint identity; consumer policy is not part of it."""
    global _service_cache
    fingerprint = _fingerprint(config)
    if _service_cache is None or _service_cache[0] != fingerprint:
        _service_cache = (fingerprint, DecisionService(config))
    return _service_cache[1]


def _fingerprint(config: DecisionsConfig) -> tuple[object, ...]:
    key_digest = hashlib.sha256((config.api_key or "").encode()).hexdigest()
    return (
        config.api_base,
        config.model,
        key_digest,
        config.wire_api,
        config.identity_contract,
        config.timeout_seconds,
        config.max_input_tokens,
        config.max_request_bytes,
        config.resolved_backend_max_state_tokens,
        config.resolved_backend_max_question_tokens,
        config.long_context,
        config.failure_cooldown_seconds,
    )


class DecisionService:
    def __init__(
        self,
        config: DecisionsConfig,
        *,
        exchange_observer: Callable[[httpx.Response], None] | None = None,
    ) -> None:
        self._config = config
        self._exchange_observer = exchange_observer
        self._cooldown_until = 0.0

    async def choose(
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, ChoiceQuestion],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[ChoiceAnswer]:
        """Return validated Choice answers, or raise ``DecisionsUnavailable``."""
        return await self._request(consumer, state, questions, _parse_choice, timeout_seconds)

    async def _request[Q: (ChoiceQuestion, str, ScoreQuestion, BoxQuestion), A](
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, Q],
        parse_answer: Callable[[Any, Q], A],
        timeout_seconds: float | None,
    ) -> DecisionResult[A]:
        config = self._config
        started = time.monotonic()
        outcome = "error"
        estimated = 0
        try:
            base, model = config.api_base, config.model
            if base is None or model is None:
                raise DecisionsUnavailable("unconfigured", "decisions api_base is unset")
            _check_request(questions, config.wire_api)
            body = _request_body(model, config.wire_api, state, questions)
            estimated = _estimate_body_tokens(body)
            wire_body = _check_request_limits(body, config, estimated)
            if _now() < self._cooldown_until:
                raise DecisionsUnavailable("cooldown", "a recent decision failure is cooling down")
            result = await self._dial(
                base,
                model,
                questions,
                wire_body,
                timeout_seconds,
                parse_answer,
                has_images="assets" in body,
            )
            outcome = "ok"
            return result
        except DecisionsUnavailable as error:
            outcome = error.reason
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            logger.debug(
                "decisions.call consumer=%s model=%s question_count=%d schema_hash=%s "
                "estimated_tokens=%d latency_ms=%d outcome=%s",
                consumer,
                config.model,
                len(questions),
                _schema_hash(questions),
                estimated,
                int((time.monotonic() - started) * 1000),
                outcome,
            )

    async def noul(
        self,
        consumer: str,
        state: Mapping[str, Any],
        propositions: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[NoulAnswer]:
        """Return validated Noul probabilities, or raise ``DecisionsUnavailable``."""
        return await self._request(consumer, state, propositions, _parse_noul, timeout_seconds)

    async def score(
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, ScoreQuestion],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[ScoreAnswer]:
        """Return validated Score answers, or raise ``DecisionsUnavailable``."""
        return await self._request(consumer, state, questions, _parse_score, timeout_seconds)

    async def box(
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, BoxQuestion],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[BoxAnswer]:
        """Return validated Box answers, or raise ``DecisionsUnavailable``."""
        return await self._request(consumer, state, questions, _parse_box, timeout_seconds)

    def estimate_request_tokens(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, ChoiceQuestion]
        | Mapping[str, str]
        | Mapping[str, ScoreQuestion]
        | Mapping[str, BoxQuestion],
    ) -> int:
        """Return the estimate the ``oversize`` guard compares with ``max_input_tokens``."""
        return _estimate_body_tokens(
            _request_body(self._config.model or "", self._config.wire_api, state, questions)
        )

    def request_fits(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, ChoiceQuestion]
        | Mapping[str, str]
        | Mapping[str, ScoreQuestion]
        | Mapping[str, BoxQuestion],
    ) -> bool:
        """Apply the same request and branch budgets used before dialing."""
        body = _request_body(self._config.model or "", self._config.wire_api, state, questions)
        try:
            _check_request_limits(body, self._config, _estimate_body_tokens(body))
        except DecisionsUnavailable:
            return False
        return True

    async def _dial[Q, A](
        self,
        base: str,
        model: str,
        questions: Mapping[str, Q],
        body: bytes,
        timeout_seconds: float | None,
        parse_answer: Callable[[Any, Q], A],
        *,
        has_images: bool,
    ) -> DecisionResult[A]:
        config = self._config
        budget = config.timeout_seconds
        if timeout_seconds is not None:
            budget = min(timeout_seconds, budget)
        # A caller budget shorter than the service timeout makes a timeout local.
        local = timeout_seconds is not None and timeout_seconds < config.timeout_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        url = _endpoint(config.wire_api, base, model)
        headers = {"authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        if config.long_context:
            headers["TypeSafe-Danger-Zone-Long-Context"] = "true"
        if config.wire_api == "systemone" and has_images:
            headers["TypeSafe-Danger-Zone-Images"] = "true"
        card: asyncio.Task[str | None] | None = None
        try:
            async with asyncio.timeout(budget):
                if config.identity_contract == "model_card":
                    card = asyncio.create_task(
                        _fetch_card_identity(
                            base, model, config.resolved_backend_max_state_tokens, budget
                        )
                    )
                payload = await retry_async(
                    lambda: _post(url, headers, body, budget, self._exchange_observer),
                    max_retries=_ATTEMPTS,
                    delay=_RETRY_DELAY,
                    retry_if=_retryable,
                )
                body_json = _unwrap(payload, config.wire_api)
                response_model, answers = _parse_answers(body_json, questions, parse_answer)
                _check_not_truncated(body_json, config.resolved_backend_max_state_tokens)
            # The card waits on the budget's remainder, so the decision's own timeout cannot cut
            # a decision that already succeeded short.
            if config.identity_contract == "response_version":
                identity: str | None = f"response:{response_model}"
            elif card is not None:
                done, _ = await asyncio.wait({card}, timeout=max(0.0, deadline - loop.time()))
                identity = card.result() if card in done else None
            else:
                identity = None
            return DecisionResult(
                answers=answers, backend_identity=identity, response_model=response_model
            )
        except (
            TimeoutError,
            _HTTPStatusError,
            httpx.HTTPError,
            DecisionsUnavailable,
        ) as error:
            failure = _as_unavailable(error, budget)
            if failure.reason == "truncated" or (local and failure.reason == "timeout"):
                raise failure from None
            # A failure inside an open window neither extends it nor logs again.
            now = _now()
            if now >= self._cooldown_until:
                self._cooldown_until = now + config.failure_cooldown_seconds
                logger.warning(
                    "decisions cooldown opened after %s; calls fail fast for %ss",
                    failure.reason,
                    config.failure_cooldown_seconds,
                )
            raise failure from None
        finally:
            if card is not None:
                card.cancel()
                await asyncio.wait({card})


def _check_request(
    questions: Mapping[str, ChoiceQuestion | str | ScoreQuestion | BoxQuestion], wire_api: str
) -> None:
    if not 1 <= len(questions) <= _MAX_QUESTIONS:
        raise DecisionsUnavailable(
            "invalid_request", f"{len(questions)} questions; 1 to {_MAX_QUESTIONS} allowed"
        )
    images = [q.image for q in questions.values() if isinstance(q, BoxQuestion)]
    if images:
        if wire_api == "workers-ai":
            raise DecisionsUnavailable("invalid_request", "Workers AI does not support Box")
        _check_images(images)
    for key, question in questions.items():
        if _KEY_PATTERN.fullmatch(key) is None:
            raise DecisionsUnavailable(
                "invalid_request", "question keys must match [A-Za-z0-9_.-]{1,100}"
            )
        instructions = question if isinstance(question, str) else question.instructions
        if not isinstance(instructions, str) or not instructions.strip():
            raise DecisionsUnavailable("invalid_request", "instructions must be non-empty text")
        if isinstance(question, ScoreQuestion) and (
            not 2 <= len(question.levels) <= 10
            or any(not isinstance(level, str) or not level.strip() for level in question.levels)
        ):
            raise DecisionsUnavailable("invalid_request", "Score takes 2 to 10 non-empty levels")
        if isinstance(question, ChoiceQuestion) and (
            not _MIN_OPTIONS <= len(question.criteria) <= _MAX_OPTIONS
            or any(
                not isinstance(option, str) or not isinstance(value, str)
                for option, value in question.criteria.items()
            )
        ):
            raise DecisionsUnavailable(
                "invalid_request",
                f"a Choice question takes {_MIN_OPTIONS} to {_MAX_OPTIONS} options",
            )


def _request_body(
    model: str,
    wire_api: str,
    state: Mapping[str, Any],
    questions: Mapping[str, ChoiceQuestion | str | ScoreQuestion | BoxQuestion],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "state": dict(state),
        "questions": {},
    }
    for key, question in questions.items():
        if isinstance(question, BoxQuestion):
            image = question.image
            part: str | dict[str, Any]
            if wire_api == "systemone":
                part = f"gobby-image-{uuid4().hex}"
                body.setdefault("assets", {})[part] = {
                    "type": "image",
                    "format": image.mime_type.removeprefix("image/"),
                    "data_base64": image.data_base64,
                }
            else:
                part = {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.mime_type};base64,{image.data_base64}"},
                }
            body["questions"][key] = {"type": "box", "instructions": [question.instructions, part]}
        else:
            body["questions"][key] = _question_body(question)
    if wire_api == "openrouter-decisions":
        body["provider"] = {"zdr": True, "data_collection": "deny"}
    return body


def _check_request_limits(body: dict[str, Any], config: DecisionsConfig, estimated: int) -> bytes:
    if estimated > config.max_input_tokens:
        raise DecisionsUnavailable(
            "oversize", f"~{estimated} tokens exceeds {config.max_input_tokens}"
        )
    limit = config.resolved_backend_max_question_tokens
    if limit is not None and estimated > limit:
        for key, question in body["questions"].items():
            branch = {**body, "questions": {key: question}}
            if _estimate_body_tokens(branch) > limit:
                raise DecisionsUnavailable("oversize", f"question {key} exceeds {limit} tokens")
    wire_body = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    if config.max_request_bytes is not None and len(wire_body) > config.max_request_bytes:
        raise DecisionsUnavailable(
            "oversize", f"{len(wire_body)} bytes exceeds {config.max_request_bytes}"
        )
    return wire_body


def _estimate_body_tokens(body: dict[str, Any]) -> int:
    """Image tokens are unknown until capture; estimate the textual payload only."""
    text_body = dict(body)
    if "assets" in body:
        text_body["assets"] = {
            key: {**asset, "data_base64": ""} for key, asset in body["assets"].items()
        }
    text_body["questions"] = dict(body["questions"])
    for key, question in body["questions"].items():
        if question["type"] == "box" and isinstance(question["instructions"][1], dict):
            instruction, image = question["instructions"]
            url = image["image_url"]["url"].split(",", 1)[0] + ","
            text_body["questions"][key] = {
                **question,
                "instructions": [instruction, {"type": "image_url", "image_url": {"url": url}}],
            }
    return estimate_tokens(text_body)


def _check_images(images: list[BoxImage]) -> None:
    if len(images) > MAX_REQUEST_IMAGES:
        raise DecisionsUnavailable("invalid_request", "too many request images")
    total = 0
    for image in images:
        if len(image.data_base64) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise DecisionsUnavailable("invalid_request", "image exceeds byte limit")
        try:
            data = base64.b64decode(image.data_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise DecisionsUnavailable("invalid_request", "invalid image base64") from error
        total += len(data)
        if len(data) > MAX_IMAGE_BYTES or total > MAX_REQUEST_IMAGE_BYTES:
            raise DecisionsUnavailable("invalid_request", "images exceed byte limit")
        width, height = _image_dimensions(data, image.mime_type)
        if not (
            280 <= width <= 1568 and 280 <= height <= 1568 and 78400 <= width * height <= 2458624
        ):
            raise DecisionsUnavailable("invalid_request", "image dimensions exceed Box limits")


def _image_dimensions(data: bytes, mime_type: str) -> tuple[int, int]:
    if (
        mime_type == "image/png"
        and len(data) >= 33
        and data[:16] == (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    ):
        width, height = struct.unpack_from(">II", data, 16)
        return width, height
    if mime_type == "image/jpeg" and data.startswith(b"\xff\xd8"):
        offset = 2
        while offset < len(data) and data[offset] == 0xFF:
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset : offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if length < 8:
                    break
                height, width = struct.unpack_from(">HH", data, offset + 3)
                return width, height
            offset += length
    raise DecisionsUnavailable("invalid_request", "missing PNG IHDR or JPEG SOF dimensions")


def _question_body(question: ChoiceQuestion | str | ScoreQuestion | BoxQuestion) -> dict[str, Any]:
    if isinstance(question, str):
        return {"type": "noul", "instructions": question}
    if isinstance(question, ScoreQuestion):
        return {
            "type": "score",
            "instructions": question.instructions,
            "criteria": list(question.levels),
        }
    if isinstance(question, ChoiceQuestion):
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": dict(question.criteria),
        }
    raise DecisionsUnavailable("invalid_request", "unsupported question shape")


def _endpoint(wire_api: str, base: str, model: str) -> str:
    root = base.rstrip("/")
    if wire_api == "systemone":
        return f"{root}/v1/systemone"
    if wire_api == "openrouter-decisions":
        return f"{root}/alpha/decisions"
    return f"{root}/ai/run/@cf/cloudflare/{model}"


def _client(timeout_seconds: float) -> httpx.AsyncClient:
    # The wire URL is the only host dialed: no environment proxy and no redirect hop.
    return httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout_seconds)


async def _post(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    exchange_observer: Callable[[httpx.Response], None] | None = None,
) -> Any:
    async with _client(timeout_seconds) as client:
        response = await client.post(
            url, content=body, headers={"content-type": "application/json", **headers}
        )
    if exchange_observer is not None:
        exchange_observer(response)
    if not 200 <= response.status_code < 300:
        raise _HTTPStatusError(response.status_code)
    try:
        return response.json()
    except ValueError as error:
        raise _parse_error("response body is not JSON") from error


def _retryable(error: Exception) -> bool:
    if isinstance(error, _HTTPStatusError):
        return error.status == 429 or error.status >= 500
    return isinstance(error, httpx.TransportError)


def _as_unavailable(error: BaseException, budget: float) -> DecisionsUnavailable:
    if isinstance(error, DecisionsUnavailable):
        return error
    if isinstance(error, TimeoutError):
        return DecisionsUnavailable("timeout", f"no answer within {budget:g}s")
    if isinstance(error, _HTTPStatusError):
        return DecisionsUnavailable("http_status", f"HTTP {error.status}")
    return DecisionsUnavailable("transport", type(error).__name__)


def _unwrap(payload: Any, wire_api: str) -> dict[str, Any]:
    if wire_api == "workers-ai":
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise _parse_error("workers-ai envelope did not succeed")
        payload = payload.get("result")
    if not isinstance(payload, dict):
        raise _parse_error("response is not a JSON object")
    return payload


def _parse_answers[Q, A](
    body: dict[str, Any], questions: Mapping[str, Q], parse_answer: Callable[[Any, Q], A]
) -> tuple[str, dict[str, A]]:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _parse_error("response model is missing")
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, dict) or raw_answers.keys() != questions.keys():
        raise _parse_error("answers do not match the questions")
    answers = {key: parse_answer(raw_answers[key], question) for key, question in questions.items()}
    return model, answers


def _parse_noul(raw: Any, proposition: str) -> NoulAnswer:
    if not isinstance(raw, dict) or raw.get("type") != "noul":
        raise _parse_error("answer is not a noul")
    return NoulAnswer(probability=_unit(raw.get("noul")))


def _parse_box(raw: Any, question: BoxQuestion) -> BoxAnswer:
    if (
        not isinstance(raw, dict)
        or raw.get("type") != "box"
        or not isinstance(raw.get("box"), dict)
    ):
        raise _parse_error("answer is not a box")
    box = raw["box"]
    top, bottom, left, right = (_unit(box.get(edge)) for edge in ("top", "bottom", "left", "right"))
    if left >= right or top >= bottom:
        raise _parse_error("box edges are not ordered")
    return BoxAnswer(top, bottom, left, right, _unit(raw.get("confidence")))


def _score_abs_tolerance(score: float, probabilities: dict[str, float]) -> float:
    """Allow observed hundredth-grid wire rounding; retain the high-precision profile."""
    level_count = len(probabilities)
    if all(
        math.isclose(value, round(value, 2), rel_tol=0.0, abs_tol=1e-12)
        for value in (score, *probabilities.values())
    ):
        # Normalized rounding errors sum to zero. With weights 0..n-1, their
        # worst weighted sum is .005 * floor(n²/4), plus .005 for score rounding.
        # The final epsilon covers binary representation at the exact bound.
        return 0.005 * (level_count * level_count // 4 + 1) + 1e-12
    return (level_count - 1) * _SUM_TOLERANCE


def _parse_score(raw: Any, question: ScoreQuestion) -> ScoreAnswer:
    if not isinstance(raw, dict) or raw.get("type") != "score":
        raise _parse_error("answer is not a score")
    legend = {str(index): level for index, level in enumerate(question.levels)}
    if raw.get("legend") != legend:
        raise _parse_error("legend does not match the requested levels")
    raw_probabilities = raw.get("probabilities")
    if not isinstance(raw_probabilities, dict) or raw_probabilities.keys() != legend.keys():
        raise _parse_error("probabilities do not match the levels")
    probabilities = {key: _unit(value) for key, value in raw_probabilities.items()}
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=_SUM_TOLERANCE):
        raise _parse_error("probabilities do not sum to 1")
    score = raw.get("score")
    maximum = len(question.levels) - 1
    if (
        isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(score)
        or not 0 <= score <= maximum
    ):
        raise _parse_error("score is not a finite index within the requested levels")
    expected = sum(int(key) * value for key, value in probabilities.items())
    if not math.isclose(score, expected, abs_tol=_score_abs_tolerance(score, probabilities)):
        raise _parse_error("score differs from the probability-weighted index")
    return ScoreAnswer(float(score), probabilities, _unit(raw.get("confidence")))


def _parse_choice(raw: Any, question: ChoiceQuestion) -> ChoiceAnswer:
    if not isinstance(raw, dict) or raw.get("type") != "choice":
        raise _parse_error("answer is not a choice")
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in question.criteria:
        raise _parse_error("choice is not an offered option")
    raw_probabilities = raw.get("probabilities")
    if (
        not isinstance(raw_probabilities, dict)
        or raw_probabilities.keys() != question.criteria.keys()
    ):
        raise _parse_error("probabilities do not match the options")
    probabilities = {option: _unit(value) for option, value in raw_probabilities.items()}
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=_SUM_TOLERANCE):
        raise _parse_error("probabilities do not sum to 1")
    if probabilities[choice] < max(probabilities.values()) - _TIE_TOLERANCE:
        raise _parse_error("choice is not the most probable option")
    return ChoiceAnswer(
        choice=choice,
        probabilities=probabilities,
        confidence=_unit(raw.get("confidence")),
    )


def _unit(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise _parse_error("probability or confidence is not a finite number in [0, 1]")
    return float(value)


def _check_not_truncated(body: dict[str, Any], backend_max: int) -> None:
    usage = body.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    if (
        isinstance(tokens, bool)
        or not isinstance(tokens, int)
        or tokens < 0
        or tokens >= backend_max - 1
    ):
        raise DecisionsUnavailable(
            "truncated", "input_tokens is missing or at the backend state-token ceiling"
        )


async def _fetch_card_identity(
    base: str, model: str, backend_max: int, timeout_seconds: float
) -> str | None:
    try:
        async with _client(timeout_seconds) as client:
            response = await client.get(f"{base.rstrip('/')}/v1/models")
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return _card_identity(payload, model, backend_max)


def _card_identity(payload: Any, model: str, backend_max: int) -> str | None:
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    card = next(
        (entry for entry in entries if isinstance(entry, dict) and entry.get("id") == model),
        None,
    )
    if card is None or any(key not in card for key in _CARD_FIELDS):
        return None
    fields = {key: card[key] for key in _CARD_FIELDS}
    fields["backend_max_state_tokens"] = backend_max
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _schema_hash(
    questions: Mapping[str, ChoiceQuestion | str | ScoreQuestion | BoxQuestion],
) -> str:
    shape: dict[str, dict[str, object]] = {
        key: {"type": "choice", "options": sorted(question.criteria)}
        if isinstance(question, ChoiceQuestion)
        else {"type": "score", "levels": len(question.levels)}
        if isinstance(question, ScoreQuestion)
        else {"type": "box"}
        if isinstance(question, BoxQuestion)
        else {"type": "noul"}
        for key, question in questions.items()
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]


def _parse_error(detail: str) -> DecisionsUnavailable:
    return DecisionsUnavailable("parse", detail)
