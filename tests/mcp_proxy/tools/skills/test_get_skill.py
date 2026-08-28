"""Tests for get_skill MCP tool (TDD - written before implementation)."""

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager, SkillFile


def _skill_file(skill_id: str, path: str, file_type: str = "reference") -> SkillFile:
    content = f"content:{path}"
    return SkillFile(
        id="",
        skill_id=skill_id,
        path=path,
        file_type=file_type,
        content=content,
        content_hash="0" * 64,
        size_bytes=len(content.encode()),
    )


def _make_level_session(db: HubDatabase, project_id: str) -> Session:
    return SessionManager(db).register(
        external_id="level-ext-id",
        machine_id=None,
        source="claude",
        project_id=project_id,
    )


pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    """Create a fresh database with migrations applied."""
    database = temp_db
    yield database


@pytest.fixture
def project_id(db: HubDatabase) -> str:
    """Create a test project and return its ID."""
    project_mgr = LocalProjectManager(db)
    project = project_mgr.create(name="test-project", repo_path="/tmp/test-skills")
    return project.id


@pytest.fixture
def storage(db: HubDatabase) -> LocalSkillManager:
    """Create a LocalSkillManager for storage operations."""
    return LocalSkillManager(db)


@pytest.fixture
def populated_db(db: HubDatabase, storage: LocalSkillManager) -> HubDatabase:
    """Create database with test skills."""
    storage.create_skill(
        name="git-commit",
        description="Generate conventional commit messages",
        content="# Git Commit Helper\n\nThis skill helps you write commit messages.\n\n## Usage\n\n...",
        version="1.0.0",
        license="MIT",
        compatibility="Claude 3.5+",
        allowed_tools=["Bash", "Read"],
        metadata={
            "skillport": {
                "category": "git",
                "tags": ["git", "commits"],
                "alwaysApply": False,
            }
        },
        enabled=True,
    )
    storage.create_skill(
        name="minimal-skill",
        description="A minimal skill",
        content="# Minimal\n\nContent",
        enabled=True,
    )
    return db


@pytest.mark.integration
class TestGetSkillTool:
    """Tests for get_skill MCP tool."""

    @pytest.mark.asyncio
    async def test_get_skill_by_name(self, populated_db):
        """Test getting a skill by name returns full content."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", brief=False)

        assert result["success"] is True
        assert result["skill"]["name"] == "git-commit"
        assert "Git Commit Helper" in result["skill"]["content"]

    @pytest.mark.asyncio
    async def test_get_skill_returns_full_content(self, populated_db):
        """Test that get_skill returns full content field."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", brief=False)

        assert result["success"] is True
        skill = result["skill"]

        # Full content should be present
        assert "content" in skill
        assert len(skill["content"]) > 50  # Not truncated

    @pytest.mark.asyncio
    async def test_get_skill_returns_all_fields(self, populated_db):
        """Test that get_skill returns all skill fields."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", brief=False)

        assert result["success"] is True
        skill = result["skill"]

        # All fields should be present
        assert skill["id"] is not None
        assert skill["name"] == "git-commit"
        assert skill["description"] == "Generate conventional commit messages"
        assert skill["version"] == "1.0.0"
        assert skill["license"] == "MIT"
        assert skill["compatibility"] == "Claude 3.5+"
        assert skill["allowed_tools"] == ["Bash", "Read"]
        assert skill["enabled"] is True

    @pytest.mark.asyncio
    async def test_get_skill_returns_metadata(self, populated_db):
        """Test that get_skill returns metadata including skillport."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", brief=False)

        assert result["success"] is True
        skill = result["skill"]

        # Metadata should be present
        assert "metadata" in skill
        assert skill["metadata"]["skillport"]["category"] == "git"
        assert "git" in skill["metadata"]["skillport"]["tags"]

    @pytest.mark.asyncio
    async def test_get_skill_not_found(self, populated_db):
        """Test that get_skill returns error for non-existent skill."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_get_skill_by_id(self, populated_db, storage):
        """Test getting a skill by ID."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Get the actual skill ID
        skill = storage.get_by_name("git-commit")

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(skill_id=skill.id)

        assert result["success"] is True
        assert result["skill"]["name"] == "git-commit"

    @pytest.mark.asyncio
    async def test_get_skill_prefers_id_over_name(self, populated_db, storage):
        """Test that skill_id takes precedence over name."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Get the actual skill ID for minimal-skill
        skill = storage.get_by_name("minimal-skill")

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        # Pass both id and name - id should win
        result = await tool(skill_id=skill.id, name="git-commit")

        assert result["success"] is True
        assert result["skill"]["name"] == "minimal-skill"

    @pytest.mark.asyncio
    async def test_get_skill_requires_identifier(self, populated_db):
        """Test that get_skill requires either name or skill_id."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool()

        assert result["success"] is False
        assert "name or skill_id" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_get_skill_minimal_fields(self, populated_db):
        """Test getting a skill with minimal fields set."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="minimal-skill", brief=False)

        assert result["success"] is True
        skill = result["skill"]

        # Should still have the fields even if None
        assert skill["name"] == "minimal-skill"
        assert skill["version"] is None
        assert skill["license"] is None
        assert skill["compatibility"] is None
        assert skill["allowed_tools"] == []

    @pytest.mark.asyncio
    async def test_get_skill_records_usage_with_session_id(self, populated_db, project_id):
        """Test that passing session_id records skill usage in session_skills."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Create a session to track against
        session_mgr = SessionManager(populated_db)
        session = session_mgr.register(
            external_id="test-ext-id",
            machine_id=None,
            source="claude",
            project_id=project_id,
        )

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", session_id=session.id)

        assert result["success"] is True

        # Verify skill usage was recorded
        row = populated_db.fetchone(
            "SELECT skill_name FROM session_skills WHERE session_id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["skill_name"] == "git-commit"

    @pytest.mark.asyncio
    async def test_get_skill_appends_loaded_skills_variable(self, populated_db, project_id):
        """A served skill is granted in loaded_skills without a PostToolUse echo."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.workflows.state_manager import SessionVariableManager

        session_mgr = SessionManager(populated_db)
        session = session_mgr.register(
            external_id="test-ext-id",
            machine_id=None,
            source="codex",
            project_id=project_id,
        )

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", session_id=session.id)
        assert result["success"] is True
        # A repeat load stays deduplicated (set semantics).
        await tool(name="git-commit", session_id=session.id)

        variables = SessionVariableManager(populated_db).get_variables(session.id)
        assert variables["loaded_skills"] == ["git-commit"]

    @pytest.mark.asyncio
    async def test_get_skill_without_session_id_skips_tracking(self, populated_db):
        """Test that omitting session_id does not record skill usage."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit")

        assert result["success"] is True

        # No usage should be recorded
        row = populated_db.fetchone("SELECT COUNT(*) AS count FROM session_skills", ())
        assert row["count"] == 0

    @pytest.mark.asyncio
    async def test_get_skill_tracking_is_idempotent(self, populated_db, project_id):
        """Test that calling get_skill twice with same session records only one row."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        session_mgr = SessionManager(populated_db)
        session = session_mgr.register(
            external_id="test-ext-id",
            machine_id=None,
            source="claude",
            project_id=project_id,
        )

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        await tool(name="git-commit", session_id=session.id)
        await tool(name="git-commit", session_id=session.id)

        row = populated_db.fetchone(
            "SELECT COUNT(*) AS count FROM session_skills WHERE session_id = %s",
            (session.id,),
        )
        assert row["count"] == 1

    @pytest.mark.asyncio
    async def test_get_skill_tracking_bad_session_does_not_fail(self, populated_db):
        """Test that an invalid session_id doesn't break the skill lookup."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(populated_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="git-commit", session_id="nonexistent-session")

        # Skill lookup should still succeed
        assert result["success"] is True
        assert result["skill"]["name"] == "git-commit"

    @pytest.mark.asyncio
    async def test_get_skill_resolves_internal_skill_by_name(
        self, db: HubDatabase, storage: LocalSkillManager
    ) -> None:
        """get_skill must still resolve internal skills — they're loaded by other skills."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        storage.create_skill(
            name="plan-methodology",
            description="Internal drafting methodology",
            content="# Methodology\n\nInternal content.",
            metadata={"internal": True},
            enabled=True,
        )

        registry = create_skills_registry(db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="plan-methodology")

        assert result["success"] is True
        assert result["skill"]["name"] == "plan-methodology"
        assert "Internal content." in result["skill"]["content"]


@pytest.fixture
def leveled_db(db: HubDatabase, storage: LocalSkillManager) -> HubDatabase:
    """Create database with a leveled skill and a plain (non-leveled) skill."""
    storage.create_skill(
        name="leveled-skill",
        description="A skill with levels",
        content="# Leveled\n\nContent",
        metadata={"gobby": {"levels": ["lite", "normal", "max"], "default_level": "normal"}},
        enabled=True,
    )
    storage.create_skill(
        name="plain-skill",
        description="A skill without levels",
        content="# Plain\n\nContent",
        enabled=True,
    )
    return db


@pytest.mark.integration
class TestGetSkillLevels:
    """Tests for the level parameter on get_skill."""

    @pytest.mark.asyncio
    async def test_explicit_level_stamps_content_and_sets_variable(self, leveled_db, project_id):
        """Valid level stamps content and persists <skill>_level (hyphen -> underscore)."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.workflows.state_manager import SessionVariableManager

        session = _make_level_session(leveled_db, project_id)
        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", level="max", session_id=session.id)

        assert result["success"] is True
        assert result["skill"]["content"].startswith("Active level: max\n\n")
        variables = SessionVariableManager(leveled_db).get_variables(session.id)
        assert variables["leveled_skill_level"] == "max"


class TestSkillFileManifest:
    @pytest.mark.asyncio
    async def test_manifest_shape_and_caps(
        self, db: HubDatabase, storage: LocalSkillManager
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        skill = storage.create_skill(
            name="manifest-shape",
            description="Manifest fixture",
            content="# Manifest",
        )
        storage.set_skill_files(
            skill.id,
            [
                *[_skill_file(skill.id, f"zz-references/{index:03d}.md") for index in range(105)],
                _skill_file(skill.id, "scripts/build/run.py", "script"),
                _skill_file(skill.id, "scripts/build/check.py", "script"),
                _skill_file(skill.id, "scripts/test/test.py", "script"),
            ],
        )

        tool = create_skills_registry(db).get_tool("get_skill")
        assert tool is not None
        result = await tool(skill_id=skill.id, brief=False)

        assert result["success"] is True
        manifest = result["files"]
        assert len(manifest["entries"]) <= 100
        assert manifest["total_files"] == 105
        assert manifest["remaining_file_count"] == 105 - len(manifest["entries"])
        assert all("content_hash" not in entry for entry in manifest["entries"])
        assert manifest["scripts"]["total_files"] == 3
        assert manifest["scripts"]["per_top_level_dir"] == {"build": 2, "test": 1}
        assert "materialize_skill_scripts" in manifest["scripts"]["note"]
        assert skill.id in manifest["overflow_note"]
        assert all(entry["path"].startswith("zz-references/") for entry in manifest["entries"])

    @pytest.mark.asyncio
    async def test_get_skill_files_tool_paginates_same_prefix(
        self, db: HubDatabase, storage: LocalSkillManager
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        skill = storage.create_skill(
            name="page-files",
            description="Page fixture",
            content="# Page",
        )
        expected = [f"references/{index:03d}.md" for index in range(205)]
        storage.set_skill_files(skill.id, [_skill_file(skill.id, path) for path in expected])
        tool = create_skills_registry(db).get_tool("get_skill_files")
        assert tool is not None

        paths: list[str] = []
        cursor = None
        remaining_counts: list[int] = []
        while True:
            result = await tool(
                skill_id=skill.id,
                path_prefix="references/",
                after_path=cursor,
            )
            assert result["success"] is True
            paths.extend(entry["path"] for entry in result["files"])
            remaining_counts.append(result["remaining_file_count"])
            cursor = result["next_after_path"]
            if cursor is None:
                break

        assert paths == expected
        assert len(paths) == len(set(paths))
        assert remaining_counts[-1] == 0
        assert remaining_counts == sorted(remaining_counts, reverse=True)

    @pytest.mark.asyncio
    async def test_get_skill_files_resolves_same_skill_as_manifest(
        self, db: HubDatabase, storage: LocalSkillManager, project_id: str
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        global_skill = storage.create_skill(
            name="shadowed-files",
            description="Global",
            content="# Global",
        )
        project_skill = storage.create_skill(
            name="shadowed-files",
            description="Project",
            content="# Project",
            project_id=project_id,
        )
        storage.set_skill_files(
            global_skill.id, [_skill_file(global_skill.id, "references/global.md")]
        )
        storage.set_skill_files(
            project_skill.id, [_skill_file(project_skill.id, "references/project.md")]
        )
        registry = create_skills_registry(db, project_id=project_id)
        manifest_tool = registry.get_tool("get_skill")
        files_tool = registry.get_tool("get_skill_files")
        assert manifest_tool is not None
        assert files_tool is not None

        explicit_global = await files_tool(
            skill_id=global_skill.id,
            name="different-name",
        )
        invalid_id_fallback = await files_tool(
            skill_id="00000000-0000-4000-8000-000000000099",
            name="shadowed-files",
        )
        name_only = await files_tool(name="shadowed-files")
        manifest = await manifest_tool(name="shadowed-files", brief=False)

        assert explicit_global["skill_id"] == global_skill.id
        assert [item["path"] for item in explicit_global["files"]] == ["references/global.md"]
        assert invalid_id_fallback["skill_id"] == project_skill.id
        assert name_only["skill_id"] == project_skill.id
        assert [item["path"] for item in name_only["files"]] == ["references/project.md"]
        assert manifest["skill"]["id"] == project_skill.id

        deleted = storage.create_skill(
            name="deleted-selector",
            description="Deleted",
            content="# Deleted",
        )
        storage.delete_skill(deleted.id)
        deleted_fallback = await files_tool(
            skill_id=deleted.id,
            name="shadowed-files",
        )
        assert deleted_fallback["skill_id"] == project_skill.id

        bundled_global = storage.create_skill(
            name="bundled-shadow",
            description="Bundled global",
            content="# Global",
        )
        bundled_project = storage.create_skill(
            name="bundled-shadow",
            description="Bundled project",
            content="# Project",
            project_id=project_id,
        )
        with db.transaction() as conn:
            conn.execute(
                "UPDATE skills SET source_path = %s WHERE id = %s",
                (
                    "/tmp/gobby/install/shared/skills/bundled-shadow/SKILL.md",
                    bundled_project.id,
                ),
            )
        bundled_result = await files_tool(name="bundled-shadow")
        assert bundled_result["skill_id"] == bundled_global.id

    @pytest.mark.asyncio
    async def test_manifest_bounds_scripts_summary(
        self, db: HubDatabase, storage: LocalSkillManager
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.mcp_proxy.tools.skills.get_skill import MAX_MANIFEST_RESPONSE_BYTES

        skill = storage.create_skill(
            name="bounded-manifest",
            description="Bounds fixture",
            content="# Bounds",
        )
        long_suffix = "x" * 900
        files = [
            _skill_file(
                skill.id,
                f"scripts/directory-{index:03d}-{long_suffix}/run.py",
                "script",
            )
            for index in range(190)
        ]
        files.extend(
            _skill_file(skill.id, f"references/{index:03d}-{long_suffix}.md")
            for index in range(120)
        )
        storage.set_skill_files(skill.id, files)

        tool = create_skills_registry(db).get_tool("get_skill")
        assert tool is not None
        result = await tool(skill_id=skill.id, brief=False)

        assert result["success"] is True
        manifest = result["files"]
        encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
        assert len(encoded) <= MAX_MANIFEST_RESPONSE_BYTES
        assert len(manifest["entries"]) <= 100
        assert len(manifest["scripts"]["per_top_level_dir"]) <= 20
        assert manifest["scripts"]["remaining_directory_count"] > 0
        assert all(not item["path"].startswith("scripts/") for item in manifest["entries"])

    @pytest.mark.asyncio
    async def test_get_skill_files_pages_obey_serialized_byte_budget(
        self, db: HubDatabase, storage: LocalSkillManager
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.mcp_proxy.tools.skills.get_skill import MAX_MANIFEST_RESPONSE_BYTES

        skill = storage.create_skill(
            name="bounded-pages",
            description="Bounded page fixture",
            content="# Bounded pages",
        )
        suffix = "x" * 975
        expected = [f"references/{index:03d}-{suffix}.md" for index in range(40)]
        storage.set_skill_files(skill.id, [_skill_file(skill.id, path) for path in expected])
        tool = create_skills_registry(db).get_tool("get_skill_files")
        assert tool is not None

        paths: list[str] = []
        cursor = None
        while True:
            result = await tool(skill_id=skill.id, after_path=cursor)
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            assert len(encoded) <= MAX_MANIFEST_RESPONSE_BYTES
            paths.extend(item["path"] for item in result["files"])
            cursor = result["next_after_path"]
            if cursor is None:
                assert result["remaining_file_count"] == 0
                break

        assert paths == expected
        assert len(paths) == len(set(paths))

    @pytest.mark.asyncio
    async def test_get_skill_files_legacy_oversized_terminal_page(
        self, db: HubDatabase, storage: LocalSkillManager
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        skill = storage.create_skill(
            name="legacy-page",
            description="Legacy page fixture",
            content="# Legacy",
        )
        storage.set_skill_files(
            skill.id,
            [_skill_file(skill.id, "references/visible.md")],
        )
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO skill_files
                   (id, skill_id, path, file_type, content, content_hash,
                    size_bytes, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())""",
                (
                    "89000000-0000-4000-8000-000000000002",
                    skill.id,
                    "z" * 1025,
                    "reference",
                    "legacy",
                    "0" * 64,
                    len("legacy"),
                ),
            )
        tool = create_skills_registry(db).get_tool("get_skill_files")
        assert tool is not None

        result = await tool(
            skill_id=skill.id,
            after_path="references/visible.md",
        )

        assert result["success"] is True
        assert result["files"] == []
        assert result["total_files"] == 0
        assert result["remaining_file_count"] == 0
        assert result["next_after_path"] is None
        assert result["omitted_oversized_path_count"] == 1

    @pytest.mark.asyncio
    async def test_handlers_keep_event_loop_responsive(
        self, leveled_db: HubDatabase, project_id: str
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        session = _make_level_session(leveled_db, project_id)
        calls: list[str] = []
        blocking_started = threading.Event()
        blocking_release = threading.Event()

        async def blocking_runner(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            calls.append(func.__name__)

            def invoke() -> Any:
                blocking_started.set()
                assert blocking_release.wait(timeout=5)
                return func(*args, **kwargs)

            return await asyncio.to_thread(invoke)

        registry = create_skills_registry(leveled_db, run_db=blocking_runner)
        get_skill_tool = registry.get_tool("get_skill")
        get_files_tool = registry.get_tool("get_skill_files")
        assert get_skill_tool is not None
        assert get_files_tool is not None

        async def release_blocked_worker() -> bool:
            started = await asyncio.to_thread(blocking_started.wait, 5)
            if started:
                blocking_release.set()
            return started

        release_task = asyncio.create_task(release_blocked_worker())
        result = await get_skill_tool(
            name="leveled-skill",
            level="max",
            session_id=session.id,
        )
        files_result = await get_files_tool(name="leveled-skill")
        worker_was_released = await release_task

        assert result["success"] is True
        assert files_result["success"] is True
        assert calls == [
            "get_skill_with_manifest",
            "resolve_session_reference",
            "record_skills_used",
            "append_to_set_variable",
            "set_variable",
            "get_skill_file_page",
        ]
        assert worker_was_released is True

    @pytest.mark.asyncio
    async def test_get_skill_reads_one_revision(
        self,
        db: HubDatabase,
        storage: LocalSkillManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        old_metadata = {"gobby": {"runtime": {"skill_release": "1.0.0"}}}
        new_metadata = {"gobby": {"runtime": {"skill_release": "2.0.0"}}}
        skill = storage.create_skill(
            name="revision-snapshot",
            description="Old revision",
            content="# Old",
            metadata=old_metadata,
        )
        storage.set_skill_files(
            skill.id,
            [_skill_file(skill.id, "references/old.md")],
        )
        writer = LocalSkillManager(db)
        original_set_files = writer._set_skill_files
        replacement_written = threading.Event()
        allow_commit = threading.Event()

        def pause_before_commit(*args: Any, **kwargs: Any) -> int:
            changed = original_set_files(*args, **kwargs)
            replacement_written.set()
            assert allow_commit.wait(timeout=5)
            return changed

        monkeypatch.setattr(writer, "_set_skill_files", pause_before_commit)

        def publish() -> None:
            writer.update_skill_with_files(
                skill.id,
                description="New revision",
                content="# New",
                version=None,
                license=None,
                compatibility=None,
                allowed_tools=None,
                metadata=new_metadata,
                files=[_skill_file(skill.id, "references/new.md")],
            )

        tool = create_skills_registry(db).get_tool("get_skill")
        assert tool is not None
        publish_task = asyncio.create_task(asyncio.to_thread(publish))
        assert await asyncio.to_thread(replacement_written.wait, 5)
        try:
            during = await tool(skill_id=skill.id, brief=False)
        finally:
            allow_commit.set()
        await publish_task
        after = await tool(skill_id=skill.id, brief=False)

        assert during["skill"]["description"] == "Old revision"
        assert during["skill"]["metadata"] == old_metadata
        assert [item["path"] for item in during["files"]["entries"]] == ["references/old.md"]
        assert after["skill"]["description"] == "New revision"
        assert after["skill"]["metadata"] == new_metadata
        assert [item["path"] for item in after["files"]["entries"]] == ["references/new.md"]

    @pytest.mark.asyncio
    async def test_manifest_is_one_revision_during_file_replacement(
        self,
        db: HubDatabase,
        storage: LocalSkillManager,
    ) -> None:
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        skill = storage.create_skill(
            name="file-replacement-snapshot",
            description="Replacement snapshot",
            content="# Replacement",
        )
        old_files = [
            _skill_file(skill.id, "references/old-a.md"),
            _skill_file(skill.id, "references/old-b.md"),
            *[_skill_file(skill.id, f"scripts/old/run-{index}.py", "script") for index in range(3)],
        ]
        new_files = [
            *[_skill_file(skill.id, f"references/new-{index}.md") for index in range(4)],
            *[_skill_file(skill.id, f"scripts/new/run-{index}.py", "script") for index in range(5)],
        ]
        storage.set_skill_files(skill.id, old_files)
        tool = create_skills_registry(db).get_tool("get_skill")
        assert tool is not None

        def signature(result: dict[str, Any]) -> tuple[Any, ...]:
            manifest = result["files"]
            scripts = manifest["scripts"]
            return (
                tuple(item["path"] for item in manifest["entries"]),
                manifest["total_files"],
                manifest["remaining_file_count"],
                scripts["total_files"],
                tuple(sorted(scripts["per_top_level_dir"].items())),
                scripts["remaining_directory_count"],
                scripts["remaining_file_count"],
            )

        valid_signatures = {
            (
                ("references/old-a.md", "references/old-b.md"),
                2,
                0,
                3,
                (("old", 3),),
                0,
                0,
            ),
            (
                tuple(f"references/new-{index}.md" for index in range(4)),
                4,
                0,
                5,
                (("new", 5),),
                0,
                0,
            ),
        }

        def publish(start: threading.Barrier, replacement: list[SkillFile]) -> None:
            start.wait()
            storage.set_skill_files(skill.id, replacement)

        for revision in range(12):
            replacement = new_files if revision % 2 == 0 else old_files
            start = threading.Barrier(2)
            publish_task = asyncio.create_task(asyncio.to_thread(publish, start, replacement))
            await asyncio.to_thread(start.wait)
            result = await tool(skill_id=skill.id, brief=False)
            await publish_task

            assert signature(result) in valid_signatures


class TestGetSkillLevelsRemaining:
    @pytest.mark.asyncio
    async def test_omitted_level_uses_default_and_sets_variable(self, leveled_db, project_id):
        """Omitting level on a leveled skill stamps the default and sets the variable."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.workflows.state_manager import SessionVariableManager

        session = _make_level_session(leveled_db, project_id)
        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", session_id=session.id)

        assert result["success"] is True
        assert result["skill"]["content"].startswith("Active level: normal\n\n")
        variables = SessionVariableManager(leveled_db).get_variables(session.id)
        assert variables["leveled_skill_level"] == "normal"

    @pytest.mark.asyncio
    async def test_invalid_level_errors_listing_valid_levels(self, leveled_db):
        """An unknown level fails and the error names the valid options."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", level="bogus")

        assert result["success"] is False
        assert "bogus" in result["message"]
        assert "lite, normal, max" in result["message"]

    @pytest.mark.asyncio
    async def test_level_on_non_leveled_skill_errors(self, leveled_db):
        """Passing level to a skill without metadata.gobby.levels fails."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="plain-skill", level="max")

        assert result["success"] is False
        assert "does not declare levels" in result["message"]

    @pytest.mark.asyncio
    async def test_level_without_session_stamps_but_skips_variable(self, leveled_db):
        """No session_id: content is stamped, no variable is written, call succeeds."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")

        result = await tool(name="leveled-skill", level="lite")

        assert result["success"] is True
        assert result["skill"]["content"].startswith("Active level: lite\n\n")
        row = leveled_db.fetchone("SELECT COUNT(*) AS count FROM session_variables", ())
        assert row["count"] == 0

    @pytest.mark.asyncio
    async def test_non_leveled_skill_without_level_is_unstamped(self, leveled_db):
        """Non-leveled skills load unchanged when level is omitted."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")
        assert tool is not None

        result = await tool(name="plain-skill")

        assert result["success"] is True
        assert result["skill"]["content"] == "# Plain\n\nContent"

    @pytest.mark.asyncio
    async def test_ambient_session_context_persists_level(self, leveled_db, project_id):
        """Wrapper-seeded ambient session context is used when session_id is omitted."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.utils.session_context import session_context_for_test
        from gobby.workflows.state_manager import SessionVariableManager

        session = _make_level_session(leveled_db, project_id)
        registry = create_skills_registry(leveled_db)
        tool = registry.get_tool("get_skill")
        assert tool is not None

        with session_context_for_test(session.id):
            result = await tool(name="leveled-skill", level="max")

        assert result["success"] is True
        variables = SessionVariableManager(leveled_db).get_variables(session.id)
        assert variables["leveled_skill_level"] == "max"
