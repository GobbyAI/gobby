"""DecisionService: Choice wires, request ceiling, cooldown, and identity (task #24244).

Every test runs against a fake httpx transport. No test dials a live decision server.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.ai import decisions
from gobby.ai.decisions import (
    ChoiceQuestion,
    DecisionService,
    DecisionsUnavailable,
    get_decision_service,
)
from gobby.config.ai import DecisionsConfig

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
        _wire_config("openrouter-decisions", api_key="key-one"),
    ]
    for variant in variants:
        service = get_decision_service(variant)
        assert service is not base
        assert get_decision_service(variant) is service


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
