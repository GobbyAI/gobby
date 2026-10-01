"""Tests for the path-scope condition helpers used by seat write-scope rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.workflows.condition_helpers_paths import write_paths_match, write_paths_within
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit

DIGEST = r"/digests/gobby-digest-\d{4}-\d{2}-\d{2}\.md"


def _write(project: Path, path: str, *, cwd: Path | None = None) -> dict[str, Any]:
    return {
        "canonical_tool_kind": "write",
        "canonical_file_paths": [path],
        "tool_input": {"file_path": path},
        "cwd": str(cwd or project),
        "project_path": str(project),
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "docs" / "escape").symlink_to(outside, target_is_directory=True)
    return root.resolve()


def test_write_path_helpers_resolve_before_matching(project: Path) -> None:
    docs = ["docs/"]

    assert write_paths_within(_write(project, "docs/guide.md"), {}, docs) is True
    assert write_paths_within(_write(project, str(project / "docs/a/b.md")), {}, docs) is True
    assert write_paths_within(_write(project, "guide.md", cwd=project / "docs"), {}, docs) is True
    assert write_paths_within(_write(project, "src/app.py"), {}, docs) is False
    assert write_paths_within(_write(project, "docs-other/guide.md"), {}, docs) is False
    assert write_paths_within(_write(project, "docs/../src/app.py"), {}, docs) is False
    assert write_paths_within(_write(project, "docs/escape/secret.md"), {}, docs) is False
    assert write_paths_within({"canonical_tool_kind": "write"}, {}, docs) is False

    digest = str(project.parent / "digests" / "gobby-digest-2026-09-27.md")
    pattern = str(project.parent) + DIGEST
    assert write_paths_match(_write(project, digest), {}, pattern) is True
    assert write_paths_match(_write(project, digest + ".bak"), {}, pattern) is False
    traversed = str(project.parent / "digests" / "x" / ".." / "gobby-digest-2026-09-27.md")
    assert write_paths_match(_write(project, traversed), {}, pattern) is True
    assert write_paths_match(_write(project, "docs/guide.md"), {}, pattern) is False
    assert write_paths_match({"canonical_tool_kind": "write"}, {}, pattern) is False

    helpers = build_condition_helpers()
    evaluator = SafeExpressionEvaluator(
        {"event_data": _write(project, "docs/guide.md"), "tool_input": {}}, helpers
    )
    assert evaluator.evaluate("write_paths_within(event_data, tool_input, ['docs/'])") is True
    assert evaluator.evaluate("write_paths_match(event_data, tool_input, 'nothing')") is False
    assert "touches_docker_policy_path" in helpers


def test_write_paths_match_anchors_tilde_at_current_home(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = project.parent / "home.d"
    monkeypatch.setenv("HOME", str(home))
    pattern = "~" + DIGEST
    digest = "gobby-digest-2026-09-27.md"

    assert write_paths_match(_write(project, str(home / "digests" / digest)), {}, pattern)
    elsewhere = str(project.parent / "other" / "digests" / digest)
    assert write_paths_match(_write(project, elsewhere), {}, pattern) is False
    # The home path is literal: its "." does not match any character.
    lookalike = str(project.parent / "homexd" / "digests" / digest)
    assert write_paths_match(_write(project, lookalike), {}, pattern) is False
