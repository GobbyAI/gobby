from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.services import TerminalServices
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.terminal_fixtures import make_live_terminal
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.terminals.fakes import FakeRuntime

pytestmark = pytest.mark.unit
DETECTION_REGISTRY = cast(DetectionManifestRegistry, BundledDetectionRegistry())
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
RESET_TIME = "Sep 6th, 2026 9:28 PM"
QUOTA_PANE = (
    "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    f"to purchase more credits or try again at {RESET_TIME}.\n"
)


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def parent_session(temp_db: HubDatabase, sample_project: dict[str, Any]) -> dict[str, Any]:
    return (
        SessionManager(temp_db)
        .register(
            external_id="watchdog-quota-parent",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        .to_dict()
    )


def _run_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby:test:{label}"))


def _make_codex_run(
    agent_run_manager: LocalAgentRunManager,
    parent_session: dict[str, Any],
    *,
    label: str,
) -> AgentRun:
    run = agent_run_manager.create(
        parent_session_id=parent_session["id"],
        provider="codex",
        prompt="test",
        run_id=_run_id(label),
    )
    agent_run_manager.start(run.id)
    stored = agent_run_manager.get(run.id)
    assert stored is not None
    make_live_terminal(stored, db=agent_run_manager.db, session_name=f"gobby-{label}")
    stored = agent_run_manager.get(run.id)
    assert stored is not None
    return stored


def _monitor(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    completion_registry: CompletionEventRegistry | None = None,
) -> AgentLifecycleMonitor:
    runtime = FakeRuntime(snapshot_text=QUOTA_PANE)
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    return AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        completion_registry=completion_registry,
        check_interval_seconds=1.0,
        tmux_config=TmuxConfig(
            idle_check_enabled=True,
            idle_timeout_seconds=10,
            max_reprompt_attempts=2,
        ),
        terminal_services=TerminalServices(
            manager=TerminalManager(temp_db),
            registry=registry,
        ),
    )


@pytest.mark.asyncio
async def test_codex_usage_limit_marks_run_quota_exhausted(
    agent_run_manager: LocalAgentRunManager,
    parent_session: dict[str, Any],
    temp_db: HubDatabase,
) -> None:
    monitor = _monitor(agent_run_manager, temp_db)
    run = _make_codex_run(agent_run_manager, parent_session, label="quota-exhausted")

    handled = await monitor.check_idle_agents()

    failed = agent_run_manager.get(run.id)
    assert handled == 1
    assert failed is not None
    assert failed.status == "error"
    assert failed.terminal_reason == "provider_quota_exhausted"
    assert failed.error == f"Provider quota exhausted: provider=codex; reset_time={RESET_TIME}"
    assert "autonomous stuck" not in failed.error
    assert "MCP proxy tools unavailable" not in failed.error


@pytest.mark.asyncio
async def test_quota_reason_reaches_parent_completion_notification(
    agent_run_manager: LocalAgentRunManager,
    parent_session: dict[str, Any],
    temp_db: HubDatabase,
) -> None:
    notifications: list[tuple[str, str, dict[str, Any]]] = []

    async def wake_parent(
        session_id: str,
        message: str,
        result: dict[str, Any],
    ) -> dict[str, bool]:
        notifications.append((session_id, message, result))
        return {"ism_persisted": True}

    registry = CompletionEventRegistry(wake_callback=wake_parent)
    monitor = _monitor(agent_run_manager, temp_db, registry)
    run = _make_codex_run(agent_run_manager, parent_session, label="quota-notification")
    registry.register(run.id, [parent_session["id"]])

    handled = await monitor.check_idle_agents()

    assert handled == 1
    assert len(notifications) == 1
    session_id, message, result = notifications[0]
    assert session_id == parent_session["id"]
    assert message == f"Agent {run.id} failed"
    assert result["status"] == "error"
    assert result["terminal_reason"] == "provider_quota_exhausted"
    assert result["error"] == (f"Provider quota exhausted: provider=codex; reset_time={RESET_TIME}")
