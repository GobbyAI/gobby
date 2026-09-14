"""Spawn_agent provider/model argument checks (task #22323)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.dry_run import evaluate_spawn
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    INCOMPATIBLE_MODEL_PROVIDER,
    PROVIDER_REQUIRED_FOR_MODEL,
    resolve_spawn_provider,
)
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.resolve import CapabilityResolver
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = pytest.mark.unit


class _SnapshotStore:
    def __init__(self, snapshots: dict[str, ProviderSnapshot]) -> None:
        self._snapshots = snapshots

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        return self._snapshots.get(provider)


class _NoMetadata:
    def get_context_window(self, model: str) -> None:
        return None

    def get_model_metadata(self, model: str) -> None:
        return None


def _model(canonical_model: str, *aliases: str) -> ModelCapability:
    return ModelCapability(
        canonical_model=canonical_model,
        display_name=canonical_model,
        aliases=aliases,
        available=True,
        hidden=False,
        is_default=False,
        context_length=None,
        max_output_tokens=None,
        reasoning=ReasoningSupport.UNKNOWN,
        supported_efforts=None,
        default_effort=None,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        provenance={},
    )


def _resolver() -> CapabilityResolver:
    snapshots = {
        "codex": ProviderSnapshot(
            provider="codex",
            generation=1,
            models=(_model("gpt-5.6-luna"),),
            sources=(),
        ),
        "grok": ProviderSnapshot(
            provider="grok",
            generation=1,
            models=(_model("grok-4.6"),),
            sources=(),
        ),
    }
    return CapabilityResolver(_SnapshotStore(snapshots), _NoMetadata())


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
    return runner


@pytest.fixture
def capability_resolver(monkeypatch: pytest.MonkeyPatch) -> CapabilityResolver:
    resolver = _resolver()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._provider_resolution.spawn_capability_resolver",
        lambda: resolver,
    )
    return resolver


def test_explicit_provider_precedes_agent_and_session_default() -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider="grok",
            agent_provider="claude",
            default_provider="codex",
        )
        == "grok"
    )


def test_agent_definition_provider_precedes_session_default() -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider=None,
            agent_provider="grok",
            default_provider="codex",
        )
        == "grok"
    )


def test_session_default_used_only_without_explicit_or_agent_provider() -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider=None,
            agent_provider="inherit",
            default_provider="codex",
        )
        == "codex"
    )


@pytest.mark.asyncio
async def test_supplied_model_without_provider_is_rejected_before_allocation() -> None:
    runner = _runner()
    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
        ) as isolation,
        patch("gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn") as execute,
    ):
        result = await spawn_agent_impl(
            prompt="work",
            runner=runner,
            model="grok-4.6",
            terminal_backend="tmux",
            parent_session_id="parent",
            isolation="worktree",
        )

    assert result["success"] is False
    assert result["error_code"] == PROVIDER_REQUIRED_FOR_MODEL
    assert result["model"] == "grok-4.6"
    assert "provider" in result["error"]
    isolation.assert_not_called()
    execute.assert_not_called()
    runner.can_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_supplied_model_does_not_use_agent_or_session_provider() -> None:
    runner = _runner()
    agent_body = make_agent_definition(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name="reviewer",
        provider="grok",
    )
    session_manager = MagicMock()
    session_manager.get.return_value = MagicMock(source="codex")

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
    ) as isolation:
        result = await spawn_agent_impl(
            prompt="work",
            runner=runner,
            agent_body=agent_body,
            model="grok-4.6",
            terminal_backend="tmux",
            parent_session_id="parent",
            caller_session_id="caller",
            session_manager=session_manager,
        )

    assert result["success"] is False
    assert result["error_code"] == PROVIDER_REQUIRED_FOR_MODEL
    isolation.assert_not_called()


@pytest.mark.asyncio
async def test_incompatible_pair_does_not_create_worktree(
    tmp_path: Path, capability_resolver: CapabilityResolver
) -> None:
    runner = _runner()
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree_storage = MagicMock()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
        ) as isolation,
        patch("gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn") as execute,
    ):
        result = await spawn_agent_impl(
            prompt="work",
            runner=runner,
            provider="codex",
            model="grok-4.6",
            isolation="worktree",
            worktree_storage=worktree_storage,
            project_path=str(repo),
            terminal_backend="tmux",
            parent_session_id="parent",
        )

    assert result["success"] is False
    assert result["error_code"] == INCOMPATIBLE_MODEL_PROVIDER
    assert result["provider"] == "codex"
    assert result["model"] == "grok-4.6"
    assert "grok-4.6" in result["error"]
    assert "codex" in result["error"]
    assert result["compatible_providers"] == ["grok"]
    assert capability_resolver.find_model("codex", "grok-4.6") is None
    isolation.assert_not_called()
    execute.assert_not_called()
    worktree_storage.create.assert_not_called()
    worktree_storage.get_by_branch.assert_not_called()
    assert list(worktree_root.iterdir()) == []
    runner.can_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_spawn_requires_provider_when_model_supplied() -> None:
    result = await evaluate_spawn(model="grok-4.6")

    assert result.can_spawn is False
    required = [item for item in result.items if item.code == "PROVIDER_REQUIRED_FOR_MODEL"]
    assert len(required) == 1
    assert required[0].detail is not None
    assert required[0].detail["model"] == "grok-4.6"


@pytest.mark.asyncio
async def test_evaluate_spawn_rejects_incompatible_pair(
    capability_resolver: CapabilityResolver,
) -> None:
    result = await evaluate_spawn(provider="codex", model="grok-4.6")

    assert result.can_spawn is False
    pair = [item for item in result.items if item.code == "INCOMPATIBLE_MODEL_PROVIDER"]
    assert len(pair) == 1
    assert pair[0].detail is not None
    assert pair[0].detail["provider"] == "codex"
    assert pair[0].detail["model"] == "grok-4.6"
    assert pair[0].detail["compatible_providers"] == ["grok"]
    assert capability_resolver.find_model("grok", "grok-4.6") is not None
    assert "grok-4.6" in pair[0].message
    assert "codex" in pair[0].message


@pytest.mark.asyncio
async def test_evaluate_spawn_tool_forwards_model() -> None:
    from unittest.mock import AsyncMock

    from gobby.mcp_proxy.tools.agents import create_agents_registry

    evaluation = MagicMock()
    evaluation.to_dict.return_value = {"can_spawn": False}

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._resolve_spawn_project_context",
            return_value=({"id": "target-project"}, "/repo"),
        ),
        patch(
            "gobby.agents.dry_run.evaluate_spawn",
            new=AsyncMock(return_value=evaluation),
        ) as evaluate,
    ):
        registry = create_agents_registry(MagicMock(), db=MagicMock())
        result = await registry._tools["evaluate_spawn"].func(
            agent="test-agent",
            provider="codex",
            model="grok-4.6",
        )

    assert result == {"can_spawn": False}
    assert evaluate.await_args is not None
    assert evaluate.await_args.kwargs["model"] == "grok-4.6"
    assert evaluate.await_args.kwargs["provider"] == "codex"
