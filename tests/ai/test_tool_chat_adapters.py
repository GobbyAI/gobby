"""Tests for the Family A (openai_compatible) tool_chat adapter loop."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.ai import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.ai import _tool_chat_tools as tools
from gobby.ai._tool_chat_adapters import OpenAICompatibleToolChatAdapter
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolLoopLimits, ToolPolicy


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: Any = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage, usage: Any = None) -> None:
        self.choices = [_FakeChoice(message)]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, scripted: list[_FakeResponse], *, repeat_last: bool = False) -> None:
        self._scripted = list(scripted)
        self._repeat_last = repeat_last
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if len(self._scripted) > 1 or not self._repeat_last:
            return self._scripted.pop(0)
        return self._scripted[0]


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, scripted: list[_FakeResponse], *, repeat_last: bool = False) -> None:
        self.chat = _FakeChat(_FakeCompletions(scripted, repeat_last=repeat_last))


def _binding() -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider="local:lm-studio",
        adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
        available=True,
        models=("gemma",),
        metadata={"endpoint": "lm-studio"},
    )


def _request(**overrides: Any) -> ToolChatRequest:
    base: dict[str, Any] = {
        "prompt": "Document the auth module.",
        "tool_policy": ToolPolicy(cli="gcode", tools=("search", "outline")),
        "project_path": "/repo",
    }
    base.update(overrides)
    return ToolChatRequest(**base)


@pytest.mark.asyncio
async def test_openai_loop_executes_tool_then_returns_narrative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_argv(argv: list[str], **kwargs: Any) -> str:
        assert argv[:2] == ["gcode", "search"]
        return "SEARCH RESULTS"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)

    client = _FakeClient(
        [
            _FakeResponse(
                _FakeMessage(
                    tool_calls=[_FakeToolCall("c1", "gcode_search", '{"args": ["auth"]}')]
                ),
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
            _FakeResponse(
                _FakeMessage(content="## Auth\n\nGrounded narrative."),
                usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            ),
        ]
    )
    adapter = OpenAICompatibleToolChatAdapter(client_factory=lambda _binding: client)

    result = await adapter.chat(_request(), _binding())

    assert result.text == "## Auth\n\nGrounded narrative."
    assert result.provider == "local:lm-studio"
    assert result.model == "gemma"
    assert result.tool_use_count == 1
    assert result.turns == 2
    assert result.tools == {"gcode_search": 1}
    assert result.stop_reason == "completed"
    assert result.usage == {"prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43}

    first_call = client.chat.completions.calls[0]
    assert first_call["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in first_call["tools"]} == {
        "gcode_search",
        "gcode_outline",
    }
    second_call = client.chat.completions.calls[1]
    tool_messages = [m for m in second_call["messages"] if m["role"] == "tool"]
    assert tool_messages[0]["content"] == "SEARCH RESULTS"


@pytest.mark.asyncio
async def test_openai_loop_denies_out_of_policy_tool_without_running_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_calls = {"count": 0}

    async def fake_run_argv(argv: list[str], **kwargs: Any) -> str:
        run_calls["count"] += 1
        return "SHOULD NOT RUN"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)

    client = _FakeClient(
        [
            _FakeResponse(
                _FakeMessage(tool_calls=[_FakeToolCall("c1", "gcode_index", '{"args": []}')])
            ),
            _FakeResponse(_FakeMessage(content="done")),
        ]
    )
    adapter = OpenAICompatibleToolChatAdapter(client_factory=lambda _binding: client)

    result = await adapter.chat(_request(), _binding())

    # The mutating tool was outside the read-only policy: never executed.
    assert run_calls["count"] == 0
    assert result.text == "done"
    second_call = client.chat.completions.calls[1]
    tool_messages = [m for m in second_call["messages"] if m["role"] == "tool"]
    assert tool_messages[0]["content"].startswith("[error")


@pytest.mark.asyncio
async def test_openai_loop_stops_at_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_argv(argv: list[str], **kwargs: Any) -> str:
        return "RESULT"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)

    client = _FakeClient(
        [
            _FakeResponse(
                _FakeMessage(tool_calls=[_FakeToolCall("c1", "gcode_search", '{"args": ["x"]}')])
            )
        ],
        repeat_last=True,
    )
    adapter = OpenAICompatibleToolChatAdapter(client_factory=lambda _binding: client)

    result = await adapter.chat(_request(limits=ToolLoopLimits(max_turns=2)), _binding())

    assert result.stop_reason == "max_turns"
    assert result.turns == 2
    assert result.tool_use_count == 2


@pytest.mark.asyncio
async def test_openai_loop_stops_at_max_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_argv(argv: list[str], **kwargs: Any) -> str:
        return "RESULT"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)

    client = _FakeClient(
        [
            _FakeResponse(
                _FakeMessage(
                    tool_calls=[
                        _FakeToolCall("c1", "gcode_search", '{"args": ["a"]}'),
                        _FakeToolCall("c2", "gcode_outline", '{"args": ["b"]}'),
                    ]
                )
            )
        ],
        repeat_last=True,
    )
    adapter = OpenAICompatibleToolChatAdapter(client_factory=lambda _binding: client)

    result = await adapter.chat(_request(limits=ToolLoopLimits(max_tool_calls=1)), _binding())

    assert result.stop_reason == "max_tool_calls"
    assert result.tool_use_count == 1
