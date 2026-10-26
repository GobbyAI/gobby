"""DecisionService: Choice wires, request ceiling, cooldown, and identity (task #24244).

Every test runs against a fake httpx transport. No test dials a live decision server.
"""

import asyncio
import base64
import json
import logging
import re
import struct
import time
import zlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.ai import decisions
from gobby.ai.decisions import (
    BoxAnswer,
    BoxImage,
    BoxQuestion,
    ChoiceQuestion,
    DecisionService,
    DecisionsUnavailable,
    NoulAnswer,
    ScoreAnswer,
    ScoreQuestion,
    get_decision_service,
)
from gobby.config.ai import DecisionsConfig
from gobby.llm.image_payloads import MAX_IMAGE_BYTES, MAX_REQUEST_IMAGE_BYTES, MAX_REQUEST_IMAGES

FIXTURES = Path(__file__).parent / "fixtures"
REAL_ASYNC_CLIENT = httpx.AsyncClient
SYSTEMONE_URL = "https://decide.test/v1/systemone"
STATE: dict[str, Any] = {"finding": "unused import in module foo"}
TRIAGE = ChoiceQuestion(
    instructions="Keep only actionable findings.",
    criteria={"keep": "Actionable finding", "drop": "Noise"},
)
QUESTIONS: dict[str, ChoiceQuestion] = {"triage": TRIAGE}
USAGE: dict[str, Any] = {"input_tokens": 10, "output_tokens": 1}


class FakeWire:
    """Records every request and every client's kwargs; ``handler`` answers each request."""

    def __init__(self) -> None:
        self.handler: Callable[[httpx.Request], Any] = _must_not_dial
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict[str, Any]] = []


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _must_not_dial(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("the service dialed a server it should not have")


def _hang(_request: httpx.Request) -> Any:
    async def hang() -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)

    return hang()


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _overloaded(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(529)


def _unauthorized(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(401)


def _garbled(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"not": "a decision"})


def _truncated(request: httpx.Request) -> httpx.Response:
    return _respond("systemone", request, usage={"input_tokens": 999})


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(code)


def _json_reply(body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json=body)


def _responder(wire_api: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: _respond(wire_api, request)


def _fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return loaded


def _config(**overrides: Any) -> DecisionsConfig:
    values: dict[str, Any] = {"api_base": "https://decide.test", "model": "jev-latest"}
    values.update(overrides)
    return DecisionsConfig(**values)


def _wire_config(wire_api: str, **overrides: Any) -> DecisionsConfig:
    values: dict[str, Any] = {"wire_api": wire_api}
    if wire_api == "openrouter-decisions":
        values |= {"api_base": "https://openrouter.test/api", "model": "typesafe/jev-1.13"}
    if wire_api == "workers-ai":
        values |= {"api_base": "https://cf.test/client/v4/accounts/acct-1", "model": "clef"}
    return _config(**(values | overrides))


def _echo_answers(sent: Mapping[str, Any]) -> dict[str, Any]:
    """Answer every question with its first option, as a valid response would."""
    answers: dict[str, Any] = {}
    for key, question in sent["questions"].items():
        options = list(question["criteria"])
        probabilities = dict.fromkeys(options, 0.0)
        probabilities[options[0]] = 1.0
        answers[key] = {
            "type": "choice",
            "choice": options[0],
            "probabilities": probabilities,
            "confidence": 0.5,
        }
    return answers


def _decision(
    request: httpx.Request,
    *,
    model: str | None = None,
    usage: Mapping[str, Any] = USAGE,
) -> dict[str, Any]:
    sent = json.loads(request.content)
    return {
        "model": model or sent["model"],
        "answers": _echo_answers(sent),
        "usage": dict(usage),
    }


def _respond(
    wire_api: str,
    request: httpx.Request,
    *,
    model: str | None = None,
    usage: Mapping[str, Any] = USAGE,
) -> httpx.Response:
    body = _decision(request, model=model, usage=usage)
    if wire_api == "workers-ai":
        return httpx.Response(
            200, json={"result": body, "success": True, "errors": [], "messages": []}
        )
    return httpx.Response(200, json=body)


def _card(run_path: str, **overrides: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": "jev-latest",
        "object": "model",
        "run_path": run_path,
        "base_model": "kev-384",
        "lora_config": None,
        "dtype": "bfloat16",
        "temperature": 0.7,
        "loss": 0.12,
    }
    card.update(overrides)
    return card


def _card_handler(card_for: Callable[[], dict[str, Any] | None]) -> Callable[[httpx.Request], Any]:
    """Serve ``card_for()`` on ``/v1/models`` and a valid decision everywhere else."""

    def handler(request: httpx.Request) -> Any:
        if request.url.path != "/v1/models":
            return _respond("systemone", request)
        card = card_for()
        if card is None:
            return httpx.Response(500)
        return httpx.Response(200, json={"object": "list", "data": [card]})

    return handler


async def _identity_for(wire: FakeWire, card: dict[str, Any], **overrides: Any) -> str | None:
    wire.handler = _card_handler(lambda: card)
    service = DecisionService(_config(identity_contract="model_card", **overrides))
    result = await service.choose("test", STATE, QUESTIONS)
    return result.backend_identity


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> FakeWire:
    fake = FakeWire()

    def handle(request: httpx.Request) -> Any:
        fake.requests.append(request)
        return fake.handler(request)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        fake.client_kwargs.append(kwargs)
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(decisions, "_now", fake)
    return fake


@pytest.fixture(autouse=True)
def _fresh_service_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decisions, "_service_cache", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400])
async def test_exchange_observer_records_success_and_failure(wire: FakeWire, status: int) -> None:
    payload = _fixture("systemone_choice_response.json") if status == 200 else {"error": "refused"}
    wire.handler = lambda _request: httpx.Response(status, json=payload)
    observed: list[httpx.Response] = []
    service = DecisionService(_config(), exchange_observer=observed.append)

    if status == 200:
        await service.choose("capture", STATE, QUESTIONS)
    else:
        with pytest.raises(DecisionsUnavailable, match="400"):
            await service.choose("capture", STATE, QUESTIONS)

    assert len(observed) == 1
    assert observed[0].status_code == status
    assert observed[0].json() == payload
    assert json.loads(observed[0].request.content)["questions"]["triage"]["type"] == "choice"


@pytest.mark.asyncio
async def test_choose_posts_and_parses_documented_wire(wire: FakeWire) -> None:
    wire.handler = lambda _request: httpx.Response(
        200, json=_fixture("systemone_choice_response.json")
    )

    result = await DecisionService(_config()).choose("test", STATE, QUESTIONS)

    request = wire.requests[0]
    assert str(request.url) == SYSTEMONE_URL
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "model": "jev-latest",
        "state": STATE,
        "questions": {
            "triage": {
                "type": "choice",
                "instructions": TRIAGE.instructions,
                "criteria": dict(TRIAGE.criteria),
            }
        },
    }
    assert result.answers["triage"].choice == "keep"
    assert result.answers["triage"].probabilities == {"keep": 0.8, "drop": 0.2}
    assert result.answers["triage"].confidence == 0.75
    assert result.response_model == "jev-1.13.0"
    assert result.backend_identity is None


MALFORMED_ANSWERS = [
    pytest.param(lambda b: b.__setitem__("model", ""), id="empty-model"),
    pytest.param(lambda b: b.__setitem__("model", 7), id="non-string-model"),
    pytest.param(lambda b: b["answers"].clear(), id="missing-answer"),
    pytest.param(
        lambda b: b["answers"].update(
            extra={
                "type": "choice",
                "choice": "keep",
                "probabilities": {"keep": 0.8, "drop": 0.2},
                "confidence": 0.75,
            }
        ),
        id="extra-answer",
    ),
    pytest.param(lambda b: b["answers"]["triage"].update(type="noul"), id="wrong-answer-type"),
    pytest.param(lambda b: b["answers"]["triage"].update(choice="maybe"), id="choice-not-offered"),
    pytest.param(lambda b: b["answers"]["triage"].update(choice="drop"), id="choice-below-maximum"),
    pytest.param(
        lambda b: b["answers"]["triage"].update(probabilities={"keep": 0.8, "maybe": 0.2}),
        id="probability-keys-mismatch",
    ),
    pytest.param(
        lambda b: b["answers"]["triage"].update(probabilities={"keep": 0.8, "drop": 0.5}),
        id="probabilities-not-unit-sum",
    ),
    pytest.param(
        lambda b: b["answers"]["triage"].update(probabilities={"keep": float("nan"), "drop": 0.2}),
        id="non-finite-probability",
    ),
    pytest.param(
        lambda b: b["answers"]["triage"].update(probabilities={"keep": 1.5, "drop": -0.5}),
        id="probability-out-of-range",
    ),
    pytest.param(
        lambda b: b["answers"]["triage"].update(confidence=1.5), id="confidence-out-of-range"
    ),
    pytest.param(lambda b: b["answers"]["triage"].pop("confidence"), id="missing-confidence"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", MALFORMED_ANSWERS)
async def test_choose_rejects_malformed_answers_as_parse(
    wire: FakeWire, mutate: Callable[[dict[str, Any]], None]
) -> None:
    body = _fixture("systemone_choice_response.json")
    mutate(body)
    # json= refuses NaN, which a Python-side server can emit; send the raw serialization.
    wire.handler = lambda _request: httpx.Response(
        200, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )

    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_config()).choose("test", STATE, QUESTIONS)

    assert caught.value.reason == "parse"


@pytest.mark.asyncio
async def test_choose_accepts_tied_maximum_and_resolved_model(wire: FakeWire) -> None:
    body = _fixture("systemone_choice_response.json")
    body["model"] = "jev-1.13.0"
    body["answers"]["triage"].update(choice="drop", probabilities={"keep": 0.5, "drop": 0.5})
    wire.handler = lambda _request: httpx.Response(200, json=body)

    result = await DecisionService(_config()).choose("test", STATE, QUESTIONS)

    assert result.answers["triage"].choice == "drop"
    assert result.response_model == "jev-1.13.0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "calls", "reason"),
    [
        (301, 1, "http_status"),
        (302, 1, "http_status"),
        (307, 1, "http_status"),
        (401, 1, "http_status"),
        (403, 1, "http_status"),
        (404, 1, "http_status"),
        (422, 1, "http_status"),
        (429, 3, "http_status"),
        (500, 3, "http_status"),
        (529, 3, "http_status"),
        ("transport", 3, "transport"),
    ],
)
async def test_retry_only_on_transient_status(
    wire: FakeWire, failure: int | str, calls: int, reason: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            return _refuse(request)
        assert isinstance(failure, int)
        return httpx.Response(failure)

    wire.handler = handler

    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_config()).choose("test", STATE, QUESTIONS)

    assert len(wire.requests) == calls
    assert caught.value.reason == reason


@pytest.mark.asyncio
async def test_short_budget_ends_retries_at_timeout(wire: FakeWire) -> None:
    wire.handler = _overloaded

    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_config(timeout_seconds=0.15)).choose("test", STATE, QUESTIONS)

    assert caught.value.reason == "timeout"
    assert len(wire.requests) < 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller_budget", "expected_budget"), [(None, 0.5), (0.1, 0.1), (1.0, 0.5)]
)
async def test_request_timeouts_follow_effective_budget(
    wire: FakeWire, caller_budget: float | None, expected_budget: float
) -> None:
    wire.handler = _card_handler(lambda: _card("/models/kev-a"))
    service = DecisionService(_config(identity_contract="model_card", timeout_seconds=0.5))

    result = await service.choose("test", STATE, QUESTIONS, timeout_seconds=caller_budget)

    assert result.backend_identity is not None
    assert {request.url.path for request in wire.requests} == {"/v1/systemone", "/v1/models"}
    assert [request.extensions["timeout"] for request in wire.requests] == [
        dict.fromkeys(("connect", "read", "write", "pool"), expected_budget)
    ] * 2


@pytest.mark.asyncio
async def test_no_proxy_or_redirect_hop(wire: FakeWire, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    wire.handler = lambda _request: httpx.Response(
        302, headers={"location": "https://elsewhere.test/v1/systemone"}
    )

    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_config()).choose("test", STATE, QUESTIONS)

    assert caught.value.reason == "http_status"
    assert wire.client_kwargs[0]["trust_env"] is False
    assert wire.client_kwargs[0]["follow_redirects"] is False
    assert {request.url.host for request in wire.requests} == {"decide.test"}


def test_service_identity_fingerprint() -> None:
    base = get_decision_service(_config(api_key="key-one"))

    assert get_decision_service(_config(api_key="key-one")) is base
    # Consumer policy sits outside the service fingerprint.
    assert (
        get_decision_service(_config(api_key="key-one", community_label={"mode": "shadow"})) is base
    )

    variants = [
        _config(api_key="key-two"),
        _config(api_key="key-one", timeout_seconds=3.0),
        _config(api_key="key-one", max_input_tokens=4096),
        _config(api_key="key-one", failure_cooldown_seconds=30),
        _config(api_key="key-one", identity_contract="response_version"),
        _config(api_key="key-one", identity_contract="model_card"),
        _config(api_key="key-one", backend_max_state_tokens=16384),
        _config(api_key="key-one", backend_max_question_tokens=16000),
        _config(api_key="key-one", long_context=True),
        _wire_config("openrouter-decisions", api_key="key-one"),
    ]
    for variant in variants:
        service = get_decision_service(variant)
        assert service is not base
        assert get_decision_service(variant) is service


@pytest.mark.parametrize("wire_api", ["systemone", "openrouter-decisions"])
async def test_long_context_opt_in_raises_cap_and_sends_header(
    wire: FakeWire, wire_api: str
) -> None:
    wire.handler = lambda request: httpx.Response(200, json=_decision(request))
    config = _wire_config(wire_api, long_context=True, max_input_tokens=200000)
    service = DecisionService(config)
    result = await service.choose("test", {"text": "x" * 270000}, QUESTIONS)
    assert result.answers["triage"].choice in TRIAGE.criteria
    assert len(wire.requests) == 1
    assert wire.requests[0].headers["TypeSafe-Danger-Zone-Long-Context"] == "true"
    assert config.resolved_backend_max_state_tokens == 262144
    assert config.resolved_backend_max_question_tokens == 262144


async def test_question_context_limit_is_separate_from_request_budget(wire: FakeWire) -> None:
    wire.handler = lambda request: httpx.Response(200, json=_decision(request))
    config = _wire_config("openrouter-decisions", max_input_tokens=60000)
    service = DecisionService(config)
    # Each branch fits 32k, while all questions together exceed 32k.
    question = ChoiceQuestion("Choose " + "x" * 80000, TRIAGE.criteria)
    result = await service.choose("test", {}, {"a": question, "b": question})
    assert set(result.answers) == {"a", "b"}
    assert "TypeSafe-Danger-Zone-Long-Context" not in wire.requests[0].headers
    with pytest.raises(DecisionsUnavailable, match="question") as error:
        await service.choose("test", {"text": "x" * 128000}, QUESTIONS)
    assert error.value.reason == "oversize"
    assert len(wire.requests) == 1


@pytest.mark.asyncio
async def test_backend_identity_from_model_card(wire: FakeWire) -> None:
    checkpoint = {"card": _card("/models/kev-a")}
    wire.handler = _card_handler(lambda: checkpoint["card"])
    service = DecisionService(_config(identity_contract="model_card"))

    before = await service.choose("test", STATE, QUESTIONS)
    checkpoint["card"] = _card("/models/kev-b")  # restart onto another checkpoint
    after = await service.choose("test", STATE, QUESTIONS)

    assert before.backend_identity is not None
    assert before.backend_identity.startswith("sha256:")
    assert after.backend_identity != before.backend_identity

    base = _card("/models/kev-a")
    assert await _identity_for(wire, _card("/models/kev-a", loss=0.5)) == before.backend_identity
    assert (
        await _identity_for(wire, _card("/models/kev-a", dtype="float16"))
        != before.backend_identity
    )
    assert (
        await _identity_for(wire, _card("/models/kev-a", temperature=0.9))
        != before.backend_identity
    )
    assert (
        await _identity_for(wire, base, backend_max_state_tokens=16384) != before.backend_identity
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "card_response",
    [
        pytest.param(lambda _request: httpx.Response(500), id="card-500"),
        pytest.param(
            lambda _request: httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{k: v for k, v in _card("/m").items() if k != "dtype"}],
                },
            ),
            id="incomplete-card",
        ),
    ],
)
async def test_card_failure_yields_no_identity_without_cooldown(
    wire: FakeWire, card_response: Callable[[httpx.Request], httpx.Response]
) -> None:
    def handler(request: httpx.Request) -> Any:
        if request.url.path == "/v1/models":
            return card_response(request)
        return _respond("systemone", request)

    wire.handler = handler
    service = DecisionService(_config(identity_contract="model_card"))

    first = await service.choose("test", STATE, QUESTIONS)
    second = await service.choose("test", STATE, QUESTIONS)

    assert first.backend_identity is None
    assert second.backend_identity is None
    assert len([r for r in wire.requests if r.url.path != "/v1/models"]) == 2


@pytest.mark.asyncio
async def test_slow_card_yields_no_identity_within_budget(wire: FakeWire) -> None:
    async def slow_card(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200, json={"object": "list", "data": [_card("/m")]})

    def handler(request: httpx.Request) -> Any:
        if request.url.path == "/v1/models":
            return slow_card(request)
        return _respond("systemone", request)

    wire.handler = handler
    service = DecisionService(_config(identity_contract="model_card", timeout_seconds=0.5))

    started = time.monotonic()
    result = await service.choose("test", STATE, QUESTIONS)

    assert result.backend_identity is None
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_failed_decision_cancels_card_fetch(wire: FakeWire) -> None:
    card_cancelled: list[bool] = []
    card_started = asyncio.Event()

    async def card(_request: httpx.Request) -> httpx.Response:
        card_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            card_cancelled.append(True)
            raise
        return httpx.Response(200)

    async def refused(_request: httpx.Request) -> httpx.Response:
        await card_started.wait()  # the decision fails only once its card fetch is in flight
        return httpx.Response(401)

    def handler(request: httpx.Request) -> Any:
        if request.url.path == "/v1/models":
            return card(request)
        return refused(request)

    wire.handler = handler

    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_config(identity_contract="model_card")).choose(
            "test", STATE, QUESTIONS
        )

    assert caught.value.reason == "http_status"
    assert card_cancelled == [True]


@pytest.mark.asyncio
async def test_backend_identity_from_response_version(wire: FakeWire) -> None:
    answered = {"model": "jev-1.13.0"}

    def handler(request: httpx.Request) -> httpx.Response:
        return _respond("systemone", request, model=answered["model"])

    wire.handler = handler
    service = DecisionService(_config(identity_contract="response_version"))

    first = await service.choose("test", STATE, QUESTIONS)
    answered["model"] = "jev-1.14.0"
    second = await service.choose("test", STATE, QUESTIONS)
    unset = await DecisionService(_config()).choose("test", STATE, QUESTIONS)

    assert first.backend_identity == "response:jev-1.13.0"
    assert second.backend_identity == "response:jev-1.14.0"
    assert unset.backend_identity is None
    assert not any(request.url.path == "/v1/models" for request in wire.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"path": "/".join(["segment"] * 200)}, id="path-heavy"),
        pytest.param({"text": "x" * 1200}, id="low-chars-per-token"),
    ],
)
async def test_oversize_request_never_dials(wire: FakeWire, state: dict[str, Any]) -> None:
    wire.handler = _must_not_dial

    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_config(max_input_tokens=256)).choose("test", state, QUESTIONS)

    assert caught.value.reason == "oversize"
    assert wire.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "outcome"),
    [
        pytest.param({"input_tokens": 999}, "truncated", id="at-limit-minus-one"),
        pytest.param({"input_tokens": 998}, "pass", id="one-below"),
        pytest.param({}, "truncated", id="missing-input-tokens"),
        pytest.param({"input_tokens": "998"}, "truncated", id="malformed-input-tokens"),
    ],
)
async def test_truncation_guard_reads_usage_per_response(
    wire: FakeWire, usage: dict[str, Any], outcome: str
) -> None:
    wire.handler = lambda request: _respond("systemone", request, usage=usage)
    service = DecisionService(_config(backend_max_state_tokens=1000, max_input_tokens=256))

    if outcome == "pass":
        result = await service.choose("test", STATE, QUESTIONS)
        assert result.answers["triage"].choice == "keep"
        return

    with pytest.raises(DecisionsUnavailable) as caught:
        await service.choose("test", STATE, QUESTIONS)
    assert caught.value.reason == "truncated"

    # A truncated response does not open the cooldown, so the next call dials.
    with pytest.raises(DecisionsUnavailable) as again:
        await service.choose("test", STATE, QUESTIONS)
    assert again.value.reason == "truncated"
    assert len(wire.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "overrides"),
    [
        pytest.param(_refuse, {}, id="transport"),
        pytest.param(_overloaded, {}, id="exhausted-529"),
        pytest.param(_unauthorized, {}, id="unauthorized"),
        pytest.param(_garbled, {}, id="parse"),
        pytest.param(_hang, {"timeout_seconds": 0.05}, id="full-budget-timeout"),
    ],
)
async def test_cooldown_fails_fast_then_recovers(
    wire: FakeWire,
    clock: FakeClock,
    failure: Callable[[httpx.Request], Any],
    overrides: dict[str, Any],
) -> None:
    wire.handler = failure
    service = DecisionService(_config(failure_cooldown_seconds=60, **overrides))

    with pytest.raises(DecisionsUnavailable) as opened:
        await service.choose("test", STATE, QUESTIONS)
    assert opened.value.reason != "cooldown"
    dialed = len(wire.requests)

    clock.now += 30
    with pytest.raises(DecisionsUnavailable) as blocked:
        await service.choose("test", STATE, QUESTIONS)
    assert blocked.value.reason == "cooldown"
    assert len(wire.requests) == dialed

    clock.now += 31
    wire.handler = lambda request: _respond("systemone", request)
    result = await service.choose("test", STATE, QUESTIONS)
    assert result.answers["triage"].choice == "keep"
    assert len(wire.requests) == dialed + 1


@pytest.mark.asyncio
async def test_concurrent_failures_open_cooldown_with_one_warning(
    wire: FakeWire, caplog: pytest.LogCaptureFixture
) -> None:
    both_in_flight = asyncio.Event()
    waiting = 0

    async def fail_together(_request: httpx.Request) -> httpx.Response:
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            both_in_flight.set()
        await both_in_flight.wait()
        return httpx.Response(500)

    wire.handler = fail_together
    service = DecisionService(_config(failure_cooldown_seconds=60))

    results = await asyncio.gather(
        service.choose("test", STATE, QUESTIONS),
        service.choose("test", STATE, QUESTIONS),
        return_exceptions=True,
    )

    assert all(isinstance(result, DecisionsUnavailable) for result in results)
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1


LOCAL_OUTCOMES = [
    pytest.param(
        {"api_base": None},
        _must_not_dial,
        STATE,
        QUESTIONS,
        {},
        "unconfigured",
        0,
        id="unconfigured",
    ),
    pytest.param(
        {"max_input_tokens": 256},
        _must_not_dial,
        {"text": "x" * 1200},
        QUESTIONS,
        {},
        "oversize",
        0,
        id="oversize",
    ),
    pytest.param({}, _must_not_dial, STATE, {}, {}, "invalid_request", 0, id="invalid-request"),
    pytest.param(
        {},
        _hang,
        STATE,
        QUESTIONS,
        {"timeout_seconds": 0.05},
        "timeout",
        1,
        id="caller-budget-timeout",
    ),
    pytest.param(
        {"backend_max_state_tokens": 1000, "max_input_tokens": 256},
        _truncated,
        STATE,
        QUESTIONS,
        {},
        "truncated",
        1,
        id="truncated",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "handler", "state", "questions", "call_kwargs", "reason", "dials"),
    LOCAL_OUTCOMES,
)
async def test_local_outcomes_never_open_cooldown(
    wire: FakeWire,
    overrides: dict[str, Any],
    handler: Callable[[httpx.Request], Any],
    state: dict[str, Any],
    questions: dict[str, ChoiceQuestion],
    call_kwargs: dict[str, Any],
    reason: str,
    dials: int,
) -> None:
    wire.handler = handler
    service = DecisionService(_config(**overrides))

    for _ in range(2):
        with pytest.raises(DecisionsUnavailable) as caught:
            await service.choose("test", state, questions, **call_kwargs)
        assert caught.value.reason == reason

    assert len(wire.requests) == 2 * dials


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_without_cooldown(wire: FakeWire) -> None:
    started = asyncio.Event()

    def hang_once_started(request: httpx.Request) -> Any:
        started.set()
        return _hang(request)

    wire.handler = hang_once_started
    service = DecisionService(_config(timeout_seconds=2.0))

    task = asyncio.create_task(service.choose("test", STATE, QUESTIONS))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    wire.handler = lambda request: _respond("systemone", request)
    result = await service.choose("test", STATE, QUESTIONS)
    assert result.answers["triage"].choice == "keep"


@pytest.mark.asyncio
async def test_call_log_redacts_state(wire: FakeWire, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret_question = ChoiceQuestion(
        instructions="Triage.", criteria={"keep": "SECRET-OPTION-TEXT", "drop": "noise"}
    )
    secret_state = {"finding": "SECRET-STATE-TOKEN"}

    wire.handler = lambda request: _respond("systemone", request)
    await DecisionService(_config(api_key="SECRET-KEY-VALUE")).choose(
        "found_work", secret_state, {"triage": secret_question}
    )
    wire.handler = _unauthorized
    with pytest.raises(DecisionsUnavailable):
        await DecisionService(_config(api_key="SECRET-KEY-VALUE")).choose(
            "found_work", secret_state, {"triage": secret_question}
        )

    records = [record for record in caplog.records if "decisions" in record.name]
    assert any(record.getMessage().startswith("decisions.call") for record in records)
    for record in caplog.records:
        rendered = repr(record.__dict__)
        assert "SECRET-STATE-TOKEN" not in rendered
        assert "SECRET-OPTION-TEXT" not in rendered
        assert "SECRET-KEY-VALUE" not in rendered


@pytest.mark.asyncio
async def test_openrouter_wire_posts_and_parses(wire: FakeWire) -> None:
    config = _wire_config(
        "openrouter-decisions",
        api_key="sk-or-test",
        identity_contract="response_version",
    )
    wire.handler = lambda _request: httpx.Response(
        200, json=_fixture("openrouter_decisions_choice_response.json")
    )

    result = await DecisionService(config).choose("test", STATE, QUESTIONS)

    request = wire.requests[0]
    assert str(request.url) == "https://openrouter.test/api/alpha/decisions"
    assert request.headers["authorization"] == "Bearer sk-or-test"
    assert json.loads(request.content)["provider"] == {"zdr": True, "data_collection": "deny"}
    assert result.answers["triage"].choice == "keep"
    assert result.response_model == "typesafe/jev-1.13-20260917"
    assert result.backend_identity == "response:typesafe/jev-1.13-20260917"

    for status, calls in [(400, 1), (402, 1), (413, 1), (502, 3), (503, 3), (524, 3)]:
        wire.requests.clear()
        wire.handler = _status(status)
        with pytest.raises(DecisionsUnavailable) as caught:
            await DecisionService(config).choose("test", STATE, QUESTIONS)
        assert caught.value.reason == "http_status", status
        assert len(wire.requests) == calls, status


@pytest.mark.asyncio
async def test_workers_ai_wire_posts_and_parses(wire: FakeWire) -> None:
    base = "https://cf.test/client/v4/accounts/acct-1"
    config = _wire_config("workers-ai", api_key="cf-token", identity_contract="response_version")
    wire.handler = lambda _request: httpx.Response(
        200, json=_fixture("workers_ai_choice_response.json")
    )

    result = await DecisionService(config).choose("test", STATE, QUESTIONS)

    request = wire.requests[0]
    assert str(request.url) == f"{base}/ai/run/@cf/cloudflare/clef"
    assert request.headers["authorization"] == "Bearer cf-token"
    assert set(json.loads(request.content)) == {"model", "state", "questions"}
    assert result.answers["triage"].choice == "keep"
    assert result.response_model == "clef"
    assert result.backend_identity == "response:clef"

    wire.requests.clear()
    wire.handler = lambda request: _respond("workers-ai", request)
    await DecisionService(_wire_config("workers-ai", model="clef-flash")).choose(
        "test", STATE, QUESTIONS
    )
    assert str(wire.requests[0].url) == f"{base}/ai/run/@cf/cloudflare/clef-flash"

    envelopes = [
        {"success": False, "result": None, "errors": [{"code": 1}], "messages": []},
        {"success": True, "errors": [], "messages": []},
    ]
    for envelope in envelopes:
        wire.handler = _json_reply(envelope)
        with pytest.raises(DecisionsUnavailable) as caught:
            await DecisionService(config).choose("test", STATE, QUESTIONS)
        assert caught.value.reason == "parse"


@pytest.mark.asyncio
async def test_request_limits_never_dial(wire: FakeWire) -> None:
    rejected: dict[str, dict[str, ChoiceQuestion]] = {
        "no-questions": {},
        "sixty-five-questions": {f"q{i}": TRIAGE for i in range(65)},
        "key-with-slash": {"a/b": TRIAGE},
        "key-over-100-characters": {"k" * 101: TRIAGE},
        "one-option": {"triage": ChoiceQuestion(instructions="x", criteria={"keep": "only"})},
        "256-options": {
            "triage": ChoiceQuestion(instructions="x", criteria={f"o{i}": "t" for i in range(256)})
        },
    }
    accepted: dict[str, dict[str, ChoiceQuestion]] = {
        "one-question": {"triage": TRIAGE},
        "sixty-four-questions": {f"q{i}": TRIAGE for i in range(64)},
        "255-options": {
            "triage": ChoiceQuestion(instructions="x", criteria={f"o{i}": "t" for i in range(255)})
        },
    }

    for wire_api in ["systemone", "openrouter-decisions", "workers-ai"]:
        service = DecisionService(_wire_config(wire_api))
        wire.requests.clear()
        wire.handler = _must_not_dial
        for name, questions in rejected.items():
            with pytest.raises(DecisionsUnavailable) as caught:
                await service.choose("test", STATE, questions)
            assert caught.value.reason == "invalid_request", (wire_api, name)
        assert wire.requests == [], wire_api

        wire.handler = _responder(wire_api)
        for name, questions in accepted.items():
            result = await service.choose("test", STATE, questions)
            assert len(result.answers) == len(questions), (wire_api, name)
        assert len(wire.requests) == len(accepted), wire_api


# --- community-label-decisions P1: Noul, Score and Box shapes (#22604) ---

WIRES = ["systemone", "openrouter-decisions", "workers-ai"]
WIRE_URLS = {
    "systemone": SYSTEMONE_URL,
    "openrouter-decisions": "https://openrouter.test/api/alpha/decisions",
    "workers-ai": "https://cf.test/client/v4/accounts/acct-1/ai/run/@cf/cloudflare/clef",
}
WIRE_FIXTURE_PREFIX = {
    "systemone": "systemone",
    "openrouter-decisions": "openrouter_decisions",
    "workers-ai": "workers_ai",
}
WIRE_RESPONSE_MODELS = {
    "systemone": "jev-1.14.0",
    "openrouter-decisions": "typesafe/jev-1.14-20261014",
    "workers-ai": "clef",
}
PROPOSITIONS: dict[str, str] = {
    "carry": "The name `Ask Evidence` describes what the files in community c1 have in common.",
    "split": "The files in community c2 serve two unrelated purposes.",
    "rename": "The name `helpers` describes what the files in community c3 have in common.",
}
NOUL_PROBABILITIES = {"carry": 0.91, "split": 0.12, "rename": 0.5}
SCORE_LEVELS = ("misleading", "vague", "clear", "clear and specific")
SCORE_QUESTIONS: dict[str, ScoreQuestion] = {
    key: ScoreQuestion(instructions=f"Rate the {key} of this label.", levels=SCORE_LEVELS)
    for key in ("naming", "scope", "tests")
}
SCORE_ANSWERS = {
    "naming": ScoreAnswer(
        score=1.95, probabilities={"0": 0.05, "1": 0.15, "2": 0.6, "3": 0.2}, confidence=0.6
    ),
    "scope": ScoreAnswer(
        score=0.4, probabilities={"0": 0.7, "1": 0.2, "2": 0.1, "3": 0.0}, confidence=0.7
    ),
    "tests": ScoreAnswer(
        score=2.5, probabilities={"0": 0.0, "1": 0.1, "2": 0.3, "3": 0.6}, confidence=0.55
    ),
}
BOX_MODEL = "typesafe/jev-1.14"


def _shape_fixture(wire_api: str, shape: str) -> dict[str, Any]:
    return _fixture(f"{WIRE_FIXTURE_PREFIX[wire_api]}_{shape}_response.json")


def _raw_reply(body: Any) -> Callable[[httpx.Request], httpx.Response]:
    # json= refuses NaN and infinity, which a Python-side server can emit.
    return lambda _request: httpx.Response(
        200, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )


def _shape_answer(question: Mapping[str, Any]) -> dict[str, Any]:
    """A valid answer for any question shape: first option, lowest level, or a centred box."""
    kind = question["type"]
    if kind == "choice":
        options = list(question["criteria"])
        probabilities = dict.fromkeys(options, 0.0)
        probabilities[options[0]] = 1.0
        return {
            "type": "choice",
            "choice": options[0],
            "probabilities": probabilities,
            "confidence": 0.5,
        }
    if kind == "noul":
        return {"type": "noul", "noul": 0.5}
    if kind == "score":
        levels = question["criteria"]
        return {
            "type": "score",
            "score": 0.0,
            "confidence": 1.0,
            "legend": {str(index): level for index, level in enumerate(levels)},
            "probabilities": {str(index): float(index == 0) for index in range(len(levels))},
        }
    assert kind == "box", kind
    return {
        "type": "box",
        "box": {"top": 0.1, "bottom": 0.9, "left": 0.1, "right": 0.9},
        "confidence": 0.9,
    }


def _shape_responder(wire_api: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        body = {
            "model": sent["model"],
            "answers": {key: _shape_answer(q) for key, q in sent["questions"].items()},
            "usage": dict(USAGE),
        }
        if wire_api == "workers-ai":
            body = {"result": body, "success": True, "errors": [], "messages": []}
        return httpx.Response(200, json=body)

    return handler


def _png(width: int, height: int, *, padding: int = 0) -> bytes:
    """A real grayscale PNG; ``padding`` bytes ride in an ancillary chunk."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + bytes(width) for _ in range(height))
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    body += chunk(b"IDAT", zlib.compress(rows))
    if padding:
        body += chunk(b"tEXt", b"pad\x00" + b"x" * padding)
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")


def _jpeg(width: int, height: int) -> bytes:
    """JPEG signature plus a baseline SOF0 header carrying the dimensions."""
    sof = struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xc0" + sof + b"\xff\xd9"


def _image(data: bytes, mime_type: str = "image/png") -> BoxImage:
    assert mime_type in ("image/png", "image/jpeg")
    return BoxImage(
        mime_type="image/png" if mime_type == "image/png" else "image/jpeg",
        data_base64=base64.b64encode(data).decode(),
    )


BOX_QUESTIONS: dict[str, BoxQuestion] = {
    "logo": BoxQuestion(instructions="Where is the logo?", image=_image(_png(320, 300))),
    "button": BoxQuestion(
        instructions="Where is the submit button?", image=_image(_jpeg(400, 300), "image/jpeg")
    ),
}
BOX_ANSWERS = {
    "logo": BoxAnswer(top=0.1, bottom=0.4, left=0.2, right=0.6, confidence=0.95),
    "button": BoxAnswer(top=0.5, bottom=0.9, left=0.05, right=0.3, confidence=0.8),
}


async def _assert_cooldown_is_shared(
    wire: FakeWire, clock: FakeClock, service: DecisionService, shape_call: Any
) -> None:
    """A failed shape call fails the next Choice fast, and a failed Choice blocks the shape."""
    wire.requests.clear()
    wire.handler = _unauthorized
    with pytest.raises(DecisionsUnavailable) as opened:
        await shape_call()
    assert opened.value.reason == "http_status"
    with pytest.raises(DecisionsUnavailable) as blocked:
        await service.choose("test", STATE, QUESTIONS)
    assert blocked.value.reason == "cooldown"
    assert len(wire.requests) == 1

    clock.now += 61
    with pytest.raises(DecisionsUnavailable) as reopened:
        await service.choose("test", STATE, QUESTIONS)
    assert reopened.value.reason == "http_status"
    with pytest.raises(DecisionsUnavailable) as blocked_again:
        await shape_call()
    assert blocked_again.value.reason == "cooldown"
    assert len(wire.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", WIRES)
async def test_noul_returns_probabilities_by_key(
    wire: FakeWire, clock: FakeClock, caplog: pytest.LogCaptureFixture, wire_api: str
) -> None:
    caplog.set_level(logging.DEBUG)
    wire.handler = _json_reply(_shape_fixture(wire_api, "noul"))
    service = DecisionService(_wire_config(wire_api, failure_cooldown_seconds=60))

    result = await service.noul("carry_check", STATE, PROPOSITIONS)

    request = wire.requests[0]
    assert str(request.url) == WIRE_URLS[wire_api]
    sent = json.loads(request.content)
    assert sent["state"] == STATE
    assert sent["questions"] == {
        key: {"type": "noul", "instructions": text} for key, text in PROPOSITIONS.items()
    }
    assert result.answers == {
        key: NoulAnswer(probability=probability) for key, probability in NOUL_PROBABILITIES.items()
    }
    assert result.response_model == WIRE_RESPONSE_MODELS[wire_api]
    assert any(
        record.getMessage().startswith("decisions.call consumer=carry_check")
        for record in caplog.records
    )

    # The request profile: 1 to 64 non-empty propositions under valid keys.
    wire.requests.clear()
    wire.handler = _must_not_dial
    rejected: dict[str, dict[str, str]] = {
        "none": {},
        "sixty-five": {f"p{index}": "A proposition." for index in range(65)},
        "empty-proposition": {"carry": ""},
        "bad-key": {"a/b": "A proposition."},
    }
    for name, propositions in rejected.items():
        with pytest.raises(DecisionsUnavailable) as caught:
            await service.noul("carry_check", STATE, propositions)
        assert caught.value.reason == "invalid_request", name
    with pytest.raises(DecisionsUnavailable) as oversize:
        await DecisionService(_wire_config(wire_api, max_input_tokens=256)).noul(
            "carry_check", {"text": "x" * 1200}, PROPOSITIONS
        )
    assert oversize.value.reason == "oversize"
    assert wire.requests == []

    wire.handler = _shape_responder(wire_api)
    sixty_four = {f"p{index}": "A proposition." for index in range(64)}
    assert len((await service.noul("carry_check", STATE, sixty_four)).answers) == 64

    await _assert_cooldown_is_shared(
        wire, clock, service, lambda: service.noul("carry_check", STATE, PROPOSITIONS)
    )


MALFORMED_NOUL_ANSWERS = [
    pytest.param(lambda b: b["answers"]["carry"].update(type="choice"), id="wrong-type"),
    pytest.param(lambda b: b["answers"]["carry"].pop("noul"), id="missing-noul"),
    pytest.param(lambda b: b["answers"]["carry"].update(noul=float("nan")), id="nan-noul"),
    pytest.param(lambda b: b["answers"]["carry"].update(noul=float("inf")), id="infinite-noul"),
    pytest.param(lambda b: b["answers"]["carry"].update(noul=1.01), id="above-one"),
    pytest.param(lambda b: b["answers"]["carry"].update(noul=-0.01), id="below-zero"),
    pytest.param(lambda b: b["answers"]["carry"].update(noul="0.5"), id="string-noul"),
    pytest.param(lambda b: b["answers"]["carry"].update(noul=True), id="boolean-noul"),
    pytest.param(lambda b: b["answers"].pop("rename"), id="missing-key"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", MALFORMED_NOUL_ANSWERS)
async def test_noul_rejects_malformed_answers_as_parse(
    wire: FakeWire, mutate: Callable[[dict[str, Any]], None]
) -> None:
    body = _shape_fixture("systemone", "noul")
    mutate(body)
    wire.handler = _raw_reply(body)

    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_config()).noul("carry_check", STATE, PROPOSITIONS)

    assert caught.value.reason == "parse"


async def _answer_count(service: DecisionService, shape: str, state: dict[str, Any]) -> int:
    if shape == "choice":
        return len((await service.choose("test", state, QUESTIONS)).answers)
    if shape == "noul":
        return len((await service.noul("test", state, PROPOSITIONS)).answers)
    return len((await service.score("test", state, SCORE_QUESTIONS)).answers)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", ["systemone", "openrouter-decisions"])
@pytest.mark.parametrize("shape", ["choice", "noul", "score"])
async def test_estimate_request_tokens_matches_oversize_guard(
    wire: FakeWire, wire_api: str, shape: str
) -> None:
    state = {"members": [f"src/gobby/code_index/module_{index:03d}.py" for index in range(60)]}
    shape_questions: dict[str, Mapping[str, Any]] = {
        "choice": QUESTIONS,
        "noul": PROPOSITIONS,
        "score": SCORE_QUESTIONS,
    }
    questions = shape_questions[shape]
    estimate = DecisionService(_wire_config(wire_api)).estimate_request_tokens(state, questions)
    assert estimate > 256

    wire.handler = _must_not_dial
    with pytest.raises(DecisionsUnavailable) as caught:
        await _answer_count(
            DecisionService(_wire_config(wire_api, max_input_tokens=estimate - 1)), shape, state
        )
    assert caught.value.reason == "oversize"
    assert wire.requests == []

    wire.handler = _shape_responder(wire_api)
    answered = await _answer_count(
        DecisionService(_wire_config(wire_api, max_input_tokens=estimate)), shape, state
    )
    assert answered == len(questions)
    assert len(wire.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", WIRES)
async def test_score_returns_weighted_levels_by_key(
    wire: FakeWire, clock: FakeClock, wire_api: str
) -> None:
    wire.handler = _json_reply(_shape_fixture(wire_api, "score"))
    service = DecisionService(_wire_config(wire_api, failure_cooldown_seconds=60))

    result = await service.score("sufficiency", STATE, SCORE_QUESTIONS)

    request = wire.requests[0]
    assert str(request.url) == WIRE_URLS[wire_api]
    sent = json.loads(request.content)
    assert sent["questions"] == {
        key: {
            "type": "score",
            "instructions": question.instructions,
            "criteria": list(SCORE_LEVELS),
        }
        for key, question in SCORE_QUESTIONS.items()
    }
    assert result.answers == SCORE_ANSWERS
    # The consumer knows its own levels; the validated legend is not carried.
    assert not hasattr(result.answers["naming"], "legend")
    assert result.response_model == WIRE_RESPONSE_MODELS[wire_api]

    wire.requests.clear()
    wire.handler = _must_not_dial
    with pytest.raises(DecisionsUnavailable) as oversize:
        await DecisionService(_wire_config(wire_api, max_input_tokens=256)).score(
            "sufficiency", {"text": "x" * 1200}, SCORE_QUESTIONS
        )
    assert oversize.value.reason == "oversize"
    assert wire.requests == []

    await _assert_cooldown_is_shared(
        wire, clock, service, lambda: service.score("sufficiency", STATE, SCORE_QUESTIONS)
    )


def _naming(body: dict[str, Any]) -> dict[str, Any]:
    answer: dict[str, Any] = body["answers"]["naming"]
    return answer


MALFORMED_SCORE_ANSWERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "missing-legend": lambda b: _naming(b).pop("legend"),
    "missing-probabilities": lambda b: _naming(b).pop("probabilities"),
    "missing-confidence": lambda b: _naming(b).pop("confidence"),
    "missing-score": lambda b: _naming(b).pop("score"),
    "wrong-type": lambda b: _naming(b).update(type="choice"),
    "legend-keys-off-by-one": lambda b: _naming(b).update(
        legend={"1": "misleading", "2": "vague", "3": "clear", "4": "clear and specific"}
    ),
    "probability-keys-off-by-one": lambda b: _naming(b).update(
        probabilities={"1": 0.05, "2": 0.15, "3": 0.6, "4": 0.2}
    ),
    # Index 0 carries no weight, so the weighted index stays 1.95 and only the sum is wrong.
    "probabilities-sum-above-one": lambda b: _naming(b).update(
        probabilities={"0": 0.15, "1": 0.15, "2": 0.6, "3": 0.2}
    ),
    "probability-below-zero": lambda b: _naming(b).update(
        probabilities={"0": -0.05, "1": 0.25, "2": 0.6, "3": 0.2}, score=2.05
    ),
    "score-below-zero": lambda b: _naming(b).update(score=-0.5),
    "score-above-top-level": lambda b: _naming(b).update(score=3.5),
    "score-not-weighted-index": lambda b: _naming(b).update(score=1.5),
    "string-score": lambda b: _naming(b).update(score="1.95"),
    "confidence-above-one": lambda b: _naming(b).update(confidence=1.5),
    "missing-key": lambda b: b["answers"].pop("tests"),
}


@pytest.mark.asyncio
async def test_score_rejects_malformed_answers_as_parse(wire: FakeWire) -> None:
    for name, mutate in MALFORMED_SCORE_ANSWERS.items():
        body = _shape_fixture("systemone", "score")
        mutate(body)
        wire.handler = _raw_reply(body)
        with pytest.raises(DecisionsUnavailable) as caught:
            await DecisionService(_config()).score("sufficiency", STATE, SCORE_QUESTIONS)
        assert caught.value.reason == "parse", name

    # Within (n - 1) x tolerance of the weighted index is consistent.
    body = _shape_fixture("systemone", "score")
    _naming(body).update(score=1.952)
    wire.handler = _raw_reply(body)
    result = await DecisionService(_config()).score("sufficiency", STATE, SCORE_QUESTIONS)
    assert result.answers["naming"].score == pytest.approx(1.952)

    wire.requests.clear()
    wire.handler = _must_not_dial
    rejected_levels: dict[str, ScoreQuestion] = {
        "one-level": ScoreQuestion(instructions="Rate it.", levels=("only",)),
        "eleven-levels": ScoreQuestion(
            instructions="Rate it.", levels=tuple(f"level {i}" for i in range(11))
        ),
        "empty-level": ScoreQuestion(instructions="Rate it.", levels=("bad", "")),
        "empty-instructions": ScoreQuestion(instructions="", levels=SCORE_LEVELS),
    }
    service = DecisionService(_config())
    for name, question in rejected_levels.items():
        with pytest.raises(DecisionsUnavailable) as caught:
            await service.score("sufficiency", STATE, {"naming": question})
        assert caught.value.reason == "invalid_request", name
    assert wire.requests == []

    wire.handler = _shape_responder("systemone")
    for levels in (("low", "high"), tuple(f"level {i}" for i in range(10))):
        question = ScoreQuestion(instructions="Rate it.", levels=levels)
        result = await service.score("sufficiency", STATE, {"naming": question})
        assert set(result.answers["naming"].probabilities) == {str(i) for i in range(len(levels))}
    assert len(wire.requests) == 2


@pytest.mark.asyncio
async def test_box_returns_edges_by_key_per_wire_transport(wire: FakeWire) -> None:
    # OpenRouter: each image rides inline as an image_url part of the instructions array.
    openrouter = _wire_config(
        "openrouter-decisions",
        model=BOX_MODEL,
        api_key="sk-or-test",
        identity_contract="response_version",
    )
    wire.handler = _json_reply(_shape_fixture("openrouter-decisions", "box"))

    result = await DecisionService(openrouter).box("evaluate", STATE, BOX_QUESTIONS)

    request = wire.requests[0]
    sent = json.loads(request.content)
    assert str(request.url) == WIRE_URLS["openrouter-decisions"]
    assert sent["questions"] == {
        key: {
            "type": "box",
            "instructions": [
                question.instructions,
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{question.image.mime_type};base64,"
                        f"{question.image.data_base64}"
                    },
                },
            ],
        }
        for key, question in BOX_QUESTIONS.items()
    }
    assert "assets" not in sent
    assert "images" not in sent
    assert "typesafe-danger-zone-images" not in request.headers
    assert result.answers == BOX_ANSWERS
    assert result.response_model == "typesafe/jev-1.14-20261014"
    assert result.backend_identity == "response:typesafe/jev-1.14-20261014"

    # systemone: each image is a whole-value asset reference behind the danger-zone header.
    seen_refs: set[str] = set()
    for _ in range(2):
        wire.requests.clear()
        wire.handler = _json_reply(_shape_fixture("systemone", "box"))
        result = await DecisionService(_config(model="jev-1.14")).box(
            "evaluate", STATE, BOX_QUESTIONS
        )
        request = wire.requests[0]
        sent = json.loads(request.content)
        assert str(request.url) == SYSTEMONE_URL
        assert request.headers["typesafe-danger-zone-images"] == "true"
        refs: dict[str, str] = {}
        for key, question in BOX_QUESTIONS.items():
            sent_question = sent["questions"][key]
            assert sent_question["type"] == "box"
            text, ref = sent_question["instructions"]
            assert text == question.instructions
            assert re.fullmatch(r"gobby-image-[0-9a-f]{32}", ref)
            refs[key] = ref
        assert sent["assets"] == {
            refs["logo"]: {
                "type": "image",
                "format": "png",
                "data_base64": BOX_QUESTIONS["logo"].image.data_base64,
            },
            refs["button"]: {
                "type": "image",
                "format": "jpeg",
                "data_base64": BOX_QUESTIONS["button"].image.data_base64,
            },
        }
        assert result.answers == BOX_ANSWERS
        assert seen_refs.isdisjoint(refs.values())
        seen_refs.update(refs.values())
    assert len(seen_refs) == 4

    # Workers AI has no Box in Clef's schema: refused before dialing.
    wire.requests.clear()
    wire.handler = _must_not_dial
    with pytest.raises(DecisionsUnavailable) as caught:
        await DecisionService(_wire_config("workers-ai")).box("evaluate", STATE, BOX_QUESTIONS)
    assert caught.value.reason == "invalid_request"
    assert wire.requests == []

    # Image bytes never count toward the estimate; their token cost is unknown.
    probe = DecisionService(openrouter)
    small = BoxQuestion(instructions="Where is the logo?", image=_image(_png(280, 280)))
    large = BoxQuestion(
        instructions="Where is the logo?", image=_image(_png(280, 280, padding=100_000))
    )
    assert probe.estimate_request_tokens(STATE, {"logo": small}) == probe.estimate_request_tokens(
        STATE, {"logo": large}
    )


def _invalid_box_requests() -> dict[str, dict[str, BoxQuestion]]:
    def one(image: BoxImage, instructions: str = "Where is the logo?") -> dict[str, BoxQuestion]:
        return {"logo": BoxQuestion(instructions=instructions, image=image)}

    near_limit = _image(_png(280, 280, padding=MAX_IMAGE_BYTES - 4096))
    over_total = MAX_REQUEST_IMAGE_BYTES // (MAX_IMAGE_BYTES - 4096) + 1
    assert over_total <= MAX_REQUEST_IMAGES
    small = _image(_png(280, 280))
    return {
        "gif-declared-png": one(_image(b"GIF89a" + bytes(64))),
        "png-declared-jpeg": one(_image(_png(300, 300), "image/jpeg")),
        "jpeg-declared-png": one(_image(_jpeg(300, 300))),
        "not-base64": one(BoxImage(mime_type="image/png", data_base64="!!not base64!!")),
        "width-279": one(_image(_png(279, 300))),
        "height-279": one(_image(_png(300, 279))),
        "width-1569": one(_image(_png(1569, 300))),
        "jpeg-height-1569": one(_image(_jpeg(300, 1569), "image/jpeg")),
        "oversized-image": one(_image(_png(280, 280, padding=MAX_IMAGE_BYTES))),
        "too-many-images": {
            f"q{index}": BoxQuestion(instructions="Where?", image=small)
            for index in range(MAX_REQUEST_IMAGES + 1)
        },
        "request-bytes-over-total": {
            f"q{index}": BoxQuestion(instructions="Where?", image=near_limit)
            for index in range(over_total)
        },
        "empty-instructions": one(small, instructions=""),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", ["systemone", "openrouter-decisions"])
async def test_box_rejects_invalid_images_before_dialing(
    wire: FakeWire, clock: FakeClock, wire_api: str
) -> None:
    wire.handler = _must_not_dial
    service = DecisionService(_wire_config(wire_api, model=BOX_MODEL, failure_cooldown_seconds=60))

    for name, questions in _invalid_box_requests().items():
        with pytest.raises(DecisionsUnavailable) as caught:
            await service.box("evaluate", STATE, questions)
        assert caught.value.reason == "invalid_request", name
    assert wire.requests == []

    # The documented bounds are inclusive, and no rejection opened the cooldown.
    wire.handler = _shape_responder(wire_api)
    boundary = {
        "smallest": BoxQuestion(instructions="Where?", image=_image(_png(280, 280))),
        "largest": BoxQuestion(instructions="Where?", image=_image(_png(1568, 1568))),
        "jpeg": BoxQuestion(instructions="Where?", image=_image(_jpeg(280, 1568), "image/jpeg")),
    }
    result = await service.box("evaluate", STATE, boundary)
    assert set(result.answers) == set(boundary)
    assert len(wire.requests) == 1


def _logo(body: dict[str, Any]) -> dict[str, Any]:
    answer: dict[str, Any] = body["answers"]["logo"]
    return answer


MALFORMED_BOX_ANSWERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "top-below-zero": lambda b: _logo(b)["box"].update(top=-0.1),
    "right-above-one": lambda b: _logo(b)["box"].update(right=1.1),
    "nan-edge": lambda b: _logo(b)["box"].update(left=float("nan")),
    "string-edge": lambda b: _logo(b)["box"].update(bottom="0.4"),
    "missing-edge": lambda b: _logo(b)["box"].pop("left"),
    "left-equals-right": lambda b: _logo(b)["box"].update(left=0.6, right=0.6),
    "left-above-right": lambda b: _logo(b)["box"].update(left=0.7, right=0.6),
    "top-equals-bottom": lambda b: _logo(b)["box"].update(top=0.4, bottom=0.4),
    "top-below-bottom-inverted": lambda b: _logo(b)["box"].update(top=0.5, bottom=0.4),
    "missing-box": lambda b: _logo(b).pop("box"),
    "missing-confidence": lambda b: _logo(b).pop("confidence"),
    "null-confidence": lambda b: _logo(b).update(confidence=None),
    "confidence-above-one": lambda b: _logo(b).update(confidence=1.5),
    "wrong-type": lambda b: _logo(b).update(type="choice"),
    "missing-key": lambda b: b["answers"].pop("button"),
}


@pytest.mark.asyncio
async def test_box_rejects_malformed_answers_as_parse(wire: FakeWire) -> None:
    service_config = _config(model="jev-1.14")
    for name, mutate in MALFORMED_BOX_ANSWERS.items():
        body = _shape_fixture("systemone", "box")
        mutate(body)
        wire.handler = _raw_reply(body)
        with pytest.raises(DecisionsUnavailable) as caught:
            await DecisionService(service_config).box("evaluate", STATE, BOX_QUESTIONS)
        assert caught.value.reason == "parse", name


# --- Request byte ceiling and guard-matrix gaps (#22604 round 2) ---

DEFAULT_BYTE_FLOOR = 4 * 8192 + 3  # the smallest max_request_bytes the default token budget allows
IDENTITY_MODES = ["model_card", "response_version"]
# Non-ASCII text and escaped control characters: UTF-8 bytes, characters and escapes all differ.
UTF8_STATE: dict[str, Any] = {"finding": "naïve café — 模块 重复导入 \t\x07\x01 end"}
WIDE_STATE: dict[str, Any] = {
    "members": [f"src/gobby/code_index/module_{index:03d}.py" for index in range(60)]
}
SEVERITY = ChoiceQuestion(
    instructions="Rate the finding’s severity — 严重性.",
    criteria={"high": "Breaks users", "low": "Cosmetic"},
)


def _large_logo() -> dict[str, BoxQuestion]:
    """A Box request whose asset bytes dwarf its textual token estimate."""
    image = _image(_png(320, 300, padding=60000))
    assert len(base64.b64decode(image.data_base64)) < MAX_IMAGE_BYTES
    return {"logo": BoxQuestion(instructions="Where is the 标志 logo?", image=image)}


def _require_max_request_bytes() -> None:
    assert "max_request_bytes" in DecisionsConfig.model_fields


@pytest.mark.asyncio
async def test_full_utf8_body_byte_limit_before_dispatch(wire: FakeWire) -> None:
    _require_max_request_bytes()
    questions = _large_logo()
    wire.handler = _shape_responder("systemone")

    unbounded = DecisionService(_config())
    await unbounded.box("test", UTF8_STATE, questions)
    sent = wire.requests[0].content
    size = len(sent)
    # The asset makes the body large while the text estimate still fits the token budget.
    assert unbounded.estimate_request_tokens(UTF8_STATE, questions) <= 8192
    assert size - 1 >= DEFAULT_BYTE_FLOOR
    assert len(sent.decode()) < size
    assert len(json.dumps(json.loads(sent))) != size

    at_cap = DecisionService(_config(max_request_bytes=size))
    assert at_cap.request_fits(UTF8_STATE, questions)
    await at_cap.box("test", UTF8_STATE, questions)
    assert len(wire.requests) == 2
    assert len(wire.requests[1].content) == size

    over_cap = DecisionService(_config(max_request_bytes=size - 1))
    assert not over_cap.request_fits(UTF8_STATE, questions)
    wire.handler = _must_not_dial
    with pytest.raises(DecisionsUnavailable) as caught:
        await over_cap.box("test", UTF8_STATE, questions)
    assert caught.value.reason == "oversize"
    assert len(wire.requests) == 2

    assert get_decision_service(_config(max_request_bytes=size)) is not get_decision_service(
        _config()
    )


@pytest.mark.asyncio
async def test_http_413_is_unavailable_without_retry_and_opens_cooldown(wire: FakeWire) -> None:
    _require_max_request_bytes()
    wire.handler = _status(413)
    service = DecisionService(_config())

    with pytest.raises(DecisionsUnavailable) as refused:
        await service.choose("test", STATE, QUESTIONS)
    assert (refused.value.reason, refused.value.detail) == ("http_status", "HTTP 413")
    assert len(wire.requests) == 1
    with pytest.raises(DecisionsUnavailable) as blocked:
        await service.choose("test", STATE, QUESTIONS)
    assert blocked.value.reason == "cooldown"
    assert len(wire.requests) == 1

    # A local preflight oversize builds no client and leaves the service available.
    preflight = DecisionService(_config(max_request_bytes=DEFAULT_BYTE_FLOOR))
    wire.requests.clear()
    wire.client_kwargs.clear()
    wire.handler = _must_not_dial
    with pytest.raises(DecisionsUnavailable) as oversize:
        await preflight.box("test", STATE, _large_logo())
    assert oversize.value.reason == "oversize"
    assert wire.client_kwargs == []
    wire.handler = _responder("systemone")
    result = await preflight.choose("test", STATE, QUESTIONS)
    assert result.answers["triage"].choice == "keep"
    assert len(wire.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_contract", IDENTITY_MODES)
async def test_systemone_identity_modes_share_the_estimate_boundary(
    wire: FakeWire, identity_contract: str
) -> None:
    estimate = DecisionService(
        _config(identity_contract=identity_contract)
    ).estimate_request_tokens(WIDE_STATE, QUESTIONS)
    assert estimate > 256

    over = DecisionService(
        _config(identity_contract=identity_contract, max_input_tokens=estimate - 1)
    )
    assert not over.request_fits(WIDE_STATE, QUESTIONS)
    wire.handler = _must_not_dial
    with pytest.raises(DecisionsUnavailable) as caught:
        await over.choose("test", WIDE_STATE, QUESTIONS)
    assert caught.value.reason == "oversize"
    assert (wire.requests, wire.client_kwargs) == ([], [])

    at = DecisionService(_config(identity_contract=identity_contract, max_input_tokens=estimate))
    assert at.request_fits(WIDE_STATE, QUESTIONS)
    wire.handler = _card_handler(lambda: _card("/models/kev-a"))
    result = await at.choose("test", WIDE_STATE, QUESTIONS)
    assert result.backend_identity is not None
    assert [request.url.path for request in wire.requests].count("/v1/systemone") == 1


@pytest.mark.asyncio
async def test_multi_question_choice_fits_each_branch_but_not_the_aggregate(
    wire: FakeWire,
) -> None:
    question = ChoiceQuestion("Choose " + "x" * 1200, TRIAGE.criteria)
    questions = {"a": question, "b": question}
    probe = DecisionService(_config())
    branch = probe.estimate_request_tokens(STATE, {"a": question})
    total = probe.estimate_request_tokens(STATE, questions)
    assert 256 <= branch < total - 1

    service = DecisionService(_config(max_input_tokens=total - 1))
    assert all(service.request_fits(STATE, {key: question}) for key in questions)
    assert not service.request_fits(STATE, questions)
    wire.handler = _must_not_dial
    with pytest.raises(DecisionsUnavailable) as caught:
        await service.choose("test", STATE, questions)
    assert caught.value.reason == "oversize"
    assert wire.requests == []

    wire.handler = _responder("systemone")
    result = await service.choose("test", STATE, {"a": question})
    assert set(result.answers) == {"a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_contract", IDENTITY_MODES)
@pytest.mark.parametrize("limit", ["tokens", "bytes"])
async def test_oversize_constructs_no_client_card_or_post(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, identity_contract: str, limit: str
) -> None:
    poisoned = {"on": True}
    real_client, real_post, real_card = (
        httpx.AsyncClient,
        decisions._post,
        decisions._fetch_card_identity,
    )

    def guard(name: str) -> None:
        if poisoned["on"]:
            raise AssertionError(f"an oversize request reached {name}")

    def client(**kwargs: Any) -> httpx.AsyncClient:
        guard("the client constructor")
        return real_client(**kwargs)

    def post(*args: Any, **kwargs: Any) -> Any:
        guard("the decision POST")
        return real_post(*args, **kwargs)

    def card(*args: Any, **kwargs: Any) -> Any:
        guard("the model-card GET")
        return real_card(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(decisions, "_post", post)
    monkeypatch.setattr(decisions, "_fetch_card_identity", card)
    if limit == "bytes":
        _require_max_request_bytes()
        service = DecisionService(
            _config(identity_contract=identity_contract, max_request_bytes=DEFAULT_BYTE_FLOOR)
        )
    else:
        service = DecisionService(
            _config(identity_contract=identity_contract, max_input_tokens=256)
        )

    for _ in range(2):
        with pytest.raises(DecisionsUnavailable) as caught:
            if limit == "bytes":
                await service.box("test", STATE, _large_logo())
            else:
                await service.choose("test", {"text": "x" * 1200}, QUESTIONS)
        assert caught.value.reason == "oversize"
    assert wire.requests == []

    # Preflight opened no cooldown: the next fitting request dials once.
    poisoned["on"] = False
    wire.handler = _card_handler(lambda: _card("/models/kev-a"))
    result = await service.choose("test", STATE, QUESTIONS)
    assert result.answers["triage"].choice == "keep"
    assert [request.url.path for request in wire.requests].count("/v1/systemone") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_contract", IDENTITY_MODES)
@pytest.mark.parametrize("status", [429, 504])
async def test_retry_resends_identical_bodies_within_limits(
    wire: FakeWire, identity_contract: str, status: int
) -> None:
    _require_max_request_bytes()
    questions = {"triage": TRIAGE, "severity": SEVERITY}
    state = {**UTF8_STATE, **WIDE_STATE}
    config = _config(identity_contract=identity_contract, max_request_bytes=DEFAULT_BYTE_FLOOR)
    failures = [status]
    answer = _card_handler(lambda: _card("/models/kev-a"))

    def handler(request: httpx.Request) -> Any:
        if request.url.path == "/v1/systemone" and failures:
            return httpx.Response(failures.pop())
        return answer(request)

    wire.handler = handler
    service = DecisionService(config)

    result = await service.choose("test", state, questions)

    assert set(result.answers) == set(questions)
    posts = [request.content for request in wire.requests if request.url.path == "/v1/systemone"]
    assert len(posts) == 2
    assert posts[0] == posts[1]
    assert set(json.loads(posts[0])["questions"]) == set(questions)
    assert service.estimate_request_tokens(state, questions) <= config.max_input_tokens
    assert all(len(body) <= DEFAULT_BYTE_FLOOR for body in posts)
