"""Task gates consume configurable path exemptions without knowing their purpose."""

from pathlib import Path

import pytest
import yaml

from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

pytestmark = pytest.mark.unit


@pytest.fixture
def patterns() -> list[str]:
    definition = yaml.safe_load(
        r"""
        exempt_path_patterns:
          - './exports/entry-[0-9]+\.md'
          - './notes/snapshot-[a-z]+\.txt'
        """
    )
    return [str(pattern) for pattern in definition["exempt_path_patterns"]]


@pytest.mark.parametrize(
    ("paths", "requires_task"),
    [
        (["exports/entry-12.md"], False),
        (["notes/snapshot-alpha.txt"], False),
        (["exports/entry-12.md", "notes/snapshot-alpha.txt"], False),
        (["exports/nested/entry-12.md"], True),
        (["exports/entry-other.md"], True),
        (["exports/entry-12.md", "src/main.py"], True),
        (["../exports/entry-12.md"], True),
        ([], True),
    ],
)
def test_inline_patterns_apply_to_every_resolved_path(
    tmp_path: Path, patterns: list[str], paths: list[str], requires_task: bool
) -> None:
    event = {
        "project_path": str(tmp_path),
        "cwd": str(tmp_path),
        "canonical_file_paths": [str(tmp_path / path) for path in paths],
        "canonical_structured_mutation": True,
    }
    assert (
        requires_task_for_any_touched_file({}, None, False, event, False, patterns) is requires_task
    )


def test_exemptions_are_opt_in(tmp_path: Path) -> None:
    event = {"project_path": str(tmp_path), "canonical_structured_mutation": True}
    assert requires_task_for_any_touched_file(
        {"file_path": str(tmp_path / "exports/entry-12.md")}, event_data=event
    )


def test_pattern_anchor_uses_project_root_not_tool_cwd(tmp_path: Path, patterns: list[str]) -> None:
    event = {
        "project_path": str(tmp_path),
        "cwd": str(tmp_path / "elsewhere"),
        "canonical_structured_mutation": True,
    }
    assert not requires_task_for_any_touched_file(
        {"file_path": str(tmp_path / "exports/entry-12.md")}, None, False, event, False, patterns
    )


def test_different_exemptions_combine_per_path(tmp_path: Path, patterns: list[str]) -> None:
    event = {
        "project_path": str(tmp_path),
        "cwd": str(tmp_path),
        "canonical_file_paths": [
            str(tmp_path / ".gobby/plans/fixture.md"),
            str(tmp_path / "exports/entry-12.md"),
        ],
        "canonical_structured_mutation": True,
    }
    assert not requires_task_for_any_touched_file({}, None, False, event, True, patterns)
