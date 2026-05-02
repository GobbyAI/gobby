"""Phase 5 migration 236 legacy-column removal contracts."""

from __future__ import annotations

import re

import pytest

from tests.phase5_contract_helpers import (
    LEGACY_CAP_COLUMNS,
    migration_action,
    source_text,
    table_columns,
)

pytestmark = pytest.mark.unit


def test_lifecycle_column_dropped(temp_db) -> None:
    migration_action(236)
    assert "lifecycle" not in table_columns(temp_db, "tasks")


def test_lifecycle_stage_column_dropped(temp_db) -> None:
    migration_action(236)
    assert "lifecycle_stage" not in table_columns(temp_db, "tasks")


def test_status_column_dropped(temp_db) -> None:
    migration_action(236)
    assert "status" not in table_columns(temp_db, "tasks")


@pytest.mark.parametrize("column", LEGACY_CAP_COLUMNS)
def test_legacy_cap_column_dropped(temp_db, column: str) -> None:
    migration_action(236)
    assert column not in table_columns(temp_db, "task_artifacts")


def test_fresh_schema_lacks_all_five_legacy_cap_columns(temp_db) -> None:
    migration_action(236)
    assert table_columns(temp_db, "task_artifacts").isdisjoint(LEGACY_CAP_COLUMNS)


def test_upgrade_path_preserves_per_stage_caps_after_drop(temp_db, sample_project) -> None:
    migration_action(236)
    from gobby.storage.tasks import LocalTaskManager

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Preserve caps")

    stage_columns = table_columns(temp_db, "task_stage_states")
    assert {"max_work_attempts", "max_review_rounds"} <= stage_columns
    assert table_columns(temp_db, "task_artifacts").isdisjoint(LEGACY_CAP_COLUMNS)

    rows = temp_db.fetchall(
        """
        SELECT stage_name, max_work_attempts, max_review_rounds
          FROM task_stage_states
         WHERE task_id = ?
        """,
        (task.id,),
    )
    assert rows


def test_no_runtime_reader_references_legacy_cap_columns() -> None:
    text = "\n".join(
        source_text(path)
        for path in (
            "src/gobby/storage/tasks/_artifacts.py",
            "src/gobby/dispatch/rules.py",
            "src/gobby/mcp_proxy/tools/tasks/_artifacts.py",
            "src/gobby/servers/routes/tasks.py",
            "src/gobby/cli/tasks/crud.py",
        )
    )
    for column in LEGACY_CAP_COLUMNS:
        assert f"task_artifacts.{column}" not in text
        assert f"TaskArtifacts.{column}" not in text


def test_legacy_column_audit_grep_returns_zero_runtime_matches() -> None:
    runtime_files = (
        "src/gobby/storage/tasks/_crud.py",
        "src/gobby/storage/tasks/_manager.py",
        "src/gobby/tasks/state_semantics.py",
        "src/gobby/sync/tasks.py",
        "src/gobby/dispatch/rules.py",
        "src/gobby/mcp_proxy/tools/tasks/_crud.py",
        "src/gobby/mcp_proxy/tools/tasks/_search.py",
        "src/gobby/servers/routes/tasks.py",
        "src/gobby/agents/lifecycle_monitor.py",
        "src/gobby/workflows/pipeline_heartbeat.py",
        "src/gobby/hooks/event_handlers/_plan.py",
    )
    scoped = "\n".join(source_text(path) for path in runtime_files)
    for legacy_pattern in (
        r"\btasks\.status\b",
        r"\btasks\.lifecycle\b",
        r"\bTask\.status\b",
        r"\bstatus\s*=\s*\?",
        r"\bstatus\s+IN\s*\(",
        r"\blifecycle\s*=\s*\?",
        r"\blifecycle_stage\b",
    ):
        assert not re.search(legacy_pattern, scoped)


def test_dynamic_dict_write_audit_returns_zero_matches() -> None:
    scoped = source_text("src/gobby/sync/tasks.py") + source_text(
        "src/gobby/storage/tasks/_crud.py"
    )
    for legacy_key in ("'status':", "'lifecycle':", "'lifecycle_stage':"):
        assert legacy_key not in scoped
    for legacy_key in ('"status":', '"lifecycle":', '"lifecycle_stage":'):
        assert legacy_key not in scoped


def test_post_port_grep_finds_no_state_sql_helpers_or_lifecycle_stage_in_runtime_scopes() -> None:
    scoped = "\n".join(
        source_text(path)
        for path in (
            "src/gobby/storage/tasks/_aggregates.py",
            "src/gobby/storage/tasks/_search.py",
            "src/gobby/mcp_proxy/tools/tasks/_crud.py",
            "src/gobby/mcp_proxy/tools/tasks/_search.py",
            "src/gobby/servers/routes/admin/_stats.py",
            "src/gobby/cli/tasks/_utils.py",
        )
    )
    for forbidden in (
        "status_filter_sql",
        "canonical_status_case",
        "is_ready_sql",
        "lifecycle_stage",
    ):
        assert forbidden not in scoped
