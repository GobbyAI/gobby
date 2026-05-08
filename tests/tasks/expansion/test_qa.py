"""Phase 2 tests for expansion QA checks."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_check_manifest_coverage_rejects_missing_leaves() -> None:
    from gobby.tasks.expansion._qa import check_manifest_coverage

    result = check_manifest_coverage(
        manifest_entries=[{"source_section": "1.1"}, {"source_section": "1.2"}],
        compiled_tasks=[{"source_section_id": "1.1"}],
    )

    assert result.valid is False
    assert "1.2" in "\n".join(result.errors)


def test_check_routing_rejects_unknown_agent() -> None:
    from gobby.tasks.expansion._qa import check_routing

    result = check_routing(
        compiled_tasks=[{"id": "leaf", "assigned_agent": "unknown-agent"}],
        known_agents={"backend-developer", "frontend-developer"},
    )

    assert result.valid is False
    assert "unknown-agent" in "\n".join(result.errors)
