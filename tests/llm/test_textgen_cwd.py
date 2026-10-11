"""Fixed and per-call text generation working directories."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.llm.textgen_cwd import (
    fixed_textgen_cwd,
    neutral_textgen_cwd,
)

pytestmark = pytest.mark.unit


class TestFixedTextgenCwd:
    def test_yields_same_existing_dir_across_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with fixed_textgen_cwd() as first, fixed_textgen_cwd() as second:
            assert first == second
            assert first.is_dir()
            assert first == tmp_path / ".gobby" / "tmp" / "textgen"


def test_neutral_cwd_removes_per_call_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    with neutral_textgen_cwd() as cwd:
        assert cwd.parent == tmp_path
        artifact = cwd / "output.txt"
        artifact.write_text("generated output")
        assert artifact.read_text() == "generated output"
    assert not cwd.exists()
