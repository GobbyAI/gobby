"""Session reporting through real isolated storage and public adapters."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import sessions
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import (
    SessionContext,
    reset_session_context,
    set_session_context,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("command", ["list", "show"])
def test_cli_session_reads_hydrate_claims(
    temp_db: HubDatabase,
    canonical_task_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    manager = SessionManager(temp_db)
    owner = canonical_task_session
    idle = manager.register(
        external_id="claim-free-reporting-session",
        machine_id=owner.machine_id,
        source="codex",
        project_id=owner.project_id,
    )
    assert owner.project_id is not None
    task = LocalTaskManager(temp_db).create_task(
        project_id=owner.project_id,
        title="Reporting claim",
        validation_criteria="Claim appears in session output",
        claimed_by_session_id=owner.id,
    )

    @contextmanager
    def isolated_manager() -> Iterator[SessionManager]:
        yield manager

    monkeypatch.setattr("gobby.cli.sessions.session_manager_context", isolated_manager)
    monkeypatch.setattr("gobby.cli.sessions.resolve_session_id", lambda value: value)
    runner = CliRunner()
    if command == "list":
        result = runner.invoke(sessions, ["list", "--json"])
        assert result.exit_code == 0, result.output
        rows = {row["id"]: row for row in json.loads(result.output)}
        assert rows[owner.id]["claimed_task_refs"] == [task.seq_num]
        assert rows[idle.id]["claimed_task_refs"] == []
    else:
        for session, expected in ((owner, [task.seq_num]), (idle, [])):
            result = runner.invoke(sessions, ["show", session.id, "--json"])
            assert result.exit_code == 0, result.output
            assert json.loads(result.output)["claimed_task_refs"] == expected


@pytest.mark.asyncio
async def test_session_task_history_uses_json_native_brief_cards(
    temp_db: HubDatabase, canonical_task_session: Session
) -> None:
    session = canonical_task_session
    assert session.project_id is not None
    manager = LocalTaskManager(temp_db)
    links = SessionTaskManager(temp_db)
    task_ids: list[str] = []
    for index in range(12):
        task = manager.create_task(
            project_id=session.project_id,
            title=f"History {index}",
            validation_criteria="Structured history row",
            labels=["reporting"],
            claimed_by_session_id=session.id if index == 0 else None,
        )
        task_ids.append(task.id)
        links.link_task(session.id, task.id, "created")
    registry = create_task_registry(manager)
    result = await registry.call("get_session_tasks", {"session_id": session.id})

    encoded = json.dumps(result)
    assert "Task(" not in encoded
    assert len(result["tasks"]) == 12
    cards: dict[str, dict[str, Any]] = {}
    for row in result["tasks"]:
        card = row["task"]
        assert isinstance(card, dict)
        assert isinstance(row["link_created_at"], str)
        assert card["labels"] == ["reporting"]
        assert isinstance(card["created_at"], str)
        assert isinstance(card["checkout_mode"], str)
        assert card == await registry.call("get_task", {"task_id": card["id"]})
        cards[card["id"]] = card
    assert cards[task_ids[0]]["state"]["claimed_by_session_id"] == session.id
    assert cards[task_ids[-1]]["state"]["claimed_by_session_id"] is None


@pytest.mark.asyncio
async def test_message_history_scopes_local_refs_from_caller_without_project_context(
    temp_db: HubDatabase, canonical_task_session: Session
) -> None:
    session = canonical_task_session
    registry = InternalToolRegistry(name="gobby-agents", description="Reporting regression")
    add_messaging_tools(
        registry,
        InterSessionMessageManager(temp_db),
        SessionManager(temp_db),
        temp_db,
    )
    session_token = set_session_context(SessionContext(session_id=session.id))
    project_token = set_project_context(None)
    try:
        result = await registry.call(
            "get_inter_session_messages", {"target_session_id": f"#{session.seq_num}"}
        )
        assert result["success"] is True, result
        assert result["messages"] == []
        missing = await registry.call(
            "get_inter_session_messages", {"target_session_id": "#999999999"}
        )
        assert missing["success"] is False
        assert "resolve" in missing["error"].lower() or "not found" in missing["error"].lower()
    finally:
        reset_project_context(project_token)
        reset_session_context(session_token)
