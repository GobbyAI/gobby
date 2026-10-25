"""Epic-mode procedures and the devteam runbook binding (addendum 8.5).

The epic-mode contract lives in the seat procedures and definitions; the devteam
runbook binds either an epic or a leaf with one seat catalogue (Decision 15).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import (
    AgentDefinitionBody,
    AgentPromptBlocks,
    AgentSelector,
    AgentWorkflows,
)
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.templates import TemplateEngine
from tests.agents._yaml_helpers import flat
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import SLUG, TITLE, TeamWorld, execution_id, make_team_world

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "src/gobby/install/shared"
SKILLS = SHARED / "skills/gobby"
PROCEDURES = SKILLS / "references/agents/procedures"
AGENTS = get_bundled_agents_path()
RUNBOOKS_GUIDE = ROOT / "docs/guides/runbooks.md"
DEVTEAM_PATH = SHARED / "workflows/pipelines/devteam.yaml"
DEVTEAM = PipelineDefinition.model_validate(yaml.safe_load(DEVTEAM_PATH.read_text()))
RENDERER = StepRenderer(TemplateEngine())
WORKSPACE = "pilot-workspace-id"
REVIEWERS = ("code-reviewer", "docs-reviewer", "research-reviewer")


def _guidance(name: str) -> str:
    """A definition's agent prompt plus its seat procedure, flattened and lowercased."""
    body = AgentDefinitionBody.model_validate(yaml.safe_load((AGENTS / f"{name}.yaml").read_text()))
    assert body.prompts is not None and body.prompts.agent, name
    text = body.prompts.agent
    procedure = body.workflows.variables.get("_seat_procedure")
    if procedure:
        text += "\n" + (SKILLS / procedure.split(":", 1)[1]).read_text()
    return flat(text).lower()


def _missing(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text) is None]


# Decisions 16-24 as the Dev Manager's epic procedure must state them.
DEV_MANAGER_EPIC = (
    r"epic mode",
    r"dependency order",  # test phase
    r"\[gobby-#<leaf>\] test:",  # per-leaf test commits
    r"implementation_domain",  # dispatch rule: one primary per domain
    r"primary",
    r"sub-worktree",
    r"git merge --no-ff",  # merges into epic main
    r"merged_into",  # sub-worktree deletion
    r"never rebase or squash",
    r"approved implementation (?:step )?record",  # intra-epic unlock
    r"docs-reviewer",  # per-leaf routing and reviewer spawns
    r"research-reviewer",
    r"disposition",  # no-work path
    r"epic[- ]tip",  # review at the end and epic-tip repair
    r"found-work leaf",
    r"hold",  # Decisions 22-23
    r"merge 0\.5\.0",  # landing order
    r"bottom-up",  # nested close order
    r"no-work leaves first",
    r"cascade",
)
ORCHESTRATOR_EPIC = (
    r"reviewer_agent",
    r"precedence",
    r"open children",  # topology refusal for a non-epic parent
    r"found-work leaf",
    r"epic-type descendant",
)
DEVELOPER_EPIC = (
    r"leaf",
    r"claim",
    r"record_implementation_step",
    r"never rebase or squash",
    r"merge epic main",  # the conflict-merge job
    r"end_agent_run",  # a sub-worktree developer ends after recording
)
TEST_WRITER_EPIC = (r"dependency order", r"\[gobby-#<leaf>\] test:", r"release")
REVIEWER_EPIC = (r"epic[- ]tip", r"each leaf", r"disposition", r"never (?:fix|edit)")
# Ruling 3 and Decision 23: no detach-by-revert path and no reviewer-as-fixer step in epic mode.
FORBIDDEN_EPIC = re.compile(r"detach|back(?:ed)? out|revert the leaf", re.IGNORECASE)


def test_epic_procedures() -> None:
    dev_manager = _guidance("dev-manager")
    assert _missing(dev_manager, DEV_MANAGER_EPIC) == []
    # The landing order (Decision 24): merge 0.5.0, land, then each leaf's closing record and close.
    landing = re.compile(
        r"merge 0\.5\.0.*?land_commit.*?record_closing_step.*?close_task", re.DOTALL
    )
    assert landing.search(dev_manager[dev_manager.find("epic mode") :]) is not None
    # Epic mode drops the redesign's reviewer-becomes-fixer step (Decision 21).
    assert re.search(r"epic mode[^.]*\b(?:no|never)\b[^.]*fix", dev_manager) is not None
    assert FORBIDDEN_EPIC.findall(dev_manager) == []

    orchestrator = _guidance("orchestrator")
    assert _missing(orchestrator, ORCHESTRATOR_EPIC) == []
    assert _missing(_guidance("developer"), DEVELOPER_EPIC) == []
    assert _missing(_guidance("test-writer"), TEST_WRITER_EPIC) == []

    for reviewer in REVIEWERS:
        procedure = PROCEDURES / f"{reviewer}.md"
        assert procedure.exists(), reviewer
        text = flat(procedure.read_text()).lower()
        assert _missing(text, REVIEWER_EPIC) == [], reviewer
        assert FORBIDDEN_EPIC.findall(text) == [], reviewer

    guide = flat(RUNBOOKS_GUIDE.read_text()).lower()
    assert _missing(guide, (r"epic mode", r"epic/<seq>/main", r"wt-epic-<seq>")) == []


def test_runbook_guide_says_the_dev_manager_never_claims_the_epic() -> None:
    # No claim on the epic: the Dev Manager lands and closes it, and land_commit refuses a
    # session that ever claimed the task. The runtime skip is pinned by
    # test_team_manager_spawn_takes_no_task_claim in tests/mcp_proxy/tools/spawn_agent/test_execution.py.
    guide = flat(RUNBOOKS_GUIDE.read_text())
    assert "The Dev Manager holds the epic claim" not in guide
    assert "The Dev Manager never claims the epic" in guide


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

    def seat_body(name: str, *args: Any, **kwargs: Any) -> AgentDefinitionBody:
        return AgentDefinitionBody(
            name=name,
            prompts=AgentPromptBlocks(agent="Runbook seat"),
            workflows=AgentWorkflows(rule_selectors=AgentSelector()),
        )

    # admit_team binds only an enabled reviewer definition with the review grant.
    AgentDefinitionManager(temp_db).create(
        "code-reviewer",
        {
            "name": "code-reviewer",
            "version": "1.0",
            "prompts": {"agent": "Review the leaf."},
            "workflows": {"rule_selectors": {"include": []}},
            "task_step_writes": ["review"],
        },
        project_id=world.case.project_id,
    )
    monkeypatch.setattr(_factory, "_load_agent_body", seat_body)
    return world


def _context(inputs: dict[str, Any]) -> dict[str, Any]:
    values = {name: spec.get("default") for name, spec in DEVTEAM.inputs.items()}
    values.update(workspace="pilot-workspace", report_to="gobby#14972", **inputs)
    return {
        "inputs": values,
        "steps": {"guard": {"output": {"workspace_id": WORKSPACE}}},
        "_string_inputs": frozenset(
            name for name, spec in DEVTEAM.inputs.items() if spec.get("type") == "string"
        ),
    }


def _render(run: Session, step_id: str, context: dict[str, Any]) -> dict[str, Any] | None:
    step = next(step for step in DEVTEAM.steps if step.id == step_id)
    step_context = {**context, "invocation_id": step_invocation_id(execution_id(run), step_id)}
    if not RENDERER.should_run_step(step, step_context):
        return None
    assert step.mcp is not None and step.mcp.arguments is not None
    return RENDERER.render_mcp_arguments(step.mcp.arguments, step_context, drop_none=True)


async def _dry_run(world: TeamWorld, task: Task) -> tuple[dict[str, Any], dict[str, str]]:
    """Admit ``task`` through the rendered admit step; return its reply and launched seats."""
    run = world.child(task, DEVTEAM)
    context = _context({"task": task.id, "reviewer_agent": "code-reviewer"})
    arguments = _render(run, "admit", context)
    assert arguments is not None
    with session_context_for_test(run.id):
        reply = cast(dict[str, Any], await world.tools.call("admit_team", arguments))
    assert reply.get("success") is True, reply
    context["steps"]["admit"] = {"output": reply}
    # A fresh team launches every seat; check_runbook_seats reports them as ``launch``.
    context["steps"]["guard"]["output"]["launch"] = [
        "dev-manager",
        "test-writer",
        "developer",
        "code-reviewer",
    ]
    seats: dict[str, str] = {}
    for step in DEVTEAM.steps:
        rendered = _render(run, step.id, context)
        if rendered is not None and step.mcp is not None and step.mcp.tool == "spawn_agent":
            seats[step.id] = rendered["agent"]
    return reply, seats


async def test_runbook_binds_epic_or_leaf(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    project_id = world.case.project_id
    epic = tasks.create_task(
        project_id, TITLE, task_type="epic", created_in_session_id=world.case.creator
    )
    tasks.create_task(
        project_id,
        "Wire the outage banner",
        parent_task_id=epic.id,
        created_in_session_id=world.case.creator,
        validation_criteria="The banner renders.",
    )
    leaf = world.case.task("Standalone leaf task")

    epic_reply, epic_seats = await _dry_run(world, epic)
    leaf_reply, leaf_seats = await _dry_run(world, leaf)

    # Decision 18: the epic branch and its titled worktree; a leaf keeps the spawn default.
    assert epic_reply["branch"] == f"epic/{epic.seq_num}/main"
    assert Path(epic_reply["project_path"]).name == f"wt-epic-{epic.seq_num}-{SLUG}"
    assert leaf_reply["branch"].startswith(f"task-{leaf.seq_num}-")
    # One runbook, one seat catalogue, whichever task it binds.
    assert epic_seats == leaf_seats
    assert sorted(epic_seats.values()) == [
        "code-reviewer",
        "dev-manager",
        "developer",
        "test-writer",
    ]
