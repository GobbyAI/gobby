"""Decision service: per-type endpoint routing, dialing, cooldowns, and the shared-service cache."""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from gobby.ai.decisions_wire import (
    _MAX_QUESTIONS,
    BoxAnswer,
    BoxImage,
    BoxQuestion,
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionResult,
    DecisionsUnavailable,
    DecisionsUnavailableReason,
    NoulAnswer,
    ScoreAnswer,
    ScoreQuestion,
    _as_unavailable,
    _check_not_truncated,
    _check_request,
    _check_request_limits,
    _endpoint,
    _estimate_body_tokens,
    _fetch_card_identity,
    _HTTPStatusError,
    _parse_answers,
    _parse_box,
    _parse_choice,
    _parse_noul,
    _parse_score,
    _post,
    _request_body,
    _retryable,
    _schema_hash,
    _unwrap,
    estimate_tokens,
)
from gobby.config.ai import (
    DECISION_KINDS,
    DecisionEndpointConfig,
    DecisionKind,
    DecisionsConfig,
    decision_wire_refusal,
)
from gobby.llm.claude_runtime import retry_async

__all__ = [
    "BoxAnswer",
    "BoxImage",
    "BoxQuestion",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "DecisionQuestion",
    "DecisionResult",
    "DecisionService",
    "DecisionsUnavailable",
    "DecisionsUnavailableReason",
    "NoulAnswer",
    "QuestionOutcome",
    "ScoreAnswer",
    "ScoreQuestion",
    "estimate_tokens",
    "get_decision_service",
]

logger = logging.getLogger(__name__)

_service_cache: tuple[tuple[object, ...], "DecisionService"] | None = None
_ATTEMPTS = 3
_RETRY_DELAY = 0.1

type DecisionQuestion = ChoiceQuestion | str | ScoreQuestion | BoxQuestion


def _now() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class QuestionOutcome:
    """One question's result from ``evaluate``: its answer, or the failure that refused it."""

    kind: DecisionKind
    answer: ChoiceAnswer | NoulAnswer | ScoreAnswer | BoxAnswer | None
    error: DecisionsUnavailable | None
    response_model: str | None
    backend_identity: str | None


@dataclass(frozen=True)
class _Route:
    """The endpoint that serves one decision type. ``slot`` keys its cooldown and logs."""

    slot: str
    endpoint: DecisionEndpointConfig
    base: str
    model: str


_PARSERS: dict[DecisionKind, Callable[[Any, Any], Any]] = {
    "choice": _parse_choice,
    "noul": _parse_noul,
    "score": _parse_score,
    "box": _parse_box,
}


def _kind(question: DecisionQuestion) -> DecisionKind:
    if isinstance(question, ChoiceQuestion):
        return "choice"
    if isinstance(question, ScoreQuestion):
        return "score"
    if isinstance(question, BoxQuestion):
        return "box"
    return "noul"


def _parse_question(raw: Any, question: DecisionQuestion) -> Any:
    """Parse one answer by its own question's type; a request may carry several types."""
    return _PARSERS[_kind(question)](raw, question)


def _failed(kind: DecisionKind, error: DecisionsUnavailable) -> QuestionOutcome:
    return QuestionOutcome(
        kind=kind, answer=None, error=error, response_model=None, backend_identity=None
    )


def get_decision_service(config: DecisionsConfig) -> "DecisionService":
    """Return the shared service for these endpoints; consumer policy is not part of it."""
    global _service_cache
    fingerprint = _fingerprint(config)
    if _service_cache is None or _service_cache[0] != fingerprint:
        _service_cache = (fingerprint, DecisionService(config))
    return _service_cache[1]


def _endpoint_identity(endpoint: DecisionEndpointConfig | None) -> tuple[object, ...] | None:
    if endpoint is None:
        return None
    key_digest = hashlib.sha256((endpoint.api_key or "").encode()).hexdigest()
    return (
        endpoint.api_base,
        endpoint.model,
        key_digest,
        endpoint.wire_api,
        endpoint.identity_contract,
        endpoint.timeout_seconds,
        endpoint.max_input_tokens,
        endpoint.max_request_bytes,
        endpoint.resolved_backend_max_state_tokens,
        endpoint.resolved_backend_max_question_tokens,
        endpoint.long_context,
        endpoint.failure_cooldown_seconds,
    )


def _fingerprint(config: DecisionsConfig) -> tuple[object, ...]:
    """Identity of the default endpoint and every per-type override."""
    return (
        _endpoint_identity(config),
        *(_endpoint_identity(config.overrides.endpoint(kind)) for kind in DECISION_KINDS),
    )


class DecisionService:
    """Dials each decision type's routed endpoint; one cooldown, identity and limit set per endpoint."""

    def __init__(
        self,
        config: DecisionsConfig,
        *,
        exchange_observer: Callable[[httpx.Response], None] | None = None,
    ) -> None:
        self._config = config
        self._exchange_observer = exchange_observer
        self._cooldowns: dict[str, float] = {}

    async def choose(
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, ChoiceQuestion],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[ChoiceAnswer]:
        """Return validated Choice answers, or raise ``DecisionsUnavailable``."""
        route = self._serving_route("choice")
        return await self._request(route, consumer, state, questions, timeout_seconds)

    async def noul(
        self,
        consumer: str,
        state: Mapping[str, Any],
        propositions: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[NoulAnswer]:
        """Return validated Noul answers, or raise ``DecisionsUnavailable``."""
        route = self._serving_route("noul")
        return await self._request(route, consumer, state, propositions, timeout_seconds)

    async def score(
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, ScoreQuestion],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[ScoreAnswer]:
        """Return validated Score answers, or raise ``DecisionsUnavailable``."""
        route = self._serving_route("score")
        return await self._request(route, consumer, state, questions, timeout_seconds)

    async def box(
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, BoxQuestion],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[BoxAnswer]:
        """Return validated Box answers, or raise ``DecisionsUnavailable``."""
        route = self._serving_route("box")
        return await self._request(route, consumer, state, questions, timeout_seconds)

    async def evaluate(
        self,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, DecisionQuestion],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, QuestionOutcome]:
        """Answer questions on their routed endpoints, one request per endpoint.

        Returns one outcome per question, in question order. A failure fills only the outcomes
        of the questions it refused; the other endpoints' answers are kept.
        """
        if not 1 <= len(questions) <= _MAX_QUESTIONS:
            raise DecisionsUnavailable(
                "invalid_request", f"{len(questions)} questions; 1 to {_MAX_QUESTIONS} allowed"
            )
        groups: dict[str, tuple[_Route, dict[str, DecisionQuestion]]] = {}
        merged: dict[str, QuestionOutcome] = {}
        for key, question in questions.items():
            kind = _kind(question)
            try:
                route = self._serving_route(kind)
            except DecisionsUnavailable as error:
                merged[key] = _failed(kind, error)
                continue
            groups.setdefault(route.slot, (route, {}))[1][key] = question
        settled = await asyncio.gather(
            *(
                self._settle(route, consumer, state, group, timeout_seconds)
                for route, group in groups.values()
            )
        )
        for outcomes in settled:
            merged.update(outcomes)
        return {key: merged[key] for key in questions}

    async def _settle(
        self,
        route: _Route,
        consumer: str,
        state: Mapping[str, Any],
        group: Mapping[str, DecisionQuestion],
        timeout_seconds: float | None,
    ) -> dict[str, QuestionOutcome]:
        try:
            result = await self._request(route, consumer, state, group, timeout_seconds)
        except DecisionsUnavailable as error:
            return {key: _failed(_kind(question), error) for key, question in group.items()}
        return {
            key: QuestionOutcome(
                kind=_kind(group[key]),
                answer=answer,
                error=None,
                response_model=result.response_model,
                backend_identity=result.backend_identity,
            )
            for key, answer in result.answers.items()
        }

    def estimate_request_tokens(
        self, state: Mapping[str, Any], questions: Mapping[str, DecisionQuestion]
    ) -> int:
        """Return the estimate the ``oversize`` guard compares with ``max_input_tokens``.

        A type with no route estimates against the default endpoint's body shape; ``request_fits``
        refuses it.
        """
        try:
            endpoint = self._single_route(questions).endpoint
        except DecisionsUnavailable:
            endpoint = self._config
        return _estimate_body_tokens(
            _request_body(endpoint.model or "", endpoint.wire_api, state, questions)
        )

    def request_fits(
        self, state: Mapping[str, Any], questions: Mapping[str, DecisionQuestion]
    ) -> bool:
        """Apply the same route, wire and request budgets used before dialing.

        An unserved type raises ``unconfigured``: that is a routing fact, not a size. The
        result is False only when a size budget is exceeded.
        """
        route = self._single_route(questions)
        body = _request_body(route.model, route.endpoint.wire_api, state, questions)
        try:
            _check_request_limits(body, route.endpoint, _estimate_body_tokens(body))
        except DecisionsUnavailable:
            return False
        return True

    def _serving_route(self, kind: DecisionKind) -> _Route:
        """The route that serves one type; an unserved or refused type raises ``unconfigured``."""
        route = self._route(kind)
        if route is None:
            raise DecisionsUnavailable("unconfigured", f"no decisions endpoint serves {kind}")
        refusal = decision_wire_refusal(route.endpoint.wire_api, kind)
        if refusal is not None:
            raise DecisionsUnavailable("unconfigured", refusal)
        return route

    def _single_route(self, questions: Mapping[str, DecisionQuestion]) -> _Route:
        """The one route serving every question in a request; a request never spans endpoints."""
        routes = [self._serving_route(_kind(question)) for question in questions.values()]
        if not routes or len({route.slot for route in routes}) != 1:
            raise ValueError("a decision request goes to exactly one endpoint")
        return routes[0]

    def _route(self, kind: DecisionKind) -> _Route | None:
        endpoint = self._config.endpoint_for(kind)
        if endpoint is None or endpoint.api_base is None or endpoint.model is None:
            return None
        slot = "default" if endpoint is self._config else kind
        return _Route(slot, endpoint, endpoint.api_base, endpoint.model)

    async def _request(
        self,
        route: _Route,
        consumer: str,
        state: Mapping[str, Any],
        questions: Mapping[str, DecisionQuestion],
        timeout_seconds: float | None,
    ) -> DecisionResult[Any]:
        started = time.monotonic()
        outcome = "error"
        estimated = 0
        try:
            _check_request(questions)
            body = _request_body(route.model, route.endpoint.wire_api, state, questions)
            estimated = _estimate_body_tokens(body)
            wire_body = _check_request_limits(body, route.endpoint, estimated)
            if _now() < self._cooldowns.get(route.slot, 0.0):
                raise DecisionsUnavailable("cooldown", "a recent decision failure is cooling down")
            result = await self._dial(route, questions, wire_body, timeout_seconds, _parse_question)
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
                "decisions.call consumer=%s slot=%s model=%s question_count=%d schema_hash=%s "
                "estimated_tokens=%d latency_ms=%d outcome=%s",
                consumer,
                route.slot,
                route.model,
                len(questions),
                _schema_hash(questions),
                estimated,
                int((time.monotonic() - started) * 1000),
                outcome,
            )

    async def _dial[A](
        self,
        route: _Route,
        questions: Mapping[str, Any],
        body: bytes,
        timeout_seconds: float | None,
        parse_answer: Callable[[Any, Any], A],
    ) -> DecisionResult[A]:
        config = route.endpoint
        budget = config.timeout_seconds
        if timeout_seconds is not None:
            budget = min(timeout_seconds, budget)
        # A caller budget shorter than the service timeout makes a timeout local.
        local = timeout_seconds is not None and timeout_seconds < config.timeout_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        url = _endpoint(config.wire_api, route.base, route.model)
        headers = {"authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        if config.long_context:
            headers["TypeSafe-Danger-Zone-Long-Context"] = "true"
        card: asyncio.Task[str | None] | None = None
        try:
            async with asyncio.timeout(budget):
                if config.identity_contract == "model_card":
                    card = asyncio.create_task(
                        _fetch_card_identity(
                            route.base,
                            route.model,
                            config.resolved_backend_max_state_tokens,
                            budget,
                            headers,
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
            if now >= self._cooldowns.get(route.slot, 0.0):
                self._cooldowns[route.slot] = now + config.failure_cooldown_seconds
                logger.warning(
                    "decisions cooldown opened for %s after %s; calls fail fast for %ss",
                    route.slot,
                    failure.reason,
                    config.failure_cooldown_seconds,
                )
            raise failure from None
        finally:
            if card is not None:
                card.cancel()
                await asyncio.wait({card})
