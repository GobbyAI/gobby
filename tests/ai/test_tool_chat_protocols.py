"""Protocol fixtures for enforceable Codex and Droid tool-chat adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.client_rpc import handle_incoming_request
from gobby.ai import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.ai._tool_chat_builtins import (
    BuiltinExecutionContext,
    BuiltinToolResult,
    BuiltinToolSpec,
)
from gobby.ai._tool_chat_codex import CodexSpawnToolChatAdapter
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolLoopLimits, ToolPolicy
from gobby.ai._tool_chat_droid import (
    DroidProtocolError,
    DroidRpcClient,
    DroidSpawnToolChatAdapter,
    _request_message,
    _version_tuple,
)
from gobby.ai._tool_chat_mcp_server import ToolLoopController, ToolRuntimeMCPServer
from gobby.ai._tool_chat_tools import ToolRuntime
from gobby.ai.registry import CapabilityUnavailableError
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit

DynamicHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class FakeCodexThread:
    id: str


def _binding(provider: str, style: AIAdapterStyle) -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider=provider,
        adapter_style=style,
        available=True,
        models=("fixture-model",),
        metadata={},
    )


def _builtin(calls: list[dict[str, Any]]) -> BuiltinToolSpec:
    async def lookup(
        arguments: dict[str, Any],
        _context: BuiltinExecutionContext,
    ) -> BuiltinToolResult:
        calls.append(arguments)
        return BuiltinToolResult(payload={"ok": True, "query": arguments.get("query")})

    return BuiltinToolSpec(
        name="lookup",
        description="Look up a fixture value.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=lookup,
    )


def _request(
    tmp_path: Path,
    calls: list[dict[str, Any]],
    *,
    limits: ToolLoopLimits,
) -> ToolChatRequest:
    return ToolChatRequest(
        prompt="Investigate the fixture.",
        system_prompt="Use only the provided dynamic tool.",
        tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
        project_path=str(tmp_path),
        limits=limits,
        builtins=(_builtin(calls),),
    )


class FakeCodexClient:
    def __init__(self, actions: list[tuple[str, object]]) -> None:
        self.actions = actions
        self.handler: DynamicHandler | None = None
        self.handler_removed = False
        self.notification_handlers: dict[str, list[Callable[[str, dict[str, Any]], None]]] = {}
        self.started = False
        self.stopped = False
        self.interrupts: list[tuple[str, str]] = []
        self.tool_results: list[dict[str, Any]] = []
        self.thread_options: dict[str, object] = {}

    def register_request_handler(self, method: str, handler: DynamicHandler) -> None:
        assert method == "item/tool/call"
        self.handler = handler

    def remove_request_handler(self, method: str) -> None:
        assert method == "item/tool/call"
        self.handler = None
        self.handler_removed = True

    def add_notification_handler(
        self,
        method: str,
        handler: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.notification_handlers.setdefault(method, []).append(handler)

    def remove_notification_handler(
        self,
        method: str,
        handler: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.notification_handlers[method].remove(handler)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        ephemeral: bool = False,
        dynamic_tools: list[dict[str, Any]] | None = None,
        experimental_raw_events: bool = False,
    ) -> FakeCodexThread:
        self.thread_options = {
            "cwd": cwd,
            "model": model,
            "approval_policy": approval_policy,
            "sandbox": sandbox,
            "terminal_context": terminal_context,
            "ephemeral": ephemeral,
            "dynamic_tools": dynamic_tools,
            "experimental_raw_events": experimental_raw_events,
        }
        return FakeCodexThread(id="thread-fixture")

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))

    async def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        del thread_id, prompt, images, config_overrides
        for action, payload in self.actions:
            if action == "event":
                assert isinstance(payload, dict)
                event_type = payload.get("type")
                if isinstance(event_type, str):
                    for handler in self.notification_handlers.get(event_type, []):
                        handler(event_type, payload)
                yield payload
            elif action == "tool":
                assert self.handler is not None
                assert isinstance(payload, dict)
                self.tool_results.append(await self.handler(payload))


class CleanupFailingCodexClient(FakeCodexClient):
    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        ephemeral: bool = False,
        dynamic_tools: list[dict[str, Any]] | None = None,
        experimental_raw_events: bool = False,
    ) -> FakeCodexThread:
        raise RuntimeError("start-thread-primary")

    def remove_notification_handler(
        self,
        _method: str,
        _handler: Callable[[str, dict[str, Any]], None],
    ) -> None:
        raise RuntimeError("notification-cleanup-secondary")

    def remove_request_handler(self, _method: str) -> None:
        raise RuntimeError("request-cleanup-secondary")

    async def stop(self) -> None:
        raise RuntimeError("stop-cleanup-secondary")


class FakeCodexFactory:
    def __init__(self, client: FakeCodexClient) -> None:
        self.client = client
        self.options: dict[str, object] = {}

    def __call__(self, **kwargs: object) -> FakeCodexClient:
        self.options = kwargs
        return self.client


def _raw_turn() -> tuple[str, object]:
    return ("event", {"type": "rawResponse/completed"})


def _codex_tool(turn_id: str) -> tuple[str, object]:
    return (
        "tool",
        {
            "threadId": "thread-fixture",
            "turnId": turn_id,
            "callId": f"call-{turn_id}",
            "tool": "lookup",
            "arguments": {"query": turn_id},
        },
    )


def _codex_final(text: str) -> tuple[str, object]:
    return (
        "event",
        {
            "type": "item/completed",
            "item": {"content": [{"type": "output_text", "text": text}]},
        },
    )


@pytest.mark.asyncio
async def test_codex_stops_at_turn_cap_and_cleans_up(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeCodexClient(
        [_raw_turn(), _codex_tool("turn-1"), _raw_turn(), _codex_final("done")]
    )
    factory = FakeCodexFactory(client)
    adapter = CodexSpawnToolChatAdapter(
        command_path="codex",
        client_factory=factory,
    )

    result = await adapter.chat(
        _request(tmp_path, calls, limits=ToolLoopLimits(max_turns=2)),
        _binding("codex", AIAdapterStyle.DAEMON),
    )

    assert result.text == ""
    assert result.stop_reason == "max_turns"
    assert result.turns == 2
    assert result.calls_used == 1
    assert result.tools == {"lookup": 1}
    assert calls == [{"query": "turn-1"}]
    assert client.thread_options["experimental_raw_events"] is True
    assert client.thread_options["dynamic_tools"]
    assert client.handler_removed is True
    assert client.stopped is True
    assert factory.options["experimental_api"] is True
    overrides = factory.options["config_overrides"]
    assert isinstance(overrides, tuple)
    assert "mcp_servers={}" in overrides
    assert "features.shell_tool=false" in overrides
    assert "features.plugins=false" in overrides
    assert "features.browser_use=false" in overrides
    assert "features.multi_agent=false" in overrides
    assert "tools.web_search=false" in overrides


@pytest.mark.asyncio
async def test_codex_tool_free_turn_cap_interrupts_active_turn(tmp_path: Path) -> None:
    client = FakeCodexClient(
        [
            (
                "event",
                {
                    "type": "rawResponse/completed",
                    "turnId": "turn-tool-free",
                },
            )
        ]
    )
    adapter = CodexSpawnToolChatAdapter(
        command_path="codex",
        client_factory=FakeCodexFactory(client),
    )

    result = await adapter.chat(
        _request(tmp_path, [], limits=ToolLoopLimits(max_turns=1)),
        _binding("codex", AIAdapterStyle.DAEMON),
    )
    checkpoint = asyncio.Event()
    asyncio.get_running_loop().call_soon(checkpoint.set)
    await checkpoint.wait()

    assert result.stop_reason == "max_turns"
    assert result.turns == 1
    assert client.interrupts == [("thread-fixture", "turn-tool-free")]


@pytest.mark.asyncio
async def test_codex_startup_error_is_not_replaced_by_cleanup_failures(
    tmp_path: Path,
) -> None:
    client = CleanupFailingCodexClient([])
    adapter = CodexSpawnToolChatAdapter(
        command_path="codex",
        client_factory=FakeCodexFactory(client),
    )

    with pytest.raises(RuntimeError, match="start-thread-primary"):
        await adapter.chat(
            _request(tmp_path, [], limits=ToolLoopLimits()),
            _binding("codex", AIAdapterStyle.DAEMON),
        )


def test_codex_responses_binding_uses_descriptive_endpoint_resolution() -> None:
    binding = CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider="endpoint:missing",
        adapter_style=AIAdapterStyle.DAEMON,
        available=True,
        models=("fixture-model",),
        metadata={"wire_api": "responses", "endpoint": "missing"},
    )
    adapter = CodexSpawnToolChatAdapter(
        command_path="codex",
        config=DaemonConfig(),
    )

    with pytest.raises(ValueError, match="missing"):
        adapter._client_options(binding, model="fixture-model")


def test_spawn_module_exports_every_public_adapter() -> None:
    from gobby.ai import _tool_chat_spawn

    assert set(_tool_chat_spawn.__all__) == {
        "CodexSpawnToolChatAdapter",
        "DroidSpawnToolChatAdapter",
        "GrokSpawnToolChatAdapter",
        "QwenSpawnToolChatAdapter",
    }


@pytest.mark.asyncio
async def test_codex_never_executes_tool_call_n_plus_one(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeCodexClient(
        [
            _raw_turn(),
            _codex_tool("turn-1"),
            _raw_turn(),
            _codex_tool("turn-2"),
        ]
    )
    adapter = CodexSpawnToolChatAdapter(
        command_path="codex",
        client_factory=FakeCodexFactory(client),
    )

    result = await adapter.chat(
        _request(tmp_path, calls, limits=ToolLoopLimits(max_tool_calls=1)),
        _binding("codex", AIAdapterStyle.DAEMON),
    )

    assert result.stop_reason == "max_tool_calls"
    assert result.calls_used == 1
    assert calls == [{"query": "turn-1"}]
    assert client.tool_results[-1]["success"] is False
    assert client.interrupts == [("thread-fixture", "turn-2")]


@pytest.mark.asyncio
async def test_codex_never_executes_tool_on_last_finite_turn(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeCodexClient([_raw_turn(), _codex_tool("turn-1")])
    adapter = CodexSpawnToolChatAdapter(
        command_path="codex",
        client_factory=FakeCodexFactory(client),
    )

    result = await adapter.chat(
        _request(tmp_path, calls, limits=ToolLoopLimits(max_turns=1)),
        _binding("codex", AIAdapterStyle.DAEMON),
    )

    assert result.stop_reason == "max_turns"
    assert result.turns == 1
    assert result.calls_used == 0
    assert calls == []


@pytest.mark.asyncio
async def test_codex_requires_raw_turn_events(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeCodexClient([_codex_final("unaccounted")])
    adapter = CodexSpawnToolChatAdapter(
        command_path="codex",
        client_factory=FakeCodexFactory(client),
    )

    with pytest.raises(CapabilityUnavailableError, match="rawResponse/completed"):
        await adapter.chat(
            _request(tmp_path, calls, limits=ToolLoopLimits()),
            _binding("codex", AIAdapterStyle.DAEMON),
        )

    assert client.handler_removed is True
    assert client.stopped is True


@pytest.mark.asyncio
async def test_codex_client_serializes_dynamic_tool_thread_options() -> None:
    client = CodexAppServerClient(experimental_api=True)
    response = {
        "thread": {
            "id": "thread-fixture",
            "preview": "",
            "modelProvider": "openai",
            "createdAt": 1,
            "ephemeral": True,
        }
    }
    dynamic_tools = [
        {
            "name": "lookup",
            "description": "Look up a fixture.",
            "inputSchema": {"type": "object"},
        }
    ]

    with patch.object(
        client,
        "_send_request",
        new_callable=AsyncMock,
        return_value=response,
    ) as send_request:
        await client.start_thread(
            cwd="/repo",
            dynamic_tools=dynamic_tools,
            experimental_raw_events=True,
        )

    send_request.assert_awaited_once_with(
        "thread/start",
        {
            "cwd": "/repo",
            "dynamicTools": dynamic_tools,
            "experimentalRawEvents": True,
        },
    )
    assert client._experimental_api is True


@pytest.mark.asyncio
async def test_codex_client_routes_dynamic_tool_request_response() -> None:
    client = CodexAppServerClient()
    handler = AsyncMock(return_value={"success": True, "contentItems": []})
    client.register_request_handler("item/tool/call", handler)
    responses: list[dict[str, object]] = []

    with patch.object(
        client,
        "_send_stdin_response",
        new=AsyncMock(side_effect=responses.append),
    ):
        await handle_incoming_request(
            client,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "item/tool/call",
                "params": {"tool": "lookup", "arguments": {"query": "auth"}},
            },
        )

    handler.assert_awaited_once_with({"tool": "lookup", "arguments": {"query": "auth"}})
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"success": True, "contentItems": []},
        }
    ]


class FakeDroidClient:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.requests: list[tuple[str, Mapping[str, object]]] = []
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.mcp: dict[str, Any] | None = None
        self.native_disabled = False
        self.interrupts = 0
        self.stopped = False
        self.tool_results: list[dict[str, Any]] = []
        self.mcp_ready = asyncio.Event()
        self._scenario_task: asyncio.Task[None] | None = None
        self._observer: Callable[[dict[str, Any]], None] | None = None

    def set_notification_observer(
        self,
        observer: Callable[[dict[str, Any]], None],
    ) -> None:
        self._observer = observer

    async def start(self) -> None:
        return None

    async def request(self, method: str, params: Mapping[str, object]) -> object:
        self.requests.append((method, params))
        if method == "droid.initialize_session":
            servers = params.get("mcpServers")
            assert isinstance(servers, list) and len(servers) == 1
            server = servers[0]
            assert isinstance(server, dict)
            self.mcp = server
            self.mcp_ready.set()
            return {"sessionId": "fixture"}
        if method == "droid.list_tools":
            return {
                "tools": [
                    {
                        "id": "execute-cli",
                        "currentlyAllowed": not self.native_disabled,
                    },
                    {
                        "id": "read-cli",
                        "currentlyAllowed": not self.native_disabled,
                    },
                ]
            }
        if method == "droid.update_session_settings":
            assert params["enabledToolIds"] == []
            assert params["disabledToolIds"] == ["execute-cli", "read-cli"]
            self.native_disabled = True
            return {"settings": dict(params)}
        if method == "droid.add_user_message":
            self._scenario_task = asyncio.create_task(self._run_scenario())
            return {"messageId": "user-fixture"}
        if method == "droid.interrupt_session":
            self.interrupts += 1
            return {}
        return {}

    async def next_notification(self) -> dict[str, Any]:
        return await self.notifications.get()

    async def stop(self) -> None:
        if self._scenario_task is not None:
            await self._scenario_task
        self.stopped = True

    async def _run_scenario(self) -> None:
        await self._notify({"type": "droid_working_state_changed", "newState": "executing_tool"})
        for turn, action in enumerate(self.actions, start=1):
            if action == "tool":
                await self._notify(
                    {
                        "type": "create_message",
                        "message": {
                            "id": f"assistant-{turn}",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"tool-{turn}",
                                    "name": "lookup",
                                    "input": {"query": f"turn-{turn}"},
                                }
                            ],
                        },
                    }
                )
                await self.mcp_ready.wait()
                self.tool_results.append(await self._call_mcp(turn))
            else:
                await self._notify(
                    {
                        "type": "create_message",
                        "message": {
                            "id": f"assistant-{turn}",
                            "role": "assistant",
                            "content": [{"type": "text", "text": action}],
                        },
                    }
                )
        await self._notify({"type": "droid_working_state_changed", "newState": "idle"})

    async def _call_mcp(self, turn: int) -> dict[str, Any]:
        assert self.mcp is not None
        headers = self.mcp["headers"]
        assert isinstance(headers, list)
        authorization = headers[0]
        assert isinstance(authorization, dict)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.mcp["url"],
                headers={authorization["name"]: authorization["value"]},
                json={
                    "jsonrpc": "2.0",
                    "id": turn,
                    "method": "tools/call",
                    "params": {
                        "name": "lookup",
                        "arguments": {"query": f"turn-{turn}"},
                    },
                },
            )
            payload = response.json()
        assert isinstance(payload, dict)
        result = payload["result"]
        assert isinstance(result, dict)
        return result

    async def _notify(self, notification: dict[str, Any]) -> None:
        event = {
            "method": "droid.session_notification",
            "params": {"notification": notification},
        }
        if self._observer is not None:
            self._observer(event)
        await self.notifications.put(event)


class HangingDroidClient(FakeDroidClient):
    async def request(self, method: str, params: Mapping[str, object]) -> object:
        if method == "droid.initialize_session":
            await asyncio.Event().wait()
        return await super().request(method, params)


class StopFailingDroidClient(FakeDroidClient):
    async def stop(self) -> None:
        await super().stop()
        raise RuntimeError("client stop failed")


class FakeDroidFactory:
    def __init__(self, client: FakeDroidClient) -> None:
        self.client = client
        self.options: dict[str, object] = {}

    def __call__(self, **kwargs: object) -> FakeDroidClient:
        self.options = kwargs
        return self.client


@pytest.mark.asyncio
async def test_droid_uses_bearer_mcp_and_disables_all_native_tools(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeDroidClient(["tool", "done"])
    factory = FakeDroidFactory(client)
    adapter = DroidSpawnToolChatAdapter(
        command_path="droid",
        client_factory=factory,
    )

    result = await adapter.chat(
        _request(tmp_path, calls, limits=ToolLoopLimits(max_turns=2)),
        _binding("droid", AIAdapterStyle.CLI),
    )

    assert result.text == "done"
    assert result.turns == 2
    assert result.calls_used == 1
    assert calls == [{"query": "turn-1"}]
    assert client.native_disabled is True
    assert client.stopped is True
    assert Path(str(factory.options["cwd"])) != tmp_path
    assert client.mcp is not None
    headers = client.mcp["headers"]
    assert isinstance(headers, list)
    assert str(headers[0]["value"]).startswith("Bearer ")


@pytest.mark.asyncio
async def test_droid_unknown_server_request_receives_method_not_found(
    tmp_path: Path,
) -> None:
    client = DroidRpcClient(command_path="droid", cwd=tmp_path, env={})

    with patch.object(client, "_write", new_callable=AsyncMock) as write:
        await client._dispatch(
            {
                "type": "request",
                "id": "request-1",
                "method": "future.unhandled",
            }
        )

    await_args = write.await_args
    assert await_args is not None
    payload = await_args.args[0]
    assert payload["id"] == "request-1"
    assert payload["error"] == {
        "code": -32601,
        "message": "Method not found: future.unhandled",
    }


@pytest.mark.asyncio
async def test_droid_loop_deadline_covers_stalled_requests(tmp_path: Path) -> None:
    client = HangingDroidClient([])
    adapter = DroidSpawnToolChatAdapter(
        command_path="droid",
        client_factory=FakeDroidFactory(client),
    )

    result = await asyncio.wait_for(
        adapter.chat(
            _request(
                tmp_path,
                [],
                limits=ToolLoopLimits(loop_timeout_seconds=1),
            ),
            _binding("droid", AIAdapterStyle.CLI),
        ),
        timeout=2,
    )

    assert result.stop_reason == "timeout"
    assert client.stopped is True


@pytest.mark.asyncio
async def test_droid_server_stops_even_when_client_stop_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StopFailingDroidClient(["done"])
    adapter = DroidSpawnToolChatAdapter(
        command_path="droid",
        client_factory=FakeDroidFactory(client),
    )
    server_stopped = asyncio.Event()
    original_stop = ToolRuntimeMCPServer.stop

    async def recording_stop(server: ToolRuntimeMCPServer) -> None:
        server_stopped.set()
        await original_stop(server)

    monkeypatch.setattr(ToolRuntimeMCPServer, "stop", recording_stop)

    with pytest.raises(RuntimeError, match="client stop failed"):
        await adapter.chat(
            _request(tmp_path, [], limits=ToolLoopLimits()),
            _binding("droid", AIAdapterStyle.CLI),
        )

    assert server_stopped.is_set()


@pytest.mark.asyncio
async def test_droid_never_executes_tool_call_n_plus_one(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeDroidClient(["tool", "tool"])
    adapter = DroidSpawnToolChatAdapter(
        command_path="droid",
        client_factory=FakeDroidFactory(client),
    )

    result = await adapter.chat(
        _request(tmp_path, calls, limits=ToolLoopLimits(max_tool_calls=1)),
        _binding("droid", AIAdapterStyle.CLI),
    )

    assert result.stop_reason == "max_tool_calls"
    assert result.calls_used == 1
    assert calls == [{"query": "turn-1"}]
    assert client.tool_results[-1]["isError"] is True
    assert client.interrupts == 1


@pytest.mark.asyncio
async def test_droid_never_executes_tool_on_last_finite_turn(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeDroidClient(["tool"])
    adapter = DroidSpawnToolChatAdapter(
        command_path="droid",
        client_factory=FakeDroidFactory(client),
    )

    result = await adapter.chat(
        _request(tmp_path, calls, limits=ToolLoopLimits(max_turns=1)),
        _binding("droid", AIAdapterStyle.CLI),
    )

    assert result.stop_reason == "max_turns"
    assert result.turns == 1
    assert result.calls_used == 0
    assert calls == []
    assert client.interrupts == 1


class IncompatibleDroidClient(FakeDroidClient):
    async def request(self, method: str, params: Mapping[str, object]) -> object:
        if method == "droid.initialize_session":
            raise DroidProtocolError(
                "Droid protocol 1.130.0 is unsupported; require 1.131.0 or newer"
            )
        return await super().request(method, params)


@pytest.mark.asyncio
async def test_droid_unsupported_protocol_is_capability_unavailable(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    client = IncompatibleDroidClient([])
    adapter = DroidSpawnToolChatAdapter(
        command_path="droid",
        client_factory=FakeDroidFactory(client),
    )

    with pytest.raises(CapabilityUnavailableError, match="1.130.0"):
        await adapter.chat(
            _request(tmp_path, calls, limits=ToolLoopLimits()),
            _binding("droid", AIAdapterStyle.CLI),
        )

    assert client.stopped is True


def test_droid_json_rpc_envelope_and_protocol_version_contract() -> None:
    assert _request_message("request-1", "droid.list_tools", {}) == {
        "jsonrpc": "2.0",
        "factoryApiVersion": "1.0.0",
        "factoryProtocolVersion": "1.131.0",
        "type": "request",
        "id": "request-1",
        "method": "droid.list_tools",
        "params": {},
    }
    assert _version_tuple("1.131.0") == (1, 131, 0)
    with pytest.raises(DroidProtocolError, match="unsupported"):
        _version_tuple("1.130.9")


@pytest.mark.asyncio
async def test_droid_mcp_server_rejects_missing_bearer_token(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    limits = ToolLoopLimits()
    runtime = ToolRuntime(
        ToolPolicy(cli="gcode", tools=("search",)),
        project_path=str(tmp_path),
        limits=limits,
        builtins=(_builtin(calls),),
    )
    server = ToolRuntimeMCPServer(runtime, ToolLoopController(limits))
    await server.start()
    try:
        assert server.url is not None
        async with httpx.AsyncClient() as client:
            response = await client.post(
                server.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 401
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_droid_mcp_server_closes_socket_when_server_shutdown_raises(
    tmp_path: Path,
) -> None:
    runtime = ToolRuntime(
        ToolPolicy(cli="gcode", tools=("search",)),
        project_path=str(tmp_path),
        limits=ToolLoopLimits(),
    )
    server = ToolRuntimeMCPServer(runtime, ToolLoopController(ToolLoopLimits()))
    await server.start()
    socket = server._socket
    runner = server._runner
    assert socket is not None
    assert runner is not None

    with (
        patch.object(
            type(runner),
            "shutdown",
            new_callable=AsyncMock,
            side_effect=RuntimeError("shutdown failed"),
        ),
        pytest.raises(RuntimeError, match="shutdown failed"),
    ):
        await server.stop()

    assert socket.fileno() == -1
    assert server._socket is None
