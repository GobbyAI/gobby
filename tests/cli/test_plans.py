from __future__ import annotations

from pathlib import Path

import pytest

from gobby.cli.plans import _root_ref_from_file

pytestmark = pytest.mark.unit


def test_root_ref_from_file_reads_front_matter(tmp_path: Path) -> None:
    plan = tmp_path / "manual-plan.md"
    plan.write_text(
        """---
root_task_ref: "root-123"
---

# Planning Notes
""",
        encoding="utf-8",
    )

    assert _root_ref_from_file(plan) == "root-123"


def test_root_ref_from_file_reads_top_level_metadata(tmp_path: Path) -> None:
    plan = tmp_path / "manual-plan.md"
    plan.write_text(
        """root_task_ref: #123

# Planning Notes
""",
        encoding="utf-8",
    )

    assert _root_ref_from_file(plan) == "#123"
