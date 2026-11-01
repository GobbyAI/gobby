"""Tests for the agent-facing gobby-decisions:evaluate MCP tool."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import zlib
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from gobby.ai import decisions
from gobby.ai.decisions import ChoiceQuestion, DecisionsUnavailable
from gobby.config.ai import AIConfig, DecisionsConfig
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.tools.decisions import create_decisions_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.workflows.definitions import MCPStepConfig, PipelineStep
from gobby.workflows.pipeline.handlers import execute_mcp_step

REAL_ASYNC_CLIENT = httpx.AsyncClient
STATE: dict[str, Any] = {"finding": "unused import in module foo"}
CHOICE: dict[str, Any] = {
    "type": "choice",
    "instructions": "Pick the triage lane.",
    "criteria": {"fix": "fix now", "defer": "defer to backlog"},
}
NOUL: dict[str, Any] = {"type": "noul", "instructions": "The finding is a real defect."}
SCORE: dict[str, Any] = {
    "type": "score",
    "instructions": "Rate the severity.",
    "levels": ["low", "medium", "high"],
}


class FakeWire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict[str, Any]] = []
        self.handler: Callable[[httpx.Request], Any] = _respond_with_answers


def _png(width: int = 280, height: int = 280) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = b"".join(b"\x00" + bytes(width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def _answer_for(question: Mapping[str, Any]) -> dict[str, Any]:
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
        return {"type": "noul", "noul": 0.25}
    if kind == "score":
        legend = {str(index): level for index, level in enumerate(question["criteria"])}
        probabilities = {key: (1.0 if key == "0" else 0.0) for key in legend}
        return {
            "type": "score",
            "legend": legend,
            "probabilities": probabilities,
            "score": 0.0,
            "confidence": 0.5,
        }
    if kind == "box":
        box = {"top": 0.1, "bottom": 0.4, "left": 0.2, "right": 0.6}
        return {"type": "box", "box": box, "confidence": 0.9}
    raise AssertionError(f"unexpected question type {kind!r}")


def _respond_with_answers(request: httpx.Request) -> httpx.Response:
    sent = json.loads(request.content)
    answers = {key: _answer_for(question) for key, question in sent["questions"].items()}
    body = {"model": sent["model"], "answers": answers, "usage": {"input_tokens": 10}}
    return httpx.Response(200, json=body)


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(code)


def _box(image: Path) -> dict[str, Any]:
    return {"type": "box", "instructions": "Locate the logo.", "image_path": str(image)}


def _choice_with(criteria: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "choice", "instructions": "x", "criteria": dict(criteria)}


@pytest.fixture(autouse=True)
def _fresh_service_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decisions, "_service_cache", None)
    monkeypatch.setattr(decisions, "_RETRY_DELAY", 0.0)


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


def _config(**overrides: Any) -> DecisionsConfig:
    values: dict[str, Any] = {"api_base": "https://decide.test", "model": "jev-latest"}
    values.update(overrides)
    return DecisionsConfig(**values)


def _registry(config: DecisionsConfig | None) -> InternalToolRegistry:
    daemon = None if config is None else DaemonConfig(ai=AIConfig(decisions=config))
    return create_decisions_registry(lambda: daemon)


async def _evaluate(
    registry: InternalToolRegistry, questions: Mapping[str, Any], **extra: Any
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"state": STATE, "questions": dict(questions), **extra}
    result: dict[str, Any] = await registry.call("evaluate", arguments)
    return result


async def test_evaluate_round_trips_every_decision_shape(
    wire: FakeWire, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.ai.decisions")
    registry = _registry(_config())
    image = tmp_path / "logo.png"
    image.write_bytes(_png())

    choice = await _evaluate(registry, {"lane": CHOICE})
    noul = await _evaluate(registry, {"real": NOUL})
    score = await _evaluate(registry, {"severity": SCORE})
    box = await _evaluate(registry, {"logo": _box(image)})

    assert choice == {
        "success": True,
        "type": "choice",
        "answers": {
            "lane": {
                "choice": "fix",
                "probabilities": {"fix": 1.0, "defer": 0.0},
                "confidence": 0.5,
            }
        },
        "response_model": "jev-latest",
        "backend_identity": None,
    }
    assert noul == {
        "success": True,
        "type": "noul",
        "answers": {"real": {"probability": 0.25}},
        "response_model": "jev-latest",
        "backend_identity": None,
    }
    assert score == {
        "success": True,
        "type": "score",
        "answers": {
            "severity": {
                "score": 0.0,
                "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0},
                "confidence": 0.5,
            }
        },
        "response_model": "jev-latest",
        "backend_identity": None,
    }
    assert box == {
        "success": True,
        "type": "box",
        "answers": {
            "logo": {
                "box": {"top": 0.1, "bottom": 0.4, "left": 0.2, "right": 0.6},
                "confidence": 0.9,
            }
        },
        "response_model": "jev-latest",
        "backend_identity": None,
    }
    posted = [
        next(iter(json.loads(r.content)["questions"].values()))["type"] for r in wire.requests
    ]
    assert posted == ["choice", "noul", "score", "box"]
    assert "consumer=mcp_evaluate" in caplog.text


async def test_evaluate_choice_and_noul_round_trip(wire: FakeWire) -> None:
    registry = _registry(_config())

    choice = await _evaluate(registry, {"lane": CHOICE}, timeout_seconds=30.0)
    noul = await _evaluate(registry, {"real": NOUL})

    assert choice["success"] is True
    assert noul["answers"] == {"real": {"probability": 0.25}}
    choice_body, noul_body = (json.loads(request.content) for request in wire.requests)
    assert choice_body["state"] == STATE
    assert choice_body["questions"] == {
        "lane": {
            "type": "choice",
            "instructions": CHOICE["instructions"],
            "criteria": CHOICE["criteria"],
        }
    }
    assert noul_body["questions"] == {
        "real": {"type": "noul", "instructions": NOUL["instructions"]}
    }
    # The 30-second caller budget is capped at the configured 2-second timeout.
    assert wire.client_kwargs[0]["timeout"] == 2.0


INVALID_REQUESTS = [
    pytest.param({"state": ["finding"]}, id="state-not-object"),
    pytest.param({"questions": {}}, id="empty-question-map"),
    pytest.param({"questions": {f"q{i}": NOUL for i in range(65)}}, id="sixty-five-questions"),
    pytest.param({"questions": {"a/b": NOUL}}, id="key-outside-pattern"),
    pytest.param({"questions": {"q": "The finding is real."}}, id="question-not-object"),
    pytest.param({"questions": {"q": {**NOUL, "extra": 1}}}, id="extra-question-key"),
    pytest.param({"questions": {"a": NOUL, "b": CHOICE}}, id="mixed-types"),
    pytest.param({"questions": {"q": {"type": "rank", "instructions": "x"}}}, id="unknown-type"),
    pytest.param({"questions": {"q": {"instructions": "x"}}}, id="missing-type"),
    pytest.param({"questions": {"q": {"type": "noul"}}}, id="instructions-missing"),
    pytest.param(
        {"questions": {"q": {"type": "noul", "instructions": ""}}}, id="instructions-empty"
    ),
    pytest.param(
        {"questions": {"q": {"type": "noul", "instructions": ["x"]}}},
        id="instructions-list",
    ),
    pytest.param(
        {"questions": {"q": {"type": "choice", "instructions": "x"}}},
        id="choice-criteria-missing",
    ),
    pytest.param({"questions": {"q": _choice_with({})}}, id="choice-criteria-empty"),
    pytest.param({"questions": {"q": _choice_with({"only": "one"})}}, id="choice-one-option"),
    pytest.param(
        {"questions": {"q": _choice_with({f"o{i}": f"text {i}" for i in range(256)})}},
        id="choice-256-options",
    ),
    pytest.param(
        {"questions": {"q": _choice_with({"a": 42, "b": "B"})}}, id="choice-non-string-text"
    ),
    pytest.param({"questions": {"q": _choice_with({"a": "", "b": "B"})}}, id="choice-empty-text"),
    pytest.param({"questions": {"q": {**NOUL, "criteria": {"a": "b"}}}}, id="noul-with-criteria"),
    pytest.param({"timeout_seconds": True}, id="timeout-bool"),
    pytest.param({"timeout_seconds": 0}, id="timeout-zero"),
    pytest.param({"timeout_seconds": -1.0}, id="timeout-negative"),
    pytest.param({"timeout_seconds": math.nan}, id="timeout-nan"),
    pytest.param({"timeout_seconds": math.inf}, id="timeout-infinite"),
]


@pytest.mark.parametrize("overrides", INVALID_REQUESTS)
async def test_evaluate_rejects_invalid_requests_without_dialing(
    wire: FakeWire, overrides: dict[str, Any]
) -> None:
    registry = _registry(_config())
    arguments: dict[str, Any] = {"state": STATE, "questions": {"q": NOUL}} | overrides

    result = await registry.call("evaluate", arguments)

    assert result["success"] is False
    assert result["reason"] == "invalid_request"
    assert result["error"].startswith("invalid_request: ")
    assert wire.requests == []
    assert decisions._service_cache is None


async def test_evaluate_accepts_boundary_requests(wire: FakeWire) -> None:
    registry = _registry(_config())

    sixty_four = await _evaluate(registry, {f"q{i}": NOUL for i in range(64)})
    two_options = await _evaluate(registry, {"lane": _choice_with({"a": "A", "b": "B"})})
    full_options = await _evaluate(
        registry, {"lane": _choice_with({f"o{i}": f"text {i}" for i in range(255)})}
    )

    assert [sixty_four["success"], two_options["success"], full_options["success"]] == [
        True,
        True,
        True,
    ]
    assert len(wire.requests) == 3


def test_evaluate_schema_carries_nested_bounds() -> None:
    registry = _registry(None)
    schema = registry.get_schema("evaluate")
    assert schema is not None
    input_schema = schema["inputSchema"]

    questions = input_schema["properties"]["questions"]
    assert questions["minProperties"] == 1
    assert questions["maxProperties"] == 64
    assert questions["propertyNames"]["pattern"] == r"^[A-Za-z0-9_.-]{1,100}$"
    shapes = {
        shape["properties"]["type"]["const"]: shape
        for shape in questions["additionalProperties"]["oneOf"]
    }
    assert set(shapes) == {"choice", "noul", "score", "box"}
    assert shapes["noul"]["additionalProperties"] is False
    criteria = shapes["choice"]["properties"]["criteria"]
    assert criteria["minProperties"] == 2
    assert criteria["maxProperties"] == 255


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(None, id="no-daemon-config"),
        pytest.param(DecisionsConfig(), id="api-base-unset"),
    ],
)
async def test_evaluate_reports_unconfigured_without_building_service(
    wire: FakeWire, config: DecisionsConfig | None
) -> None:
    registry = _registry(config)

    result = await _evaluate(registry, {"lane": CHOICE})

    assert result["success"] is False
    assert result["reason"] == "unconfigured"
    assert result["error"].startswith("unconfigured: ")
    assert wire.requests == []
    assert decisions._service_cache is None


@pytest.mark.parametrize(
    ("state", "status", "reason"),
    [
        pytest.param({"blob": "x" * 60_000}, None, "oversize", id="oversize"),
        pytest.param(STATE, 401, "http_status", id="http-status"),
    ],
)
async def test_evaluate_maps_unavailable_reasons(
    wire: FakeWire, state: dict[str, Any], status: int | None, reason: str
) -> None:
    registry = _registry(_config())
    if status is not None:
        wire.handler = _status(status)

    result = await registry.call("evaluate", {"state": state, "questions": {"lane": CHOICE}})

    assert result["success"] is False
    assert result["reason"] == reason
    assert result["error"].startswith(f"{reason}: ")


async def test_evaluate_fails_fast_on_cooldown_opened_by_evaluate(wire: FakeWire) -> None:
    config = _config()
    registry = _registry(config)
    wire.handler = _status(401)

    first = await _evaluate(registry, {"lane": CHOICE})
    second = await _evaluate(registry, {"lane": CHOICE})

    assert first["reason"] == "http_status"
    assert second["reason"] == "cooldown"
    assert second["error"].startswith("cooldown: ")
    assert len(wire.requests) == 1


async def test_evaluate_fails_fast_on_cooldown_opened_by_consumer(wire: FakeWire) -> None:
    config = _config()
    registry = _registry(config)
    wire.handler = _status(401)
    consumer = decisions.get_decision_service(config)
    question = ChoiceQuestion(instructions="Pick the triage lane.", criteria={"a": "A", "b": "B"})

    with pytest.raises(DecisionsUnavailable) as opened:
        await consumer.choose("consumer", STATE, {"lane": question})
    result = await _evaluate(registry, {"lane": CHOICE})

    assert opened.value.reason == "http_status"
    assert result["reason"] == "cooldown"
    assert len(wire.requests) == 1


async def test_evaluate_propagates_caller_cancellation(wire: FakeWire) -> None:
    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    wire.handler = hang
    registry = _registry(_config())
    task = asyncio.create_task(_evaluate(registry, {"lane": CHOICE}))
    for _ in range(1000):
        if wire.requests:
            break
        await asyncio.sleep(0)
    assert wire.requests, "the decision request never reached the transport"

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    service = decisions.get_decision_service(_config())
    assert service._cooldown_until == 0.0


def test_decisions_registry_is_registered() -> None:
    manager = setup_internal_registries(config_resolver=lambda: None)

    registry = manager.get_registry("gobby-decisions")
    assert registry is not None
    metadata = registry.get_tool_metadata("evaluate")
    assert metadata is not None
    assert metadata.read_only is True


async def test_pipeline_step_passes_answers_and_fails_closed(wire: FakeWire) -> None:
    registry = _registry(_config())

    async def call_tool(server: str, tool: str, arguments: dict[str, Any], **_: Any) -> Any:
        assert server == "gobby-decisions"
        return await registry.call(tool, arguments)

    proxy = SimpleNamespace(call_tool=call_tool)
    step = PipelineStep(
        id="decide",
        mcp=MCPStepConfig(
            server="gobby-decisions",
            tool="evaluate",
            arguments={"state": STATE, "questions": {"lane": CHOICE}},
        ),
    )

    output = await execute_mcp_step(step, {"inputs": {}, "steps": {}}, lambda: proxy)

    assert output == {
        "type": "choice",
        "answers": {
            "lane": {
                "choice": "fix",
                "probabilities": {"fix": 1.0, "defer": 0.0},
                "confidence": 0.5,
            }
        },
        "response_model": "jev-latest",
        "backend_identity": None,
    }

    wire.handler = _status(401)
    with pytest.raises(RuntimeError, match="http_status"):
        await execute_mcp_step(step, {"inputs": {}, "steps": {}}, lambda: proxy)
    with pytest.raises(RuntimeError, match="cooldown"):
        await execute_mcp_step(step, {"inputs": {}, "steps": {}}, lambda: proxy)
    assert len(wire.requests) == 2
