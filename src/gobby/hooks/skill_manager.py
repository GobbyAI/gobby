"""HookSkillManager - Skill management for the hook system.

This module provides skill discovery and management for the hook system,
allowing hooks to access and use skills (Agent Skills specification).

Supports DB-backed loading (preferred) with filesystem fallback.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import ParsedSkill, extract_audience_config

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

# Upper bound for skill fetch queries — high enough to get all installed skills
# without unbounded queries. Actual skill counts are typically < 200.
_MAX_SKILL_FETCH = 10_000


def _db_skill_to_parsed(skill: Any) -> ParsedSkill:
    """Convert a storage Skill to a ParsedSkill for backward compat.

    Args:
        skill: A Skill instance from storage.skills

    Returns:
        ParsedSkill with equivalent fields
    """
    # Extract triggers from metadata
    triggers: list[str] = []
    if skill.metadata and isinstance(skill.metadata, dict):
        gobby_meta = skill.metadata.get("gobby", {})
        if isinstance(gobby_meta, dict):
            raw_triggers = gobby_meta.get("triggers", [])
            if isinstance(raw_triggers, list):
                triggers = [str(t) for t in raw_triggers]
            elif isinstance(raw_triggers, str):
                triggers = [t.strip() for t in raw_triggers.split(",")]

    audience_config = extract_audience_config(skill.metadata)

    return ParsedSkill(
        name=skill.name,
        description=skill.description,
        content=skill.content,
        version=skill.version,
        license=skill.license,
        compatibility=skill.compatibility,
        allowed_tools=skill.allowed_tools,
        metadata=skill.metadata,
        source_path=skill.source_path,
        source_type=skill.source_type,
        source_ref=skill.source_ref,
        always_apply=skill.always_apply,
        injection_format=skill.injection_format,
        triggers=triggers if triggers else None,
        audience_config=audience_config,
    )


class HookSkillManager:
    """Manage skills for the hook system.

    Provides discovery and access to core skills bundled with Gobby,
    as well as project-specific skills.

    Supports DB-backed loading (preferred) with filesystem fallback.

    Example usage:
        ```python
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager(db=database)
        skills = manager.discover_core_skills()

        # Get a specific skill
        tasks_skill = manager.get_skill_by_name("gobby-tasks")
        ```
    """

    def __init__(self, db: DatabaseProtocol | None = None) -> None:
        """Initialize the skill manager.

        Args:
            db: Optional database for DB-backed loading. Falls back to
                filesystem when None.
        """
        self._db = db

        # Path to built-in skills: src/gobby/hooks/ -> src/gobby/install/shared/skills/
        self._base_dir = Path(__file__).parent.parent
        self._core_skills_path = self._base_dir / "install" / "shared" / "skills"

        # Loader for parsing skills (use "filesystem" for bundled core skills)
        self._loader = SkillLoader(default_source_type="filesystem")

        # Cache of discovered skills
        self._core_skills: list[ParsedSkill] | None = None

        # Cache of trigger index: list of (trigger_words: list[str], skill) tuples
        self._trigger_index: list[tuple[list[str], ParsedSkill]] | None = None

    def discover_core_skills(self) -> list[ParsedSkill]:
        """Discover skills — from DB (installed + enabled) or filesystem fallback.

        Returns:
            List of ParsedSkill objects for all valid core skills.
            Invalid skills are logged as warnings and skipped.
        """
        if self._core_skills is not None:
            return self._core_skills

        # Try DB-backed loading first
        if self._db is not None:
            try:
                skills = self._load_from_db()
                if skills is not None:
                    self._core_skills = skills
                    logger.debug(f"Discovered {len(self._core_skills)} core skills from DB")
                    return self._core_skills
            except Exception as e:
                logger.debug(f"DB skill loading failed, falling back to filesystem: {e}")

        # Filesystem fallback
        return self._load_from_filesystem()

    def _load_from_db(self) -> list[ParsedSkill] | None:
        """Load installed, enabled skills from the database.

        Returns:
            List of ParsedSkill, or None if DB is unavailable
        """
        if self._db is None:
            return None

        from gobby.storage.skills import LocalSkillManager

        storage = LocalSkillManager(self._db)
        db_skills = storage.list_skills(
            enabled=True,
            include_templates=False,
            include_deleted=False,
            limit=_MAX_SKILL_FETCH,
        )

        return [_db_skill_to_parsed(s) for s in db_skills]

    def _load_from_filesystem(self) -> list[ParsedSkill]:
        """Load skills from the filesystem (fallback path)."""
        if not self._core_skills_path.exists():
            logger.warning(f"Core skills path not found: {self._core_skills_path}")
            self._core_skills = []
            return self._core_skills

        # Load all skills from the core directory
        self._core_skills = self._loader.load_directory(
            self._core_skills_path,
            validate=True,
        )

        logger.debug(f"Discovered {len(self._core_skills)} core skills from filesystem")
        return self._core_skills

    def get_skill_by_name(self, name: str) -> ParsedSkill | None:
        """Get a skill by name.

        Args:
            name: The skill name to look up.

        Returns:
            ParsedSkill if found, None otherwise.
        """
        # Ensure skills are discovered
        skills = self.discover_core_skills()

        for skill in skills:
            if skill.name == name:
                return skill

        return None

    def resolve_skill_name(self, name: str) -> ParsedSkill | None:
        """Resolve a skill name using a resolution chain.

        Resolution order:
        1. Exact match on skill.name
        2. With gobby- prefix (e.g., "tasks" -> "gobby-tasks")
        3. Prefix/startswith match — only if unambiguous (exactly 1 match)

        Args:
            name: The skill name to resolve.

        Returns:
            ParsedSkill if resolved, None otherwise.
        """
        skills = self.discover_core_skills()
        name_lower = name.lower()

        # 1. Exact match
        for skill in skills:
            if skill.name.lower() == name_lower:
                return skill

        # 2. With gobby- prefix
        prefixed = f"gobby-{name_lower}"
        for skill in skills:
            if skill.name.lower() == prefixed:
                return skill

        # 3. Prefix/startswith match (only if unambiguous)
        matches = [s for s in skills if s.name.lower().startswith(name_lower)]
        if len(matches) == 1:
            return matches[0]

        return None

    def match_triggers(
        self, prompt: str, threshold: float = 0.5
    ) -> list[tuple[ParsedSkill, float]]:
        """Match a prompt against skill trigger keywords.

        Uses word-overlap scoring: count trigger words that appear in prompt,
        normalized by trigger length.

        Args:
            prompt: The user's prompt text.
            threshold: Minimum score to include (default 0.5).

        Returns:
            List of (skill, score) tuples above threshold, sorted descending by score.
        """
        if not prompt.strip():
            return []

        # Build trigger index on first call
        if self._trigger_index is None:
            self._build_trigger_index()

        if self._trigger_index is None:
            raise RuntimeError("trigger_index not built")

        import re

        prompt_words = {start.lower() for start in re.findall(r"\w+", prompt.lower())}
        results: list[tuple[ParsedSkill, float]] = []

        for trigger_words, skill in self._trigger_index:
            if not trigger_words:
                continue
            overlap = sum(1 for w in trigger_words if w in prompt_words)
            score = overlap / len(trigger_words)
            if score >= threshold:
                results.append((skill, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def _build_trigger_index(self) -> None:
        """Build the trigger index from discovered skills."""
        skills = self.discover_core_skills()
        self._trigger_index = []

        for skill in skills:
            triggers: list[str] = []

            # Extract from skill.triggers (top-level frontmatter field)
            if skill.triggers:
                triggers.extend(skill.triggers)

            # Also extract from metadata.gobby.triggers (nested format)
            if skill.metadata and isinstance(skill.metadata, dict):
                gobby_meta = skill.metadata.get("gobby", {})
                if isinstance(gobby_meta, dict):
                    nested_triggers = gobby_meta.get("triggers", [])
                    if isinstance(nested_triggers, list):
                        triggers.extend(str(t) for t in nested_triggers)
                    elif isinstance(nested_triggers, str):
                        triggers.extend(t.strip() for t in nested_triggers.split(","))

            if not triggers:
                continue

            # Build word set from all triggers for this skill
            all_words: list[str] = []
            import re

            for trigger in triggers:
                all_words.extend(re.findall(r"\w+", trigger.lower()))

            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_words: list[str] = []
            for w in all_words:
                if w not in seen:
                    seen.add(w)
                    unique_words.append(w)

            self._trigger_index.append((unique_words, skill))

    def refresh(self) -> None:
        """Clear the cache and rediscover skills."""
        self._core_skills = None
        self._trigger_index = None

    def recommend_skills(self, category: str | None = None) -> list[str]:
        """Recommend relevant skills based on task category.

        Maps task categories to relevant core skills that would be helpful
        for that type of work.

        Args:
            category: Task category (e.g., 'code', 'docs', 'test', 'config')

        Returns:
            List of skill names that are relevant for the category
        """
        # Category to skill mappings
        category_skills: dict[str, list[str]] = {
            "code": ["gobby-tasks", "gobby-expand", "gobby-worktrees"],
            "test": ["gobby-tasks", "gobby-expand"],
            "docs": ["gobby-tasks", "gobby-plan"],
            "config": ["gobby-tasks", "gobby-mcp"],
            "refactor": ["gobby-tasks", "gobby-expand", "gobby-worktrees"],
            "planning": ["gobby-tasks", "gobby-plan", "gobby-expand"],
            "research": ["gobby-tasks", "gobby-memory"],
        }

        # Get skills for the category (or empty list if no match)
        recommended = category_skills.get(category or "", [])

        # Always include alwaysApply skills
        skills = self.discover_core_skills()
        always_apply = [s.name for s in skills if s.is_always_apply()]

        # Combine and dedupe while preserving order
        result = list(always_apply)
        for skill_name in recommended:
            if skill_name not in result:
                result.append(skill_name)

        return result
