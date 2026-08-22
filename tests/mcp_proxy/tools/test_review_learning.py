from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.tools import review_learning as review_learning_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.review_learning import (
    create_review_learning_registry as _create_review_learning_registry,
)
from gobby.mcp_proxy.tools.tasks import _factory as task_factory
from gobby.mcp_proxy.tools.tasks import _ops_factory as task_ops_factory
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.memory.manager import MemoryManager
from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.file_paths import path_tag
from gobby.review_learning.service import (
    ReviewLearningMemoryManager,
    ReviewLearningService,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from tests.review_learning.conftest import (
    FakeDB,
    FakeMemory,
    FakeMemoryManager,
    FakeTaskManager,
)

pytestmark = pytest.mark.unit
SESSION_ID = "11111111-1111-1111-1111-111111111111"

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


def _scoped_memory_manager(project_id: str = "_personal") -> FakeMemoryManager:
    return FakeMemoryManager(db=FakeDB(session_id=SESSION_ID, project_id=project_id))


def create_review_learning_registry(
    memory_manager: FakeMemoryManager,
    task_manager: FakeTaskManager,
) -> InternalToolRegistry:
    service = ReviewLearningService(
        memory_manager=cast(ReviewLearningMemoryManager, memory_manager),
        task_manager=cast(RetirementTaskManager, task_manager),
    )
    return _create_review_learning_registry(service)


def test_create_review_learning_registry_registers_class_recall_tools() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    assert registry.name == "gobby-review-learning"
    tool_names = {tool["name"] for tool in registry.list_tools()}
    assert tool_names == {
        "list_check_keys",
        "recall_review_context",
        "recall_review_lessons_by_class",
        "recall_review_lessons_for_files",
        "record_review_lesson",
        "retire_review_lesson",
    }


def test_shared_service_identity(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[RegistryContext] = []
    review_services: list[ReviewLearningService] = []
    original_context = RegistryContext
    original_review_factory = review_learning_tools.create_review_learning_registry

    def record_context(**kwargs: Any) -> RegistryContext:
        context = original_context(**kwargs)
        contexts.append(context)
        return context

    def record_review_factory(service: ReviewLearningService) -> InternalToolRegistry:
        review_services.append(service)
        return original_review_factory(service)

    monkeypatch.setattr(task_factory, "RegistryContext", record_context)
    monkeypatch.setattr(task_ops_factory, "RegistryContext", record_context)
    monkeypatch.setattr(
        review_learning_tools,
        "create_review_learning_registry",
        record_review_factory,
    )
    memory_manager = _scoped_memory_manager(sample_project["id"])

    manager = setup_internal_registries(
        config_resolver=lambda: DaemonConfig(),
        memory_manager_resolver=lambda: cast(MemoryManager, memory_manager),
        task_manager=LocalTaskManager(temp_db),
    )

    assert manager.get_registry("gobby-review-learning") is not None
    assert len(contexts) >= 2
    assert len(review_services) == 1
    assert all(context.review_learning_service is review_services[0] for context in contexts)


def test_recall_review_context_schema_documents_finding_shapes() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    schema = registry.get_schema("recall_review_context")

    assert schema is not None
    findings_schema = schema["inputSchema"]["properties"]["findings"]
    assert findings_schema["type"] == "array"
    item_variants = findings_schema["items"]["oneOf"]
    object_schema = next(item for item in item_variants if item["type"] == "object")
    string_schema = next(item for item in item_variants if item["type"] == "string")
    assert string_schema["description"] == "Plain finding message."
    for property_name in (
        "title",
        "message",
        "suggestion",
        "path",
        "symbol",
        "rule_id",
        "query_hints",
    ):
        assert "description" in object_schema["properties"][property_name]


def test_record_review_lesson_schema_documents_required_field_groups() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    schema = registry.get_schema("record_review_lesson")

    assert schema is not None
    description = schema["description"]
    assert "non-empty title or message" in description
    assert "non-empty principle or prevention" in description


@pytest.mark.asyncio
async def test_recall_review_context_groups_matches_per_finding() -> None:
    memory_manager = _scoped_memory_manager()
    await memory_manager.create_memory(
        "Local memory",
        tags=["review-lesson", "pattern:example"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_context",
        {"findings": [{"title": "Local memory"}], "session_id": SESSION_ID},
    )

    assert result["success"] is True
    assert result["findings"][0]["finding_index"] == 0
    assert result["findings"][0]["matches"][0]["memory_id"] == "mem-1"


@pytest.mark.asyncio
async def test_recall_review_context_accepts_string_findings() -> None:
    memory_manager = _scoped_memory_manager()
    await memory_manager.create_memory(
        "Local memory",
        tags=["review-lesson", "pattern:example"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_context",
        {"findings": ["Local memory"], "session_id": SESSION_ID},
    )

    assert result["success"] is True
    assert result["findings"][0]["matches"][0]["memory_id"] == "mem-1"
    assert memory_manager.search_queries[0]["query"] == "Local memory"


@pytest.mark.asyncio
async def test_recall_review_context_rejects_invalid_finding_items() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    result = await registry.call(
        "recall_review_context",
        {"findings": [123]},
    )

    assert result == {
        "success": False,
        "error": "findings[0] must be an object or string",
    }


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
    memory_manager = _scoped_memory_manager()
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
            "session_id": SESSION_ID,
        },
    )

    result = await registry.call(
        "recall_review_lessons_for_files",
        {"file_paths": ["src/gobby/wiki/scheduled_jobs.py"], "session_id": SESSION_ID},
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
async def test_recall_review_lessons_matches_path_tags_across_checkouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_root = tmp_path / "main"
    worktree_root = tmp_path / "worktree"
    for root in (main_root, worktree_root):
        (root / ".gobby").mkdir(parents=True)
        (root / ".gobby" / "project.json").write_text("{}", encoding="utf-8")

    relative_path = Path("src/gobby/wiki/scheduled_jobs.py")
    memory_manager = _scoped_memory_manager()
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())
    await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-absolute-worktree",
            "decision": "confirmed",
            "finding": {
                "title": "Use shared coordinator",
                "pattern_id": "shared-coordinator",
                "path": str(worktree_root / relative_path),
                "principle": "Scheduled writes must route through the coordinator.",
            },
            "evidence": {"commit": "abc"},
            "session_id": SESSION_ID,
        },
    )
    await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-other-path",
            "decision": "confirmed",
            "finding": {
                "title": "Use shared coordinator elsewhere",
                "pattern_id": "shared-coordinator-other-path",
                "path": str(worktree_root / "src/gobby/wiki/other.py"),
                "principle": "Scheduled writes must route through the coordinator.",
            },
            "evidence": {"commit": "def"},
            "session_id": SESSION_ID,
        },
    )

    # Recording from an absolute worktree path must produce the same
    # repo-relative path tag that a main-checkout path normalizes to.
    assert path_tag(relative_path.as_posix()) in (memory_manager.memories[0].tags or [])

    list_calls: list[list[str]] = []
    original_alist_memories = memory_manager.alist_memories

    async def recording(**kwargs: Any) -> list[FakeMemory]:
        list_calls.append(list(kwargs.get("tags_all") or []))
        return await original_alist_memories(**kwargs)

    monkeypatch.setattr(memory_manager, "alist_memories", recording)

    result = await registry.call(
        "recall_review_lessons_for_files",
        {
            "file_paths": [str(main_root / relative_path)],
            "session_id": SESSION_ID,
        },
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert [lesson["pattern_id"] for lesson in result["lessons"]] == ["shared-coordinator"]
    assert list_calls == [["review-lesson", "confirmed"]]


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
async def test_recall_review_lessons_for_files_matches_legacy_evidence_paths(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "gobby-cli"
    (project_root / ".gobby").mkdir(parents=True)
    (project_root / ".gobby" / "project.json").write_text("{}", encoding="utf-8")
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
            "file_paths": [str(project_root / "crates/gcode/src/config/services.rs")],
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


async def test_recall_uses_anchored_evidence_and_renders_avoid_only_guidance() -> None:
    memory_manager = FakeMemoryManager()
    await memory_manager.create_memory(
        """# Review Lesson: Finding mentions ## Evidence inline

## Identity
- pattern_id: avoid-bare-except

## Lesson
- principle: Exception handlers should preserve actionable failures.
- prevention: Avoid using bare except.

## Evidence
{"changed_files": ["src/gobby/review_learning/service.py"]}
""",
        tags=["review-lesson", "confirmed", "pattern:avoid-bare-except"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_lessons_for_files",
        {
            "file_paths": ["src/gobby/review_learning/service.py"],
            "project_id": "_personal",
        },
    )

    assert result["count"] == 1
    assert result["lessons"][0]["evidence_path"] == "src/gobby/review_learning/service.py"
    assert "  Avoid: using bare except" in result["message"]
    assert "  Do:" not in result["message"]


@pytest.mark.asyncio
async def test_recall_review_lessons_for_files_excludes_global_review_lessons(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "gobby-cli"
    (project_root / ".gobby").mkdir(parents=True)
    (project_root / ".gobby" / "project.json").write_text("{}", encoding="utf-8")
    memory_manager = FakeMemoryManager()
    await memory_manager.create_memory(
        LEGACY_SERVICE_CONFIG_LESSON.replace(
            "service-config-propagate-db-errors",
            "global-service-config-propagate-db-errors",
        ),
        tags=[
            "review-lesson",
            "confirmed",
            "pattern:global-service-config-propagate-db-errors",
        ],
        project_id=None,
    )
    await memory_manager.create_memory(
        LEGACY_SERVICE_CONFIG_LESSON,
        tags=["review-lesson", "confirmed", "pattern:service-config-propagate-db-errors"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_lessons_for_files",
        {
            "file_paths": [str(project_root / "crates/gcode/src/config/services.rs")],
            "project_id": "_personal",
        },
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["lessons"][0]["memory_id"] == "mem-2"
    assert result["lessons"][0]["pattern_id"] == "service-config-propagate-db-errors"


@pytest.mark.asyncio
async def test_empty_file_lessons_do_not_consume_limit() -> None:
    relative_path = "src/gobby/review_learning/service.py"
    memory_manager = _scoped_memory_manager()
    memory_manager.memories.append(
        FakeMemory(
            id="empty-lesson",
            content=(
                "# Review Lesson: Empty\n\n"
                "## Identity\n"
                "- pattern_id: empty-lesson\n\n"
                "## Lesson\n"
                "- principle: \n"
                "- prevention: \n"
            ),
            project_id="_personal",
            tags=["review-lesson", "confirmed", path_tag(relative_path)],
        )
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())
    recorded = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-actionable",
            "decision": "confirmed",
            "finding": {
                "title": "Keep actionable lesson",
                "pattern_id": "actionable-lesson",
                "path": relative_path,
                "principle": "Actionable lessons must remain visible.",
            },
            "evidence": {"commit": "abc"},
            "session_id": SESSION_ID,
        },
    )
    assert recorded["success"] is True

    result = await registry.call(
        "recall_review_lessons_for_files",
        {
            "file_paths": [relative_path],
            "session_id": SESSION_ID,
            "limit": 1,
        },
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert [lesson["pattern_id"] for lesson in result["lessons"]] == ["actionable-lesson"]
    assert "empty-lesson" not in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["confirmed", "no-fix-policy"])
@pytest.mark.parametrize(
    ("finding", "missing_groups"),
    [
        ({"principle": "Use actionable guidance."}, ("title or message",)),
        ({"title": "Missing guidance"}, ("principle or prevention",)),
        (
            {
                "title": " ",
                "message": "\t",
                "principle": "\n",
                "prevention": " ",
            },
            ("title or message", "principle or prevention"),
        ),
        ({}, ("title or message", "principle or prevention")),
    ],
)
async def test_record_review_lesson_rejects_missing_required_field_groups(
    decision: str,
    finding: dict[str, str],
    missing_groups: tuple[str, ...],
) -> None:
    memory_manager = _scoped_memory_manager()
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-invalid",
            "decision": decision,
            "finding": finding,
            "evidence": {},
            "session_id": SESSION_ID,
        },
    )

    assert result["success"] is False
    for missing_group in missing_groups:
        assert missing_group in result["error"]
    assert memory_manager.memories == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finding",
    [
        {"title": "Principle only", "principle": "Keep the core rule explicit."},
        {"message": "Prevention only", "prevention": "Run the focused guardrail check."},
    ],
)
async def test_record_review_lesson_accepts_principle_or_prevention(
    finding: dict[str, str],
) -> None:
    memory_manager = _scoped_memory_manager()
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-valid",
            "decision": "confirmed",
            "finding": finding,
            "evidence": {},
            "session_id": SESSION_ID,
        },
    )

    assert result["success"] is True
    assert len(memory_manager.memories) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["stale", "invalid"])
async def test_record_review_lesson_handles_stale_invalid_noops(decision: str) -> None:
    memory_manager = FakeMemoryManager()
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-1",
            "decision": decision,
            "finding": {},
            "evidence": {},
        },
    )

    assert result["success"] is True
    assert result["skipped_reason"] == decision
    assert memory_manager.memories == []
