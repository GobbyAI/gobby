from __future__ import annotations

import pytest

from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry
from tests.review_learning.conftest import FakeDB, FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit

LEGACY_SERVICE_CONFIG_LESSON = """# Review Lesson: Propagate service config-store read failures

## Identity
- pattern_id: service-config-propagate-db-errors
- pattern_key: service-config-propagate-db-errors

## Provenance
- decision: confirmed
- repo: /Users/josh/Projects/gobby-cli
- language: rust

## Lesson
- principle: Service configuration resolution should treat missing config_store as absent config while propagating real database read failures.
- root_cause:
- prevention: Use fallible service-config sources for FalkorDB/Qdrant/vector settings; map undefined config_store to None, preserve env precedence for service keys, and avoid ok().flatten() on database reads.

## Diagnostic
- path:

## Evidence
{"changed_files": ["crates/gcode/src/config/services.rs", "crates/gcode/src/config/context.rs"]}
"""


def test_create_review_learning_registry_registers_two_tools() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    assert registry.name == "gobby-review-learning"
    tool_names = {tool["name"] for tool in registry.list_tools()}
    assert tool_names == {
        "recall_review_context",
        "recall_review_lessons_for_files",
        "record_review_lesson",
    }


@pytest.mark.asyncio
async def test_recall_review_context_groups_matches_per_finding() -> None:
    memory_manager = FakeMemoryManager()
    await memory_manager.create_memory(
        "Local memory",
        tags=["review-lesson", "pattern:example"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_context",
        {"findings": [{"title": "Local memory"}]},
    )

    assert result["success"] is True
    assert result["findings"][0]["finding_index"] == 0
    assert result["findings"][0]["matches"][0]["memory_id"] == "mem-1"


@pytest.mark.asyncio
async def test_record_review_lesson_preserves_session_scope() -> None:
    session_id = "11111111-1111-1111-1111-111111111111"
    memory_manager = FakeMemoryManager(db=FakeDB(session_id=session_id, project_id="project-a"))
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-1",
            "decision": "confirmed",
            "finding": {
                "title": "Reusable finding",
                "pattern_id": "pattern-a",
                "principle": "Prefer local convention",
            },
            "evidence": {"commit": "abc"},
            "session_id": session_id,
        },
    )

    assert result["success"] is True
    assert memory_manager.memories[0].project_id == "project-a"
    assert memory_manager.memories[0].source_session_id == session_id


@pytest.mark.asyncio
async def test_recall_review_lessons_for_files_matches_recorded_finding_path() -> None:
    memory_manager = FakeMemoryManager()
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())
    await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-1",
            "decision": "confirmed",
            "finding": {
                "title": "Use shared coordinator",
                "pattern_id": "shared-coordinator",
                "path": "src/gobby/wiki/scheduled_jobs.py",
                "principle": "Scheduled writes must route through the coordinator.",
                "prevention": "Delegate scheduled writes through WikiUpdateCoordinator.",
            },
            "evidence": {"commit": "abc"},
        },
    )

    result = await registry.call(
        "recall_review_lessons_for_files",
        {"file_paths": ["src/gobby/wiki/scheduled_jobs.py"]},
    )

    assert result["success"] is True
    assert result["count"] == 1
    lesson = result["lessons"][0]
    assert lesson["memory_id"] == "mem-1"
    assert lesson["pattern_id"] == "shared-coordinator"
    assert lesson["matched_file_path"] == "src/gobby/wiki/scheduled_jobs.py"
    assert lesson["principle"] == "Scheduled writes must route through the coordinator."
    assert "WikiUpdateCoordinator" in lesson["prevention"]
    assert "<review-guidance>" in result["message"]
    assert "## Evidence" not in result["message"]


@pytest.mark.asyncio
async def test_recall_review_lessons_for_files_ignores_unrelated_path() -> None:
    memory_manager = FakeMemoryManager()
    await memory_manager.create_memory(
        LEGACY_SERVICE_CONFIG_LESSON,
        tags=["review-lesson", "confirmed", "pattern:service-config-propagate-db-errors"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_lessons_for_files",
        {
            "file_paths_json": '["crates/gcode/src/config/other.rs"]',
            "project_id": "_personal",
        },
    )

    assert result["success"] is True
    assert result["count"] == 0
    assert result["lessons"] == []
    assert result["message"] == ""


@pytest.mark.asyncio
async def test_recall_review_lessons_for_files_matches_legacy_evidence_paths() -> None:
    memory_manager = FakeMemoryManager()
    await memory_manager.create_memory(
        LEGACY_SERVICE_CONFIG_LESSON,
        tags=["review-lesson", "confirmed", "pattern:service-config-propagate-db-errors"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_lessons_for_files",
        {
            "file_paths": ["/Users/josh/Projects/gobby-cli/crates/gcode/src/config/services.rs"],
            "project_id": "_personal",
        },
    )

    assert result["success"] is True
    assert result["count"] == 1
    lesson = result["lessons"][0]
    assert lesson["memory_id"] == "mem-1"
    assert lesson["pattern_id"] == "service-config-propagate-db-errors"
    assert lesson["evidence_path"] == "crates/gcode/src/config/services.rs"
    assert "missing config_store as absent config" in lesson["principle"]
    assert "preserve env precedence" in lesson["prevention"]
    assert "database reads" in lesson["avoid"]
    assert "## Provenance" not in result["message"]


@pytest.mark.asyncio
async def test_record_review_lesson_handles_stale_invalid_noops() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    result = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-1",
            "decision": "stale",
            "finding": {"title": "stale"},
            "evidence": {},
        },
    )

    assert result["success"] is True
    assert result["skipped_reason"] == "stale"
