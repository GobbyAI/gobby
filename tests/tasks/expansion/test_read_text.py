"""Expansion file reads must not prefix-slice existing file text."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.tasks.expansion._common import _read_text_if_exists


@pytest.mark.unit
def test_read_text_if_exists_returns_complete_file(tmp_path: Path) -> None:
    path = tmp_path / "source.py"
    body = "x" * 5000
    path.write_text(body, encoding="utf-8")

    assert _read_text_if_exists(path) == body


@pytest.mark.unit
def test_read_text_if_exists_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _read_text_if_exists(tmp_path / "missing.py") is None
