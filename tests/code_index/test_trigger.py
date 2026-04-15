"""Tests for CodeIndexTrigger debounced post-edit indexing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.code_index.trigger import CodeIndexTrigger

pytestmark = pytest.mark.unit


def _make_mock_proc(returncode: int = 0) -> AsyncMock:
    """Create a mock subprocess that returns immediately."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


@pytest.fixture
async def trigger() -> CodeIndexTrigger:
    loop = asyncio.get_running_loop()
    t = CodeIndexTrigger(
        loop=loop,
        debounce_seconds=0.05,  # Fast debounce for tests
    )
    return t


# ── Basic flush ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_file_triggers_gcode(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """A single file notification triggers gcode index after debounce."""
    mock_proc = _make_mock_proc()

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        # Create the fake binary at the patched path
        gcode_bin = tmp_path / ".gobby" / "bin" / "gcode"
        gcode_bin.parent.mkdir(parents=True, exist_ok=True)
        gcode_bin.touch()

        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")
        await asyncio.sleep(0.1)

        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        # Verify gcode was called with index --files
        args = call_args[0]
        assert "index" in args
        assert "--files" in args
        assert "/src/foo.py" in args


# ── Debounce coalescing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_files_batched(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """Multiple files in the same project are batched into one call."""
    mock_proc = _make_mock_proc()

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        gcode_bin = tmp_path / ".gobby" / "bin" / "gcode"
        gcode_bin.parent.mkdir(parents=True, exist_ok=True)
        gcode_bin.touch()

        trigger._schedule_file("/src/a.py", "proj-1", "/repo")
        trigger._schedule_file("/src/b.py", "proj-1", "/repo")
        trigger._schedule_file("/src/c.py", "proj-1", "/repo")

        await asyncio.sleep(0.1)

        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        assert "/src/a.py" in call_args
        assert "/src/b.py" in call_args
        assert "/src/c.py" in call_args


@pytest.mark.asyncio
async def test_same_file_deduped(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """Editing the same file multiple times results in one file in the batch."""
    mock_proc = _make_mock_proc()

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        gcode_bin = tmp_path / ".gobby" / "bin" / "gcode"
        gcode_bin.parent.mkdir(parents=True, exist_ok=True)
        gcode_bin.touch()

        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")

        await asyncio.sleep(0.1)

        mock_exec.assert_called_once()
        # Only one instance of the file in args
        call_args = mock_exec.call_args[0]
        assert call_args.count("/src/foo.py") == 1


# ── Timer reset ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debounce_timer_resets(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """New edits reset the debounce timer, delaying the flush."""
    mock_proc = _make_mock_proc()

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        gcode_bin = tmp_path / ".gobby" / "bin" / "gcode"
        gcode_bin.parent.mkdir(parents=True, exist_ok=True)
        gcode_bin.touch()

        trigger._schedule_file("/src/a.py", "proj-1", "/repo")

        # Wait less than debounce time, then add another file
        await asyncio.sleep(0.03)
        mock_exec.assert_not_called()

        trigger._schedule_file("/src/b.py", "proj-1", "/repo")

        # Wait for debounce after second edit
        await asyncio.sleep(0.1)

        # Should have been called once with both files
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        assert "/src/a.py" in call_args
        assert "/src/b.py" in call_args


# ── Multi-project isolation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_different_projects_independent(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """Different projects flush independently."""
    mock_proc = _make_mock_proc()

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        gcode_bin = tmp_path / ".gobby" / "bin" / "gcode"
        gcode_bin.parent.mkdir(parents=True, exist_ok=True)
        gcode_bin.touch()

        trigger._schedule_file("/repo1/a.py", "proj-1", "/repo1")
        trigger._schedule_file("/repo2/b.py", "proj-2", "/repo2")

        await asyncio.sleep(0.1)

        assert mock_exec.call_count == 2


# ── Error isolation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gcode_failure_does_not_propagate(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """gcode failure is logged but doesn't raise."""
    mock_proc = _make_mock_proc(returncode=1)

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        gcode_bin = tmp_path / ".gobby" / "bin" / "gcode"
        gcode_bin.parent.mkdir(parents=True, exist_ok=True)
        gcode_bin.touch()

        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")

        # Should not raise
        await asyncio.sleep(0.1)


# ── No gcode binary ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_gcode_warns_and_skips(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """Missing gcode binary logs warning and skips indexing."""

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")

        await asyncio.sleep(0.1)

        mock_exec.assert_not_called()


# ── Empty flush ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_flush_is_noop(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """Flushing with no pending files does nothing."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await trigger._flush("nonexistent-project")
        mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_flush_cancel_before_subprocess_assignment_is_clean(
    trigger: CodeIndexTrigger, tmp_path: Path
) -> None:
    """Cancellation before create_subprocess_exec assigns proc should not raise UnboundLocalError."""
    gcode_bin = tmp_path / ".gobby" / "bin" / "gcode"
    gcode_bin.parent.mkdir(parents=True, exist_ok=True)
    gcode_bin.touch()
    trigger._pending["proj-1"] = {"/src/foo.py"}
    trigger._root_paths["proj-1"] = "/repo"

    with (
        patch("gobby.code_index.trigger.Path.home", return_value=tmp_path),
        patch("asyncio.create_subprocess_exec", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await trigger._flush("proj-1")
