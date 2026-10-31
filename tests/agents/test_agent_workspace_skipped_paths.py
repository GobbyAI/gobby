"""Skipped workspace artifacts stay out of repeated backup discovery and logs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gobby.agents import agent_workspaces

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace_with_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    project_id = "11111111-1111-4111-8111-111111111111"
    anchor = agent_workspaces.ensure_workspace(project_id)
    folder = anchor / "developer/session-1"
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
            (anchor / "developer/session-1/notes.md").write_text(f"evidence cycle {cycle}\n")
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
    new_note = "developer/session-1/new-notes.md"
    (anchor / new_note).write_text("new evidence after the first backup\n")
    status = await agent_workspaces._git(
        project_id, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"
    )
    assert await agent_workspaces.commit_workspace(project_id, "timer")
    tracked = await agent_workspaces._git(project_id, "ls-files", "-z")
    assert set(tracked.stdout.split("\0")) - {""} == {
        "README.md",
        "developer/session-1/notes.md",
        new_note,
    }
    assert (anchor / "developer/session-1/links/link-0").is_symlink()
    assert (anchor / "developer/session-1/huge.bin").is_file()
    assert status.stdout == f"?? {new_note}\0"


async def test_newline_skipped_path_stays_out_and_unrelated_notes_back_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    project_id = "33333333-3333-4333-8333-333333333333"
    anchor = agent_workspaces.ensure_workspace(project_id)
    folder = anchor / "developer/session-1"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("keep this evidence\n")
    (folder / "odd\nnotes.md").symlink_to(folder / "notes.md")
    assert await agent_workspaces.commit_workspace(project_id, "timer")
    for cycle in range(2):
        note = folder / f"plan-{cycle}" / "notes.md"
        note.parent.mkdir()
        note.write_text(f"unrelated notes {cycle}\n")
        assert await agent_workspaces.commit_workspace(project_id, "timer")
        tracked = await agent_workspaces._git(project_id, "ls-files", "-z")
        names = set(tracked.stdout.split("\0")) - {""}
        assert f"developer/session-1/plan-{cycle}/notes.md" in names
        assert "developer/session-1/odd\nnotes.md" not in names
    assert (folder / "odd\nnotes.md").is_symlink()
