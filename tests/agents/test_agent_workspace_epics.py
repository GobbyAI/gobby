"""Epic workspace folders shared by one epic team (addendum 9.4)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.agents.agent_workspaces import (
    bind_epic_workspace,
    drain_workspace_commits,
    workspace_git_dir,
)
from gobby.agents.team_teardown import TEARDOWN_GRACE
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world
from tests.fixtures.team_teardown_case import (
    Team,
    Terminations,
    admitted_team,
    closed_at,
    daemon,
    seated_team,
    teardown_pass,
)

pytestmark = pytest.mark.unit

ROLES_DIR = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules/roles"


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    monkeypatch.setenv("GOBBY_HOME", str((tmp_path / "gobby-home").resolve()))
    return world


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    manager = RuleDefinitionManager(temp_db)
    for rule_file in sorted(ROLES_DIR.glob("*.yaml")):
        document = yaml.safe_load(rule_file.read_text())
        for name, rule_data in document["rules"].items():
            body_data, metadata = split_rule_definition_data(rule_data)
            manager.create(
                name=name,
                definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
                priority=metadata["priority"],
                enabled=metadata["enabled"],
                tags=document["tags"],
            )
    return RuleEngine(temp_db)


def _epic(world: TeamWorld, title: str) -> tuple[Task, Task]:
    """An unclaimed epic with one open code leaf; its Dev Manager never claims it."""
    tasks = LocalTaskManager(world.case.db)
    epic = tasks.create_task(
        world.case.project_id,
        title,
        task_type="epic",
        created_in_session_id=world.case.creator,
    )
    leaf = tasks.create_task(
        world.case.project_id,
        f"{title} leaf",
        parent_task_id=epic.id,
        category="code",
        created_in_session_id=world.case.creator,
        validation_criteria=f"{title} leaf lands with an approved implementation record.",
    )
    return tasks.get_task(epic.id), leaf


async def _epic_team(world: TeamWorld, title: str) -> tuple[Team, Task, str]:
    """An epic team with its four runbook seats plus one sub-worktree developer."""
    epic, leaf = _epic(world, title)
    team = await seated_team(world, epic, world.child(epic))
    manager = team.seats[0]
    sub_developer = world.case.session("developer", parent=manager)
    LocalAgentRunManager(world.case.db).create(
        parent_session_id=manager,
        provider="claude",
        prompt="Implement the leaf in its sub-worktree.",
        agent_name="developer",
        child_session_id=sub_developer,
        task_id=leaf.id,
    )
    return team, leaf, sub_developer


def _epic_dir(world: TeamWorld, epic: Task) -> Path:
    home = Path(os.environ["GOBBY_HOME"])
    return home / "agents_workspaces" / world.case.project_id / "epics" / f"epic-{epic.seq_num}"


def _variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    return dict(SessionVariableManager(db).get_variables(session_id))


async def _write_decision(engine: RuleEngine, world: TeamWorld, session_id: str, path: Path) -> str:
    variables = _variables(world.case.db, session_id)
    variables.setdefault("_agent_type", "developer")
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Write",
            "tool_input": {"file_path": str(path), "content": "note"},
            "cwd": str(world.case.repo),
            "project_path": str(world.case.repo),
        },
    )
    response = await engine.evaluate(event, session_id=session_id, variables=variables)
    return response.decision


async def test_epic_folder_scoped_to_team(world: TeamWorld, engine: RuleEngine) -> None:
    db = world.case.db
    team, _leaf, sub_developer = await _epic_team(world, "Shared epic notes")
    epic_dir = _epic_dir(world, team.task)
    bystander = await admitted_team(world, "Unrelated single task")
    outsider = world.case.session("outsider")

    for seat in (*team.seats, sub_developer):
        assert bind_epic_workspace(db, seat) == epic_dir, seat
        assert _variables(db, seat)["_agent_workspace_epic_dir"] == str(epic_dir)
        decision = await _write_decision(engine, world, seat, epic_dir / "decisions.md")
        assert decision == "allow", seat

    for stranger in (*bystander.seats, outsider):
        assert bind_epic_workspace(db, stranger) is None, stranger
        assert "_agent_workspace_epic_dir" not in _variables(db, stranger)
        SessionVariableManager(db).merge_variables(
            stranger,
            {"_agent_workspace_dir": str(epic_dir.parents[1] / "developer" / "session-1")},
        )
        decision = await _write_decision(engine, world, stranger, epic_dir / "decisions.md")
        assert decision == "block", stranger


def _subjects(project_id: str) -> list[str]:
    result = subprocess.run(
        ["git", f"--git-dir={workspace_git_dir(project_id)}", "log", "--format=%s"],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.splitlines() if result.returncode == 0 else []


async def test_epic_close_commit_and_lock(
    world: TeamWorld, engine: RuleEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    team, leaf, sub_developer = await _epic_team(world, "Close the epic workspace")
    epic = team.task
    epic_dir = _epic_dir(world, epic)
    seats = (*team.seats, sub_developer)
    for seat in seats:
        assert bind_epic_workspace(db, seat) == epic_dir
    epic_dir.mkdir(parents=True, exist_ok=True)
    (epic_dir / "decisions.md").write_text("merge order: leaf first\n")

    # Land the epic tip, close the leaf, then the bound epic.
    tree = Path(team.binding.worktree_path)
    (tree / "epic.py").write_text("# epic work\n")
    git_in(tree, "add", "epic.py")
    git_in(tree, "commit", "-m", "epic work")
    sha = git_in(tree, "rev-parse", "HEAD")
    world.case.git("merge", "--no-edit", team.binding.branch)
    tasks = LocalTaskManager(db)
    tasks.close_task(leaf.id, reason="moot", closed_in_session_id=team.seats[0])
    db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([sha]), epic.id))
    tasks.close_task(
        epic.id, reason="done", closed_in_session_id=team.seats[0], closed_commit_sha=sha
    )
    closed = closed_at(tasks.get_task(epic.id))
    await teardown_pass(db, daemon(db), closed + TEARDOWN_GRACE)

    marker = f"epic-{epic.seq_num}"
    await drain_workspace_commits()
    assert any(marker in subject for subject in _subjects(world.case.project_id))

    # The folder is locked: no seat keeps the epic folder, and a rebind finds no live team.
    for seat in seats:
        assert "_agent_workspace_epic_dir" not in _variables(db, seat), seat
        assert bind_epic_workspace(db, seat) is None, seat
        decision = await _write_decision(engine, world, seat, epic_dir / "late.md")
        assert decision == "block", seat
