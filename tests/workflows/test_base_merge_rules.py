"""A base merge the merge capability admits passes the gated rules, and nothing else does.

The bundled rules run through the real engine for a seat that selects them. A
definition declaring `merge_base_commits` takes the admitted merge and the commit that
completes it without a claim. Any other merge, a commit that completes no merge, and
the same calls from a definition without the grant stay gated. The repositories are
real git, a main checkout on `integration` and a linked task worktree, so admission
reads the state production reads. Definition names are synthetic; the rules read only
the capability (AGENTS.md Working Rule 16).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory
from tests.fixtures.landing_case import LandingCase, make_landing_case

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"
CAPABILITY = "merge_base_commits"
DEFINITION_NAMES = ["merge-seat", "custom-seat"]
GATED_RULES = [
    "name:require-task-before-edit",
    "name:require-task-before-commit",
    "name:block-unresolved-scope-shell-write",
    "name:require-code-review-skill",
    "name:require-code-review-self-review",
]
# Every task-enforcement rule, so a rule outside the gated set cannot block the merge.
TASK_ENFORCEMENT_RULES = [
    "tag:task-enforcement",
    "name:require-code-review-skill",
    "name:require-code-review-self-review",
]
SELECTIONS = [GATED_RULES, TASK_ENFORCEMENT_RULES]
SELECTION_IDS = ["gated-rules", "task-enforcement-rules"]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def _conflict_base_merge(worktree: Path, ref: str = "integration") -> None:
    """Merge `ref` into the task branch; `shared.txt` conflicts and stays unresolved."""
    subprocess.run(
        ["git", "merge", "--no-edit", ref],
        cwd=worktree,
        capture_output=True,
        check=False,
    )


def _start_conflicting_base_merge(worktree: Path) -> None:
    """Merge `integration` into the task branch, resolve the conflict, and stage it.

    The next commit completes the merge, so MERGE_HEAD is the admitted integration tip.
    """
    _conflict_base_merge(worktree)
    (worktree / "shared.txt").write_text("resolved\n")
    _git(worktree, "add", "shared.txt")


@pytest.fixture
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _register_seat(db: HubDatabase, project_id: str) -> tuple[WorkflowHookHandler, Session]:
    """An unclaimed session in the project, with the bundled rules the gates read."""
    session_manager = SessionManager(db)
    session = session_manager.register(
        external_id="base-merge-external",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    result = sync_bundled_rules(db, get_bundled_rules_path())
    assert result["errors"] == []
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    SessionVariableManager(db).merge_variables(
        session.id,
        {
            "_variable_defaults_loaded": True,
            "require_task_before_edit": True,
            "task_claimed": False,
            "plan_mode": False,
        },
    )
    handler = WorkflowHookHandler(rule_engine=RuleEngine(db), session_manager=session_manager)
    return handler, session


@pytest.fixture
def seat(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    _local_machine_identity: None,
) -> tuple[WorkflowHookHandler, Session, Path]:
    """A main checkout on `integration` and a task worktree on a branch from its base.

    The worktree is registered to a top-level task, and the session is unclaimed.
    `feature` is a branch that is neither the integration nor the landing tip.
    """
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "integration")
    _commit(main, "shared.txt", "base\n", "base")
    base = _git(main, "rev-parse", "HEAD")
    _commit(main, "integ.txt", "integration\n", "integration new file")
    _commit(main, "shared.txt", "integration edit\n", "integration shared edit")
    _git(main, "branch", "feature", base)
    project = isolated_checkout_factory(temp_db, "base-merge-test", root=main).project
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="Base merge test",
        validation_criteria="The base merge lands without a claim.",
    )

    worktree_path = tmp_path / "task"
    _git(main, "worktree", "add", "-q", "-b", "task-25385", str(worktree_path), base)
    # Register the path git reports, so the lookup is exact on macOS /private/var.
    worktree = Path(_git(worktree_path, "rev-parse", "--show-toplevel"))
    _commit(worktree, "shared.txt", "task edit\n", "task shared edit")
    _commit(worktree, "task.txt", "task\n", "task file")
    LocalWorktreeManager(temp_db).create(
        project_id=project.id,
        branch_name="task-25385",
        worktree_path=str(worktree),
        base_branch="integration",
        task_id=task.id,
    )

    handler, session = _register_seat(temp_db, project.id)
    return handler, session, worktree


def _seat(
    db: HubDatabase, session: Session, name: str, *, granted: bool, selectors: list[str]
) -> None:
    """A synthetic seat; only its capability list and rule selection matter to the gates."""
    AgentDefinitionManager(db).create(
        name,
        {
            "name": name,
            "prompts": {"agent": "Merge the integration branch into the task."},
            "workflows": {"rule_selectors": {"include": selectors}},
            "capabilities": [CAPABILITY] if granted else [],
        },
        project_id=session.project_id,
        enabled=True,
    )
    SessionVariableManager(db).set_variable(session.id, "_agent_type", name)


def _tool_event(
    session: Session,
    worktree: Path,
    tool_name: str,
    tool_input: dict[str, str],
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session.external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(worktree),
        project_id=session.project_id,
        data={"tool_name": tool_name, "tool_input": tool_input},
        metadata={"_platform_session_id": session.id, "project_path": str(worktree)},
    )


def _bash_event(
    session: Session,
    worktree: Path,
    command: str,
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
) -> HookEvent:
    return _tool_event(session, worktree, "Bash", {"command": command}, event_type)


@pytest.mark.parametrize("definition_name", DEFINITION_NAMES)
@pytest.mark.parametrize("selectors", SELECTIONS, ids=SELECTION_IDS)
async def test_granted_merge_of_the_integration_tip_is_admitted(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    definition_name: str,
    selectors: list[str],
) -> None:
    """A granted definition merges the integration tip into a clean task worktree unclaimed."""
    handler, session, worktree = seat
    _seat(temp_db, session, definition_name, granted=True, selectors=selectors)

    response = await handler._evaluate_rules(
        _bash_event(session, worktree, "git merge integration")
    )

    assert response.decision == "allow"


@pytest.mark.parametrize("selectors", SELECTIONS, ids=SELECTION_IDS)
async def test_ungranted_merge_of_the_integration_tip_is_blocked(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    selectors: list[str],
) -> None:
    handler, session, worktree = seat
    _seat(temp_db, session, "custom-seat", granted=False, selectors=selectors)

    response = await handler._evaluate_rules(
        _bash_event(session, worktree, "git merge integration")
    )

    assert response.decision == "block"


async def test_granted_merge_of_another_branch_is_blocked(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase
) -> None:
    handler, session, worktree = seat
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)

    response = await handler._evaluate_rules(_bash_event(session, worktree, "git merge feature"))

    assert response.decision == "block"


async def test_granted_commit_outside_a_base_merge_is_blocked(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase
) -> None:
    handler, session, worktree = seat
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)

    response = await handler._evaluate_rules(_bash_event(session, worktree, "git commit -m wip"))

    assert response.decision == "block"


@pytest.mark.parametrize("granted", [True, False])
@pytest.mark.parametrize("selectors", SELECTIONS, ids=SELECTION_IDS)
async def test_commit_completing_the_admitted_base_merge_follows_the_grant(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    granted: bool,
    selectors: list[str],
) -> None:
    handler, session, worktree = seat
    _start_conflicting_base_merge(worktree)
    _seat(temp_db, session, "merge-seat", granted=granted, selectors=selectors)

    response = await handler._evaluate_rules(_bash_event(session, worktree, "git commit --no-edit"))

    assert response.decision == ("allow" if granted else "block")


async def test_admitted_merge_leaves_the_session_unclaimed(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase
) -> None:
    """The admitted merge writes no claim, so landing still sees a session that never claimed."""
    handler, session, worktree = seat
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)
    worktree_row = LocalWorktreeManager(temp_db).get_by_path(str(worktree))
    assert worktree_row is not None and worktree_row.task_id is not None

    response = await handler._evaluate_rules(
        _bash_event(session, worktree, "git merge integration")
    )

    assert response.decision == "allow"
    task = LocalTaskManager(temp_db).get_task(worktree_row.task_id)
    assert task.claimed_by_session_id is None
    assert not SessionVariableManager(temp_db).get_variables(session.id).get("task_claimed")


@pytest.mark.parametrize("selectors", SELECTIONS, ids=SELECTION_IDS)
async def test_redirected_merge_is_refused_for_a_granted_seat(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase, selectors: list[str]
) -> None:
    """A redirect writes a file the merge admission never names, so the claim gates stay in force."""
    handler, session, worktree = seat
    _seat(temp_db, session, "merge-seat", granted=True, selectors=selectors)

    response = await handler._evaluate_rules(
        _bash_event(session, worktree, "git merge integration > task.txt")
    )

    assert response.decision == "block"


@pytest.mark.parametrize("selectors", SELECTIONS, ids=SELECTION_IDS)
async def test_script_writing_a_conflicted_path_is_refused_for_a_granted_seat(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase, selectors: list[str]
) -> None:
    """Only a structured write to a conflicted path is admitted, never a shell command's output."""
    handler, session, worktree = seat
    _conflict_base_merge(worktree)
    _seat(temp_db, session, "merge-seat", granted=True, selectors=selectors)

    response = await handler._evaluate_rules(
        _bash_event(session, worktree, "python3 evil.py > shared.txt")
    )

    assert response.decision == "block"


@pytest.mark.parametrize("selectors", SELECTIONS, ids=SELECTION_IDS)
async def test_conflicted_base_merge_resolves_and_stages_through_the_gates(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase, selectors: list[str]
) -> None:
    """Start, resolve, stage and commit a conflicted base merge, each step judged by the gates."""
    handler, session, worktree = seat
    _seat(temp_db, session, "merge-seat", granted=True, selectors=selectors)

    start = await handler._evaluate_rules(
        _bash_event(session, worktree, "git merge --no-edit integration")
    )
    assert start.decision == "allow"
    _conflict_base_merge(worktree)

    resolve = await handler._evaluate_rules(
        _tool_event(
            session,
            worktree,
            "Edit",
            {
                "file_path": str(worktree / "shared.txt"),
                "old_string": "<<<<<<< HEAD",
                "new_string": "resolved",
            },
        )
    )
    assert resolve.decision == "allow"
    (worktree / "shared.txt").write_text("resolved\n")

    stage = await handler._evaluate_rules(_bash_event(session, worktree, "git add shared.txt"))
    assert stage.decision == "allow"
    _git(worktree, "add", "shared.txt")

    commit = await handler._evaluate_rules(_bash_event(session, worktree, "git commit --no-edit"))
    assert commit.decision == "allow"


async def test_staging_an_unconflicted_path_is_refused_for_a_granted_seat(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase
) -> None:
    handler, session, worktree = seat
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)
    _conflict_base_merge(worktree)

    response = await handler._evaluate_rules(_bash_event(session, worktree, "git add task.txt"))

    assert response.decision == "block"


@pytest.fixture
def landing(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    _local_machine_identity: None,
) -> tuple[LandingCase, WorkflowHookHandler, Session, Path, Task]:
    """A landing case whose main checkout and task worktree both change `shared.txt`.

    The task is claimed by the case's claimant. The seat that merges is another session.
    """
    case = make_landing_case(temp_db, sample_project, tmp_path)
    case.direct({"shared.txt": "base\n"})
    base = case.git("rev-parse", "HEAD")
    worktree_path = tmp_path / "task"
    _git(case.repo, "worktree", "add", "-q", "-b", "task-25385", str(worktree_path), base)
    worktree = Path(_git(worktree_path, "rev-parse", "--show-toplevel"))
    _commit(worktree, "shared.txt", "task edit\n", "task shared edit")
    _commit(worktree, "task.txt", "task\n", "task file")
    case.direct({"shared.txt": "integration edit\n", "integ.txt": "integration\n"})
    task = case.task("Base merge landing")
    LocalWorktreeManager(temp_db).create(
        project_id=case.project_id,
        branch_name="task-25385",
        worktree_path=str(worktree),
        base_branch="trunk",
        task_id=task.id,
    )
    handler, session = _register_seat(temp_db, case.project_id)
    return case, handler, session, worktree, task


async def test_base_merge_lands_through_land_commit_without_a_claim(
    landing: tuple[LandingCase, WorkflowHookHandler, Session, Path, Task],
    temp_db: HubDatabase,
) -> None:
    """The seat merges, resolves and commits unclaimed, then lands that merge through land_commit."""
    case, handler, session, worktree, task = landing
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)
    case.manager_for(task, session=session.id)

    start = await handler._evaluate_rules(
        _bash_event(session, worktree, "git merge --no-edit trunk")
    )
    assert start.decision == "allow"
    _conflict_base_merge(worktree, "trunk")
    resolve = await handler._evaluate_rules(
        _tool_event(
            session,
            worktree,
            "Edit",
            {
                "file_path": str(worktree / "shared.txt"),
                "old_string": "<<<<<<< HEAD",
                "new_string": "resolved",
            },
        )
    )
    assert resolve.decision == "allow"
    (worktree / "shared.txt").write_text("resolved\n")
    stage = await handler._evaluate_rules(_bash_event(session, worktree, "git add shared.txt"))
    assert stage.decision == "allow"
    _git(worktree, "add", "shared.txt")
    commit = await handler._evaluate_rules(_bash_event(session, worktree, "git commit --no-edit"))
    assert commit.decision == "allow"
    _git(worktree, "commit", "--no-edit")
    after = await handler._evaluate_rules(
        _bash_event(session, worktree, "git commit --no-edit", HookEventType.AFTER_TOOL)
    )
    assert after.decision == "allow"

    merge = _git(worktree, "rev-parse", "HEAD")
    case.link(task, merge)
    case.approve(task, merge)
    result = await case.land(task, merge, caller=session.id)

    assert result["landed"] is True
    assert case.git("rev-parse", "trunk") == merge
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id == case.claimant
    assert not SessionVariableManager(temp_db).get_variables(session.id).get("task_claimed")
