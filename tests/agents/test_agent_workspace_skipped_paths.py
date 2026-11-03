"""Skipped workspace artifacts stay out of repeated backup discovery and logs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from gobby.agents import agent_workspaces
from gobby.utils.daemon_git import GitOk

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace_with_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    project_id = "11111111-1111-4111-8111-111111111111"
    anchor = agent_workspaces.ensure_workspace(project_id)
    folder = anchor / "fixture-worker/session-1"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("keep this evidence\n")
    for relative in ("links", "proof/gobby-home/cache/sandbox/zig-packages"):
        artifacts = folder / relative
        artifacts.mkdir(parents=True)
        for index in range(30):
            (artifacts / f"link-{index}").symlink_to(folder / "notes.md")
    (folder / "proof/gobby-home/cache/sandbox/zig-packages/generated.bin").write_bytes(b"cache")
    (folder / "huge.bin").write_bytes(b"x" * (agent_workspaces.WORKSPACE_FILE_LIMIT_BYTES + 1))
    (folder / "auth.json").write_text("secret fixture\n")
    generated = folder / "node_modules/package"
    generated.mkdir(parents=True)
    (generated / "generated.js").write_text("generated fixture\n")
    return project_id, anchor


async def test_repeated_backups_emit_only_bounded_summary_logs(
    workspace_with_skips: tuple[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    project_id, anchor = workspace_with_skips
    cycle_messages: list[list[str]] = []
    with caplog.at_level(logging.INFO, logger=agent_workspaces.__name__):
        for cycle in range(2):
            caplog.clear()
            (anchor / "fixture-worker/session-1/notes.md").write_text(f"evidence cycle {cycle}\n")
            assert await agent_workspaces.commit_workspace(project_id, "timer")
            cycle_messages.append(
                [
                    record.getMessage()
                    for record in caplog.records
                    if record.name == agent_workspaces.__name__ and record.levelno >= logging.INFO
                ]
            )

    assert all(len(messages) <= 1 for messages in cycle_messages), cycle_messages
    assert all(
        "link-" not in message and "huge.bin" not in message
        for messages in cycle_messages
        for message in messages
    ), cycle_messages


async def test_skipped_paths_are_not_rediscovered_but_new_notes_are_backed_up(
    workspace_with_skips: tuple[str, Path],
) -> None:
    project_id, anchor = workspace_with_skips
    assert await agent_workspaces.commit_workspace(project_id, "timer")
    new_note = "fixture-worker/session-1/new-notes.md"
    (anchor / new_note).write_text("new evidence after the first backup\n")
    status = await agent_workspaces._git(
        project_id, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"
    )
    assert await agent_workspaces.commit_workspace(project_id, "timer")
    tracked = await agent_workspaces._git(project_id, "ls-files", "-z")
    assert set(tracked.stdout.split("\0")) - {""} == {
        "README.md",
        "fixture-worker/session-1/notes.md",
        new_note,
    }
    assert (anchor / "fixture-worker/session-1/links/link-0").is_symlink()
    assert (anchor / "fixture-worker/session-1/huge.bin").is_file()
    assert status.stdout == f"?? {new_note}\0"


@pytest.mark.parametrize(
    "name",
    ["odd\nnotes.md", "odd\rnotes.md", "odd\t[?]*notes.md", "odd\n# END GOBBY WORKSPACE EXCLUDES"],
)
async def test_newline_skipped_path_stays_out_and_unrelated_notes_back_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    project_id = "33333333-3333-4333-8333-333333333333"
    anchor = agent_workspaces.ensure_workspace(project_id)
    folder = anchor / "fixture-worker/session-1"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("keep this evidence\n")
    skipped = f"fixture-worker/session-1/{name}"
    (anchor / skipped).symlink_to(folder / "notes.md")
    assert await agent_workspaces.commit_workspace(project_id, "timer")
    statuses: list[str] = []
    git = agent_workspaces._git

    async def observe_git(project_id: str, *args: str, **kwargs: Any) -> GitOk:
        result = await git(project_id, *args, **kwargs)
        if args[0] == "status":
            statuses.append(result.stdout)
        return result

    monkeypatch.setattr(agent_workspaces, "_git", observe_git)
    for cycle in range(2):
        note = folder / f"plan-{cycle}" / "notes.md"
        note.parent.mkdir()
        note.write_text(f"unrelated notes {cycle}\n")
        assert await agent_workspaces.commit_workspace(project_id, "timer")
        assert statuses[-1] == f"?? fixture-worker/session-1/plan-{cycle}/notes.md\0"
        tracked = await agent_workspaces._git(project_id, "ls-files", "-z")
        names = set(tracked.stdout.split("\0")) - {""}
        assert f"fixture-worker/session-1/plan-{cycle}/notes.md" in names
        assert skipped not in names
    assert (anchor / skipped).is_symlink()
    (anchor / skipped).unlink()
    (anchor / skipped).write_text("replaced with eligible evidence\n")
    assert await agent_workspaces.commit_workspace(project_id, "timer")
    tracked = await git(project_id, "ls-files", "-z")
    assert skipped in tracked.stdout.split("\0")


async def test_skipped_path_parent_replaced_by_file_does_not_block_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    project_id = "44444444-4444-4444-8444-444444444444"
    anchor = agent_workspaces.ensure_workspace(project_id)
    parent_path = "fixture-worker/session-1/evidence"
    folder = anchor / parent_path
    folder.mkdir(parents=True)
    skipped = f"{parent_path}/link.md"
    (anchor / skipped).symlink_to(anchor / "README.md")
    assert await agent_workspaces.commit_workspace(project_id, "timer")
    exclude = agent_workspaces.workspace_git_dir(project_id) / "info/exclude"
    assert skipped in exclude.read_text()

    (anchor / skipped).unlink()
    folder.rmdir()
    folder.write_text("directory replaced with eligible evidence\n")
    try:
        sha = await agent_workspaces.commit_workspace(project_id, "timer")
    except OSError as exc:
        pytest.fail(f"A stale skipped descendant must not block backups: {exc}")

    assert sha is not None
    committed = await agent_workspaces._git(project_id, "show", f"HEAD:{parent_path}")
    assert committed.stdout == "directory replaced with eligible evidence\n"
    assert skipped not in exclude.read_text()
    assert await agent_workspaces.commit_workspace(project_id, "timer") is None
    status = await agent_workspaces._git(
        project_id, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"
    )
    assert status.stdout == ""
