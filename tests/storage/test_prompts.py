"""Tests for prompt storage (LocalPromptManager)."""

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.prompts import (
    LocalPromptManager,
    PromptChangeEvent,
    PromptChangeNotifier,
    PromptRecord,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase):
    """Create a fresh database with migrations applied."""
    database = temp_db
    yield database


@pytest.fixture
def manager(db):
    """Create a prompt manager for testing."""
    return LocalPromptManager(db)


@pytest.fixture
def manager_dev(db):
    """Create a prompt manager with dev mode enabled."""
    return LocalPromptManager(db, dev_mode=True)


class TestPromptRecord:
    """Tests for the PromptRecord dataclass."""

    def test_to_dict(self) -> None:
        """Test PromptRecord.to_dict() returns all fields."""
        record = PromptRecord(
            id="pmt-123",
            name="expansion/system",
            description="System prompt for expansion",
            content="You are a {{role}}.",
            version="1.0",
            variables={"role": {"type": "str", "default": "assistant"}},
            scope="bundled",
            source_path="/path/to/expansion/system.md",
            project_id=None,
            enabled=True,
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )

        d = record.to_dict()
        assert d["id"] == "pmt-123"
        assert d["name"] == "expansion/system"
        assert d["scope"] == "bundled"
        assert d["variables"]["role"]["type"] == "str"

    def test_to_prompt_template(self) -> None:
        """Test converting PromptRecord to PromptTemplate."""
        record = PromptRecord(
            id="pmt-123",
            name="test/template",
            description="A test template",
            content="Hello, {{ name }}!",
            version="2.0",
            variables={"name": {"type": "str", "default": "World", "required": False}},
            scope="bundled",
            source_path="/path/to/test.md",
        )

        template = record.to_prompt_template()
        assert template.name == "test/template"
        assert template.description == "A test template"
        assert template.content == "Hello, {{ name }}!"
        assert template.version == "2.0"
        assert "name" in template.variables
        assert template.variables["name"].default == "World"


class TestLocalPromptManagerCRUD:
    """Tests for basic CRUD operations."""

    def test_create_prompt(self, manager) -> None:
        """Test creating a prompt."""
        record = manager.create_prompt(
            name="test/prompt",
            description="A test prompt",
            content="Hello world",
            scope="bundled",
        )

        assert record.id.startswith("pmt-")
        assert record.name == "test/prompt"
        assert record.description == "A test prompt"
        assert record.content == "Hello world"
        assert record.scope == "bundled"

    def test_get_prompt(self, manager) -> None:
        """Test retrieving a prompt by ID."""
        created = manager.create_prompt(
            name="test/get",
            content="Content here",
            scope="bundled",
        )

        fetched = manager.get_prompt(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "test/get"

    def test_get_prompt_not_found(self, manager) -> None:
        """Test getting non-existent prompt returns None."""
        assert manager.get_prompt("pmt-nonexistent") is None

    def test_get_by_name_bundled(self, manager) -> None:
        """Test get_by_name returns bundled prompt."""
        manager.create_prompt(
            name="expansion/system",
            content="Bundled content",
            scope="bundled",
        )

        record = manager.get_by_name("expansion/system")
        assert record is not None
        assert record.content == "Bundled content"
        assert record.scope == "bundled"

    def test_update_prompt_dev_mode(self, manager_dev) -> None:
        """Test updating a bundled prompt in dev mode."""
        created = manager_dev.create_prompt(
            name="test/update",
            content="Original",
            scope="bundled",
        )

        updated = manager_dev.update_prompt(created.id, content="Updated")
        assert updated is not None
        assert updated.content == "Updated"

    def test_update_bundled_raises_without_dev_mode(self, manager) -> None:
        """Test that updating bundled prompt raises without dev mode."""
        created = manager.create_prompt(
            name="test/readonly",
            content="Read only",
            scope="bundled",
        )

        with pytest.raises(ValueError, match="bundled"):
            manager.update_prompt(created.id, content="Should fail")

    def test_update_global_allowed(self, manager) -> None:
        """Test that updating global prompt is allowed without dev mode."""
        created = manager.create_prompt(
            name="test/global",
            content="Original",
            scope="global",
        )

        updated = manager.update_prompt(created.id, content="Updated")
        assert updated is not None
        assert updated.content == "Updated"

    def test_delete_prompt_dev_mode(self, manager_dev) -> None:
        """Test deleting a bundled prompt in dev mode."""
        created = manager_dev.create_prompt(
            name="test/delete",
            content="Delete me",
            scope="bundled",
        )

        result = manager_dev.delete_prompt(created.id)
        assert result is True
        assert manager_dev.get_prompt(created.id) is None

    def test_delete_bundled_raises_without_dev_mode(self, manager) -> None:
        """Test that deleting bundled prompt raises without dev mode."""
        created = manager.create_prompt(
            name="test/nodelete",
            content="Protected",
            scope="bundled",
        )

        with pytest.raises(ValueError, match="bundled"):
            manager.delete_prompt(created.id)

    def test_delete_global_allowed(self, manager) -> None:
        """Test that deleting global prompt is allowed."""
        created = manager.create_prompt(
            name="test/deleteglobal",
            content="Delete me",
            scope="global",
        )

        result = manager.delete_prompt(created.id)
        assert result is True


class TestPromptPrecedence:
    """Tests for scope-based precedence."""

    def test_global_overrides_bundled(self, manager) -> None:
        """Test that global scope overrides bundled."""
        manager.create_prompt(
            name="test/precedence",
            content="Bundled version",
            scope="bundled",
        )
        manager.create_prompt(
            name="test/precedence",
            content="Global override",
            scope="global",
        )

        record = manager.get_by_name("test/precedence")
        assert record is not None
        assert record.content == "Global override"
        assert record.scope == "global"

    def test_get_bundled_ignores_override(self, manager) -> None:
        """Test that get_bundled always returns the bundled version."""
        manager.create_prompt(
            name="test/bundled",
            content="Bundled version",
            scope="bundled",
        )
        manager.create_prompt(
            name="test/bundled",
            content="Global override",
            scope="global",
        )

        record = manager.get_bundled("test/bundled")
        assert record is not None
        assert record.content == "Bundled version"
        assert record.scope == "bundled"

    def test_get_by_name_returns_none_when_missing(self, manager) -> None:
        """Test that get_by_name returns None for unknown names."""
        assert manager.get_by_name("nonexistent/prompt") is None


class TestPromptListing:
    """Tests for listing and searching prompts."""

    def test_list_prompts(self, manager) -> None:
        """Test listing all prompts."""
        manager.create_prompt(name="a/first", content="First", scope="bundled")
        manager.create_prompt(name="b/second", content="Second", scope="bundled")

        records = manager.list_prompts()
        names = [r.name for r in records]
        assert "a/first" in names
        assert "b/second" in names

    def test_list_prompts_by_scope(self, manager) -> None:
        """Test filtering by scope."""
        manager.create_prompt(name="test/bundled", content="B", scope="bundled")
        manager.create_prompt(name="test/global", content="G", scope="global")

        bundled = manager.list_prompts(scope="bundled")
        assert all(r.scope == "bundled" for r in bundled)

        global_ = manager.list_prompts(scope="global")
        assert all(r.scope == "global" for r in global_)

    def test_list_prompts_by_category(self, manager) -> None:
        """Test filtering by category (name prefix)."""
        manager.create_prompt(name="expansion/system", content="E", scope="bundled")
        manager.create_prompt(name="memory/extract", content="M", scope="bundled")

        expansion = manager.list_prompts(category="expansion")
        assert all(r.name.startswith("expansion/") for r in expansion)

    def test_list_overrides(self, manager) -> None:
        """Test listing only override prompts."""
        manager.create_prompt(name="test/bundled", content="B", scope="bundled")
        manager.create_prompt(name="test/override", content="O", scope="global")

        overrides = manager.list_overrides()
        assert len(overrides) == 1
        assert overrides[0].name == "test/override"

    def test_count_prompts(self, manager) -> None:
        """Test counting prompts."""
        manager.create_prompt(name="test/one", content="1", scope="bundled")
        manager.create_prompt(name="test/two", content="2", scope="bundled")
        manager.create_prompt(name="test/three", content="3", scope="global")

        assert manager.count_prompts() == 3
        assert manager.count_prompts(scope="bundled") == 2
        assert manager.count_prompts(scope="global") == 1

    def test_search_prompts(self, manager) -> None:
        """Test searching prompts by text."""
        manager.create_prompt(
            name="expansion/system",
            description="System prompt for task expansion",
            content="You are a project manager.",
            scope="bundled",
        )
        manager.create_prompt(
            name="memory/extract",
            description="Memory extraction prompt",
            content="Extract memories from session.",
            scope="bundled",
        )

        results = manager.search_prompts("expansion")
        assert len(results) >= 1
        assert any(r.name == "expansion/system" for r in results)


class TestPromptChangeNotifier:
    """Tests for change notification system."""

    def test_notification_on_create(self, db) -> None:
        """Test that create triggers notification."""
        notifier = PromptChangeNotifier()
        events: list[PromptChangeEvent] = []
        notifier.add_listener(lambda e: events.append(e))

        manager = LocalPromptManager(db, notifier=notifier)
        manager.create_prompt(name="test/notify", content="Content", scope="bundled")

        assert len(events) == 1
        assert events[0].event_type == "create"
        assert events[0].prompt_name == "test/notify"

    def test_notification_on_update(self, db) -> None:
        """Test that update triggers notification."""
        notifier = PromptChangeNotifier()
        events: list[PromptChangeEvent] = []
        notifier.add_listener(lambda e: events.append(e))

        manager = LocalPromptManager(db, dev_mode=True, notifier=notifier)
        created = manager.create_prompt(name="test/notify", content="C", scope="bundled")
        manager.update_prompt(created.id, content="Updated")

        assert len(events) == 2
        assert events[1].event_type == "update"

    def test_notification_on_delete(self, db) -> None:
        """Test that delete triggers notification."""
        notifier = PromptChangeNotifier()
        events: list[PromptChangeEvent] = []
        notifier.add_listener(lambda e: events.append(e))

        manager = LocalPromptManager(db, dev_mode=True, notifier=notifier)
        created = manager.create_prompt(name="test/del", content="C", scope="bundled")
        manager.delete_prompt(created.id)

        assert len(events) == 2
        assert events[1].event_type == "delete"

    def test_remove_listener(self, db) -> None:
        """Test removing a listener."""
        notifier = PromptChangeNotifier()
        events: list[PromptChangeEvent] = []

        def listener(event: PromptChangeEvent) -> None:
            events.append(event)

        notifier.add_listener(listener)
        notifier.remove_listener(listener)

        manager = LocalPromptManager(db, notifier=notifier)
        manager.create_prompt(name="test/no-notify", content="C", scope="bundled")

        assert len(events) == 0
