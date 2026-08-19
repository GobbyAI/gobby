"""Fixed textgen cwd and stale Claude project-dir purge (#20450)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gobby.llm.textgen_cwd import (
    _gobby_textgen_project_slug_fragment,
    fixed_textgen_cwd,
    neutral_textgen_cwd,
    purge_textgen_project_dirs,
)

pytestmark = pytest.mark.unit


def _claude_project_slug(path: Path) -> str:
    """Mimic Claude Code's cwd -> ~/.claude/projects dir-name transform."""
    return str(path).replace("/", "-").replace(".", "-")


class TestFixedTextgenCwd:
    def test_yields_same_existing_dir_across_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with fixed_textgen_cwd() as first, fixed_textgen_cwd() as second:
            assert first == second
            assert first.is_dir()
            assert first == tmp_path / ".gobby" / "tmp" / "textgen"

    def test_fixed_slug_never_matches_purge_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with fixed_textgen_cwd() as fixed:
            assert "gobby-textgen-" not in _claude_project_slug(fixed)
        with neutral_textgen_cwd() as per_call:
            assert "gobby-textgen-" in _claude_project_slug(per_call)


class TestPurgeTextgenProjectDirs:
    def _make_dir(self, root: Path, name: str, *, age_seconds: float) -> Path:
        target = root / name
        target.mkdir()
        (target / "transcript.jsonl").write_text("{}")
        stamp = time.time() - age_seconds
        os.utime(target, (stamp, stamp))
        return target

    def test_removes_only_old_marker_dirs(self, tmp_path: Path) -> None:
        fragment = _gobby_textgen_project_slug_fragment()
        old_textgen = self._make_dir(tmp_path, f"{fragment}abc123", age_seconds=7200)
        young_textgen = self._make_dir(tmp_path, f"{fragment}def456", age_seconds=60)
        real_project = self._make_dir(tmp_path, "-Users-josh-Projects-gobby", age_seconds=7200)
        fixed_slug = self._make_dir(tmp_path, "-Users-josh--gobby-tmp-textgen", age_seconds=7200)
        unrelated = self._make_dir(tmp_path, "my-gobby-textgen-notes", age_seconds=7200)

        removed = purge_textgen_project_dirs(tmp_path, older_than_seconds=3600.0)

        assert removed == 1
        assert not old_textgen.exists()
        assert young_textgen.exists()
        assert real_project.exists()
        assert fixed_slug.exists()
        assert unrelated.exists()

    def test_respects_max_dirs_bound(self, tmp_path: Path) -> None:
        for index in range(5):
            self._make_dir(
                tmp_path,
                f"{_gobby_textgen_project_slug_fragment()}{index}",
                age_seconds=7200,
            )

        removed = purge_textgen_project_dirs(tmp_path, older_than_seconds=3600.0, max_dirs=3)

        assert removed == 3
        leftover = [entry.name for entry in tmp_path.iterdir()]
        assert len(leftover) == 2

    def test_missing_root_returns_zero(self, tmp_path: Path) -> None:
        assert purge_textgen_project_dirs(tmp_path / "absent") == 0
