"""Landing policy path rules and freeze file reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.tasks.landing_policy import (
    UNREADABLE_FREEZE_REASON,
    classify_paths,
    freeze_path,
    is_direct_commit_path,
    read_freeze,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["crates/gobby-core/src/lib.rs", "web/src/App.tsx"], "cutover"),
        (["src/gobby/storage/schema_expected_identity.json", "docs/x.md"], "cutover"),
        (["src/gobby/cli/main.py", "web/src/App.tsx"], "restart"),
        (["src/gobby/install/shared/workflows/agents/researcher.yaml"], "reload"),
        (["src/gobby/install/shared/workflows/agents/developer.yaml", "docs/x.md"], "reload"),
        (["src/gobby/install/shared/workflows/agents/README.md"], "restart"),
        (["src/gobby/install/shared/workflows/agents/nested/seat.yaml"], "restart"),
        (
            [
                "src/gobby/install/shared/workflows/agents/researcher.yaml",
                "src/gobby/agents/sync.py",
            ],
            "restart",
        ),
        (["web/src/App.tsx"], "ui_build"),
        ([".gobby/workflows/pipelines/crew-lane.yaml"], "sync"),
        ([".gobby/workflows/pipelines/crew-lane.yml", "docs/x.md"], "sync"),
        ([".gobby/workflows/pipelines/README.md"], "none"),
        ([".gobby/workflows/pipelines/nested/seat.yaml"], "none"),
        ([".gobby/workflows/pipelines/crew-lane.yaml", "src/gobby/cli/main.py"], "restart"),
        ([".gobby/workflows/pipelines/crew-lane.yaml", "crates/gcore/src/lib.rs"], "cutover"),
        ([".gobby/workflows/pipelines/crew-lane.yaml", "web/src/App.tsx"], "ui_build"),
        (
            [
                ".gobby/workflows/pipelines/crew-lane.yaml",
                "src/gobby/install/shared/workflows/agents/researcher.yaml",
            ],
            "reload",
        ),
        (
            ["src/gobby/install/shared/workflows/agents/researcher.yaml", "web/src/App.tsx"],
            "restart",
        ),
        (["docs/guides/x.md", "tests/test_x.py"], "none"),
        ([], "none"),
    ],
)
def test_classify_paths_strongest_class_wins(paths: list[str], expected: str) -> None:
    assert classify_paths(paths) == expected


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("README.md", True),
        (".gobby/plans/reviewer-landing.md", True),
        (".gobby/roles/lane-6.md", True),
        ("docs/guides/sessions.md", True),
        (".gobby/plans/coverage/d45545c5/23273/x.coverage.yaml", True),
        (".gobby/plans/coverage/.regenerate.log", True),
        ("docs/reference-audit/admin.json", False),
        (".gobby/plans/x.svg", False),
        (".gobby/plans/coverage-notes/x.yaml", False),
        ("src/gobby/AGENTS.md", False),
    ],
)
def test_direct_commit_allows_listed_markdown_and_coverage(path: str, allowed: bool) -> None:
    assert is_direct_commit_path(path) is allowed


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"on": "yes", "reason": "", "set_by_session_id": null, "set_at": null}',
        '{"on": false, "reason": 3, "set_by_session_id": null, "set_at": null}',
    ],
)
def test_unreadable_freeze_file_reads_as_frozen(tmp_path: Path, content: str) -> None:
    assert read_freeze(tmp_path).on is False

    path = freeze_path(tmp_path)
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    freeze = read_freeze(tmp_path)

    assert freeze.on is True
    assert freeze.reason == UNREADABLE_FREEZE_REASON
