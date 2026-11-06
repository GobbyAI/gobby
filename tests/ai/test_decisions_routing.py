"""Per-type endpoint routing: dialing, merged evaluate calls, and per-endpoint state."""

import base64
import json
import struct
import zlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from gobby.ai import decisions
from gobby.ai.decisions import (
    BoxAnswer,
    BoxImage,
    BoxQuestion,
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionService,
    DecisionsUnavailable,
    NoulAnswer,
    get_decision_service,
)
from gobby.config.ai import DecisionsConfig

pytestmark = pytest.mark.unit

REAL_ASYNC_CLIENT = httpx.AsyncClient
DEFAULT_HOST = "ollama.test"
BOX_HOST = "openrouter.test"
BOX_MODEL = "typesafe/jev-1.14"
TWIN_HOST = "twin.test"
STATE: dict[str, Any] = {"finding": "unused import in module foo"}
TRIAGE = ChoiceQuestion(instructions="Pick one.", criteria={"fix": "Fix it", "defer": "Defer"})


def _png(width: int, height: int) -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + bytes(width) for _ in range(height))
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    body += chunk(b"IDAT", zlib.compress(rows))
    data = b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")
    return base64.b64encode(data).decode()


def _box(width: int = 512, height: int = 512) -> BoxQuestion:
    return BoxQuestion(instructions="Where?", image=BoxImage("image/png", _png(width, height)))


class Endpoints:
    """Routes each request by host and records every dial."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.host)
        if route is None:
            raise AssertionError(f"unexpected dial to {request.url.host}")
        return route(request)

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


@pytest.fixture
def endpoints(monkeypatch: pytest.MonkeyPatch) -> Endpoints:
    fake = Endpoints()

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(decisions, "_RETRY_DELAY", 0.0)
    monkeypatch.setattr(decisions, "_service_cache", None)
    return fake


def _answer(question: dict[str, Any]) -> dict[str, Any]:
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
        return {"type": "noul", "noul": 0.9}
    return {
        "type": "box",
        "box": {"top": 0.25, "bottom": 0.74, "left": 0.25, "right": 0.76},
        "confidence": 0.9,
    }


def _reply(request: httpx.Request) -> httpx.Response:
    sent = json.loads(request.content)
    answers = {key: _answer(question) for key, question in sent["questions"].items()}
    body = {"model": sent["model"], "answers": answers, "usage": {"input_tokens": 10}}
    return httpx.Response(200, json=body)


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(code, json={"error": "nope"})


def _routed(**overrides: Any) -> DecisionsConfig:
    """Choice, Noul and Score on a systemone endpoint; Box on OpenRouter."""
    box: dict[str, Any] = {
        "wire_api": "openrouter-decisions",
        "api_base": f"https://{BOX_HOST}/api",
        "model": BOX_MODEL,
        "api_key": "sk-or-test",
        "identity_contract": "response_version",
    }
    values: dict[str, Any] = {
        "api_base": f"http://{DEFAULT_HOST}",
        "wire_api": "systemone",
        "model": "clef:27b",
        "overrides": {"box": box},
    }
    values.update(overrides)
    return DecisionsConfig(**values)


@pytest.mark.asyncio
async def test_each_type_dials_only_its_routed_endpoint(endpoints: Endpoints) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: _reply}
    service = DecisionService(_routed())

    await service.choose("test", STATE, {"triage": TRIAGE})
    await service.box("test", STATE, {"logo": _box()})

    assert endpoints.hosts() == [DEFAULT_HOST, BOX_HOST]


@pytest.mark.asyncio
async def test_evaluate_merges_answers_in_question_order_with_one_request_per_endpoint(
    endpoints: Endpoints,
) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: _reply}
    service = DecisionService(_routed())
    questions: dict[str, Any] = {"logo": _box(), "triage": TRIAGE, "triage2": TRIAGE}

    outcomes = await service.evaluate("test", STATE, questions)

    assert list(outcomes) == ["logo", "triage", "triage2"]
    assert sorted(endpoints.hosts()) == sorted([BOX_HOST, DEFAULT_HOST])
    assert isinstance(outcomes["triage"].answer, ChoiceAnswer)
    assert isinstance(outcomes["triage2"].answer, ChoiceAnswer)
    assert isinstance(outcomes["logo"].answer, BoxAnswer)
    assert all(outcome.error is None for outcome in outcomes.values())


@pytest.mark.asyncio
async def test_choice_and_noul_on_one_endpoint_share_one_request(endpoints: Endpoints) -> None:
    # One endpoint takes one request for every type it serves; each answer keeps its own type.
    endpoints.routes = {DEFAULT_HOST: _reply}
    service = DecisionService(_routed())

    outcomes = await service.evaluate("test", STATE, {"triage": TRIAGE, "carry": "Is it used?"})

    assert endpoints.hosts() == [DEFAULT_HOST]
    assert isinstance(outcomes["triage"].answer, ChoiceAnswer)
    assert isinstance(outcomes["carry"].answer, NoulAnswer)
    assert all(outcome.error is None for outcome in outcomes.values())


def _twin_config() -> DecisionsConfig:
    """Choice and Noul each route to their own copy of one endpoint config."""
    twin = {"api_base": f"http://{TWIN_HOST}", "wire_api": "systemone", "model": "clef:27b"}
    return DecisionsConfig(
        api_base=f"http://{DEFAULT_HOST}",
        wire_api="systemone",
        model="clef:27b",
        overrides={"choice": dict(twin), "noul": dict(twin)},
    )


@pytest.mark.asyncio
async def test_equal_endpoint_configs_share_one_request(endpoints: Endpoints) -> None:
    # Equal full configs are one endpoint: one request carries both types.
    endpoints.routes = {TWIN_HOST: _reply}
    service = DecisionService(_twin_config())

    outcomes = await service.evaluate("test", STATE, {"triage": TRIAGE, "carry": "Is it used?"})

    assert endpoints.hosts() == [TWIN_HOST]
    assert isinstance(outcomes["triage"].answer, ChoiceAnswer)
    assert isinstance(outcomes["carry"].answer, NoulAnswer)
    assert all(outcome.error is None for outcome in outcomes.values())


@pytest.mark.asyncio
async def test_equal_endpoint_configs_share_one_cooldown(endpoints: Endpoints) -> None:
    endpoints.routes = {TWIN_HOST: _status(401)}
    service = DecisionService(_twin_config())

    with pytest.raises(DecisionsUnavailable) as first:
        await service.choose("test", STATE, {"triage": TRIAGE})
    with pytest.raises(DecisionsUnavailable) as second:
        await service.noul("test", STATE, {"carry": "Is it used?"})

    assert first.value.reason == "http_status"
    assert second.value.reason == "cooldown"
    assert endpoints.hosts() == [TWIN_HOST]


@pytest.mark.asyncio
async def test_failure_on_box_endpoint_keeps_choice_answers(endpoints: Endpoints) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: _status(401)}
    service = DecisionService(_routed())

    outcomes = await service.evaluate("test", STATE, {"triage": TRIAGE, "logo": _box()})

    assert outcomes["triage"].error is None
    assert isinstance(outcomes["triage"].answer, ChoiceAnswer)
    assert outcomes["logo"].answer is None
    assert outcomes["logo"].error is not None
    assert outcomes["logo"].error.reason == "http_status"


@pytest.mark.asyncio
async def test_cooldown_is_per_endpoint(endpoints: Endpoints) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: _status(500)}
    service = DecisionService(_routed())

    with pytest.raises(DecisionsUnavailable) as first:
        await service.box("test", STATE, {"logo": _box()})
    box_dials = len(endpoints.requests)
    with pytest.raises(DecisionsUnavailable) as second:
        await service.box("test", STATE, {"logo": _box()})
    await service.choose("test", STATE, {"triage": TRIAGE})

    assert first.value.reason == "http_status"
    assert second.value.reason == "cooldown"
    assert len(endpoints.requests) == box_dials + 1
    assert endpoints.hosts()[-1] == DEFAULT_HOST


@pytest.mark.asyncio
async def test_undersized_box_is_refused_pre_dial_and_opens_no_cooldown(
    endpoints: Endpoints,
) -> None:
    endpoints.routes = {BOX_HOST: _reply}
    service = DecisionService(_routed())

    with pytest.raises(DecisionsUnavailable) as caught:
        await service.box("test", STATE, {"logo": _box(64, 64)})

    assert caught.value.reason == "invalid_request"
    assert "64x64" in caught.value.detail
    assert "280 to 1568" in caught.value.detail
    assert endpoints.requests == []

    result = await service.box("test", STATE, {"logo": _box()})
    assert isinstance(result.answers["logo"], BoxAnswer)
    assert endpoints.hosts() == [BOX_HOST]


@pytest.mark.asyncio
async def test_box_on_a_default_that_cannot_serve_it_is_refused_before_dialing(
    endpoints: Endpoints,
) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply}
    service = DecisionService(_routed(overrides={}))

    with pytest.raises(DecisionsUnavailable) as caught:
        await service.box("test", STATE, {"logo": _box()})

    assert caught.value.reason == "unconfigured"
    assert "systemone" in caught.value.detail
    assert "openrouter-decisions" in caught.value.detail
    assert endpoints.requests == []


@pytest.mark.asyncio
async def test_limits_are_per_endpoint(endpoints: Endpoints) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: _reply}
    box = {
        "wire_api": "openrouter-decisions",
        "api_base": f"https://{BOX_HOST}/api",
        "model": BOX_MODEL,
        "max_input_tokens": 256,
    }
    service = DecisionService(_routed(overrides={"box": box}))

    outcomes = await service.evaluate(
        "test", {"text": "x" * 4000}, {"triage": TRIAGE, "logo": _box()}
    )

    assert outcomes["triage"].error is None
    assert outcomes["logo"].error is not None
    assert outcomes["logo"].error.reason == "oversize"
    assert endpoints.hosts() == [DEFAULT_HOST]


@pytest.mark.asyncio
async def test_backend_identity_is_reported_per_endpoint(endpoints: Endpoints) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: _reply}
    service = DecisionService(_routed())

    outcomes = await service.evaluate("test", STATE, {"triage": TRIAGE, "logo": _box()})

    assert outcomes["triage"].backend_identity is None
    assert outcomes["logo"].backend_identity == f"response:{BOX_MODEL}"
    assert outcomes["logo"].response_model == BOX_MODEL


@pytest.mark.asyncio
async def test_unrouted_type_errors_only_its_own_questions(endpoints: Endpoints) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply}
    config = DecisionsConfig(
        overrides={
            "choice": {
                "wire_api": "systemone",
                "api_base": f"http://{DEFAULT_HOST}",
                "model": "clef:27b",
            }
        },
    )
    service = DecisionService(config)

    outcomes = await service.evaluate("test", STATE, {"triage": TRIAGE, "logo": _box()})

    assert outcomes["triage"].error is None
    assert outcomes["logo"].error is not None
    assert outcomes["logo"].error.reason == "unconfigured"
    assert endpoints.hosts() == [DEFAULT_HOST]


def test_request_fits_reports_an_unserved_type_as_unconfigured_not_oversize() -> None:
    config = DecisionsConfig(
        overrides={
            "choice": {
                "wire_api": "systemone",
                "api_base": f"http://{DEFAULT_HOST}",
                "model": "clef:27b",
            }
        },
    )
    service = DecisionService(config)

    assert service.request_fits(STATE, {"triage": TRIAGE})
    with pytest.raises(DecisionsUnavailable) as caught:
        service.request_fits(STATE, {"logo": _box()})
    assert caught.value.reason == "unconfigured"


@pytest.mark.asyncio
async def test_out_of_float_range_integer_fails_only_its_own_question(
    endpoints: Endpoints,
) -> None:
    def huge_confidence(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        answers = {key: _answer(question) for key, question in sent["questions"].items()}
        for answer in answers.values():
            answer["confidence"] = 10**400
        body = {"model": sent["model"], "answers": answers, "usage": {"input_tokens": 10}}
        return httpx.Response(200, content=json.dumps(body).encode())

    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: huge_confidence}
    service = DecisionService(_routed())

    outcomes = await service.evaluate("test", STATE, {"triage": TRIAGE, "logo": _box()})

    assert outcomes["triage"].error is None
    assert isinstance(outcomes["triage"].answer, ChoiceAnswer)
    assert outcomes["logo"].error is not None
    assert outcomes["logo"].error.reason == "parse"


@pytest.mark.asyncio
async def test_exchange_observer_sees_every_endpoint(endpoints: Endpoints) -> None:
    endpoints.routes = {DEFAULT_HOST: _reply, BOX_HOST: _reply}
    observed: list[httpx.Response] = []
    service = DecisionService(_routed(), exchange_observer=observed.append)

    await service.evaluate("test", STATE, {"triage": TRIAGE, "logo": _box()})

    assert len(observed) == 2
    assert all(response.status_code == 200 for response in observed)


def test_cache_identity_covers_the_per_type_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decisions, "_service_cache", None)
    first = get_decision_service(_routed())
    assert get_decision_service(_routed()) is first

    other_box = _routed(
        overrides={
            "box": {
                "wire_api": "openrouter-decisions",
                "api_base": f"https://{BOX_HOST}/api",
                "model": "typesafe/jev-1.15",
            }
        }
    )
    assert get_decision_service(other_box) is not first
