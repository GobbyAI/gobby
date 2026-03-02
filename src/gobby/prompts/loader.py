"""
Prompt template loader with database-backed storage.

Implements prompt loading with precedence via the database:
1. Project scope (scope='project')
2. Global scope (scope='global')
3. Bundled scope (scope='bundled')

The database is the sole runtime source. Bundled .md files are only
read by sync_bundled_prompts() on daemon startup.
"""

import logging
from typing import Any, cast

from gobby.prompts.models import PromptTemplate
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


class PromptLoader:
    """Loads prompt templates from the database with scope-based precedence.

    Usage:
        loader = PromptLoader(db=database, project_id="proj-123")
        template = loader.load("expansion/system")
        rendered = loader.render("expansion/system", {"tdd_mode": True})
    """

    def __init__(
        self,
        db: DatabaseProtocol | None = None,
        project_id: str | None = None,
        notifier: Any | None = None,
    ):
        """Initialize the prompt loader.

        Args:
            db: Database connection (if None, lazily creates a LocalDatabase)
            project_id: Project context for precedence resolution
            notifier: Optional PromptChangeNotifier for cache invalidation
        """
        self._db = db
        self._project_id = project_id
        self._cache: dict[str, PromptTemplate] = {}

        # Subscribe to change events for cache invalidation
        if notifier is not None:
            notifier.add_listener(self._on_change)

    def _on_change(self, event: Any) -> None:
        """Handle change notification by clearing cache."""
        self._cache.clear()

    def clear_cache(self) -> None:
        """Clear the template cache."""
        self._cache.clear()

    def _get_db(self) -> DatabaseProtocol:
        """Get the database connection, lazily creating one if needed."""
        if self._db is None:
            from gobby.storage.database import LocalDatabase

            self._db = LocalDatabase()
        return self._db

    def _get_manager(self) -> Any:
        """Get the prompt manager instance."""
        from gobby.storage.prompts import LocalPromptManager

        return LocalPromptManager(self._get_db())

    def _get_record(self, path: str) -> Any | None:
        """Helper to get a prompt record by path."""
        manager = self._get_manager()
        return manager.get_by_name(path, project_id=self._project_id)

    def load(self, path: str) -> PromptTemplate:
        """Load a prompt template by path.

        Args:
            path: Template path (e.g., "expansion/system")

        Returns:
            PromptTemplate instance

        Raises:
            FileNotFoundError: If template not found in database
        """
        if path in self._cache:
            return self._cache[path]

        record = self._get_record(path)

        if record is not None:
            template = cast(PromptTemplate, record.to_prompt_template())
            self._cache[path] = template
            logger.debug(f"Loaded prompt template '{path}' from database (scope={record.scope})")
            return template

        raise FileNotFoundError(f"Prompt template not found: {path}")

    def render(
        self,
        path: str,
        context: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> str:
        """Load and render a template with context.

        Args:
            path: Template path
            context: Variables to inject into template
            strict: If True, raise on missing required variables

        Returns:
            Rendered template string

        Raises:
            FileNotFoundError: If template not found
            ValueError: If strict=True and required variables missing
        """
        template = self.load(path)
        ctx = template.get_default_context()

        if context:
            ctx.update(context)

        # Validate required variables
        if strict:
            errors = template.validate_context(ctx)
            if errors:
                raise ValueError(f"Template validation failed: {'; '.join(errors)}")

        # Render with Jinja2
        return self._render_jinja(template.content, ctx)

    def _render_jinja(self, template_str: str, context: dict[str, Any]) -> str:
        """Render a template string with Jinja2."""
        try:
            from jinja2 import Environment, StrictUndefined, UndefinedError

            env = Environment(  # nosec B701 - generating raw text prompts, not HTML
                autoescape=False,
                undefined=StrictUndefined,
                extensions=[],
            )

            env.filters["default"] = lambda v, d="": d if v is None else v

            template = env.from_string(template_str)
            rendered: str = template.render(**context)
            return rendered

        except UndefinedError as e:
            logger.warning(f"Template rendering error (undefined variable): {e}")
            return self._render_simple(template_str, context)
        except ImportError:
            logger.debug("Jinja2 not available, using simple format")
            return self._render_simple(template_str, context)
        except Exception as e:
            logger.warning(f"Template rendering error: {e}")
            return self._render_simple(template_str, context)

    def _render_simple(self, template_str: str, context: dict[str, Any]) -> str:
        """Simple string formatting fallback."""
        try:
            return template_str.format(**context)
        except KeyError:
            return template_str

    def exists(self, path: str) -> bool:
        """Check if a template exists in the database.

        Args:
            path: Template path

        Returns:
            True if template exists
        """
        return self._get_record(path) is not None

    def list_templates(self, category: str | None = None) -> list[str]:
        """List available template paths.

        Args:
            category: Optional category to filter (e.g., "expansion")

        Returns:
            Sorted list of template paths
        """
        manager = self._get_manager()
        records = manager.list_prompts(
            project_id=self._project_id,
            category=category,
            enabled=True,
            limit=500,
        )

        # Deduplicate by name (higher-precedence scope wins)
        seen: set[str] = set()
        names: list[str] = []
        for r in records:
            if r.name not in seen:
                seen.add(r.name)
                names.append(r.name)

        return sorted(names)
