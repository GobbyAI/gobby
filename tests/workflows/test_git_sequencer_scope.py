"""Sequencer recovery has a known worktree scope without weakening git guards."""

import shlex
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
def claim_engine(temp_db: HubDatabase, engine: RuleEngine) -> RuleEngine:
    result = sync_rule_file(
        temp_db,
        get_bundled_rules_path() / "task-enforcement/require-task-before-edit.yaml",
        tag="gobby",
    )
    assert result["success"] is True, result
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return engine


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


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["revert", "cherry-pick"])
@pytest.mark.parametrize("flag", ["--abort", "--quit"])
@pytest.mark.parametrize("spawned", [True, False])
async def test_autonomous_sequencer_abort_guard(
    temp_db: HubDatabase, tmp_path: Path, operation: str, flag: str, spawned: bool
) -> None:
    result = sync_rule_file(
        temp_db,
        get_bundled_rules_path() / "worker-safety/no-destructive-git.yaml",
        tag="gobby",
    )
    assert result["success"] is True, result
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    engine = RuleEngine(temp_db)
    response = await engine.evaluate(
        _event(f"git {operation} {flag}", tmp_path),
        session_id=SESSION_ID,
        variables={"is_spawned_agent": spawned},
    )
    if spawned and flag == "--abort":
        assert response.decision == "block"
        assert "no-destructive-git" in (response.reason or "")
    else:
        assert response.decision != "block"


CLAIMED_VARIABLES: dict[str, Any] = {
    "require_task_before_edit": True,
    "task_claimed": True,
    "plan_mode": False,
    "is_spawned_agent": False,
    "loaded_skills": ["python", "restraint"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["env -C {root}", "env --chdir={root}", "env -v -C {root}"])
@pytest.mark.parametrize("operation", ["add x.py", "revert --quit"])
@pytest.mark.parametrize("claimed", [False, True])
async def test_env_git_writes_obey_task_claim_gate(
    claim_engine: RuleEngine,
    tmp_path: Path,
    wrapper: str,
    operation: str,
    claimed: bool,
) -> None:
    event = _event(f"{wrapper.format(root=tmp_path)} git {operation}", tmp_path)
    variables = {**CLAIMED_VARIABLES, "task_claimed": claimed}
    response = await claim_engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert event.data["canonical_repo_mutation"] is True
    assert response.decision == ("allow" if claimed else "block"), response.reason
    if not claimed:
        assert "require-task-before-edit" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper",
    ["nohup", "timeout 5", "nice -n 5", "env FOO+=x", "env a.b=1", "env -- FOO+=x", "env -- A=1"],
)
@pytest.mark.parametrize("claimed", [False, True])
async def test_utility_wrapped_writes_obey_claim_gate(
    claim_engine: RuleEngine, tmp_path: Path, wrapper: str, claimed: bool
) -> None:
    event = _event(f"{wrapper} touch x.py", tmp_path)
    response = await claim_engine.evaluate(
        event, session_id=SESSION_ID, variables={**CLAIMED_VARIABLES, "task_claimed": claimed}
    )
    assert event.data["canonical_repo_mutation"] is True
    assert event.data["canonical_file_paths"] == ["x.py"]
    assert response.decision == ("allow" if claimed else "block"), response.reason
    if not claimed:
        assert "require-task-before-edit" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["cat <(./w.sh)", "(./w.sh)"])
async def test_changed_directory_script_body_requires_claim(
    claim_engine: RuleEngine, tmp_path: Path, body: str
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "README").write_text("read only\n")
    outer = tmp_path / "w.sh"
    inner = tmp_path / "sub/w.sh"
    outer.write_text("#!/bin/sh\ncat README\n")
    inner.write_text("#!/bin/sh\ntouch evil.py\n")
    outer.chmod(0o755)
    inner.chmod(0o755)
    event = _event(f"cd sub && {body}", tmp_path)
    response = await claim_engine.evaluate(
        event, session_id=SESSION_ID, variables={**CLAIMED_VARIABLES, "task_claimed": False}
    )
    assert event.data["canonical_script_execution"] is True
    assert str(outer) not in event.data.get("canonical_script_bodies", [])
    assert response.decision == "block"
    assert "require-task-before-edit" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_directory", [False, True], ids=["cwd", "literal-C"])
@pytest.mark.parametrize(
    "prefix", ["", "true && ", "git status && ", "git --no-pager ", "git -c k=v "]
)
async def test_claimed_pending_revert_can_quit(
    engine: RuleEngine, pending_worktree: tuple[Path, Path], explicit_directory: bool, prefix: str
) -> None:
    root, revert_head = pending_worktree
    assert revert_head.exists()
    command = f"git -C {root} revert --quit" if explicit_directory else "git revert --quit"
    command = prefix + (command.removeprefix("git ") if prefix.startswith("git -") else command)
    event = _event(command, root)
    response = await engine.evaluate(
        event, session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert response.decision == "allow", response.reason
    assert event.data["canonical_repo_mutation"] is True
    assert event.data["canonical_file_paths"] == [str(root)]
    assert not event.data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix",
    [
        "GIT_DIR={target} git",
        "env GIT_DIR={target} git",
        "GIT_WORK_TREE={target} git",
        "env GIT_WORK_TREE={target} git",
        "git --git-dir={target}",
    ],
)
@pytest.mark.parametrize(
    "operation", ["revert --quit", "revert --skip", "revert --continue", "add x.py", "restore x.py"]
)
async def test_explicit_repository_selector_fails_closed(
    engine: RuleEngine, tmp_path: Path, prefix: str, operation: str
) -> None:
    own = tmp_path / "own"
    foreign = tmp_path / "other"
    own.mkdir()
    foreign.mkdir()
    event = _event(f"{prefix.format(target=foreign)} {operation}", own)
    response = await engine.evaluate(
        event, session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert response.decision == "block"
    assert "block-unresolved-scope-shell-write" in (response.reason or "")
    assert event.data["canonical_repo_mutation_scope_unknown"] is True
    assert str(own) not in event.data.get("canonical_file_paths", [])


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


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["revert", "cherry-pick"])
@pytest.mark.parametrize("form", ["cwd", "directory", "nested"])
@pytest.mark.parametrize("sequencer_first", [True, False])
@pytest.mark.parametrize(
    "write",
    [
        "python3 -c \"import os; os.remove(os.environ['F'])\"",
        "git apply change.patch",
        'git checkout -- "$FILE"',
    ],
)
async def test_sequencer_scope_cannot_mask_unresolved_sibling_write(
    engine: RuleEngine,
    pending_worktree: tuple[Path, Path],
    operation: str,
    form: str,
    sequencer_first: bool,
    write: str,
) -> None:
    root, _ = pending_worktree
    git = f"git -C {shlex.quote(str(root))}" if form == "directory" else "git"
    recovery = f"{git} {operation} --quit"
    command = f"{recovery} && {write}" if sequencer_first else f"{write} && {recovery}"
    if form == "nested":
        command = f"bash -c {shlex.quote(command)}"
    event = _event(command, root)
    response = await engine.evaluate(
        event, session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert event.data["canonical_repo_mutation_scope_unknown"] is True
    assert response.decision == "block"
    assert "block-unresolved-scope-shell-write" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("index_flag", ["-A", "-u", "--all", "--update"])
@pytest.mark.parametrize("index_first", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_pathless_index_update_keeps_literal_sibling_write_resolved(
    engine: RuleEngine,
    pending_worktree: tuple[Path, Path],
    index_flag: str,
    index_first: bool,
    nested: bool,
) -> None:
    root, _ = pending_worktree
    edit = "sed -i 's/a/b/' file.py"
    index = f"git add {index_flag}"
    command = f"{index} && {edit}" if index_first else f"{edit} && {index}"
    if nested:
        command = f"bash -c {shlex.quote(command)}"
    event = _event(command, root)
    response = await engine.evaluate(
        event, session_id=SESSION_ID, variables=dict(CLAIMED_VARIABLES)
    )
    assert event.data["canonical_file_paths"] == ["file.py"]
    assert event.data["canonical_repo_mutation"] is True
    assert not event.data.get("canonical_repo_mutation_scope_unknown")
    assert "_canonical_repo_mutation_scope_unknown" not in event.data
    assert response.decision == "allow"


@pytest.mark.parametrize("index_flag", ["-A", "-u", "--all", "--update", '"$FILE"'])
def test_index_execute_does_not_publish_unknown_write_scope(
    pending_worktree: tuple[Path, Path], index_flag: str
) -> None:
    root, _ = pending_worktree
    event = _event(f"git add {index_flag}", root)
    assert event.data["canonical_tool_kind"] == "execute"
    assert "_canonical_repo_mutation_scope_unknown" not in event.data
    assert "canonical_repo_mutation_scope_unknown" not in event.data
