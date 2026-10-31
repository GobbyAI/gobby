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
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.agent_workspaces import grant_seat_workspace_paths
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.srt_settings import render_srt_settings
from gobby.config.shell_lexing import ParsedShellCommand
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.base_merge_commits import base_merge_admission
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.seat_messages import candidate_event
from gobby.workflows.engine.seat_transitions import _candidate_rejection
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory
from tests.fixtures.landing_case import LandingCase, bind_task_manager, make_landing_case
from tests.fixtures.team_case import make_team_world

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "$(touch pwned)"',
        'git commit -m "`touch pwned`"',
        'git commit -m "prefix $(touch pwned) suffix"',
        "git commit -m \"$(printf %s 'literal'; touch pwned)\"",
        'git commit -m "$\\\n(touch pwned)"',
        'git commit -m "$((1 + $(touch pwned; printf 0)))"',
        "git commit -m #'\ntouch pwned\n'",
        'git commit -m "x\n<<EOF "\ntouch pwned\nEOF',
        "git commit -m 'x\n<<EOF '\ntouch pwned\nEOF",
        'git commit -F "x\n<<EOF "\ntouch pwned\nEOF',
        "git commit -F 'x\n<<EOF '\ntouch pwned\nEOF",
        "git commit --no-edit\ntouch pwned",
        "git commit --no-edit; touch pwned",
        "git commit --no-edit | touch pwned",
        "git commit --no-edit && touch pwned",
        "git commit --no-edit > pwned",
        'git commit -m "safe message"',
        "git commit -m 'literal $()'",
        "git commit -m 'literal\nmessage'",
        "git commit -F message.txt",
        "git merge --quit",
        "git add shared.txt",
        "git merge --no-commit integration",
        "git merge --ff-only integration",
        "git merge -unsafe",
        "git add -- -unsafe",
        "git -C -unsafe commit --no-edit",
        'git merge "$(touch pwned)"',
        'git merge "`touch pwned`"',
        'git add -- "$(touch pwned)"',
        'git add -- "`touch pwned`"',
        'git -C "$(touch pwned)" commit --no-edit',
        'git -C "`touch pwned`" merge --abort',
    ],
)
async def test_raw_grammar_refuses_bypasses_before_an_all_clear_lexer(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    command: str,
) -> None:
    """Criterion 5: lexer bugs cannot turn arbitrary raw text into a merge grant."""
    handler, session, worktree = seat
    _start_conflicting_base_merge(worktree)
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)
    all_clear = ParsedShellCommand(segments=(("git", "commit", "--no-edit"),), operators=())

    with ExitStack() as patches:
        lexers: list[MagicMock] = []
        for module in (
            "gobby.config.shell_lexing",
            "gobby.config.validation_detection",
            "gobby.hooks.code_navigation_recovery",
            "gobby.workflows.code_review_scope",
            "gobby.workflows.engine.proxy_hooks",
            "gobby.workflows.found_work_gate",
            "gobby.workflows.base_merge_commits",
        ):
            lexers.append(
                patches.enter_context(
                    patch(f"{module}.parse_shell_command", return_value=all_clear, create=True)
                )
            )
        admission = await base_merge_admission(
            temp_db,
            session_id=session.id,
            project_id=session.project_id,
            project_path=str(worktree),
            cwd=str(worktree),
            command=command,
            tool_kind="shell",
            tool_input={},
        )
        assert not admission.git_step, command
        for lexer in lexers:
            lexer.assert_not_called()

    response = await handler._evaluate_rules(_bash_event(session, worktree, command))
    assert response.decision == "block", command


@pytest.mark.parametrize("with_cwd", [False, True], ids=["git", "git-C"])
@pytest.mark.parametrize(
    "arguments",
    [
        "merge integration",
        "merge --no-ff integration",
        "merge --no-edit integration",
        "merge --no-ff --no-edit integration",
        "merge --continue",
        "merge --abort",
        "add -- shared.txt",
        "add -- shared.txt ./shared.txt",
        "commit --no-edit",
        "commit -m 'Merge base: #25385 (task/main), safe_message.'",
        "commit --no-edit -m 'Merge base'",
    ],
)
async def test_closed_grammar_forms_are_admitted_during_base_merge(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    arguments: str,
    with_cwd: bool,
) -> None:
    """Every allowed spelling still passes the real worktree and merge-state checks."""
    _, session, worktree = seat
    if arguments.startswith("add "):
        _conflict_base_merge(worktree)
    elif arguments.startswith(("commit ", "merge --continue", "merge --abort")):
        _start_conflicting_base_merge(worktree)
    # A merge start runs on the clean task worktree, before any merge is in progress.
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)
    prefix = f"git -C {worktree}" if with_cwd else "git"

    admission = await base_merge_admission(
        temp_db,
        session_id=session.id,
        project_id=session.project_id,
        project_path=str(worktree),
        cwd=str(worktree),
        command=f"{prefix} {arguments}",
        tool_kind="shell",
        tool_input={},
    )

    assert admission.git_step, f"{prefix} {arguments}"


@pytest.mark.parametrize("arguments", ["commit --no-edit", "merge --abort"])
async def test_git_c_target_in_base_merge_is_admitted_from_main_checkout(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    arguments: str,
) -> None:
    _, session, worktree = seat
    _start_conflicting_base_merge(worktree)
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)
    main = Path(_git(worktree, "rev-parse", "--git-common-dir")).parent

    admission = await base_merge_admission(
        temp_db,
        session_id=session.id,
        project_id=session.project_id,
        project_path=str(main),
        cwd=str(main),
        command=f"git -C {worktree} {arguments}",
        tool_kind="shell",
        tool_input={},
    )

    assert admission.git_step


@pytest.mark.parametrize(
    "target_kind,arguments",
    [
        ("registered", "commit --no-edit"),
        ("registered", "merge --abort"),
        ("main", "commit --no-edit"),
        ("main", "merge --abort"),
        ("unregistered", "commit --no-edit"),
        ("unregistered", "merge --abort"),
        ("elsewhere", "merge integration"),
    ],
)
async def test_git_c_cannot_borrow_cwd_base_merge_admission(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    target_kind: str,
    arguments: str,
) -> None:
    _, session, worktree = seat
    _start_conflicting_base_merge(worktree)
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)
    main = Path(_git(worktree, "rev-parse", "--git-common-dir")).parent
    if target_kind == "elsewhere":
        target = Path("/elsewhere")
    elif target_kind == "main":
        target = main
    else:
        target = worktree.parent / "other-task"
        _git(main, "worktree", "add", "-q", "-b", "other-task", str(target), "integration")
        target = Path(_git(target, "rev-parse", "--show-toplevel"))
        if target_kind == "registered":
            task = LocalTaskManager(temp_db).create_task(
                project_id=session.project_id,
                title="Another task without a base merge",
                validation_criteria="It cannot borrow another worktree's merge admission.",
            )
            LocalWorktreeManager(temp_db).create(
                project_id=session.project_id,
                branch_name="other-task",
                worktree_path=str(target),
                base_branch="integration",
                task_id=task.id,
            )

    admission = await base_merge_admission(
        temp_db,
        session_id=session.id,
        project_id=session.project_id,
        project_path=str(worktree),
        cwd=str(worktree),
        command=f"git -C {target} {arguments}",
        tool_kind="shell",
        tool_input={},
    )

    assert not admission.git_step


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
    db: HubDatabase,
    session: Session,
    name: str,
    *,
    granted: bool,
    selectors: list[str],
    extra_capabilities: tuple[str, ...] = (),
    team_manager: bool = False,
) -> None:
    """A synthetic seat; only its capability list and rule selection matter to the gates."""
    AgentDefinitionManager(db).create(
        name,
        {
            "name": name,
            "prompts": {"agent": "Merge the integration branch into the task."},
            "workflows": {
                "rule_selectors": {"include": selectors},
                "variables": {"team_manager": team_manager},
            },
            "capabilities": [*([CAPABILITY] if granted else []), *extra_capabilities],
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

    stage = await handler._evaluate_rules(_bash_event(session, worktree, "git add -- shared.txt"))
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
    stage = await handler._evaluate_rules(_bash_event(session, worktree, "git add -- shared.txt"))
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


@pytest.mark.parametrize(
    "message",
    [
        '"$(touch unrelated.txt)"',
        '"`touch unrelated.txt`"',
        '"prefix $(touch unrelated.txt) suffix"',
        "\"$(printf %s 'literal'; touch unrelated.txt)\"",
        '"$\\\n(touch unrelated.txt)"',
        '"$((1 + $(touch unrelated.txt; printf 0)))"',
        "#'\ntouch unrelated.txt\n'",
    ],
)
@pytest.mark.parametrize("selectors", SELECTIONS, ids=SELECTION_IDS)
async def test_merge_commit_with_shell_expansion_is_refused(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    message: str,
    selectors: list[str],
) -> None:
    handler, session, worktree = seat
    _start_conflicting_base_merge(worktree)
    _seat(temp_db, session, "merge-seat", granted=True, selectors=selectors)

    response = await handler._evaluate_rules(
        _bash_event(session, worktree, f"git commit -m {message}")
    )

    assert response.decision == "block"


@pytest.mark.parametrize("message", ["'$(literal)'", "'`literal`'", r'"\$(literal)"'])
async def test_merge_commit_with_literal_substitution_outside_safe_charset_is_refused(
    seat: tuple[WorkflowHookHandler, Session, Path], temp_db: HubDatabase, message: str
) -> None:
    handler, session, worktree = seat
    _start_conflicting_base_merge(worktree)
    _seat(temp_db, session, "merge-seat", granted=True, selectors=GATED_RULES)

    response = await handler._evaluate_rules(
        _bash_event(session, worktree, f"git commit -m {message}")
    )

    assert response.decision == "block"


@pytest.mark.parametrize("tool", ["Edit", "Write"])
@pytest.mark.parametrize(
    "condition,allowed",
    [
        ("unmerged", True),
        ("ordinary-path", False),
        ("resolved-and-staged", False),
        ("merge-aborted", False),
        ("unregistered", False),
        ("ungranted", False),
        ("unadmitted-base", False),
    ],
)
async def test_conflict_write_overrides_native_block_only_with_scoped_grant(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    tool: str,
    condition: str,
    allowed: bool,
) -> None:
    """A blocked-tool seat gets only an admitted merge's currently unmerged file."""
    handler, session, worktree = seat
    _seat(temp_db, session, "custom-seat", granted=condition != "ungranted", selectors=GATED_RULES)
    SessionVariableManager(temp_db).set_variable(
        session.id, "_agent_blocked_tools", ["Edit", "Write"]
    )
    if condition == "unadmitted-base":
        _git(worktree, "branch", "untrusted", "feature")
        _git(worktree, "checkout", "untrusted")
        _commit(worktree, "shared.txt", "untrusted\n", "untrusted change")
        _git(worktree, "checkout", "task-25385")
        _conflict_base_merge(worktree, "untrusted")
    else:
        _conflict_base_merge(worktree)
    if condition == "merge-aborted":
        _git(worktree, "merge", "--abort")
    if condition == "resolved-and-staged":
        (worktree / "shared.txt").write_text("resolved\n")
        _git(worktree, "add", "shared.txt")
        assert _git(worktree, "rev-parse", "--verify", "MERGE_HEAD")
        assert _git(worktree, "diff", "--name-only", "--diff-filter=U") == ""
    if condition == "unregistered":
        temp_db.execute("DELETE FROM worktrees WHERE worktree_path = %s", (str(worktree),))
    path = worktree / ("task.txt" if condition == "ordinary-path" else "shared.txt")
    response = await handler._evaluate_rules(
        _tool_event(
            session,
            worktree,
            tool,
            {
                "file_path": str(path),
                "old_string": "<<<<<<< HEAD",
                "new_string": "resolved",
                "content": "resolved\n",
            },
        )
    )
    assert response.decision == ("allow" if allowed else "block"), response.reason
    assert not SessionVariableManager(temp_db).get_variables(session.id).get("task_claimed")


@pytest.mark.parametrize(
    "condition,allowed",
    [
        ("authored-base-merge", True),
        ("non-merge", False),
        ("not-latest", False),
        ("unadmitted-parent", False),
        ("ungranted", False),
        ("other-author", False),
        ("unregistered", False),
    ],
)
async def test_claim_free_base_merge_candidate_binding(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    condition: str,
    allowed: bool,
) -> None:
    """Reviewer intake admits only the latest linked base merge authored by the granted sender."""
    handler, session, worktree = seat
    _seat(temp_db, session, "custom-seat", granted=condition != "ungranted", selectors=GATED_RULES)
    record = LocalWorktreeManager(temp_db).get_by_path(str(worktree))
    assert record is not None and record.task_id is not None
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(record.task_id)
    assert task.claimed_by_session_id is None
    if condition == "unadmitted-parent":
        _git(worktree, "branch", "untrusted", "feature")
        _git(worktree, "checkout", "untrusted")
        _commit(worktree, "shared.txt", "untrusted\n", "untrusted change")
        _git(worktree, "checkout", "task-25385")
        _conflict_base_merge(worktree, "untrusted")
        (worktree / "shared.txt").write_text("resolved\n")
        _git(worktree, "add", "shared.txt")
    elif condition != "non-merge":
        _start_conflicting_base_merge(worktree)
    if condition != "non-merge":
        # Deliver the real before/after commit events: authorship must come from
        # the admitted operation, never just the sender's capability or NOTE.
        author = session
        if condition == "other-author":
            author = SessionManager(temp_db).register(
                external_id="other-merge-author",
                machine_id=LOCAL_MACHINE_ID,
                source="claude",
                project_id=session.project_id,
            )
            _seat(temp_db, author, "other-capable-seat", granted=True, selectors=GATED_RULES)
        await handler._evaluate_rules(_bash_event(author, worktree, "git commit --no-edit"))
        output = _git(worktree, "commit", "--no-edit")
        completed = _bash_event(author, worktree, "git commit --no-edit", HookEventType.AFTER_TOOL)
        completed.data["tool_output"] = output
        await handler._evaluate_rules(completed)
    sha = _git(worktree, "rev-parse", "HEAD")
    tasks.link_commit(task.id, sha)
    if condition == "not-latest":
        _commit(worktree, "later.txt", "later\n", "later linked work")
        tasks.link_commit(task.id, _git(worktree, "rev-parse", "HEAD"))
    if condition == "unregistered":
        temp_db.execute("DELETE FROM worktrees WHERE id = %s", (record.id,))
    reviewer = SessionManager(temp_db).register(
        external_id="merge-reviewer",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=session.project_id,
    )
    message = InterSessionMessageManager(temp_db).create_message(
        from_session=session.id,
        to_session=reviewer.id,
        content=(
            f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=base-merge WT={worktree} "
            f"COMMIT={sha} NOTE=base merge"
        ),
    )
    event = candidate_event(temp_db, message)
    assert event is not None
    error = _candidate_rejection(temp_db, {}, event, reviewer.id)
    assert (error is None) == allowed, error
    assert tasks.get_task(task.id).claimed_by_session_id is None


@pytest.mark.parametrize("completion", ["clean", "continue", "continue-c"])
async def test_clean_base_merge_candidate_binds_after_its_authored_merge(
    seat: tuple[WorkflowHookHandler, Session, Path],
    temp_db: HubDatabase,
    tmp_path: Path,
    completion: str,
) -> None:
    """Clean and conflict-resolved admitted merges bind at reviewer intake."""
    handler, session, worktree = seat
    _seat(temp_db, session, "custom-seat", granted=True, selectors=GATED_RULES)
    # `feature` shares the base but not the task's conflicting shared.txt edit, so the
    # integration merge below creates a commit with no conflict.
    _git(worktree, "checkout", "-q", "-b", "clean-task", "feature")
    _commit(worktree, "task2.txt", "clean task\n", "clean task file")
    _commit(tmp_path / "main", "clean.txt", "clean\n", "integration clean file")
    record = LocalWorktreeManager(temp_db).get_by_path(str(worktree))
    assert record is not None and record.task_id is not None
    tasks = LocalTaskManager(temp_db)
    task = tasks.get_task(record.task_id)
    if completion != "clean":
        _git(worktree, "checkout", "-q", "task-25385")
        _start_conflicting_base_merge(worktree)
        _git(worktree, "config", "core.editor", "true")
    args = (
        ["merge", "--no-ff", "--no-edit", "integration"]
        if completion == "clean"
        else ["merge", "--continue"]
    )
    command = "git " + " ".join(args)
    event_cwd = worktree
    if completion == "continue-c":
        command = f"git -C {worktree} " + " ".join(args)
        event_cwd = tmp_path / "main"
    await handler._evaluate_rules(_bash_event(session, event_cwd, command))
    output = _git(worktree, *args)
    completed = _bash_event(session, event_cwd, command, HookEventType.AFTER_TOOL)
    completed.data["tool_output"] = output
    await handler._evaluate_rules(completed)
    sha = _git(worktree, "rev-parse", "HEAD")
    tasks.link_commit(task.id, sha)
    reviewer = SessionManager(temp_db).register(
        external_id="merge-reviewer",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=session.project_id,
    )
    message = InterSessionMessageManager(temp_db).create_message(
        from_session=session.id,
        to_session=reviewer.id,
        content=(
            f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=base-merge WT={worktree} "
            f"COMMIT={sha} NOTE=clean base merge"
        ),
    )
    event = candidate_event(temp_db, message)
    assert event is not None
    error = _candidate_rejection(temp_db, {}, event, reviewer.id)
    assert error is None, error


@pytest.mark.parametrize("granted", [True, False], ids=["merge-capability", "no-capability"])
async def test_team_worktree_scope_is_static_in_srt_launch_policy(
    landing: tuple[LandingCase, WorkflowHookHandler, Session, Path, Task],
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    granted: bool,
) -> None:
    """The merge capability grants its bound team's checkout before any merge starts."""
    case, _, session, worktree, task = landing
    # Match the managed checkout layout while keeping the operator's HOME untouched.
    home = tmp_path / "home"
    managed = home / ".gobby" / "worktrees" / "gobby" / f"task-{task.seq_num}-base-merge"
    managed.parent.mkdir(parents=True)
    _git(case.repo, "worktree", "move", str(worktree), str(managed))
    temp_db.execute(
        "UPDATE worktrees SET worktree_path = %s WHERE task_id = %s",
        (str(managed.resolve()), task.id),
    )
    worktree = managed.resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    case.manager_for(task, session=session.id)
    registered = LocalWorktreeManager(temp_db).get_by_task(task.id)
    assert registered is not None
    run = temp_db.execute(
        "SELECT id FROM agent_runs WHERE child_session_id = %s AND task_id = %s",
        (session.id, task.id),
    ).fetchone()
    assert run is not None
    temp_db.execute(
        "UPDATE agent_runs SET worktree_id = %s WHERE id = %s", (registered.id, run["id"])
    )
    temp_db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (run["id"], session.id))
    assignment = LocalWorktreeManager(temp_db).get_for_session_run(session.id)
    assert assignment is not None and assignment.worktree_path == str(worktree)
    _seat(temp_db, session, "custom-seat", granted=granted, selectors=GATED_RULES)
    assert not _git(worktree, "ls-files", "--unmerged")
    assert not (Path(_git(worktree, "rev-parse", "--absolute-git-dir")) / "MERGE_HEAD").exists()

    unrelated_task = case.task("Another team's work")
    unrelated = managed.parent / "unrelated-task"
    _git(case.repo, "worktree", "add", "-q", "-b", "unrelated-team", str(unrelated))
    LocalWorktreeManager(temp_db).create(
        project_id=case.project_id,
        branch_name="unrelated-team",
        worktree_path=str(unrelated.resolve()),
        base_branch="trunk",
        task_id=unrelated_task.id,
    )
    private = home / ".gobby" / "agents_workspaces"
    with patch("gobby.agents.agent_workspaces.workspaces_root", return_value=private):
        scoped = grant_seat_workspace_paths(
            SandboxConfig(backend="srt"), temp_db, session.id, "custom-seat"
        )
        # The manager launches inside its own granted folder, while the task
        # checkout belongs to a different seat.
        paths = await compute_sandbox_paths(scoped, scoped.extra_write_paths[0])
        filesystem = render_srt_settings(paths)["filesystem"]
    writes = filesystem["allowWrite"]
    # SRT's launch grant is static. The live BEFORE_TOOL rule matrix above
    # owns path and MERGE_HEAD restrictions inside this checkout.
    assert (str(worktree) in writes) == granted
    assert str(worktree.parent) not in writes
    assert str(unrelated.resolve()) not in writes


@pytest.mark.parametrize(
    "sender_kind,claim_state,linked",
    [
        (sender, claim, linked)
        for sender in ("team-manager", "root-coordinator")
        for claim in ("claimed", "released", "never-claimed")
        for linked in (True, False)
    ]
    + [
        ("unbound-manager", "never-claimed", True),
        ("other-team-manager", "never-claimed", True),
        ("team-developer", "never-claimed", True),
    ],
)
async def test_base_merge_candidate_sender_claim_and_link_matrix(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _local_machine_identity: None,
    sender_kind: str,
    claim_state: str,
    linked: bool,
) -> None:
    """A coordinator's authored base merge binds independently of the maker's claim lifecycle."""
    case = make_landing_case(temp_db, sample_project, tmp_path)
    case.direct({"shared.txt": "base\n"})
    world = make_team_world(case, tmp_path, monkeypatch)
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        case.project_id, "Merge candidate bindings", validation_criteria="Bind."
    )
    runbook = world.child(task)
    binding = await world.admit(runbook, task)
    assert binding.worktree_path is not None
    worktree = Path(binding.worktree_path).resolve()
    handler, root = _register_seat(temp_db, case.project_id)

    if claim_state != "never-claimed":
        tasks.claim_task(task.id, case.claimant)
        if claim_state == "released":
            tasks.release_task_claim(task.id)
    manager = sender_kind in {"team-manager", "unbound-manager", "other-team-manager"}
    if sender_kind == "root-coordinator":
        sender = root
    else:
        parent = runbook
        managed_task = task
        if sender_kind == "other-team-manager":
            managed_task = tasks.create_task(
                case.project_id, "Other team", validation_criteria="Separate team."
            )
            parent = world.child(managed_task)
            await world.admit(parent, managed_task)
        parent_id = case.creator if sender_kind == "unbound-manager" else parent.id
        sender_id = case.session(sender_kind, parent=parent_id)
        if manager and sender_kind != "unbound-manager":
            bind_task_manager(
                temp_db,
                manager=sender_id,
                parent=parent_id,
                project_id=case.project_id,
                task=managed_task,
            )
        else:
            LocalAgentRunManager(temp_db).create(
                parent_session_id=parent_id,
                provider="claude",
                prompt="Candidate sender.",
                agent_name=sender_kind,
                child_session_id=sender_id,
                task_id=task.id if sender_kind == "team-developer" else None,
            )
        loaded_sender = SessionManager(temp_db).get(sender_id)
        assert loaded_sender is not None
        sender = loaded_sender
    extra = (
        ("manage_task_claims",)
        if manager
        else (("coordinate_tasks",) if sender_kind == "root-coordinator" else ("implementation",))
    )
    _seat(
        temp_db,
        sender,
        f"synthetic-{sender_kind}",
        granted=sender_kind != "team-developer",
        selectors=GATED_RULES,
        extra_capabilities=extra,
        team_manager=manager,
    )
    reviewer_id = case.session("merge-intake-reviewer", parent=runbook.id)
    LocalAgentRunManager(temp_db).create(
        parent_session_id=runbook.id,
        provider="claude",
        prompt="Review candidate.",
        agent_name="synthetic-reviewer",
        child_session_id=reviewer_id,
        task_id=task.id,
    )

    case.direct({"shared.txt": "integration edit\n"})
    _commit(worktree, "shared.txt", "task edit\n", "task edit")
    _conflict_base_merge(worktree, "trunk")
    (worktree / "shared.txt").write_text("resolved\n")
    _git(worktree, "add", "shared.txt")
    await handler._evaluate_rules(_bash_event(sender, worktree, "git commit --no-edit"))
    output = _git(worktree, "commit", "--no-edit")
    completed = _bash_event(sender, worktree, "git commit --no-edit", HookEventType.AFTER_TOOL)
    completed.data["tool_output"] = output
    await handler._evaluate_rules(completed)
    sha = _git(worktree, "rev-parse", "HEAD")
    if linked:
        tasks.link_commit(task.id, sha)
    message = InterSessionMessageManager(temp_db).create_message(
        from_session=sender.id,
        to_session=reviewer_id,
        content=(
            f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN={binding.team_id} "
            f"WT={worktree} COMMIT={sha} NOTE=authored base merge"
        ),
    )
    event = candidate_event(temp_db, message)
    assert event is not None
    error = _candidate_rejection(temp_db, {}, event, reviewer_id)
    allowed = linked and sender_kind in {"team-manager", "root-coordinator"}
    assert (error is None) == allowed, error
    expected_holder = case.claimant if claim_state == "claimed" else None
    assert tasks.get_task(task.id).claimed_by_session_id == expected_holder
    assert sender.id != expected_holder
