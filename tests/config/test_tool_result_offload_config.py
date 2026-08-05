"""Tests for tool-result offload configuration and schema."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig, load_config
from gobby.config.features import ToolResultOffloadConfig
from gobby.storage.hub.protocol import HubDatabase


@pytest.mark.unit
def test_tool_result_offload_defaults_and_app_accessor() -> None:
    config = ToolResultOffloadConfig()

    assert config.model_dump() == {
        "enabled": True,
        "threshold_chars": 15_000,
        "max_envelope_chars": 8_000,
        "preview_chars": 2_000,
        "chunk_chars": 2_000,
        "max_stored_chars": 2_000_000,
        "intent_match_limit": 5,
        "retention_days": 7,
        "exempt_tools": [],
    }

    daemon_config = DaemonConfig()
    assert daemon_config.get_tool_result_offload_config() is daemon_config.tool_result_offload


def test_tool_result_offload_threshold_config_store_override_wins(tmp_path: Path) -> None:
    class DummyConfigStore:
        def get_all(self) -> dict[str, object]:
            return {"tool_result_offload.threshold_chars": 21_000}

    config = load_config(
        config_file=str(tmp_path / "missing.yaml"),
        config_store=DummyConfigStore(),
    )

    assert config.tool_result_offload.threshold_chars == 21_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_envelope_chars": 1},
        {"chunk_chars": 0},
        {"retention_days": 0},
        {"preview_chars": 8_001},
        {"max_envelope_chars": 15_000},
        {"max_stored_chars": 9_999},
        {"retention_days": 100_000},
    ],
    ids=[
        "envelope-below-floor",
        "zero-chunk",
        "zero-retention",
        "preview-exceeds-envelope",
        "envelope-not-strict-win",
        "storage-below-threshold",
        "retention-above-cap",
    ],
)
def test_tool_result_offload_rejects_reachable_invalid_values(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValidationError):
        ToolResultOffloadConfig(**kwargs)


@pytest.mark.integration
def test_tool_results_schema_is_applied(temp_db: HubDatabase) -> None:
    tables = {
        row["table_name"]
        for row in temp_db.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name IN ('tool_results', 'tool_result_chunks')
            """
        ).fetchall()
    }
    assert tables == {"tool_results", "tool_result_chunks"}

    chunk_indexes = {
        row["indexname"]: row["indexdef"]
        for row in temp_db.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'tool_result_chunks'
            """
        ).fetchall()
    }
    search_index = chunk_indexes["tool_result_chunks_search_bm25"]
    assert "USING bm25 (id, content)" in search_index
    assert "key_field=id" in search_index

    check_constraints = [
        row["definition"]
        for row in temp_db.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid = 'tool_results'::regclass
              AND contype = 'c'
            """
        ).fetchall()
    ]
    assert any(
        "content_kind" in definition and "'json'" in definition and "'text'" in definition
        for definition in check_constraints
    )
