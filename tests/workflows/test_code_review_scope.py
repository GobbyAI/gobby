"""Commit path scope behind the code-review gates."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.workflows.code_review_scope import (
    commit_has_reviewable_paths,
    inspect_commit_review_scope,
    parse_commit_scope,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("agent_type", ["plan-writer", "custom-seat"])
@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        ".gobby/plans/coverage/project/24231/plan.coverage.yaml",
        ".gobby/plans/coverage/.regenerate.log",
    ],
)
async def test_coverage_review_scope_uses_definition_grant(
    repo: Path,
    gate_handler: tuple[WorkflowHookHandler, Session, Project],
    temp_db: HubDatabase,
    agent_type: str,
    granted: bool,
    enabled: bool,
    path: str,
) -> None:
    _, session, project = gate_handler
    _plan_definition(temp_db, session, agent_type, granted=granted, enabled=enabled)
    artifact = repo / path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("coverage: pending\n", encoding="utf-8")
    _git(repo, "add", path)
    variables: dict[str, object] = {
        "_agent_type": agent_type,
        "task_claimed": True,
        "session_dirty_files": {str(repo): [path]},
    }
    scope = await inspect_commit_review_scope(
        _gate_event("git commit -m coverage", session, project, repo),
        str(repo),
        variables,
        db=temp_db,
    )
    assert scope.has_reviewable_paths is not (granted and enabled)


def _plan_definition(
    db: HubDatabase, session: Session, name: str, *, granted: bool, enabled: bool = True
) -> None:
    AgentDefinitionManager(db).create(
        name,
        {
            "name": name,
            "prompts": {"agent": "Maintain the assigned plan."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": (
                ["write_plan_artifacts_without_task", "commit_plan_artifacts_without_review"]
                if granted
                else []
            ),
        },
        project_id=session.project_id,
        enabled=enabled,
    )
    SessionVariableManager(db).set_variable(session.id, "_agent_type", name)


SESSION_ID = "33333333-3333-4333-8333-333333333333"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    (repo_path / "docs").mkdir(parents=True)
    (repo_path / "src").mkdir()
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.email", "tests@gobby.local")
    _git(repo_path, "config", "user.name", "Gobby Tests")
    (repo_path / "docs" / "guide.md").write_text("base guide\n", encoding="utf-8")
    (repo_path / "docs" / "notes.txt").write_text("base notes\n", encoding="utf-8")
    (repo_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo_path / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "base")
    return repo_path


def _event(command: str, cwd: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(cwd),
        project_id=None,
        data={"tool_name": "Bash", "tool_input": {"command": command}},
        metadata={},
    )


async def _reviewable(command: str, repo: Path, *, cwd: Path | None = None) -> bool:
    return await commit_has_reviewable_paths(_event(command, cwd or repo), str(repo))


def _edit(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def test_documentation_only_staged_commit_needs_no_review(repo: Path) -> None:
    _edit(repo, "docs/guide.md", "changed guide\n")
    _edit(repo, "docs/notes.txt", "changed notes\n")
    _git(repo, "add", "--", "docs/guide.md", "docs/notes.txt")

    assert await _reviewable("git commit -m docs", repo) is False


async def test_mixed_staged_commit_stays_gated(repo: Path) -> None:
    _edit(repo, "docs/guide.md", "changed guide\n")
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "-A")

    assert await _reviewable("git commit -m mixed", repo) is True


async def test_code_only_staged_commit_stays_gated(repo: Path) -> None:
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "--", "src/app.py")

    assert await _reviewable("git commit -m code", repo) is True


async def test_unknown_extension_stays_gated(repo: Path) -> None:
    _edit(repo, "data/blob.xyz", "payload\n")
    _git(repo, "add", "-A")

    assert await _reviewable("git commit -m blob", repo) is True


async def test_extensionless_path_stays_gated(repo: Path) -> None:
    _edit(repo, "Makefile", "all:\n\t@false\n")
    _git(repo, "add", "--", "Makefile")

    assert await _reviewable("git commit -m build", repo) is True


async def test_uppercase_documentation_extension_needs_no_review(repo: Path) -> None:
    _edit(repo, "READ.MD", "shouting docs\n")
    _git(repo, "add", "--", "READ.MD")

    assert await _reviewable("git commit -m docs", repo) is False


async def test_pathspec_commit_reads_only_its_own_paths(repo: Path) -> None:
    _edit(repo, "docs/guide.md", "changed guide\n")
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "-A")

    # The staged code stays staged; only the documentation path is recorded.
    assert await _reviewable("git commit docs/guide.md -m docs", repo) is False
    assert await _reviewable("git commit -m code -- src/app.py", repo) is True


async def test_pathspec_commit_reads_unstaged_working_tree_content(repo: Path) -> None:
    _edit(repo, "src/app.py", "value = 2\n")

    # Only mode records working-tree content, staged or not.
    assert await _reviewable("git commit src -m code", repo) is True


async def test_include_pathspec_commit_also_reads_staged_paths(repo: Path) -> None:
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "--", "src/app.py")
    _edit(repo, "docs/guide.md", "changed guide\n")

    assert await _reviewable("git commit -i docs/guide.md -m mixed", repo) is True


async def test_cd_tail_wrapped_only_commit_excludes_unrelated_staged_code(repo: Path) -> None:
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "--", "src/app.py")
    _edit(repo, "docs/guide.md", "changed guide\n")
    wrapper = f"cd {repo} && git commit --only -m docs -- docs/guide.md 2>&1 | tail -20"

    assert await _reviewable(wrapper, repo, cwd=repo.parent) is False
    assert await _reviewable(wrapper.replace("--only", "--include"), repo, cwd=repo.parent)


async def test_chained_add_then_pathspec_commit_of_new_documentation_needs_no_review(
    repo: Path,
) -> None:
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "--", "src/app.py")
    _edit(repo, "docs/new.md", "new page\n")

    # The add stages the untracked page; only mode leaves the staged code out.
    assert (
        await _reviewable("git add -- docs/new.md && git commit -m d -- docs/new.md", repo) is False
    )


async def test_chained_add_then_heredoc_message_commit_needs_no_review(repo: Path) -> None:
    _edit(repo, "docs/new.md", "new page\n")
    command = "git add -- docs/new.md && git commit -F - -- docs/new.md <<'EOF'\ndocs\nEOF"

    assert await _reviewable(command, repo) is False


async def test_chained_add_then_pathspec_commit_of_new_code_stays_gated(repo: Path) -> None:
    _edit(repo, "src/new.py", "value = 3\n")

    assert await _reviewable("git add -- src/new.py && git commit -m c -- src/new.py", repo) is True


async def test_chained_commit_records_only_untracked_paths_the_add_stages(repo: Path) -> None:
    _edit(repo, "docs/new.md", "new page\n")
    _edit(repo, "docs/tool.py", "value = 4\n")

    # The untracked code under the commit pathspec is never added, so never recorded.
    assert await _reviewable("git add docs/new.md && git commit -m d docs", repo) is False
    assert await _reviewable("git add docs && git commit -m d docs", repo) is True


async def test_commit_all_reads_unstaged_tracked_changes(repo: Path) -> None:
    _edit(repo, "docs/guide.md", "changed guide\n")
    assert await _reviewable("git commit -am docs", repo) is False

    _edit(repo, "src/app.py", "value = 2\n")
    assert await _reviewable("git commit -am mixed", repo) is True


async def test_commit_all_reads_staged_changes_too(repo: Path) -> None:
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "--", "src/app.py")
    _edit(repo, "docs/guide.md", "changed guide\n")

    assert await _reviewable("git commit -a -m mixed", repo) is True


async def test_staged_code_deletion_stays_gated(repo: Path) -> None:
    _git(repo, "rm", "-q", "--", "src/app.py")
    _edit(repo, "docs/guide.md", "changed guide\n")
    _git(repo, "add", "--", "docs/guide.md")

    assert await _reviewable("git commit -m cleanup", repo) is True


async def test_rename_of_code_to_documentation_stays_gated(repo: Path) -> None:
    _git(repo, "mv", "src/app.py", "docs/app.md")

    assert await _reviewable("git commit -m move", repo) is True


async def test_empty_commit_stays_gated(repo: Path) -> None:
    assert await _reviewable("git commit --allow-empty -m empty", repo) is True


def _prepare_reviewed_integration(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "0.5.0")
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "reviewed integration change")
    _git(repo, "branch", "reviewed-alias")
    _git(repo, "checkout", "-q", "-b", "lane", base)


@pytest.mark.parametrize("target", ["0.5.0", "reviewed-alias"])
async def test_reviewed_fast_forward_needs_no_review(repo: Path, target: str) -> None:
    _prepare_reviewed_integration(repo)

    scope = await inspect_commit_review_scope(
        _event(f"git merge --ff-only {target}", repo),
        str(repo),
        integration_branch="0.5.0",
    )

    assert scope.has_reviewable_paths is False


@pytest.mark.parametrize(
    "command",
    [
        "git merge --ff-only unreviewed",
        "git merge --no-ff 0.5.0",
        "git merge 0.5.0",
        "git merge --ff-only 0.5.0 unreviewed",
        "git merge --ff-only missing",
        "git merge --ff-only 0.5.0 && git commit --allow-empty -m other",
        "git add src/app.py && git merge --ff-only 0.5.0",
        "git merge --ff-only $TARGET",
        "git merge --ff-only 0.5.0 > result.txt",
        "git -c core.hooksPath=elsewhere merge --ff-only 0.5.0",
    ],
)
async def test_unproven_fast_forward_stays_gated(repo: Path, command: str) -> None:
    _prepare_reviewed_integration(repo)
    _git(repo, "checkout", "-q", "-b", "unreviewed", "0.5.0")
    _edit(repo, "src/app.py", "value = 3\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "unreviewed change")
    _git(repo, "checkout", "-q", "lane")

    scope = await inspect_commit_review_scope(
        _event(command, repo), str(repo), integration_branch="0.5.0"
    )

    assert scope.has_reviewable_paths is True


async def test_divergent_fast_forward_stays_gated(repo: Path) -> None:
    _prepare_reviewed_integration(repo)
    _edit(repo, "src/app.py", "value = 3\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "divergent lane change")

    scope = await inspect_commit_review_scope(
        _event("git merge --ff-only 0.5.0", repo), str(repo), integration_branch="0.5.0"
    )

    assert scope.has_reviewable_paths is True


@pytest.mark.parametrize("integration_branch", [None, "missing"])
async def test_unknown_integration_branch_stays_gated(
    repo: Path, integration_branch: str | None
) -> None:
    _prepare_reviewed_integration(repo)

    scope = await inspect_commit_review_scope(
        _event("git merge --ff-only 0.5.0", repo),
        str(repo),
        integration_branch=integration_branch,
    )

    assert scope.has_reviewable_paths is True


@pytest.mark.parametrize("staged_path", ["src/app.py", "docs/guide.md"])
async def test_reviewed_fast_forward_with_staged_work_stays_gated(
    repo: Path, staged_path: str
) -> None:
    _prepare_reviewed_integration(repo)
    _edit(repo, staged_path, "staged work\n")
    _git(repo, "add", staged_path)

    scope = await inspect_commit_review_scope(
        _event("git merge --ff-only 0.5.0", repo), str(repo), integration_branch="0.5.0"
    )

    assert scope.has_reviewable_paths is True


async def test_first_commit_of_a_repository_reads_its_staged_documentation(
    tmp_path: Path,
) -> None:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q")
    _git(unborn, "config", "user.email", "tests@gobby.local")
    _git(unborn, "config", "user.name", "Gobby Tests")
    (unborn / "README.md").write_text("first\n", encoding="utf-8")
    _git(unborn, "add", "-A")

    assert await _reviewable("git commit -m docs", unborn) is False


async def test_chdir_commit_is_inspected_in_that_checkout(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "tests@gobby.local")
    _git(other, "config", "user.name", "Gobby Tests")
    (other / "notes.md").write_text("other notes\n", encoding="utf-8")
    _git(other, "add", "-A")

    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "--", "src/app.py")

    # The staged code lives in `repo`; the commit records `other`'s docs.
    assert await _reviewable(f"git -C {other} commit -m docs", repo) is False


async def test_subdirectory_commit_reads_the_whole_staged_set(repo: Path) -> None:
    _edit(repo, "docs/guide.md", "changed guide\n")
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "-A")

    # Run from docs/: git still records every staged path, including the code.
    assert await _reviewable("git commit -m mixed", repo, cwd=repo / "docs") is True


async def test_git_failure_stays_gated(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _edit(repo, "docs/guide.md", "changed guide\n")
    _git(repo, "add", "--", "docs/guide.md")

    assert await _reviewable("git commit -m docs", repo, cwd=outside) is True


@pytest.mark.parametrize(
    "command",
    [
        "git add src/app.py && git commit -m mixed",
        "git add -A && git commit -m docs -- docs/guide.md",
        "git add -- 'docs/*.md' && git commit -m docs -- docs/guide.md",
        "git add -- docs/guide.md && git commit -i -m docs -- docs/guide.md",
        "git add -- docs/guide.md; git commit -m docs -- docs/guide.md",
        "git rm -- src/app.py && git commit -m docs -- docs/guide.md",
        "git -C other add docs/guide.md && git commit -m docs -- docs/guide.md",
        "cd other && git commit -m docs",
        "git commit -m docs | tee log.txt",
        "git commit --amend --no-edit",
        "git commit -p -m docs",
        "git commit --pathspec-from-file=paths -m docs",
        "git commit --badflag -m docs",
        "git merge --no-ff feature",
        "git cherry-pick abc1234",
        "git revert --no-edit abc1234",
        "sudo git commit -m docs",
        "GIT_ICASE_PATHSPECS=1 git commit docs -m docs",
        "git --work-tree=/elsewhere commit -m docs",
        "git -C 'literal`directory' commit -m docs -- docs/guide.md",
        "git commit -a docs/guide.md -m docs",
        "",
    ],
)
async def test_undeterminable_commands_stay_gated(repo: Path, command: str) -> None:
    _edit(repo, "docs/guide.md", "changed guide\n")
    _git(repo, "add", "--", "docs/guide.md")

    assert await _reviewable(command, repo) is True


@pytest.fixture
def gate_handler(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    repo: Path,
    _local_machine_identity: None,
) -> tuple[WorkflowHookHandler, Session, Project]:
    """A registered session for native code-review scope inspection."""
    project = isolated_checkout_factory(temp_db, "code-review-scope-test", root=repo).project
    session_manager = SessionManager(temp_db)
    session = session_manager.register(
        external_id="scope-external",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project.id,
    )

    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        session_manager=session_manager,
    )
    return handler, session, project


def _gate_event(command: str, session: Session, project: Project, repo: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session.external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(repo),
        project_id=project.id,
        data={"tool_name": "Bash", "tool_input": {"command": command}},
        metadata={"_platform_session_id": session.id, "project_path": str(repo)},
    )


@pytest.mark.asyncio
async def test_commit_review_scope_intersects_recorded_paths_with_session_ledger(
    repo: Path,
) -> None:
    _edit(repo, "src/owned.py", "owned = True\n")
    _edit(repo, "src/foreign.py", "foreign = True\n")
    _git(repo, "add", "-A")
    variables = {
        "session_dirty_files": ["src/owned.py"],
        "session_dirty_file_checkouts": {str(repo): ["src/owned.py"]},
    }

    scope = await inspect_commit_review_scope(
        _event("git commit -m mixed", repo),
        str(repo),
        variables,
    )

    assert scope.has_reviewable_paths is True
    assert scope.session_owned_reviewable_paths == ("src/owned.py",)


@pytest.mark.parametrize(
    ("command", "pathspecs", "all_tracked", "include_staged"),
    [
        ("git commit -m docs", (), False, False),
        ("git commit -q --no-verify -S -uno -m docs", (), False, False),
        ("git commit -am docs", (), True, False),
        ("git commit --message=docs docs/guide.md", ("docs/guide.md",), False, False),
        (
            "git commit -F msg.txt -- docs/guide.md src/app.py",
            ("docs/guide.md", "src/app.py"),
            False,
            False,
        ),
        ("git commit -i docs/guide.md -m docs", ("docs/guide.md",), False, True),
        ("git commit -i -m docs", (), False, False),
    ],
)
def test_parsed_commit_scope(
    command: str,
    pathspecs: tuple[str, ...],
    all_tracked: bool,
    include_staged: bool,
) -> None:
    scope = parse_commit_scope(command)

    assert scope is not None
    assert scope.pathspecs == pathspecs
    assert scope.all_tracked is all_tracked
    assert scope.include_staged is include_staged
