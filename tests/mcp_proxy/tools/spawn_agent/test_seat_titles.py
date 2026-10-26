"""Caller-supplied titles never replace a defined seat's pane or session title.

Contract: docs/contracts/seat-titles.md, rule 1 and the spawn_agent caller rule. A
seat's title is its agent definition display name (dev-manager -> Dev Manager), whether
or not the caller passes a title.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import prepare_terminal_spawn
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id

PIPELINES = Path(__file__).resolve().parents[4] / "src/gobby/install/shared/workflows/pipelines"


async def _spawn_developer(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    title: str | None,
) -> Any:
    from gobby.workflows.agent_models import (
        AgentDefinitionBody,
        AgentPromptBlocks,
        AgentSelector,
        AgentWorkflows,
    )

    parent = session_manager.register(
        external_id="parent-seat-titles",
        machine_id=get_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )

    async def launch(**kwargs: Any) -> dict[str, Any]:
        prepared = prepare_terminal_spawn(
            session_manager=ChildSessionManager(session_manager),
            parent_session_id=parent.id,
            project_id=parent.project_id,
            machine_id=parent.machine_id,
            source="codex",
            agent_name=kwargs["agent_lookup_name"],
            initial_variables=kwargs["initial_variables"],
        )
        return {"success": True, "prepared": prepared}

    monkeypatch.setattr(
        _factory,
        "_load_agent_body",
        Mock(
            return_value=AgentDefinitionBody(
                name="developer",
                prompts=AgentPromptBlocks(agent="Developer seat"),
                workflows=AgentWorkflows(rule_selectors=AgentSelector()),
            )
        ),
    )
    monkeypatch.setattr(_factory, "enforce_spawn_caller", AsyncMock(return_value=parent.id))
    monkeypatch.setattr(_factory, "pipeline_caller", Mock(return_value=parent))
    monkeypatch.setattr(_factory, "pipeline_invocation_reply", Mock(return_value=None))
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    registry = _factory.create_spawn_agent_registry(
        Mock(), session_manager=session_manager, db=session_manager.db
    )
    arguments: dict[str, Any] = {
        "prompt": "Write the developer seat's change.",
        "agent": "developer",
        "execution_mode": "interactive",
        "checkout_mode": "none",
        "project_path": str(PIPELINES),
    }
    if title is not None:
        arguments["title"] = title
    result = await registry.call("spawn_agent", arguments)
    assert result["success"]
    child = session_manager.get(result["prepared"].session_id)
    assert child is not None
    return child


@pytest.mark.asyncio
async def test_caller_title_does_not_replace_a_defined_seat_title(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = await _spawn_developer(
        session_manager,
        sample_project,
        monkeypatch,
        title="Caller Slug Developer",
    )

    assert child.title == "Developer"
    assert child.title_source == "definition"


@pytest.mark.asyncio
async def test_defined_seat_without_caller_title_takes_its_definition_name(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = await _spawn_developer(session_manager, sample_project, monkeypatch, title=None)

    assert child.title == "Developer"
    assert child.title_source == "definition"
