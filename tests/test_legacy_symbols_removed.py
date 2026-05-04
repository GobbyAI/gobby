"""Legacy lifecycle/status symbols must be gone after Phase 5 cutover."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import assert_no_regex_matches

pytestmark = pytest.mark.unit


def test_no_lifecycle_imports() -> None:
    import gobby.storage.tasks as task_module

    assert not hasattr(task_module, "Lifecycle")
    assert_no_regex_matches(
        r"\b(class\s+Lifecycle|TaskLifecycleStage|project_legacy_status|"
        r"lifecycle_stage_from_status)\b",
        (
            "src/gobby/storage/tasks",
            "src/gobby/tasks",
            "src/gobby/mcp_proxy/tools/tasks",
            "src/gobby/servers/routes/tasks.py",
        ),
    )
