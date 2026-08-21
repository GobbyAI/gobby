"""Tests for the Family A (openai_compatible) tool_chat adapter loop."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.ai import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.ai import _tool_chat_tools as tools
from gobby.ai._tool_chat_adapters import (
    _DISALLOWED_TOOLS,
    ClaudeToolChatAdapter,
    OpenAICompatibleToolChatAdapter,
    _make_tool_handler,
)
from gobby.ai._tool_chat_builtins import (
    BuiltinExecutionContext,
    BuiltinToolResult,
    BuiltinToolSpec,
)
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolLoopLimits, ToolPolicy
from gobby.llm.claude_errors import classify_result_message

pytestmark = pytest.mark.unit


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
        provider="endpoint:lm-studio",
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
    assert result.provider == "endpoint:lm-studio"
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
    assert result.tool_use_count == 1


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
    assert result.calls_used == 1
    assert result.budget_exhausted is True
    assert result.trace_available is True
    assert len(result.trace) == 1


@pytest.mark.asyncio
async def test_openai_loop_default_stops_at_thirty_tool_calls(
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

    result = await adapter.chat(_request(), _binding())

    assert result.stop_reason == "max_tool_calls"
    assert result.tool_use_count == 30
    assert result.turns == 16
    assert len(result.trace) == 30


# --- Family B (llm_provider / Claude Agent SDK) ---


class _FakeAgenticResult:
    def __init__(self) -> None:
        self.text = "## Module\n\nGrounded narrative."
        self.model = "opus"
        self.tool_use_count = 3
        self.turns = 4
        self.tools = {"mcp__repo__gcode_search": 2, "mcp__repo__gcode_outline": 1}
        self.usage = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
        self.applied_reasoning_effort = "high"


class _FakeClaudeProvider:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def generate_agentic(self, **kwargs: Any) -> _FakeAgenticResult:
        self.kwargs = kwargs
        return _FakeAgenticResult()


def _claude_binding() -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider="claude",
        adapter_style=AIAdapterStyle.LLM_PROVIDER,
        available=True,
        models=("opus",),
        metadata={},
    )


@pytest.mark.asyncio
async def test_claude_adapter_constrains_tools_and_maps_result() -> None:
    provider = _FakeClaudeProvider()
    adapter = ClaudeToolChatAdapter(provider_factory=lambda _binding: provider)
    request = ToolChatRequest(
        prompt="Document the auth module.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search", "outline")),
        project_path="/repo",
        system_prompt="You are a code historian.",
        reasoning_effort="high",
        limits=ToolLoopLimits(max_turns=3),
    )

    result = await adapter.chat(request, _claude_binding())

    kw = provider.kwargs
    assert kw is not None
    # Read-only enforcement: mutation/shell tools are hard-denied.
    assert kw["disallowed_tools"] == _DISALLOWED_TOOLS
    # The agent is steered to the caller's MCP tools only.
    assert set(kw["allowed_tools"]) == {
        "mcp__repo__gcode_search",
        "mcp__repo__gcode_outline",
    }
    assert "repo" in kw["mcp_servers"]
    assert kw["system_prompt"] == "You are a code historian."
    assert kw["project_path"] == "/repo"
    assert kw["model"] == "opus"
    assert kw["max_turns"] == 3

    assert result.text == "## Module\n\nGrounded narrative."
    assert result.provider == "claude"
    assert result.model == "opus"
    assert result.tool_use_count == 3
    assert result.turns == 4
    assert result.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "total_tokens": 140,
    }
    assert result.applied_reasoning_effort == "high"
    assert result.stop_reason == "completed"


async def test_claude_adapter_uses_canonical_limit_max_turns() -> None:
    provider = _FakeClaudeProvider()
    adapter = ClaudeToolChatAdapter(provider_factory=lambda _binding: provider)
    request = ToolChatRequest(
        prompt="Document the auth module.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/repo",
        limits=ToolLoopLimits(max_turns=12),
    )

    await adapter.chat(request, _claude_binding())

    assert provider.kwargs is not None
    assert provider.kwargs["max_turns"] == 12


@pytest.mark.asyncio
async def test_claude_adapter_maps_sdk_max_turns_to_budget_exhaustion() -> None:
    class MaxTurnsProvider:
        async def generate_agentic(self, **kwargs: Any) -> _FakeAgenticResult:
            message = SimpleNamespace(
                result=None,
                is_error=True,
                subtype="error_max_turns",
                api_error_status=None,
            )
            raise classify_result_message(
                message,
                "generate_agentic[tasks.validation.tool_loop]",
            )

    adapter = ClaudeToolChatAdapter(provider_factory=lambda _binding: MaxTurnsProvider())
    request = ToolChatRequest(
        prompt="Validate.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/repo",
        limits=ToolLoopLimits(max_turns=16, max_tool_calls=12),
    )

    result = await adapter.chat(request, _claude_binding())

    assert result.stop_reason == "max_turns"
    assert result.budget_exhausted is True
    assert result.trace_available is True
    assert result.turns == 16
    assert result.provider == "claude"


@pytest.mark.asyncio
async def test_claude_budget_exhaustion_comes_from_runtime_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_box: dict[str, tools.ToolRuntime] = {}

    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        return BuiltinToolResult(payload={"ok": True})

    builtin = BuiltinToolSpec(
        name="read_page",
        description="Read a page.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )

    def fake_build_repo_mcp_server(
        runtime: tools.ToolRuntime,
    ) -> tuple[dict[str, object], list[str]]:
        runtime_box["runtime"] = runtime
        return {}, []

    class BudgetProvider:
        async def generate_agentic(self, **kwargs: Any) -> _FakeAgenticResult:
            runtime = runtime_box["runtime"]
            await runtime.execute("read_page", {})
            await runtime.execute("read_page", {})
            return _FakeAgenticResult()

    monkeypatch.setattr(
        "gobby.ai._tool_chat_adapters.build_repo_mcp_server",
        fake_build_repo_mcp_server,
    )
    adapter = ClaudeToolChatAdapter(provider_factory=lambda _binding: BudgetProvider())
    request = ToolChatRequest(
        prompt="Validate.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path="/repo",
        limits=ToolLoopLimits(max_tool_calls=1),
        builtins=(builtin,),
    )

    result = await adapter.chat(request, _claude_binding())

    assert result.stop_reason == "max_tool_calls"
    assert result.calls_used == 1
    assert result.budget_exhausted is True
    assert result.trace_available is True
    assert [entry["error_code"] for entry in result.trace] == [
        None,
        "tool_call_budget_exhausted",
    ]


@pytest.mark.asyncio
async def test_repo_mcp_tool_handler_executes_then_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_argv(argv: list[str], **kwargs: Any) -> str:
        return "OUTLINE OUTPUT"

    monkeypatch.setattr(tools, "run_argv", fake_run_argv)
    runtime = tools.ToolRuntime(ToolPolicy(cli="gcode", tools=("outline",)), project_path="/repo")

    handler = _make_tool_handler(runtime, "gcode_outline")
    ok = await handler({"args": ["src/x.py"]})
    assert ok == {"content": [{"type": "text", "text": "OUTLINE OUTPUT"}]}

    # A tool outside the policy is denied and flagged is_error (never executed).
    denied = _make_tool_handler(runtime, "gcode_index")
    out = await denied({"args": []})
    assert out["is_error"] is True
    assert out["content"][0]["text"].startswith("[error")

    bounded_runtime = tools.ToolRuntime(
        ToolPolicy(cli="gcode", tools=("outline",)),
        project_path="/repo",
        limits=ToolLoopLimits(max_tool_calls=1),
    )
    bounded_handler = _make_tool_handler(bounded_runtime, "gcode_outline")
    await bounded_handler({"args": []})
    exhausted = await bounded_handler({"args": []})
    assert exhausted["is_error"] is True
    assert '"error_code":"tool_call_budget_exhausted"' in exhausted["content"][0]["text"]


@pytest.mark.asyncio
async def test_openai_loop_puts_resolved_model_on_the_wire() -> None:
    client = _FakeClient([_FakeResponse(_FakeMessage(content="done"))])
    seen: list[tuple[str, str | None]] = []

    async def resolver(binding: CapabilityBinding, model: str | None) -> str:
        seen.append((binding.provider, model))
        return "Qwen/Qwen2.5-VL-7B-Instruct"

    adapter = OpenAICompatibleToolChatAdapter(
        client_factory=lambda _binding: client,
        model_resolver=resolver,
    )
    binding = CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider="endpoint:vllm-local",
        adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
        available=True,
        models=("auto",),
        metadata={"endpoint": "vllm-local", "protocol": "vllm"},
    )

    result = await adapter.chat(_request(model="auto"), binding)

    assert seen == [("endpoint:vllm-local", "auto")]
    assert client.chat.completions.calls[0]["model"] == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert result.model == "Qwen/Qwen2.5-VL-7B-Instruct"
