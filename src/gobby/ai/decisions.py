"""DecisionService: the Choice call path over the decision wires (task #24244)."""

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from gobby.config.ai import DecisionsConfig
from gobby.llm.claude_runtime import retry_async

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
        config.resolved_backend_max_state_tokens,
        config.failure_cooldown_seconds,
    )


class DecisionService:
    def __init__(self, config: DecisionsConfig) -> None:
        self._config = config
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
        config = self._config
        started = time.monotonic()
        outcome = "error"
        estimated = 0
        try:
            base, model = config.api_base, config.model
            if base is None or model is None:
                raise DecisionsUnavailable("unconfigured", "decisions api_base is unset")
            _check_request(questions)
            body = _request_body(model, config.wire_api, state, questions)
            estimated = estimate_tokens(body)
            if estimated > config.max_input_tokens:
                raise DecisionsUnavailable(
                    "oversize", f"~{estimated} tokens exceeds {config.max_input_tokens}"
                )
            if _now() < self._cooldown_until:
                raise DecisionsUnavailable("cooldown", "a recent decision failure is cooling down")
            result = await self._dial(base, model, questions, body, timeout_seconds)
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

    async def _dial(
        self,
        base: str,
        model: str,
        questions: Mapping[str, ChoiceQuestion],
        body: dict[str, Any],
        timeout_seconds: float | None,
    ) -> DecisionResult[ChoiceAnswer]:
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
        card: asyncio.Task[str | None] | None = None
        try:
            async with asyncio.timeout(budget):
                if config.identity_contract == "model_card":
                    card = asyncio.create_task(
                        _fetch_card_identity(base, model, config.resolved_backend_max_state_tokens)
                    )
                payload = await retry_async(
                    lambda: _post(url, headers, body),
                    max_retries=_ATTEMPTS,
                    delay=_RETRY_DELAY,
                    retry_if=_retryable,
                )
                body_json = _unwrap(payload, config.wire_api)
                response_model, answers = _parse_answers(body_json, questions)
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
            self._cooldown_until = _now() + config.failure_cooldown_seconds
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


def _check_request(questions: Mapping[str, ChoiceQuestion]) -> None:
    if not 1 <= len(questions) <= _MAX_QUESTIONS:
        raise DecisionsUnavailable(
            "invalid_request", f"{len(questions)} questions; 1 to {_MAX_QUESTIONS} allowed"
        )
    for key, question in questions.items():
        if _KEY_PATTERN.fullmatch(key) is None:
            raise DecisionsUnavailable(
                "invalid_request", "question keys must match [A-Za-z0-9_.-]{1,100}"
            )
        if not _MIN_OPTIONS <= len(question.criteria) <= _MAX_OPTIONS:
            raise DecisionsUnavailable(
                "invalid_request",
                f"a Choice question takes {_MIN_OPTIONS} to {_MAX_OPTIONS} options",
            )


def _request_body(
    model: str, wire_api: str, state: Mapping[str, Any], questions: Mapping[str, ChoiceQuestion]
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "state": dict(state),
        "questions": {
            key: {
                "type": "choice",
                "instructions": question.instructions,
                "criteria": dict(question.criteria),
            }
            for key, question in questions.items()
        },
    }
    if wire_api == "openrouter-decisions":
        body["provider"] = {"zdr": True, "data_collection": "deny"}
    return body


def _endpoint(wire_api: str, base: str, model: str) -> str:
    root = base.rstrip("/")
    if wire_api == "systemone":
        return f"{root}/v1/systemone"
    if wire_api == "openrouter-decisions":
        return f"{root}/alpha/decisions"
    return f"{root}/ai/run/@cf/cloudflare/{model}"


def _client() -> httpx.AsyncClient:
    # The wire URL is the only host dialed: no environment proxy and no redirect hop.
    return httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=None)


async def _post(url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> Any:
    async with _client() as client:
        response = await client.post(url, json=body, headers=headers)
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


def _parse_answers(
    body: dict[str, Any], questions: Mapping[str, ChoiceQuestion]
) -> tuple[str, dict[str, ChoiceAnswer]]:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _parse_error("response model is missing")
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, dict) or raw_answers.keys() != questions.keys():
        raise _parse_error("answers do not match the questions")
    answers = {
        key: _parse_choice(raw_answers[key], question) for key, question in questions.items()
    }
    return model, answers


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


async def _fetch_card_identity(base: str, model: str, backend_max: int) -> str | None:
    try:
        async with _client() as client:
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


def _schema_hash(questions: Mapping[str, ChoiceQuestion]) -> str:
    shape = {key: sorted(question.criteria) for key, question in questions.items()}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]


def _parse_error(detail: str) -> DecisionsUnavailable:
    return DecisionsUnavailable("parse", detail)
