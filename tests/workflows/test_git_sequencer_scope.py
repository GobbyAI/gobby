"""Sequencer recovery has a known worktree scope without weakening git guards."""

import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields

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
def test_sequencer_scope_cannot_mask_unresolved_sibling_write(
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
    assert event.data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("index_flag", ["-A", "-u", "--all", "--update"])
@pytest.mark.parametrize("index_first", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_pathless_index_update_keeps_literal_sibling_write_resolved(
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
    assert event.data["canonical_file_paths"] == ["file.py"]
    assert event.data["canonical_repo_mutation"] is True
    assert not event.data.get("canonical_repo_mutation_scope_unknown")
    assert "_canonical_repo_mutation_scope_unknown" not in event.data


@pytest.mark.parametrize("index_flag", ["-A", "-u", "--all", "--update", '"$FILE"'])
def test_index_execute_does_not_publish_unknown_write_scope(
    pending_worktree: tuple[Path, Path], index_flag: str
) -> None:
    root, _ = pending_worktree
    event = _event(f"git add {index_flag}", root)
    assert event.data["canonical_tool_kind"] == "execute"
    assert "_canonical_repo_mutation_scope_unknown" not in event.data
    assert "canonical_repo_mutation_scope_unknown" not in event.data
