"""Decision-classifier rerank for hybrid MCP tool recommendations (tool_rerank consumer)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.ai import decisions_shadow
from gobby.ai.decisions import DecisionResult, DecisionsUnavailable, NoulAnswer
from gobby.config.ai import DecisionsConfig, RerankConsumerConfig
from gobby.mcp_proxy.services import recommendation as recommendation_module
from gobby.mcp_proxy.services.recommendation import RecommendationService

MODEL = "jev-1"
IDENTITY = "response:jev-1.13.0"
TASK = "rename a git branch"
_PROPOSITION_ID = re.compile(r"Tool `([^`]+)`")


def _candidate(index: int) -> dict[str, str]:
    return {"server": "git", "tool": f"tool_{index}", "reason": f"does thing {index}"}


def _candidate_id(index: int) -> str:
    return f"git/tool_{index}"


def _candidates(count: int) -> list[dict[str, str]]:
    return [_candidate(index) for index in range(count)]


def _llm_response(indexes: list[int]) -> str:
    return json.dumps({"recommendations": [_candidate(index) for index in indexes]})


def _pick[T](values: list[T] | None, index: int, default: T) -> T:
    if values is None or index >= len(values):
        return default
    return values[index]


def _proposition_id(text: str) -> str:
    match = _PROPOSITION_ID.search(text)
    assert match is not None, text
    return match.group(1)


def _pending_tasks() -> list[asyncio.Task[Any]]:
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current and not task.done()]


def _ids(recommendations: list[dict[str, str]]) -> list[str]:
    return [f"{item['server']}/{item['tool']}" for item in recommendations]


class FakeDecisions:
    """Stands in for DecisionService; answers come from scores keyed by candidate id."""

    def __init__(
        self,
        scores: Mapping[str, float],
        *,
        identities: list[str | None] | None = None,
        models: list[str] | None = None,
        fail_at: int | None = None,
        error: BaseException | None = None,
        delay: float = 0.0,
        oversized: frozenset[str] = frozenset(),
        entered: asyncio.Event | None = None,
    ) -> None:
        self.scores = scores
        self.identities = identities
        self.models = models
        self.fail_at = fail_at
        self.error = error
        self.delay = delay
        self.oversized = oversized
        self.entered = entered
        self.calls: list[dict[str, str]] = []

    def request_fits(self, state: Mapping[str, Any], questions: Mapping[str, str]) -> bool:
        if len(questions) > 64:
            return False
        return not any(_proposition_id(text) in self.oversized for text in questions.values())

    def estimate_request_tokens(
        self, state: Mapping[str, Any], questions: Mapping[str, str]
    ) -> int:
        return 10 * len(questions)

    async def noul(
        self,
        consumer: str,
        state: Mapping[str, Any],
        propositions: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[NoulAnswer]:
        index = len(self.calls)
        self.calls.append(dict(propositions))
        if self.entered is not None:
            self.entered.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.fail_at == index:
            raise DecisionsUnavailable("http_status", "upstream returned 500")
        answers = {
            key: NoulAnswer(probability=self.scores[_proposition_id(text)])
            for key, text in propositions.items()
        }
        return DecisionResult(
            answers=answers,
            backend_identity=_pick(self.identities, index, IDENTITY),
            response_model=_pick(self.models, index, MODEL),
        )


def _config(rerank: RerankConsumerConfig, *, timeout_seconds: float = 2.0) -> DecisionsConfig:
    return DecisionsConfig(
        api_base="https://decisions.test",
        model=MODEL,
        timeout_seconds=timeout_seconds,
        tool_rerank=rerank,
    )


def _shadow() -> RerankConsumerConfig:
    return RerankConsumerConfig(mode="shadow")


def _enforce(**overrides: Any) -> RerankConsumerConfig:
    values: dict[str, Any] = {
        "mode": "enforce",
        "evaluated_model": MODEL,
        "evaluated_backend": IDENTITY,
        "min_probability": 0.5,
    }
    values |= overrides
    return RerankConsumerConfig(**values)


def _llm(
    response: str | None = None,
    *,
    error: BaseException | None = None,
    delay: float = 0.0,
) -> MagicMock:
    async def call_feature(*args: Any, **kwargs: Any) -> str:
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        assert response is not None
        return response

    llm = MagicMock()
    llm.call_feature = AsyncMock(side_effect=call_feature)
    return llm


@pytest.fixture
def shadow_records(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def record(consumer: str, body: Mapping[str, Any]) -> None:
        records.append({"consumer": consumer, **body})

    monkeypatch.setattr(decisions_shadow, "record", record)
    return records


def _service(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeDecisions,
    llm: MagicMock,
    current: dict[str, DecisionsConfig],
    semantic: list[dict[str, str]],
) -> RecommendationService:
    monkeypatch.setattr(recommendation_module, "get_decision_service", lambda config: fake)
    svc = RecommendationService(
        llm_service=llm,
        mcp_manager=MagicMock(),
        db=MagicMock(),
        decisions_resolver=lambda: current["config"],
    )
    svc._loader = MagicMock()
    svc._loader.render.return_value = "recommend tools for this task"
    semantic_result = {"success": True, "search_mode": "semantic", "recommendations": semantic}
    monkeypatch.setattr(svc, "_recommend_semantic", AsyncMock(return_value=semantic_result))
    return svc


async def _recommend(svc: RecommendationService, top_k: int = 10) -> dict[str, Any]:
    return await svc.recommend_tools(TASK, search_mode="hybrid", top_k=top_k)


async def test_shadow_keeps_llm_rerank(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    # The classifier would put tool_0 first; the LLM rerank puts tool_2 first. The LLM call
    # is slower than the classifier, as in production, so the classifier finishes and records.
    fake = FakeDecisions({"git/tool_0": 0.9, "git/tool_1": 0.2, "git/tool_2": 0.1})
    llm = _llm(_llm_response([2, 0, 1]), delay=0.1)
    current = {"config": _config(_shadow())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(3))

    result = await _recommend(svc)

    assert result["search_mode"] == "hybrid"
    assert _ids(result["recommendations"]) == ["git/tool_2", "git/tool_0", "git/tool_1"]
    assert _pending_tasks() == []
    assert len(shadow_records) == 1
    record = shadow_records[0]
    assert record["consumer"] == "tool_rerank"
    assert record["k"] == 10
    assert record["candidates"] == ["git/tool_0", "git/tool_1", "git/tool_2"]
    assert record["classifier"] == {
        "status": "ok",
        "reason": None,
        "answers": {"git/tool_0": 0.9, "git/tool_1": 0.2, "git/tool_2": 0.1},
    }
    assert record["incumbent"] == {
        "status": "ok",
        "verdict": ["git/tool_2", "git/tool_0", "git/tool_1"],
    }
    assert record["backend_identity"] == IDENTITY
    assert record["response_model"] == MODEL


async def test_shadow_cancels_stalled_classifier(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    fake = FakeDecisions({"git/tool_0": 0.9, "git/tool_1": 0.8}, delay=3600)
    llm = _llm(_llm_response([1, 0]))
    current = {"config": _config(_shadow())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    result = await _recommend(svc)

    assert _ids(result["recommendations"]) == ["git/tool_1", "git/tool_0"]
    assert _pending_tasks() == []
    assert shadow_records[0]["classifier"]["status"] == "unavailable"
    assert shadow_records[0]["classifier"]["reason"] == "cancelled"


async def test_shadow_incumbent_keeps_result_past_classifier_timeout(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    fake = FakeDecisions({"git/tool_0": 0.9, "git/tool_1": 0.8}, delay=10)
    llm = _llm(_llm_response([1, 0]), delay=0.2)
    current = {"config": _config(_shadow(), timeout_seconds=0.05)}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    result = await _recommend(svc)

    assert _ids(result["recommendations"]) == ["git/tool_1", "git/tool_0"]
    assert result["search_mode"] == "hybrid"
    assert shadow_records[0]["classifier"]["reason"] == "timeout"


async def test_shadow_classifier_exception_keeps_incumbent(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    fake = FakeDecisions({}, error=RuntimeError("classifier exploded"))
    llm = _llm(_llm_response([1, 0]), delay=0.1)
    current = {"config": _config(_shadow())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    result = await _recommend(svc)

    assert _ids(result["recommendations"]) == ["git/tool_1", "git/tool_0"]
    assert shadow_records[0]["classifier"] == {
        "status": "unavailable",
        "reason": "error",
        "answers": None,
    }


async def test_shadow_caller_cancellation_leaves_no_task(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    entered = asyncio.Event()
    fake = FakeDecisions({"git/tool_0": 0.9, "git/tool_1": 0.8}, delay=3600, entered=entered)
    llm = _llm(_llm_response([1, 0]), delay=3600)
    current = {"config": _config(_shadow())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    request = asyncio.create_task(_recommend(svc))
    await entered.wait()
    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request
    assert _pending_tasks() == []
    assert shadow_records == []


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        pytest.param(
            {"git/tool_0": 0.9, "git/tool_1": 0.2, "git/tool_2": 0.7},
            ["git/tool_0", "git/tool_2"],
            id="ranks-and-drops-below-floor",
        ),
        pytest.param(
            {"git/tool_0": 0.1, "git/tool_1": 0.2, "git/tool_2": 0.3},
            [],
            id="reject-all-is-valid",
        ),
    ],
)
async def test_enforce_ranks_rejects_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
    scores: dict[str, float],
    expected: list[str],
) -> None:
    fake = FakeDecisions(scores)
    llm = _llm(_llm_response([1, 2, 0]))
    current = {"config": _config(_enforce())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(3))

    result = await _recommend(svc)

    assert result["search_mode"] == "decide"
    assert _ids(result["recommendations"]) == expected
    llm.call_feature.assert_not_awaited()
    assert shadow_records == []


async def test_enforce_unavailable_classifier_runs_llm_rerank(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    fake = FakeDecisions({}, error=DecisionsUnavailable("transport", "connection reset"))
    llm = _llm(_llm_response([1, 0]))
    current = {"config": _config(_enforce())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    result = await _recommend(svc)

    assert result["search_mode"] == "hybrid"
    assert _ids(result["recommendations"]) == ["git/tool_1", "git/tool_0"]
    assert shadow_records[0]["classifier"]["reason"] == "transport"
    assert shadow_records[0]["incumbent"]["status"] == "ok"


async def test_mismatch_warning_follows_the_routed_noul_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning window comes from the endpoint that serves Noul, not the default."""
    fake = FakeDecisions({}, error=DecisionsUnavailable("transport", "connection reset"))
    llm = _llm(error=RuntimeError("llm down"))
    config = DecisionsConfig(
        api_base="https://decisions.test",
        model=MODEL,
        failure_cooldown_seconds=3600,
        overrides={
            "noul": {
                "api_base": "https://noul.test",
                "model": "noul-model",
                "failure_cooldown_seconds": 0,
            }
        },
        tool_rerank=_enforce(evaluated_model="jev-0"),
    )
    svc = _service(monkeypatch, fake, llm, {"config": config}, _candidates(2))

    await _recommend(svc)
    await _recommend(svc)

    mismatch_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "evaluated_backend_mismatch" in record.getMessage()
    ]
    assert len(mismatch_warnings) == 2


async def test_enforce_double_failure_returns_semantic_order(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    fake = FakeDecisions({}, error=DecisionsUnavailable("transport", "connection reset"))
    llm = _llm(error=RuntimeError("llm down"))
    current = {"config": _config(_enforce())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    result = await _recommend(svc)

    assert result["search_mode"] == "hybrid_fallback"
    assert _ids(result["recommendations"]) == ["git/tool_0", "git/tool_1"]
    assert shadow_records[0]["incumbent"] == {
        "status": "unavailable",
        "verdict": ["git/tool_0", "git/tool_1"],
    }


@pytest.mark.parametrize(
    ("rerank", "fake_kwargs"),
    [
        pytest.param(_enforce(evaluated_model="jev-0"), {}, id="model-mismatch"),
        pytest.param(_enforce(evaluated_backend="response:other"), {}, id="backend-mismatch"),
        pytest.param(_enforce(), {"identities": [None]}, id="no-backend-identity"),
    ],
)
async def test_model_mismatch_downgrades_to_shadow(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
    rerank: RerankConsumerConfig,
    fake_kwargs: dict[str, Any],
) -> None:
    fake = FakeDecisions({"git/tool_0": 0.9, "git/tool_1": 0.8}, **fake_kwargs)
    llm = _llm(_llm_response([1, 0]))
    current = {"config": _config(rerank)}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    result = await _recommend(svc)

    assert result["search_mode"] == "hybrid"
    assert _ids(result["recommendations"]) == ["git/tool_1", "git/tool_0"]
    assert len(shadow_records) == 1


async def test_mode_and_floor_changes_reach_next_call(
    monkeypatch: pytest.MonkeyPatch, shadow_records: list[dict[str, Any]]
) -> None:
    fake = FakeDecisions({"git/tool_0": 0.9, "git/tool_1": 0.6})
    llm = _llm(_llm_response([1, 0]))
    current = {"config": _config(_shadow())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(2))

    first = await _recommend(svc)
    assert first["search_mode"] == "hybrid"

    current["config"] = _config(_enforce(min_probability=0.5))
    second = await _recommend(svc)
    assert second["search_mode"] == "decide"
    assert _ids(second["recommendations"]) == ["git/tool_0", "git/tool_1"]

    current["config"] = _config(_enforce(min_probability=0.95))
    third = await _recommend(svc)
    assert third["search_mode"] == "decide"
    assert third["recommendations"] == []


@pytest.mark.parametrize(
    ("scores", "fake_kwargs", "top_k", "expected", "search_mode", "reason"),
    [
        pytest.param(
            {_candidate_id(index): 0.5 + index / 200 for index in range(65)},
            {},
            10,
            [_candidate_id(index) for index in range(64, 54, -1)],
            "decide",
            None,
            id="split-keeps-top-k-and-maps-keys-back",
        ),
        pytest.param(
            {_candidate_id(index): 0.9 for index in range(3)},
            {},
            2,
            ["git/tool_0", "git/tool_1"],
            "decide",
            None,
            id="equal-probabilities-keep-semantic-order",
        ),
        pytest.param(
            {_candidate_id(index): 0.9 for index in range(65)},
            {"fail_at": 1},
            10,
            [_candidate_id(index) for index in range(1, 11)],
            "hybrid",
            "http_status",
            id="failed-second-batch-is-whole-unavailable",
        ),
        pytest.param(
            {_candidate_id(index): 0.9 for index in range(65)},
            {"identities": [IDENTITY, "response:other"]},
            10,
            [_candidate_id(index) for index in range(1, 11)],
            "hybrid",
            "identity_mismatch",
            id="differing-identities-are-unavailable",
        ),
        pytest.param(
            {_candidate_id(index): 0.9 for index in range(65)},
            {"models": [MODEL, "jev-2"]},
            10,
            [_candidate_id(index) for index in range(1, 11)],
            "hybrid",
            "identity_mismatch",
            id="differing-response-models-are-unavailable",
        ),
        pytest.param(
            {_candidate_id(index): 0.9 for index in range(65)},
            {"identities": [IDENTITY, None]},
            10,
            [_candidate_id(index) for index in range(1, 11)],
            "hybrid",
            "identity_mismatch",
            id="known-beside-unknown-identity-is-unavailable",
        ),
        pytest.param(
            {_candidate_id(index): 0.9 for index in range(65)},
            {"oversized": frozenset({_candidate_id(64)})},
            10,
            [_candidate_id(index) for index in range(1, 11)],
            "hybrid",
            "oversize",
            id="oversize-singleton-is-whole-unavailable",
        ),
    ],
)
async def test_rerank_batches_are_bounded_and_complete(
    monkeypatch: pytest.MonkeyPatch,
    shadow_records: list[dict[str, Any]],
    scores: dict[str, float],
    fake_kwargs: dict[str, Any],
    top_k: int,
    expected: list[str],
    search_mode: str,
    reason: str | None,
) -> None:
    count = len(scores)
    fake = FakeDecisions(scores, **fake_kwargs)
    # The LLM rerank puts tool_1 first, so a fallback is distinguishable from a ranking.
    llm = _llm(_llm_response([*range(1, count), 0]))
    current = {"config": _config(_enforce())}
    svc = _service(monkeypatch, fake, llm, current, _candidates(count))

    result = await _recommend(svc, top_k=top_k)

    assert result["search_mode"] == search_mode
    assert _ids(result["recommendations"]) == expected
    if reason is None:
        assert [len(call) for call in fake.calls] == [min(count, 64), *([1] if count > 64 else [])]
        assert list(fake.calls[0]) == [f"c{index}" for index in range(min(count, 64))]
    else:
        assert shadow_records[0]["classifier"]["status"] == "unavailable"
        assert shadow_records[0]["classifier"]["reason"] == reason
