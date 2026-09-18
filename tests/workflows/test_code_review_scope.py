"""Commit path scope behind the code-review gates."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.workflows.code_review_scope import (
    commit_has_reviewable_paths,
    parse_commit_scope,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

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
    """The real engine with only the two code-review gates enabled."""
    project = isolated_checkout_factory(temp_db, "code-review-scope-test", root=repo).project
    session_manager = SessionManager(temp_db)
    session = session_manager.register(
        external_id="scope-external",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project.id,
    )

    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    temp_db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s))",
        ("require-code-review-skill", "require-code-review-self-review"),
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


async def test_gate_allows_a_documentation_only_commit(
    gate_handler: tuple[WorkflowHookHandler, Session, Project],
    repo: Path,
) -> None:
    handler, session, project = gate_handler
    _edit(repo, "docs/guide.md", "changed guide\n")
    _git(repo, "add", "--", "docs/guide.md")

    response = await handler._evaluate_rules(
        _gate_event("git commit -m docs", session, project, repo)
    )

    assert response.decision == "allow"


async def test_gate_blocks_a_commit_that_records_code(
    gate_handler: tuple[WorkflowHookHandler, Session, Project],
    repo: Path,
) -> None:
    handler, session, project = gate_handler
    _edit(repo, "docs/guide.md", "changed guide\n")
    _edit(repo, "src/app.py", "value = 2\n")
    _git(repo, "add", "-A")

    response = await handler._evaluate_rules(
        _gate_event("git commit -m mixed", session, project, repo)
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "code-review" in response.reason


@pytest.mark.parametrize(
    ("command", "pathspecs", "all_tracked", "include_staged"),
    [
        ("git commit -m docs", (), False, False),
        ('git commit -m "$(cat <<EOF\ndocs\nEOF\n)"', (), False, False),
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
