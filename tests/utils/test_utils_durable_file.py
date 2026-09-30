"""Publication-fault contracts for ``gobby.utils.durable_file``."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.utils.durable_file import DurableFileError, durable_replace_text

pytestmark = pytest.mark.unit


def test_durable_replace_preserves_file_and_temp_when_write_fails(tmp_path: Path) -> None:
    """A fault before the rename leaves the original file and no temp behind."""
    target = tmp_path / "anchor"
    target.write_text("old")

    with (
        patch("gobby.utils.durable_file.os.write", side_effect=OSError("write failed")),
        pytest.raises(OSError, match="write failed"),
    ):
        durable_replace_text(target, "new")

    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["anchor"]


def test_durable_replace_reports_failure_when_directory_fsync_fails(
    tmp_path: Path,
) -> None:
    """A matching readback never masks a failed directory fsync: durability is unproven."""
    target = tmp_path / "anchor"
    target.write_text("old")
    real_fsync = os.fsync
    calls: list[int] = []

    def _fsync(fd: int) -> None:
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("directory fsync failed")
        real_fsync(fd)

    with (
        patch("gobby.utils.durable_file.os.fsync", side_effect=_fsync),
        pytest.raises(OSError, match="directory fsync failed"),
    ):
        durable_replace_text(target, "new")

    # The rename landed, so the bytes match, but the caller still observes failure.
    assert target.read_text() == "new"
    assert not list(tmp_path.glob(".*.tmp"))


def test_durable_replace_raises_on_readback_mismatch(tmp_path: Path) -> None:
    """A readback that disagrees with the payload is a hard failure."""
    target = tmp_path / "anchor"

    with (
        patch("pathlib.Path.read_bytes", return_value=b"corrupted"),
        pytest.raises(DurableFileError, match="readback mismatch"),
    ):
        durable_replace_text(target, "intended")


def test_durable_replace_removes_temp_after_failed_replace(tmp_path: Path) -> None:
    """A failed rename removes the staged temp file and preserves the original."""
    target = tmp_path / "anchor"
    target.write_text("old")

    with (
        patch("gobby.utils.durable_file.os.replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        durable_replace_text(target, "new")

    assert target.read_text() == "old"
    assert not list(tmp_path.glob(".*.tmp"))
