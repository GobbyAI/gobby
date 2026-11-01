"""Cascade confirmation for found-work shirk candidates: classifier first, LLM on escalation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.ai import decisions_shadow
from gobby.ai.decisions import DecisionResult, DecisionsUnavailable, NoulAnswer
from gobby.config.ai import CascadeConsumerConfig, DecisionsConfig
from gobby.config.tasks import TaskValidationConfig
from gobby.workflows import found_work_confirm
from gobby.workflows.found_work_confirm import confirm_shirk

pytestmark = pytest.mark.unit

MODEL = "jev-1"
IDENTITY = "response:jev-1.13.0"
MESSAGE = "The parser is broken. Should I fix it?"
PROMPT = "Implement the parser."
BLOCK = {"block": True, "reason": "shirk"}
CLEAR = {"block": False, "reason": "not a shirk"}


class _Classifier:
    """Stands in for DecisionService.noul; answers one found_work proposition."""

    def __init__(
        self,
        probability: float = 0.5,
        *,
        error: BaseException | None = None,
        delay: float = 0.0,
        backend_identity: str | None = IDENTITY,
        response_model: str = MODEL,
    ) -> None:
        self.probability = probability
        self.error = error
        self.delay = delay
        self.backend_identity = backend_identity
        self.response_model = response_model
        self.consumers: list[str] = []
        self.timeouts: list[float | None] = []
        self.entered = asyncio.Event()

    async def noul(
        self,
        consumer: str,
        state: Mapping[str, Any],
        propositions: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[NoulAnswer]:
        self.consumers.append(consumer)
        self.timeouts.append(timeout_seconds)
        self.entered.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return DecisionResult(
            answers={key: NoulAnswer(probability=self.probability) for key in propositions},
            backend_identity=self.backend_identity,
            response_model=self.response_model,
        )


class _Daemon:
    """Stands in for DaemonConfig: ai.decisions plus the task validation policy."""

    def __init__(
        self,
        found_work: CascadeConsumerConfig,
        *,
        cap: float = 8.0,
        enabled: bool = True,
    ) -> None:
        self.ai = SimpleNamespace(
            decisions=DecisionsConfig(
                api_base="https://decisions.test",
                model=MODEL,
                found_work=found_work,
            )
        )
        self.validation = TaskValidationConfig(enabled=enabled, confirmation_timeout_seconds=cap)

    def get_gobby_tasks_config(self) -> SimpleNamespace:
        return SimpleNamespace(validation=self.validation)


def _found_work(
    mode: str,
    *,
    accept_below: float = 0.1,
    accept_above: float = 0.9,
    evaluated_model: str | None = MODEL,
    evaluated_backend: str | None = IDENTITY,
) -> CascadeConsumerConfig:
    return CascadeConsumerConfig(
        mode=mode,
        evaluated_model=evaluated_model,
        evaluated_backend=evaluated_backend,
        accept_below=accept_below,
        accept_above=accept_above,
    )


def _llm(
    verdict: Any = None,
    *,
    error: BaseException | None = None,
    delay: float = 0.0,
) -> MagicMock:
    async def call_json_feature(*args: Any, **kwargs: Any) -> Any:
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        return verdict

    llm = MagicMock()
    llm.call_json_feature = AsyncMock(side_effect=call_json_feature)
    return llm


def _install(monkeypatch: pytest.MonkeyPatch, classifier: _Classifier) -> None:
    monkeypatch.setattr(found_work_confirm, "get_decision_service", lambda config: classifier)


@pytest.fixture
def shadow_records(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def record(consumer: str, body: Mapping[str, Any]) -> None:
        records.append({"consumer": consumer, **body})

    monkeypatch.setattr(decisions_shadow, "record", record)
    return records


@pytest.mark.asyncio
async def test_cascade_accepts_confident_and_escalates_uncertain(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
) -> None:
    # Each LLM verdict is the opposite of the cascade's answer for the confident
    # cases, so the result proves which path decided.
    cases = [
        (0.95, CLEAR, True, 0),
        (0.05, BLOCK, False, 0),
        (0.5, BLOCK, True, 1),
    ]
    for probability, llm_verdict, expected, llm_calls in cases:
        classifier = _Classifier(probability)
        _install(monkeypatch, classifier)
        llm = _llm(llm_verdict)

        result = await confirm_shirk(
            MESSAGE,
            PROMPT,
            llm_service=llm,
            daemon_config=_Daemon(_found_work("enforce")),
        )

        assert result is expected, probability
        assert llm.call_json_feature.await_count == llm_calls, probability
        assert classifier.consumers == ["found_work"]


@pytest.mark.asyncio
async def test_outage_never_clears_a_finding(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
) -> None:
    # The classifier spends 0.2 s before failing, so the escalated LLM call gets
    # the remaining ~0.8 s of the 1 s cap.
    for error in (
        DecisionsUnavailable("http_status", "upstream returned 500"),
        RuntimeError("boom"),
    ):
        _install(monkeypatch, _Classifier(error=error, delay=0.2))
        llm = _llm(BLOCK)

        result = await confirm_shirk(
            MESSAGE,
            PROMPT,
            llm_service=llm,
            daemon_config=_Daemon(_found_work("enforce"), cap=1.0),
        )

        assert result is True
        llm.call_json_feature.assert_awaited_once()
        remaining = llm.call_json_feature.await_args.kwargs["total_timeout_seconds"]
        assert 0.5 < remaining < 0.9

    # With the LLM also unavailable, the outage yields no verdict rather than a clear.
    _install(monkeypatch, _Classifier(error=DecisionsUnavailable("http_status", "down")))
    result = await confirm_shirk(
        MESSAGE,
        PROMPT,
        llm_service=_llm(error=TimeoutError()),
        daemon_config=_Daemon(_found_work("enforce")),
    )
    assert result is None
    assert shadow_records == []


@pytest.mark.asyncio
async def test_shadow_returns_llm_verdict(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
) -> None:
    classifier = _Classifier(0.95)
    _install(monkeypatch, classifier)
    llm = _llm(CLEAR)

    result = await confirm_shirk(
        MESSAGE,
        PROMPT,
        llm_service=llm,
        daemon_config=_Daemon(_found_work("shadow")),
    )

    assert result is False
    llm.call_json_feature.assert_awaited_once()
    assert classifier.consumers == ["found_work"]
    assert len(shadow_records) == 1
    record = shadow_records[0]
    assert record["consumer"] == "found_work"
    assert record["classifier"]["status"] == "ok"
    assert record["classifier"]["probability"] == 0.95
    assert record["incumbent"]["verdict"] is False

    # An unexpected classifier failure leaves the LLM verdict intact and is recorded.
    shadow_records.clear()
    _install(monkeypatch, _Classifier(error=RuntimeError("boom")))
    llm = _llm(BLOCK)

    result = await confirm_shirk(
        MESSAGE,
        PROMPT,
        llm_service=llm,
        daemon_config=_Daemon(_found_work("shadow")),
    )

    assert result is True
    assert len(shadow_records) == 1
    assert shadow_records[0]["classifier"]["status"] == "unavailable"
    assert shadow_records[0]["incumbent"]["verdict"] is True


@pytest.mark.asyncio
async def test_budget_admission_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
) -> None:
    # Budget: a 1 s cap bounds the whole confirmation in shadow and in enforce.
    _install(monkeypatch, _Classifier(0.95, delay=5.0))
    started = time.monotonic()
    result = await confirm_shirk(
        MESSAGE,
        PROMPT,
        llm_service=_llm(BLOCK, delay=5.0),
        daemon_config=_Daemon(_found_work("shadow"), cap=1.0),
    )
    assert result is None
    assert time.monotonic() - started < 2.5

    classifier = _Classifier(0.95, delay=5.0)
    _install(monkeypatch, classifier)
    llm = _llm(BLOCK)
    started = time.monotonic()
    result = await confirm_shirk(
        MESSAGE,
        PROMPT,
        llm_service=llm,
        daemon_config=_Daemon(_found_work("enforce"), cap=1.0),
    )
    assert result is None
    assert time.monotonic() - started < 2.5
    llm.call_json_feature.assert_not_awaited()

    # Off: today's LLM path only, with no classifier call.
    classifier = _Classifier(0.95)
    _install(monkeypatch, classifier)
    llm = _llm(BLOCK)
    result = await confirm_shirk(
        MESSAGE,
        PROMPT,
        llm_service=llm,
        daemon_config=_Daemon(_found_work("off")),
    )
    assert result is True
    assert classifier.consumers == []
    llm.call_json_feature.assert_awaited_once()

    # Admission: a missing service, a missing config, or disabled validation returns
    # None before any classifier call.
    classifier = _Classifier(0.95)
    _install(monkeypatch, classifier)
    assert (
        await confirm_shirk(
            MESSAGE,
            PROMPT,
            llm_service=None,
            daemon_config=_Daemon(_found_work("enforce")),
        )
        is None
    )
    assert await confirm_shirk(MESSAGE, PROMPT, llm_service=_llm(BLOCK), daemon_config=None) is None
    disabled = _llm(BLOCK)
    assert (
        await confirm_shirk(
            MESSAGE,
            PROMPT,
            llm_service=disabled,
            daemon_config=_Daemon(_found_work("enforce"), enabled=False),
        )
        is None
    )
    assert classifier.consumers == []
    disabled.call_json_feature.assert_not_awaited()

    # A mismatched evaluated model or backend behaves as shadow: the LLM decides and
    # one shadow record is written.
    for found_work, classifier in (
        (_found_work("enforce", evaluated_model="jev-0"), _Classifier(0.95)),
        (_found_work("enforce"), _Classifier(0.95, backend_identity="response:other")),
    ):
        shadow_records.clear()
        _install(monkeypatch, classifier)
        llm = _llm(CLEAR)
        result = await confirm_shirk(
            MESSAGE,
            PROMPT,
            llm_service=llm,
            daemon_config=_Daemon(found_work),
        )
        assert result is False
        llm.call_json_feature.assert_awaited_once()
        assert len(shadow_records) == 1

    # Caller cancellation propagates and leaves no pending child task.
    classifier = _Classifier(0.95, delay=5.0)
    _install(monkeypatch, classifier)
    task = asyncio.create_task(
        confirm_shirk(
            MESSAGE,
            PROMPT,
            llm_service=_llm(BLOCK, delay=5.0),
            daemon_config=_Daemon(_found_work("shadow"), cap=8.0),
        )
    )
    await asyncio.wait_for(classifier.entered.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert leftover == []

    # A mode or threshold change reaches the next call through the same cached service.
    _install(monkeypatch, _Classifier(0.6))
    first = _llm(CLEAR)
    assert (
        await confirm_shirk(
            MESSAGE,
            PROMPT,
            llm_service=first,
            daemon_config=_Daemon(_found_work("shadow")),
        )
        is False
    )
    later = _llm(CLEAR)
    result = await confirm_shirk(
        MESSAGE,
        PROMPT,
        llm_service=later,
        daemon_config=_Daemon(_found_work("enforce", accept_above=0.5)),
    )
    assert result is True
    later.call_json_feature.assert_not_awaited()
