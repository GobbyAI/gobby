"""Session workspace folders, the persistent flag and clear inheritance (addendum 9.3)."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.agents.agent_workspaces import workspace_handoff_references
from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution
from gobby.hooks.event_handlers._session_start.materialize import activate_materialized_session
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.apply_agent_definition import apply_agent_definition_impl
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from tests.hooks.test_clear_successor_seat import _pane, _seat_handler, _stage
from tests.hooks.test_session_materialize import (
    _ACTIVATION_STUBS,
    _ATTEMPT_ID,
    _MATERIALIZE,
    _event,
    _StagedClear,
)

pytestmark = pytest.mark.unit

SHARED = Path(__file__).parents[2] / "src/gobby/install/shared/workflows"
KEEPER = "workspace-keeper"
WORKER = "workspace-worker"
PREDECESSOR_NOTE = (
    "Predecessor folders under your definition are readable only to recover from a crash "
    "or learn from mistakes in prior sessions."
)


def _define(db: HubDatabase, name: str, *, persistent: bool) -> None:
    AgentDefinitionManager(db).upsert_with_steps(
        name,
        {
            "name": name,
            "surfaces": ["persona", "spawn"],
            "prompts": {"persona": "Seat.", "agent": "Work."},
            "persistent_workspace": persistent,
            "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        },
        {"steps": [{"name": "work"}]},
        source="custom",
    )


def _activate(
    pane: _StagedClear,
    session_id: str,
    source: str,
    resolution: SessionStartResolution | None = None,
) -> None:
    """Run SessionStart activation for ``session_id`` with the given start source."""
    session = pane.sessions.get(session_id)
    assert session is not None
    event = _event({"source": source, "cwd": pane.root})
    event.task_id = None
    with ExitStack() as stack:
        for name in _ACTIVATION_STUBS:
            stack.enter_context(patch(f"{_MATERIALIZE}.{name}", MagicMock()))
        activate_materialized_session(
            _seat_handler(pane.sessions),
            event,
            session_id,
            resolution=resolution,
            session_obj=session,
            project_id=pane.project_id,
            transcript_path=None,
            terminal_context=pane.term,
        )


def _clear_into(pane: _StagedClear, external_id: str) -> str:
    """Stage a marked clear of the predecessor and activate its successor."""
    successor_id = pane.register(external_id)
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=pane.sessions.get(pane.predecessor_id),
        clear_attempt_id=_ATTEMPT_ID,
        clear_supersedes=None,
    )
    _activate(pane, successor_id, "clear", resolution)
    return successor_id


def _folder(home: Path, pane: _StagedClear, definition: str, session_id: str) -> Path:
    session = pane.sessions.get(session_id)
    assert session is not None and session.seq_num is not None
    return home / "agents_workspaces" / pane.project_id / definition / f"session-{session.seq_num}"


def _workspace(db: HubDatabase, session_id: str) -> tuple[Path | None, Any]:
    variables = SessionVariableManager(db).get_variables(session_id)
    folder = variables.get("_agent_workspace_dir")
    return (Path(folder) if folder else None), variables.get("_agent_workspace_persistent")


@pytest.mark.asyncio
async def test_session_folder_binding(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "folders")
    home = (tmp_path / "gobby-home").resolve()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    _define(temp_db, WORKER, persistent=False)
    seat = pane.predecessor_id
    assert (await apply_agent_definition_impl(WORKER, temp_db, seat))["success"]

    _activate(pane, seat, "startup")
    folder, persistent = _workspace(temp_db, seat)
    assert folder == _folder(home, pane, WORKER, seat)
    assert persistent is False

    # Compaction and resume keep the same session, so the same folder.
    for source in ("compact", "resume"):
        _activate(pane, seat, source)
        assert _workspace(temp_db, seat)[0] == folder, source

    # A seat without a definition writes under default/.
    plain = pane.register("plain-ext")
    _activate(pane, plain, "startup")
    assert SessionVariableManager(temp_db).get_variables(plain).get("_agent_type") == "default"
    expected = _folder(home, pane, "default", plain)
    assert _workspace(temp_db, plain)[0] == expected

    # A manual /clear without a handoff marker is a new session with a new folder.
    manual = pane.register("manual-clear-ext")
    _activate(pane, manual, "clear")
    manual_type = SessionVariableManager(temp_db).get_variables(manual).get("_agent_type")
    manual_folder = _workspace(temp_db, manual)[0]
    assert manual_folder == _folder(home, pane, manual_type or "default", manual)
    assert manual_folder not in (folder, expected)


@pytest.mark.asyncio
async def test_handoff_references_name_the_bound_folder(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, "handoff-refs")
    home = (tmp_path / "gobby-home").resolve()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    _define(temp_db, WORKER, persistent=False)
    seat = pane.predecessor_id
    assert (await apply_agent_definition_impl(WORKER, temp_db, seat))["success"]
    _activate(pane, seat, "startup")

    folder, _ = _workspace(temp_db, seat)
    assert folder is not None
    assert workspace_handoff_references(temp_db, seat) == [f"Agent workspace: {folder}"]
    assert workspace_handoff_references(temp_db, None) == []
    unknown = "21000000-0000-4000-8000-0000000000ff"
    assert workspace_handoff_references(temp_db, unknown) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False], ids=["persistent", "fresh"])
async def test_clear_inheritance_by_persistence(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistent: bool,
) -> None:
    pane = _pane(temp_db, tmp_path, monkeypatch, f"clear-{persistent}")
    home = (tmp_path / "gobby-home").resolve()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    name = KEEPER if persistent else WORKER
    _define(temp_db, name, persistent=persistent)
    assert (await apply_agent_definition_impl(name, temp_db, pane.predecessor_id))["success"]
    _activate(pane, pane.predecessor_id, "startup")
    lineage, _ = _workspace(temp_db, pane.predecessor_id)
    assert lineage == _folder(home, pane, name, pane.predecessor_id)
    _stage(temp_db, pane)

    successor = _clear_into(pane, "succ-ext")

    folder, flag = _workspace(temp_db, successor)
    assert flag is persistent
    if persistent:
        # The lineage keeps its first sequence number across clears.
        assert folder == lineage
    else:
        assert folder == _folder(home, pane, name, successor)
        assert folder != lineage


async def _turn(engine: RuleEngine, variables: dict[str, Any]) -> str:
    response = await engine.evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="44444444-4444-4444-8444-444444444444",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "hello"},
        ),
        session_id="44444444-4444-4444-8444-444444444444",
        variables=variables,
    )
    return response.context or ""


async def _compact(engine: RuleEngine, variables: dict[str, Any]) -> None:
    await engine.evaluate(
        HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="44444444-4444-4444-8444-444444444444",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"source": "compact"},
        ),
        session_id="44444444-4444-4444-8444-444444444444",
        variables=variables,
    )


@pytest.mark.asyncio
async def test_persistent_set_and_injection(temp_db: HubDatabase) -> None:
    assert AgentDefinitionBody.model_fields["persistent_workspace"].default is False

    manager = RuleDefinitionManager(temp_db)
    for rule_file in sorted((SHARED / "rules/roles").glob("*.yaml")):
        document = yaml.safe_load(rule_file.read_text())
        for rule_name, rule_data in document["rules"].items():
            body_data, metadata = split_rule_definition_data(rule_data)
            manager.create(
                name=rule_name,
                definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
                priority=metadata["priority"],
                enabled=metadata["enabled"],
                tags=document["tags"],
            )
    engine = RuleEngine(temp_db)

    for persistent in (True, False):
        folder = f"/ws/{'assistant' if persistent else 'developer'}/session-4"
        variables: dict[str, Any] = {
            "_agent_type": "assistant" if persistent else "developer",
            "_agent_workspace_dir": folder,
            "_agent_workspace_persistent": persistent,
        }
        first = await _turn(engine, variables)
        assert folder in first, persistent
        assert "literal paths only" in first.lower()
        assert (PREDECESSOR_NOTE in first) is persistent
        assert folder not in await _turn(engine, variables), "once per context epoch"
        await _compact(engine, variables)
        assert folder in await _turn(engine, variables), "re-armed after compaction"

    assert "literal paths only" not in (await _turn(engine, {"_agent_type": "developer"})).lower()
