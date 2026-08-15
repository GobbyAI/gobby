"""Acceptance coverage for live generic configuration consumers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from pytest import FixtureRequest

from gobby.config.app import DaemonConfig
from gobby.config.registry import CONFIG_REGISTRY
from gobby.config.runtime_models import ConfigSnapshot
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.voice import create_voice_registry
from gobby.servers.models import WebChatSessionRequest
from gobby.servers.routes.agent_spawn import create_agent_spawn_router
from gobby.servers.routes.attention import _run_tmux_payload
from gobby.servers.routes.configuration_tool_approvals import register_tool_approval_routes
from gobby.servers.routes.configuration_ui_settings import register_ui_setting_routes
from gobby.servers.routes.configuration_validation_detection import (
    ValidationDetectionPreviewRequest,
    register_validation_detection_routes,
)
from gobby.servers.routes.rules import create_rules_router
from gobby.servers.routes.sessions.core import register_core_routes
from gobby.servers.tool_approvals import get_global_approval_rules
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine


def _snapshot(
    *,
    config: DaemonConfig | None = None,
    values: Mapping[str, object] | None = None,
    revision: int = 1,
) -> ConfigSnapshot:
    active = config or DaemonConfig()
    return ConfigSnapshot(
        revision=revision,
        desired=active,
        active=active,
        row_revisions={},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_values=values,
        active_values=values,
    )


class CountingRuntime:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self.current = snapshot
        self.snapshot_reads = 0

    @property
    def snapshot(self) -> ConfigSnapshot:
        self.snapshot_reads += 1
        return self.current

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        assert revision == self.current.revision
        return self.current


@pytest.fixture
def policy_db(monkeypatch: pytest.MonkeyPatch, request: FixtureRequest) -> HubDatabase:
    """Load the isolated database with this worktree's matching native migrator."""
    binary = Path.cwd() / "target" / "debug" / "gdaemon"
    monkeypatch.setattr(
        "gobby.storage.schema_contract.resolve_native_bin",
        lambda name: str(binary) if name == "gdaemon" else None,
    )
    return cast(HubDatabase, request.getfixturevalue("temp_db"))


def _endpoint(router: APIRouter, path: str, method: str) -> Callable[..., Awaitable[Any]]:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method.upper() in route.methods:
            return cast(Callable[..., Awaitable[Any]], route.endpoint)
    raise AssertionError(f"Missing {method} {path}")


def _route_methods(router: APIRouter) -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


@pytest.mark.asyncio
async def test_rules_use_runtime_snapshot(policy_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(policy_db)
    manager.create(
        name="live-runtime-policy",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="live policy blocked")],
        ).model_dump_json(),
    )
    runtime = CountingRuntime(
        _snapshot(
            values={
                "rules.enforcement_enabled": False,
                "rules.aggregate_blocks": True,
            }
        )
    )
    engine = RuleEngine(policy_db, config_runtime=runtime)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=str(uuid4()),
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Edit"},
    )
    variables = {"project": {"id": "project-id", "path": "/tmp/project"}}

    allowed = await engine.evaluate(event, event.session_id, variables)

    assert allowed.decision == "allow"
    assert runtime.snapshot_reads == 1

    runtime.current = _snapshot(
        values={
            "rules.enforcement_enabled": True,
            "rules.aggregate_blocks": True,
        },
        revision=2,
    )
    runtime.snapshot_reads = 0

    blocked = await engine.evaluate(event, event.session_id, variables)

    assert blocked.decision == "block"
    assert blocked.reason is not None
    assert "live policy blocked" in blocked.reason
    assert runtime.snapshot_reads == 1


def test_approval_and_launch_defaults_are_registered() -> None:
    assert CONFIG_REGISTRY.resolve("tool_approvals.global_rules").key == (
        "tool_approvals.global_rules"
    )
    assert CONFIG_REGISTRY.resolve("launch_defaults.project-id").key == (
        "launch_defaults.{project_id}"
    )
    snapshot = _snapshot(
        values={"tool_approvals.global_rules": [" Bash(*) ", "mcp__gobby__call_tool"]}
    )

    assert get_global_approval_rules(snapshot) == ["Bash(*)", "mcp__gobby__call_tool"]


def test_only_specialized_setting_writers_are_removed() -> None:
    context = cast(Any, SimpleNamespace())
    configuration_router = APIRouter(prefix="/api/config")
    register_tool_approval_routes(configuration_router, context)
    register_ui_setting_routes(configuration_router, context)
    configuration_methods = _route_methods(configuration_router)

    assert ("/api/config/tool-approvals/global", "GET") in configuration_methods
    assert ("/api/config/tool-approvals/global", "PUT") not in configuration_methods
    assert ("/api/config/ui-settings", "GET") in configuration_methods
    assert ("/api/config/ui-settings", "PUT") not in configuration_methods
    assert ("/api/config/ui-settings/{key}", "DELETE") not in configuration_methods

    server = cast(Any, SimpleNamespace(services=SimpleNamespace()))
    rule_methods = _route_methods(create_rules_router(server))
    launch_methods = _route_methods(create_agent_spawn_router(server))

    assert ("/api/rules", "PUT") not in rule_methods
    assert ("/api/rules/{name}", "PUT") in rule_methods
    assert ("/api/rules/{name}", "DELETE") in rule_methods
    assert ("/api/agents/launch-defaults", "GET") in launch_methods
    assert ("/api/agents/launch-defaults", "PUT") not in launch_methods


class RecordingConfigService:
    def __init__(self, runtime: CountingRuntime) -> None:
        self.runtime = runtime
        self.patches: list[tuple[int, dict[str, object]]] = []

    async def patch_flat(
        self,
        *,
        expected_revision: int,
        values: Mapping[str, object],
        unset: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        assert not unset
        self.patches.append((expected_revision, dict(values)))
        return {}


@pytest.mark.asyncio
async def test_voice_and_route_consumers_use_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    active_config = DaemonConfig(
        tmux={"socket_path": "/tmp/live-policy.sock"},
        voice={"whisper_vocabulary": ["Gobby"], "whisper_prompt": "Live prompt"},
    )
    runtime = CountingRuntime(_snapshot(config=active_config, revision=7))
    service = RecordingConfigService(runtime)
    registry = create_voice_registry(lambda: service)
    add_vocab = registry.get_tool("add_vocab")
    assert add_vocab is not None

    result = await add_vocab(terms="PostgreSQL")

    assert result["added"] == ["PostgreSQL"]
    assert service.patches == [(7, {"voice.whisper_vocabulary": ["Gobby", "PostgreSQL"]})]
    assert runtime.snapshot_reads == 1

    runtime.snapshot_reads = 0
    stale_config = DaemonConfig(tmux={"socket_path": "/tmp/stale.sock"})
    attention_server = cast(
        Any,
        SimpleNamespace(services=SimpleNamespace(config=stale_config, config_runtime=runtime)),
    )
    payload = _run_tmux_payload(
        attention_server,
        SimpleNamespace(tmux_session_name="agent-session", pid=42),
    )

    assert payload == {
        "socket_path": "/tmp/live-policy.sock",
        "session_name": "agent-session",
        "pane_pid": 42,
    }
    assert runtime.snapshot_reads == 1

    captured: dict[str, Any] = {}

    class SessionManager:
        def create_web_chat_session(self, **kwargs: Any) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(to_dict=lambda: {"id": "web-chat-id"})

    async def run_db(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return operation(*args, **kwargs)

    session_manager = SessionManager()
    server = cast(
        Any,
        SimpleNamespace(
            session_manager=session_manager,
            resolve_project_id=lambda project_id, cwd: project_id or cwd,
            run_db=run_db,
            services=SimpleNamespace(
                config=stale_config,
                config_runtime=runtime,
                web_chat_runtime_manager=None,
            ),
        ),
    )
    monkeypatch.setattr(
        "gobby.servers.routes.sessions.core.web_chat_sandbox_policy_hash",
        lambda config: config.tmux.socket_path,
    )
    sessions_router = APIRouter(prefix="/api/sessions")

    async def broadcast_session(*args: Any, **kwargs: Any) -> None:
        return None

    register_core_routes(sessions_router, server, lambda: session_manager, broadcast_session)
    create_session = _endpoint(sessions_router, "/api/sessions/web-chat", "POST")
    runtime.snapshot_reads = 0

    response = await create_session(WebChatSessionRequest(project_id="project-id"))

    assert response["status"] == "created"
    assert captured["sandbox_policy_hash"] == "/tmp/live-policy.sock"
    assert runtime.snapshot_reads == 1


@pytest.mark.asyncio
async def test_validation_detection_uses_runtime_snapshot() -> None:
    active_config = DaemonConfig(
        validation_detection={
            "builtin_matchers_enabled": False,
            "custom_matchers": [
                {
                    "id": "project-ci",
                    "label": "Project CI",
                    "prefixes": ["./scripts/ci"],
                    "categories": ["test"],
                }
            ],
        }
    )
    runtime = CountingRuntime(_snapshot(config=active_config))
    context = cast(
        Any,
        SimpleNamespace(
            server=SimpleNamespace(services=SimpleNamespace(config=DaemonConfig())),
            get_config_snapshot=lambda: runtime.snapshot,
        ),
    )
    router = APIRouter(prefix="/api/config")
    register_validation_detection_routes(router, context)
    preview = _endpoint(router, "/api/config/validation-detection/preview", "POST")

    response = await preview(ValidationDetectionPreviewRequest(command="./scripts/ci --fast"))

    assert response["matched"] is True
    assert response["matcher_id"] == "project-ci"
    assert runtime.snapshot_reads == 1
