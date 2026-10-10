"""Sequencer recovery has a known worktree scope without weakening git guards."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_rule_file

pytestmark = pytest.mark.unit
SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def pending_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    _git("init", "-q", str(repository))
    _git(
        "-C",
        str(repository),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "Initial",
    )
    _git("-C", str(repository), "worktree", "add", "--detach", "-q", str(worktree), "HEAD")
    revert_head = Path(_git("-C", str(worktree), "rev-parse", "--git-path", "REVERT_HEAD"))
    revert_head.write_text(_git("-C", str(worktree), "rev-parse", "HEAD") + "\n")
    return worktree, revert_head


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    rules = get_bundled_rules_path()
    for relative_path in (
        "task-enforcement/block-unresolved-scope-shell-write.yaml",
        "worker-safety/no-destructive-git-interactive.yaml",
    ):
        result = sync_rule_file(temp_db, rules / relative_path, tag="gobby")
        assert result["success"] is True, result
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return RuleEngine(temp_db)


@pytest.fixture
def destructive_engine(temp_db: HubDatabase) -> RuleEngine:
    result = sync_rule_file(
        temp_db,
        get_bundled_rules_path() / "worker-safety/no-destructive-git-interactive.yaml",
        tag="gobby",
    )
    assert result["success"] is True, result
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return RuleEngine(temp_db)


def _event(command: str, root: Path) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(root)},
        "project_path": str(root),
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )


CLAIMED_VARIABLES: dict[str, Any] = {
    "require_task_before_edit": True,
    "task_claimed": True,
    "plan_mode": False,
    "is_spawned_agent": False,
    "loaded_skills": ["python", "restraint"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_directory", [False, True], ids=["cwd", "literal-C"])
async def test_claimed_pending_revert_can_quit(
    engine: RuleEngine, pending_worktree: tuple[Path, Path], explicit_directory: bool
) -> None:
    root, revert_head = pending_worktree
    assert revert_head.exists()
    command = f"git -C {root} revert --quit" if explicit_directory else "git revert --quit"
    event = _event(command, root)
    response = await engine.evaluate(
        event, session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert response.decision == "allow", response.reason
    assert event.data["canonical_repo_mutation"] is True
    assert event.data["canonical_file_paths"] == [str(root)]
    assert not event.data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_directory", [False, True], ids=["cwd", "literal-C"])
async def test_plain_cherry_pick_is_not_scope_blocked(
    engine: RuleEngine, pending_worktree: tuple[Path, Path], explicit_directory: bool
) -> None:
    root, _ = pending_worktree
    commit = _git("-C", str(root), "rev-parse", "HEAD")
    command = f"git cherry-pick {commit}"
    if explicit_directory:
        command = command.replace("git ", f"git -C {root} ", 1)
    event = _event(command, root)
    response = await engine.evaluate(
        event, session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert response.decision == "allow", response.reason
    assert event.data["canonical_tool_kind"] == "execute"
    assert not event.data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["{}", "({})", "result=$({})", "result=`{}`"])
@pytest.mark.parametrize("explicit_directory", [False, True], ids=["cwd", "literal-C"])
@pytest.mark.parametrize(
    "command", ["git revert --abort", "git cherry-pick --abort", "git reset --hard"]
)
async def test_destructive_recovery_remains_denied(
    destructive_engine: RuleEngine,
    pending_worktree: tuple[Path, Path],
    command: str,
    explicit_directory: bool,
    wrapper: str,
) -> None:
    root, _ = pending_worktree
    if explicit_directory:
        command = command.replace("git ", f"git -C {root} ", 1)
    command = wrapper.format(command)
    response = await destructive_engine.evaluate(
        _event(command, root), session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert response.decision == "block"
    assert "no-destructive-git-interactive" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ['git -C "$WT" revert --quit', 'git checkout -- "$FILE"'])
async def test_dynamic_git_scope_remains_denied(
    engine: RuleEngine, pending_worktree: tuple[Path, Path], command: str
) -> None:
    root, _ = pending_worktree
    event = _event(command, root)
    response = await engine.evaluate(
        event, session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert event.data["canonical_repo_mutation_scope_unknown"] is True
    assert response.decision == "block"
    assert "block-unresolved-scope-shell-write" in (response.reason or "")
