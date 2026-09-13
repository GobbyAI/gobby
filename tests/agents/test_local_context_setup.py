from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import local_model
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_models import SpawnRequest
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.providers.capabilities.local_context import (
    LocalContextInstance,
    LocalContextObservation,
)
from gobby.providers.capabilities.local_context_config import (
    LocalContextRoute,
    endpoint_route,
)
from gobby.providers.capabilities.resolve import ContextResolution, ContextSource
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.backends.codex import (
    CodexManagedChatSession,
    CodexWebChatBackend,
)
from gobby.sessions.context_usage import (
    LOCAL_CONTEXT_OBSERVATION_VARIABLE,
    LOCAL_CONTEXT_ROUTE_VARIABLE,
)
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit

_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


def _observation(route: LocalContextRoute, runtime_limit: int = 32_768) -> LocalContextObservation:
    return LocalContextObservation(
        machine_id=route.machine_id,
        endpoint_id=route.endpoint_id,
        configuration_fingerprint=route.configuration_fingerprint,
        provider=route.provider,
        model_id=route.model_id,
        canonical_limit=65_536,
        provenance={"runtime_limit": "test:runtime"},
        instances=(
            LocalContextInstance(
                model_id=route.model_id,
                runtime_limit=runtime_limit,
                provenance={"runtime_limit": "test:runtime"},
            ),
        ),
    )


async def _drain_spawn_background_tasks() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import _spawn_background_tasks

    tasks = tuple(_spawn_background_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


async def _spawn_request(
    *,
    tmp_path: Path,
    config: DaemonConfig,
    model: str,
    events: list[str],
    provider: str = "codex",
    initial_variables: dict[str, Any] | None = None,
) -> SpawnRequest:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner.child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
    spawn_result = SimpleNamespace(
        success=True,
        child_session_id="child-1",
        status="running",
        backend="process",
        terminal_id=None,
        pid=123,
        message="spawned",
    )

    async def execute(request: SpawnRequest) -> SimpleNamespace:
        events.append("execute")
        return spawn_result

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={
                "id": "11111111-1111-4111-8111-111111110001",
                "project_path": str(repo_path),
            },
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
            return_value=_MACHINE_ID,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
            side_effect=lambda *_args, **_kwargs: prepared_spawn(),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            new=AsyncMock(side_effect=execute),
        ) as execute_spawn,
    ):
        result = await spawn_agent_impl(
            terminal_backend="tmux",
            prompt="test local context setup",
            runner=runner,
            parent_session_id="parent-session",
            provider=provider,
            model=model,
            isolation="none",
            daemon_config=config,
            initial_variables=initial_variables,
        )
        await _drain_spawn_background_tasks()

    assert result["success"] is True, result
    execute_spawn.assert_awaited_once()
    execute_call = execute_spawn.await_args
    assert execute_call is not None
    request = execute_call.args[0]
    assert isinstance(request, SpawnRequest)
    return request


@pytest.mark.asyncio
async def test_coding_setup_context_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "metal": {
                        "protocol": "vllm",
                        "api_base": "http://127.0.0.1:8000/v1",
                        "model": "auto",
                    }
                }
            }
        }
    )
    concrete_model = "Qwen/Qwen2.5-Coder-32B-Instruct"
    events: list[str] = []
    captured: dict[str, LocalContextRoute | LocalContextObservation] = {}

    async def ensure(
        endpoint: GenerationEndpointConfig,
        run_manager: Any = None,
    ) -> str:
        del run_manager
        assert endpoint.model == "auto"
        events.append("ensure")
        return concrete_model

    async def refresh(
        endpoint: GenerationEndpointConfig,
        *,
        endpoint_name: str,
        model: str,
        machine_id: str | None = None,
    ) -> tuple[LocalContextRoute, LocalContextObservation]:
        assert model == concrete_model
        events.append("refresh")
        route = endpoint_route(
            machine_id=machine_id or "",
            endpoint_name=endpoint_name,
            endpoint=endpoint,
            model_id=model,
        )
        observation = _observation(route)
        captured.update(route=route, observation=observation)
        return route, observation

    monkeypatch.setattr(local_model, "ensure_local_model", ensure)
    monkeypatch.setattr(local_model, "refresh_local_model_context", refresh)

    request = await _spawn_request(
        tmp_path=tmp_path,
        config=config,
        model="endpoint:metal",
        events=events,
    )

    assert events == ["ensure", "refresh", "execute"]
    route = captured["route"]
    observation = captured["observation"]
    assert isinstance(route, LocalContextRoute)
    assert isinstance(observation, LocalContextObservation)
    assert route.model_id == concrete_model
    assert request.model == concrete_model
    resume_metadata = request.resume_metadata_json
    initial_variables = request.initial_variables
    assert resume_metadata is not None
    assert initial_variables is not None
    assert initial_variables[LOCAL_CONTEXT_ROUTE_VARIABLE] == route.to_dict()
    assert initial_variables[LOCAL_CONTEXT_OBSERVATION_VARIABLE] == observation.to_dict()
    assert resume_metadata[LOCAL_CONTEXT_ROUTE_VARIABLE] == route.to_dict()
    assert resume_metadata[LOCAL_CONTEXT_OBSERVATION_VARIABLE] == observation.to_dict()
    assert resume_metadata["initial_variables"] == initial_variables


@pytest.mark.asyncio
async def test_coding_setup_remote_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "remote": {
                        "protocol": "openai-compatible",
                        "wire_api": "chat-completions",
                        "api_base": "https://models.example.test/v1",
                        "api_key": "remote-secret",
                        "model": "vendor/remote-model",
                    }
                }
            }
        }
    )
    events: list[str] = []

    async def ensure(
        endpoint: GenerationEndpointConfig,
        run_manager: Any = None,
    ) -> str:
        del run_manager
        events.append("ensure")
        return endpoint.model

    monkeypatch.setattr(local_model, "ensure_local_model", ensure)
    monkeypatch.setattr(
        "gobby.app_context.get_app_context",
        MagicMock(side_effect=AssertionError("remote context must not use the local service")),
    )

    request = await _spawn_request(
        tmp_path=tmp_path,
        config=config,
        model="endpoint:remote/vendor/remote-model",
        events=events,
        provider="claude",
        initial_variables={
            LOCAL_CONTEXT_ROUTE_VARIABLE: {"stale": True},
            LOCAL_CONTEXT_OBSERVATION_VARIABLE: {"stale": True},
        },
    )

    assert events == ["ensure", "execute"]
    assert request.model == "vendor/remote-model"
    assert request.is_local is False
    resume_metadata = request.resume_metadata_json
    initial_variables = request.initial_variables
    assert resume_metadata is not None
    assert initial_variables is not None
    assert LOCAL_CONTEXT_ROUTE_VARIABLE not in initial_variables
    assert LOCAL_CONTEXT_OBSERVATION_VARIABLE not in initial_variables
    assert LOCAL_CONTEXT_ROUTE_VARIABLE not in resume_metadata
    assert LOCAL_CONTEXT_OBSERVATION_VARIABLE not in resume_metadata


@pytest.mark.asyncio
async def test_claude_endpoint_switch_rebinds_transport_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DaemonConfig(
        web_chat_sandbox={"enabled": False},
        ai={
            "generation": {
                "endpoints": {
                    "first": {
                        "protocol": "openai-compatible",
                        "api_base": "http://127.0.0.1:1234/v1",
                        "api_key": "first-token",
                        "model": "first-model",
                    },
                    "second": {
                        "protocol": "openai-compatible",
                        "api_base": "http://127.0.0.1:2234/v1",
                        "api_key": "second-token",
                        "model": "second-model",
                    },
                }
            }
        },
    )
    events: list[str] = []
    captured_options: list[dict[str, Any]] = []
    persisted: list[tuple[LocalContextRoute | None, LocalContextObservation | None]] = []

    async def refresh(selector: str) -> tuple[LocalContextRoute, LocalContextObservation]:
        endpoint_name, model = selector.removeprefix("endpoint:").split("/", 1)
        endpoint = config.ai.generation.endpoints[endpoint_name]
        events.append(f"refresh:{endpoint_name}:{model}")
        route = endpoint_route(
            machine_id=_MACHINE_ID,
            endpoint_name=endpoint_name,
            endpoint=endpoint,
            model_id=model,
        )
        return route, _observation(route)

    async def ensure(endpoint: GenerationEndpointConfig, run_manager: Any = None) -> str:
        del run_manager
        events.append(f"ensure:{endpoint.api_base}")
        return endpoint.model

    def persist(
        _manager: Any,
        _session_id: str,
        route: LocalContextRoute | None,
        observation: LocalContextObservation | None,
    ) -> None:
        persisted.append((route, observation))
        events.append(f"persist:{route.endpoint_id if route is not None else 'remote'}")

    def options_factory(**kwargs: Any) -> MagicMock:
        captured_options.append(kwargs)
        events.append(f"options:{kwargs['env'].get('ANTHROPIC_BASE_URL')}")
        return MagicMock()

    first_client = MagicMock()
    first_client.connect = AsyncMock(side_effect=lambda: events.append("connect:first"))
    first_client.disconnect = AsyncMock(side_effect=lambda: events.append("disconnect:first"))
    second_client = MagicMock()
    second_client.connect = AsyncMock(side_effect=lambda: events.append("connect:second"))
    second_client.disconnect = AsyncMock()

    session = ChatSession(conversation_id="claude-local", project_path="/tmp/project")
    session._config = config
    session._local_context_refresher = refresh
    session._session_manager_ref = SimpleNamespace(db=MagicMock())
    session.db_session_id = "db-session"

    with (
        patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
        patch(
            "gobby.servers.chat_session._build_gobby_mcp_entry",
            return_value={"command": "gobby", "args": ["mcp-server"]},
        ),
        patch(
            "gobby.servers.chat_session.materialize_claude_settings_async",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "gobby.servers.chat_session.preflight_provider_native_settings_file_async",
            new=AsyncMock(return_value={}),
        ),
        patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=options_factory),
        patch(
            "gobby.servers.chat_session.ClaudeSDKClient",
            side_effect=[first_client, second_client],
        ),
        patch("gobby.agents.local_model.ensure_local_model", new=AsyncMock(side_effect=ensure)),
        patch("gobby.sessions.context_usage.persist_local_context_variables", side_effect=persist),
    ):
        await session.start(model="endpoint:first/first-model")
        session.sdk_session_id = "sdk-session"
        await session.switch_model("endpoint:second/second-model")

    assert len(captured_options) == 2
    assert captured_options[0]["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1234/v1"
    assert captured_options[0]["env"]["ANTHROPIC_AUTH_TOKEN"] == "first-token"
    assert captured_options[1]["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:2234/v1"
    assert captured_options[1]["env"]["ANTHROPIC_AUTH_TOKEN"] == "second-token"
    assert captured_options[1]["model"] == "second-model"
    assert captured_options[1]["resume"] == "sdk-session"
    first_client.disconnect.assert_awaited_once()
    assert events.index("persist:endpoint:second") < events.index(
        "options:http://127.0.0.1:2234/v1"
    )
    assert len(persisted) == 2
    assert session._local_context_route is not None
    assert session._local_context_route.endpoint_id == "endpoint:second"
    assert session._local_context_route.model_id == "second-model"
    assert session._local_context_observation is persisted[-1][1]


@pytest.mark.asyncio
async def test_codex_auto_refreshes_concrete_model_before_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://127.0.0.1:8000/v1",
        model="auto",
    )
    concrete_model = "Qwen/Qwen2.5-Coder-32B-Instruct"
    events: list[str] = []
    persisted: list[tuple[LocalContextRoute | None, LocalContextObservation | None]] = []

    async def ensure(
        selected: GenerationEndpointConfig,
        run_manager: Any = None,
    ) -> str:
        del run_manager
        assert selected.model == "auto"
        events.append("ensure")
        return concrete_model

    async def refresh(selector: str) -> tuple[LocalContextRoute, LocalContextObservation]:
        assert selector == f"endpoint:metal/{concrete_model}"
        events.append("refresh")
        route = endpoint_route(
            machine_id=_MACHINE_ID,
            endpoint_name="metal",
            endpoint=endpoint,
            model_id=concrete_model,
        )
        return route, _observation(route)

    def persist(
        _manager: Any,
        _session_id: str,
        route: LocalContextRoute | None,
        observation: LocalContextObservation | None,
    ) -> None:
        persisted.append((route, observation))
        events.append("persist")

    async def start_thread(**kwargs: Any) -> SimpleNamespace:
        assert kwargs["model"] == concrete_model
        events.append("thread")
        return SimpleNamespace(id="thread-1", path="/tmp/codex.jsonl")

    client = MagicMock()
    client.is_connected = True
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.register_approval_handler = MagicMock()
    client.start_thread = AsyncMock(side_effect=start_thread)
    backend = CodexWebChatBackend(client=client, generation_endpoint=endpoint)
    session = CodexManagedChatSession(
        conversation_id="codex-local",
        _backend=backend,
        _model="auto",
        _model_selector="endpoint:metal",
        sandbox_config=SandboxConfig(enabled=False),
    )
    session.project_path = "/tmp/project"
    session._local_context_refresher = refresh
    session._session_manager_ref = SimpleNamespace(db=MagicMock())
    session.db_session_id = "db-session"
    monkeypatch.setattr(
        "gobby.servers.websocket.chat.backends.codex.ensure_local_model",
        ensure,
    )

    with patch(
        "gobby.sessions.context_usage.persist_local_context_variables",
        side_effect=persist,
    ):
        await session.start()

    assert events == ["ensure", "refresh", "persist", "thread"]
    assert session._model == concrete_model
    assert session._local_context_route is not None
    assert session._local_context_route.model_id == concrete_model
    assert session._local_context_observation is persisted[0][1]
    assert session._resolve_context_window() == 32_768


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_selector",
    [
        pytest.param("endpoint:metal", id="bare-endpoint"),
        pytest.param("endpoint:metal/auto", id="explicit-auto"),
    ],
)
async def test_codex_auto_switch_uses_concrete_wire_model_and_context(
    monkeypatch: pytest.MonkeyPatch,
    requested_selector: str,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://127.0.0.1:8000/v1",
        model="auto",
    )
    concrete_model = "Qwen/Qwen2.5-Coder-32B-Instruct"
    events: list[str] = []
    handlers: dict[str, list[Any]] = {}
    persisted: list[tuple[LocalContextRoute | None, LocalContextObservation | None]] = []

    async def ensure(
        selected: GenerationEndpointConfig,
        run_manager: Any = None,
    ) -> str:
        del run_manager
        assert selected.model == "auto"
        events.append("ensure")
        return concrete_model

    async def refresh(selector: str) -> tuple[LocalContextRoute, LocalContextObservation]:
        assert selector == f"endpoint:metal/{concrete_model}"
        events.append("refresh")
        route = endpoint_route(
            machine_id=_MACHINE_ID,
            endpoint_name="metal",
            endpoint=endpoint,
            model_id=concrete_model,
        )
        return route, _observation(route)

    def persist(
        _manager: Any,
        _session_id: str,
        route: LocalContextRoute | None,
        observation: LocalContextObservation | None,
    ) -> None:
        persisted.append((route, observation))
        events.append(f"persist:{route.model_id if route is not None else 'unknown'}")

    def add_handler(method: str, handler: Any) -> None:
        handlers.setdefault(method, []).append(handler)

    async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        events.append("turn")
        for handler in handlers["turn/completed"]:
            handler(
                "turn/completed",
                {"threadId": "thread-1", "turnId": "turn-1", "usage": {}},
            )
        return SimpleNamespace(id="turn-1")

    client = MagicMock()
    client.is_connected = True
    client.add_notification_handler = MagicMock(side_effect=add_handler)
    client.remove_notification_handler = MagicMock()
    client.start_turn = AsyncMock(side_effect=start_turn)
    backend = CodexWebChatBackend(client=client, generation_endpoint=endpoint)
    await backend.start()
    session = CodexManagedChatSession(
        conversation_id="codex-local",
        _backend=backend,
        _model="old-model",
        _model_selector="endpoint:metal/old-model",
    )
    session._connected = True
    session._thread_id = "thread-1"
    session._local_context_refresher = refresh
    session._session_manager_ref = SimpleNamespace(db=MagicMock())
    session.db_session_id = "db-session"
    monkeypatch.setattr(
        "gobby.servers.websocket.chat.backends.codex.ensure_local_model",
        ensure,
    )

    with patch(
        "gobby.sessions.context_usage.persist_local_context_variables",
        side_effect=persist,
    ):
        await session.switch_model(requested_selector)
        _ = [event async for event in session.send_message("hello")]

    assert events == [
        "ensure",
        "persist:unknown",
        "refresh",
        f"persist:{concrete_model}",
        "turn",
    ]
    assert session.model == requested_selector
    assert session._model == concrete_model
    assert session._local_context_route is not None
    assert session._local_context_route.model_id == concrete_model
    assert session._local_context_observation is persisted[-1][1]
    assert client.start_turn.await_args is not None
    assert client.start_turn.await_args.kwargs["model"] == concrete_model


@pytest.mark.asyncio
async def test_codex_failed_switch_restores_previous_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://127.0.0.1:8000/v1",
        model="auto",
    )
    backend = CodexWebChatBackend(client=MagicMock(), generation_endpoint=endpoint)
    session = CodexManagedChatSession(
        conversation_id="codex-local",
        _backend=backend,
        _model="old-model",
        _model_selector="endpoint:metal/old-model",
    )
    refresh = AsyncMock()
    session._local_context_refresher = refresh
    monkeypatch.setattr(
        "gobby.servers.websocket.chat.backends.codex.ensure_local_model",
        AsyncMock(side_effect=local_model.LocalModelError("model rejected")),
    )

    with pytest.raises(RuntimeError, match="pre-flight failed"):
        await session.switch_model("endpoint:metal/auto")

    assert session.model == "endpoint:metal/old-model"
    assert session._model == "old-model"
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_cancelled_refresh_invalidates_previous_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://127.0.0.1:8000/v1",
        model="auto",
    )
    concrete_model = "Qwen/Qwen2.5-Coder-32B-Instruct"
    old_route = endpoint_route(
        machine_id=_MACHINE_ID,
        endpoint_name="metal",
        endpoint=endpoint,
        model_id="old-model",
    )
    old_observation = _observation(old_route)
    persisted: list[tuple[LocalContextRoute | None, LocalContextObservation | None]] = []

    async def cancel_refresh(
        selector: str,
    ) -> tuple[LocalContextRoute | None, LocalContextObservation | None]:
        assert selector == f"endpoint:metal/{concrete_model}"
        raise asyncio.CancelledError

    def persist(
        _manager: Any,
        _session_id: str,
        route: LocalContextRoute | None,
        observation: LocalContextObservation | None,
    ) -> None:
        persisted.append((route, observation))

    backend = CodexWebChatBackend(client=MagicMock(), generation_endpoint=endpoint)
    session = CodexManagedChatSession(
        conversation_id="codex-local",
        _backend=backend,
        _model="old-model",
        _model_selector="endpoint:metal/old-model",
    )
    session._local_context_refresher = cancel_refresh
    session._session_manager_ref = SimpleNamespace(db=MagicMock())
    session.db_session_id = "db-session"
    session._context_window_overrides = {concrete_model: 99_000}
    remote_resolver = MagicMock()
    remote_resolver.resolve_context.return_value = ContextResolution(
        value=88_000,
        source=ContextSource.PROVIDER_MATRIX,
    )
    monkeypatch.setattr(
        "gobby.servers.websocket.chat.backends.codex.ensure_local_model",
        AsyncMock(return_value=concrete_model),
    )

    with (
        patch(
            "gobby.sessions.context_usage.persist_local_context_variables",
            side_effect=persist,
        ),
        patch(
            "gobby.llm.context_windows._get_capability_resolver",
            return_value=remote_resolver,
        ),
    ):
        await session._set_local_context(old_route, old_observation)
        with pytest.raises(asyncio.CancelledError):
            await session.switch_model("endpoint:metal/auto")
        assert session._resolve_context_window() is None
        session._context_window_overrides = {}
        assert session._resolve_context_window() is None

    assert session.model == "endpoint:metal/auto"
    assert session._model == concrete_model
    assert session._local_context_route is not None
    assert session._local_context_route.is_local
    assert session._local_context_route.endpoint_id == "endpoint:metal"
    assert session._local_context_route.model_id == concrete_model
    assert session._local_context_observation is None
    assert persisted[0] == (old_route, old_observation)
    assert persisted[1] == (session._local_context_route, None)
    remote_resolver.resolve_context.assert_not_called()


@pytest.mark.asyncio
async def test_claude_rejected_same_endpoint_switch_keeps_active_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DaemonConfig(
        web_chat_sandbox={"enabled": False},
        ai={
            "generation": {
                "endpoints": {
                    "first": {
                        "protocol": "openai-compatible",
                        "api_base": "http://127.0.0.1:1234/v1",
                        "model": "old-model",
                    }
                }
            }
        },
    )
    endpoint = config.ai.generation.endpoints["first"]
    old_route = endpoint_route(
        machine_id=_MACHINE_ID,
        endpoint_name="first",
        endpoint=endpoint,
        model_id="old-model",
    )
    old_observation = _observation(old_route)
    new_route = endpoint_route(
        machine_id=_MACHINE_ID,
        endpoint_name="first",
        endpoint=endpoint,
        model_id="new-model",
    )
    new_observation = _observation(new_route)
    persisted: list[tuple[LocalContextRoute | None, LocalContextObservation | None]] = []
    active_models = ["old-model"]

    async def set_model(model: str) -> None:
        if model == "new-model":
            raise RuntimeError("SDK rejected model")
        active_models.append(model)

    async def refresh(selector: str) -> tuple[LocalContextRoute, LocalContextObservation]:
        assert selector == "endpoint:first/new-model"
        return new_route, new_observation

    def persist(
        _manager: Any,
        _session_id: str,
        route: LocalContextRoute | None,
        observation: LocalContextObservation | None,
    ) -> None:
        persisted.append((route, observation))

    client = MagicMock()
    client.set_model = AsyncMock(side_effect=set_model)
    session = ChatSession(conversation_id="claude-local")
    session._config = config
    session._client = client
    session._connected = True
    session._model = "endpoint:first/old-model"
    session._last_model = active_models[-1]
    session._local_context_refresher = refresh
    session._session_manager_ref = SimpleNamespace(db=MagicMock())
    session.db_session_id = "db-session"
    monkeypatch.setattr(
        "gobby.agents.local_model.ensure_local_model",
        AsyncMock(return_value="new-model"),
    )

    with patch(
        "gobby.sessions.context_usage.persist_local_context_variables",
        side_effect=persist,
    ):
        await session._set_local_context(old_route, old_observation)
        with pytest.raises(RuntimeError, match="SDK rejected model"):
            await session.switch_model("endpoint:first/new-model")

    assert active_models == ["old-model"]
    assert session.model == "endpoint:first/old-model"
    assert session._last_model == active_models[-1]
    assert session._local_context_route is old_route
    assert session._local_context_observation is old_observation
    assert persisted == [(old_route, old_observation)]
    assert persisted[-1][0] is not None
    assert persisted[-1][0].model_id == active_models[-1]
