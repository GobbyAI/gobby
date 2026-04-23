"""Unit tests for PromptLoader (database-backed)."""

import pytest

from gobby.prompts import PromptLoader, PromptTemplate
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.prompts import LocalPromptManager, PromptChangeNotifier

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    """Create a fresh database with migrations applied."""
    database = LocalDatabase(tmp_path / "test.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def synced_db(db):
    """Database with bundled prompts synced."""
    sync_bundled_prompts(db)
    return db


@pytest.fixture
def manager(db):
    """Create a prompt manager for seeding test data."""
    return LocalPromptManager(db)


class TestPromptLoader:
    """Tests for PromptLoader class."""

    def test_load_from_database(self, synced_db) -> None:
        """Test loading prompt from database."""
        loader = PromptLoader(db=synced_db)

        template = loader.load("expansion/system")

        assert template is not None
        assert template.name == "expansion/system"
        assert "Task Expansion Compiler" in template.content

    def test_load_file_not_found(self, db) -> None:
        """Test that FileNotFoundError is raised for non-existent templates."""
        loader = PromptLoader(db=db)

        with pytest.raises(FileNotFoundError, match="Prompt template not found"):
            loader.load("completely/nonexistent/path")

    def test_render_with_jinja2_variables(self, db, manager) -> None:
        """Test rendering template with Jinja2 variables."""
        manager.create_prompt(
            name="test/template",
            description="Test template",
            content="Hello, {{ name }}!",
            variables={"name": {"type": "str", "default": "World"}},
            scope="bundled",
        )

        loader = PromptLoader(db=db)
        result = loader.render("test/template", {"name": "Claude"})

        assert result == "Hello, Claude!"

    def test_render_with_defaults(self, db, manager) -> None:
        """Test rendering uses default values from variables."""
        manager.create_prompt(
            name="test/greet",
            content="{{ greeting }}, {{ name }}!",
            variables={
                "greeting": {"type": "str", "default": "Hello"},
                "name": {"type": "str", "default": "World"},
            },
            scope="bundled",
        )

        loader = PromptLoader(db=db)
        result = loader.render("test/greet", {})

        assert result == "Hello, World!"

    def test_render_with_conditionals(self, db, manager) -> None:
        """Test rendering with Jinja2 conditionals."""
        manager.create_prompt(
            name="test/conditional",
            content="Base content.\n{% if show_extra %}\nExtra content here.\n{% endif %}",
            variables={"show_extra": {"type": "bool", "default": False}},
            scope="bundled",
        )

        loader = PromptLoader(db=db)

        result_false = loader.render("test/conditional", {"show_extra": False})
        assert "Base content." in result_false
        assert "Extra content here." not in result_false

        result_true = loader.render("test/conditional", {"show_extra": True})
        assert "Base content." in result_true
        assert "Extra content here." in result_true

    def test_precedence_global_over_bundled(self, db, manager) -> None:
        """Test that global templates take precedence over bundled."""
        manager.create_prompt(
            name="test/precedence",
            content="Bundled version",
            scope="bundled",
        )
        manager.create_prompt(
            name="test/precedence",
            content="Global version",
            scope="global",
        )

        loader = PromptLoader(db=db)
        template = loader.load("test/precedence")

        assert template.content == "Global version"

    def test_cache_behavior(self, synced_db) -> None:
        """Test that templates are cached after first load."""
        loader = PromptLoader(db=synced_db)

        template1 = loader.load("expansion/system")
        template2 = loader.load("expansion/system")

        assert template1 is template2

        loader.clear_cache()
        template3 = loader.load("expansion/system")

        assert template1 is not template3

    def test_cache_invalidation_via_notifier(self, db, manager) -> None:
        """Test that cache is invalidated when notifier fires."""
        notifier = PromptChangeNotifier()
        loader = PromptLoader(db=db, notifier=notifier)

        manager_with_notifier = LocalPromptManager(db, notifier=notifier)
        manager.create_prompt(
            name="test/cache",
            content="Original",
            scope="global",
        )

        # Load to cache
        template1 = loader.load("test/cache")
        assert template1.content == "Original"

        # Update via manager with notifier (triggers cache clear)
        records = manager_with_notifier.list_prompts()
        target = next(r for r in records if r.name == "test/cache")
        manager_with_notifier.update_prompt(target.id, content="Updated")

        # Cache should be cleared, so next load gets updated version
        template2 = loader.load("test/cache")
        assert template2.content == "Updated"

    def test_list_templates(self, synced_db) -> None:
        """Test listing available templates."""
        loader = PromptLoader(db=synced_db)

        all_templates = loader.list_templates()

        assert len(all_templates) > 0
        assert "expansion/system" in all_templates
        assert "expansion/user" in all_templates

        expansion_templates = loader.list_templates(category="expansion")
        assert all(t.startswith("expansion/") for t in expansion_templates)

    def test_exists(self, synced_db) -> None:
        """Test checking if template exists."""
        loader = PromptLoader(db=synced_db)

        assert loader.exists("expansion/system") is True
        assert loader.exists("nonexistent/path") is False

    def test_lazy_db_creation(self) -> None:
        """Test that PromptLoader lazily creates a database if none provided."""
        # This tests the fallback mechanism - PromptLoader() with no args
        loader = PromptLoader()
        # The loader should have been created without error
        assert loader._db is None
        # Accessing _get_db() would create a LocalDatabase, but we don't
        # test that here to avoid side effects on the real database


class TestPromptTemplate:
    """Tests for PromptTemplate dataclass."""

    def test_get_default_context(self) -> None:
        """Test getting default context from template."""
        from gobby.prompts.models import VariableSpec

        template = PromptTemplate(
            name="test",
            content="content",
            variables={
                "var1": VariableSpec(default="default1"),
                "var2": VariableSpec(default=42),
            },
        )

        defaults = template.get_default_context()

        assert defaults == {"var1": "default1", "var2": 42}

    def test_validate_context_required(self) -> None:
        """Test validation of required variables."""
        from gobby.prompts.models import VariableSpec

        template = PromptTemplate(
            name="test",
            content="content",
            variables={
                "required_var": VariableSpec(required=True),
                "optional_var": VariableSpec(required=False),
            },
        )

        errors = template.validate_context({})
        assert len(errors) == 1
        assert "required_var" in errors[0]

        errors = template.validate_context({"required_var": "value"})
        assert len(errors) == 0

    def test_from_frontmatter(self) -> None:
        """Test creating PromptTemplate from frontmatter."""
        frontmatter = {
            "name": "test-template",
            "description": "Test description",
            "version": "1.5",
            "variables": {
                "simple_var": "default_value",
                "complex_var": {
                    "type": "int",
                    "default": 10,
                    "description": "A complex variable",
                    "required": True,
                },
            },
        }

        template = PromptTemplate.from_frontmatter(
            name="test",
            frontmatter=frontmatter,
            content="The content",
        )

        assert template.description == "Test description"
        assert template.version == "1.5"
        assert "simple_var" in template.variables
        assert template.variables["simple_var"].default == "default_value"
        assert "complex_var" in template.variables
        assert template.variables["complex_var"].type == "int"
        assert template.variables["complex_var"].default == 10
        assert template.variables["complex_var"].required is True


class TestBundledTemplates:
    """Tests to verify all bundled default templates load correctly."""

    @pytest.mark.parametrize(
        "template_path",
        [
            "expansion/system",
            "expansion/user",
            "validation/validate",
            "validation/criteria",
            "external_validation/system",
            "external_validation/spawn",
            "external_validation/agent",
            "external_validation/external",
            "research/step",
            "features/tool_summary",
            "features/tool_summary_system",
            "features/server_description",
            "features/server_description_system",
            "features/recommend_tools",
            "features/recommend_hybrid",
            "features/recommend_llm",
            "import/system",
            "import/github_fetch",
            "import/search_fetch",
        ],
    )
    def test_bundled_template_loads(self, synced_db, template_path: str) -> None:
        """Test that each bundled template loads without error."""
        loader = PromptLoader(db=synced_db)
        template = loader.load(template_path)

        assert template is not None
        assert template.content != ""

    def test_expansion_system_no_tdd_mode_in_prompt(self, synced_db) -> None:
        """Test that expansion/system template does not include TDD mode."""
        loader = PromptLoader(db=synced_db)

        result = loader.render("expansion/system", {"tdd_mode": False})
        assert "TDD Mode Enabled" not in result

        result_with_flag = loader.render("expansion/system", {"tdd_mode": True})
        assert "TDD Mode Enabled" not in result_with_flag
        assert result == result_with_flag
