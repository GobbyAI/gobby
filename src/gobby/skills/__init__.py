"""Skills module for Agent Skills spec compliant skill management.

This module provides:
- YAML frontmatter parsing for SKILL.md files
- Validation against Agent Skills specification
- Search integration (keyword search + optional embeddings via UnifiedSearcher)
- Skill loading from filesystem, GitHub, and ZIP archives
- Skill updates from source
"""

# Embedding utilities are now in gobby.search
from gobby.search import (
    generate_embedding,
    generate_embeddings,
    is_embedding_configured,
    is_embedding_reachable,
)
from gobby.skills.loader import (
    GitHubRef,
    SkillLoader,
    SkillLoadError,
    clone_skill_repo,
    extract_zip,
    parse_github_url,
)
from gobby.skills.manager import SkillManager
from gobby.skills.parser import (
    ParsedSkill,
    SkillParseError,
    parse_frontmatter,
    parse_skill_file,
    parse_skill_text,
)
from gobby.skills.search import (
    SearchFilters,
    SkillSearch,
    SkillSearchResult,
)
from gobby.skills.updater import (
    SkillUpdateError,
    SkillUpdater,
    SkillUpdateResult,
)
from gobby.skills.validator import (
    SkillValidator,
    ValidationResult,
    validate_skill_category,
    validate_skill_compatibility,
    validate_skill_description,
    validate_skill_name,
    validate_skill_tags,
    validate_skill_version,
)

__all__ = [
    # Embeddings (from gobby.search)
    "generate_embedding",
    "generate_embeddings",
    "is_embedding_configured",
    "is_embedding_reachable",
    # Loader
    "GitHubRef",
    "SkillLoadError",
    "SkillLoader",
    "clone_skill_repo",
    "extract_zip",
    "parse_github_url",
    # Manager
    "SkillManager",
    # Updater
    "SkillUpdateError",
    "SkillUpdateResult",
    "SkillUpdater",
    # Parser
    "ParsedSkill",
    "SkillParseError",
    "parse_frontmatter",
    "parse_skill_file",
    "parse_skill_text",
    # Search
    "SearchFilters",
    "SkillSearch",
    "SkillSearchResult",
    # Validator
    "SkillValidator",
    "ValidationResult",
    "validate_skill_category",
    "validate_skill_compatibility",
    "validate_skill_description",
    "validate_skill_name",
    "validate_skill_tags",
    "validate_skill_version",
]
