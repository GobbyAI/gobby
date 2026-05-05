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
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")
        await trigger._flush(trigger._root_key("/repo"), "proj-1")

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
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("/src/a.py", "proj-1", "/repo")
        trigger._schedule_file("/src/b.py", "proj-1", "/repo")
        trigger._schedule_file("/src/c.py", "proj-1", "/repo")

        await trigger._flush(trigger._root_key("/repo"), "proj-1")

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
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")

        await trigger._flush(trigger._root_key("/repo"), "proj-1")

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
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("/src/a.py", "proj-1", "/repo")

        first_timer = trigger._flush_timers_by_root[trigger._root_key("/repo")]

        trigger._schedule_file("/src/b.py", "proj-1", "/repo")

        second_timer = trigger._flush_timers_by_root[trigger._root_key("/repo")]
        assert first_timer.cancelled()
        assert second_timer is not first_timer
        mock_exec.assert_not_called()

        # Should have been called once with both files
        await trigger._flush(trigger._root_key("/repo"), "proj-1")
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
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("/repo1/a.py", "proj-1", "/repo1")
        trigger._schedule_file("/repo2/b.py", "proj-2", "/repo2")

        await trigger._flush(trigger._root_key("/repo1"), "proj-1")
        await trigger._flush(trigger._root_key("/repo2"), "proj-2")

        assert mock_exec.call_count == 2


@pytest.mark.asyncio
async def test_two_isolated_roots_same_parent_dont_collide(
    trigger: CodeIndexTrigger, tmp_path: Path
) -> None:
    """Same logical project id in different roots flushes independently."""
    mock_proc = _make_mock_proc()
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()

    with (
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("src/shared.py", "parent-proj", str(root_a))
        trigger._schedule_file("src/shared.py", "parent-proj", str(root_b))

        await trigger._flush(trigger._root_key(str(root_a)), "parent-proj")
        await trigger._flush(trigger._root_key(str(root_b)), "parent-proj")

        assert mock_exec.call_count == 2
        cwds = {call.kwargs["cwd"] for call in mock_exec.call_args_list}
        assert cwds == {str(root_a.resolve()), str(root_b.resolve())}


@pytest.mark.asyncio
async def test_pending_paths_resolve_under_root_key_cwd(
    trigger: CodeIndexTrigger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending relative paths are resolved under the root, not daemon cwd."""
    mock_proc = _make_mock_proc()
    root = tmp_path / "repo"
    daemon_cwd = tmp_path / "daemon-cwd"
    root.mkdir()
    daemon_cwd.mkdir()
    monkeypatch.chdir(daemon_cwd)

    with (
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("src/pkg.py", "proj-1", str(root))

        await trigger._flush(trigger._root_key(str(root)), "proj-1")

        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args.kwargs["cwd"] == str(root.resolve())
        assert "src/pkg.py" in call_args.args


# ── Error isolation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gcode_failure_does_not_propagate(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """gcode failure is logged but doesn't raise."""
    mock_proc = _make_mock_proc(returncode=1)

    with (
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")

        result = await trigger._flush(trigger._root_key("/repo"), "proj-1")

    assert result is None
    assert mock_proc.communicate.await_count == 1


# ── No gcode binary ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_gcode_warns_and_skips(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """Missing gcode binary logs warning and skips indexing."""

    with (
        patch("gobby.code_index.trigger.resolve_native_bin", return_value=None),
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        trigger._schedule_file("/src/foo.py", "proj-1", "/repo")

        await trigger._flush(trigger._root_key("/repo"), "proj-1")

        mock_exec.assert_not_called()
        assert mock_exec.call_count == 0
        assert not mock_exec.called


# ── Empty flush ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_flush_is_noop(trigger: CodeIndexTrigger, tmp_path: Path) -> None:
    """Flushing with no pending files does nothing."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await trigger._flush(str(tmp_path.resolve()), "nonexistent-project")
        mock_exec.assert_not_called()
        assert mock_exec.call_count == 0
        assert not mock_exec.called


@pytest.mark.asyncio
async def test_flush_cancel_before_subprocess_assignment_is_clean(
    trigger: CodeIndexTrigger, tmp_path: Path
) -> None:
    """Cancellation before create_subprocess_exec assigns proc should not raise UnboundLocalError."""
    root_key = trigger._root_key("/repo")
    trigger._pending_by_root[root_key] = {"/src/foo.py"}

    with (
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await trigger._flush(root_key, "proj-1")
