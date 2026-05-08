"""Review tools must be first-class stage-axis transitions."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text, source_texts

pytestmark = pytest.mark.unit


def test_no_status_writes_after_rewire() -> None:
    source = source_text("src/gobby/storage/tasks/_transitions.py")

    for legacy_status in ("needs_review", "review_approved", "open"):
        assert f"status = '{legacy_status}'" not in source
        assert f'status = "{legacy_status}"' not in source


def test_review_tools_call_first_class_stage_axis_methods() -> None:
    source = source_texts(("src/gobby/storage/tasks", "src/gobby/mcp_proxy/tools/tasks"))

    assert "submit_for_review(" in source
    assert "approve_review(" in source
    assert "reject_review(" in source


def test_no_complete_stage_or_fail_stage_in_review_tool_paths() -> None:
    source = source_texts(
        (
            "src/gobby/storage/tasks/_transitions.py",
            "src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py",
        )
    )

    assert "complete_stage(" not in source
    assert "fail_stage(" not in source
