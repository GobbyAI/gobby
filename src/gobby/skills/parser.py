"""YAML frontmatter parser for SKILL.md files.

This module parses skill files following the Agent Skills specification format:
- YAML frontmatter delimited by ---
- Markdown content body

Example SKILL.md format:
```markdown
---
name: commit-message
description: Generate conventional commit messages
license: MIT
compatibility: Requires git CLI
metadata:
  author: anthropic
  version: "1.0"
  skillport:
    category: git
    tags: [git, commits]
    alwaysApply: false
  gobby:
    triggers: ["/commit"]
allowed-tools: Bash(git:*)
---

# Commit Message Generator

Instructions for the skill...
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.skills.loader import LoadedSkillFile

import yaml

# Pattern to extract YAML frontmatter (content between --- delimiters)
# Allows empty frontmatter (---\n---) or content between delimiters
# Supports both LF (\n) and CRLF (\r\n) line endings
FRONTMATTER_PATTERN = re.compile(
    r"^---[ \t]*\r?\n(.*?)^---[ \t]*\r?\n?",
    re.DOTALL | re.MULTILINE,
)


@dataclass
class SkillAudienceConfig:
    """Agent-type-aware audience configuration for a skill.

    Controls which agent types/depths receive this skill and how it's formatted
    for different audiences. When absent on a ParsedSkill, the skill falls back
    to legacy always_apply behavior.

    Attributes:
        audience: Target audience — "all", "interactive", "autonomous", "orchestrator", "worker"
        depth: Agent depth filter — None (any), int (exact), list[int], or str range "0-2"
        steps: Workflow step filter — None (any) or list of step names
        task_categories: Task category filter — None (any) or list like ["code", "test"]
        format_overrides: Per-audience injection format override, e.g. {"autonomous": "full"}
        priority: Injection ordering (lower = earlier in output)
    """

    audience: str = "all"
    depth: int | list[int] | str | None = None
    steps: list[str] | None = None
    task_categories: list[str] | None = None
    sources: list[str] | None = None
    format_overrides: dict[str, str] | None = None
    priority: int = 50


@dataclass
class ParsedSkill:
    """Parsed skill data from a SKILL.md file.

    Attributes:
        name: Skill name (required)
        description: Skill description (required)
        content: Markdown body content
        version: Version string (from metadata.version or top-level)
        license: License identifier
        compatibility: Compatibility notes
        allowed_tools: List of allowed tool patterns
        metadata: Full metadata dict (includes skillport/gobby namespaces)
        source_path: Path the skill was loaded from
        source_type: Source type (local, github, zip, etc.)
        source_ref: Git ref for GitHub imports
        scripts: List of script file paths (relative to skill dir)
        references: List of reference file paths (relative to skill dir)
        assets: List of asset file paths (relative to skill dir)
        always_apply: Whether skill should always be injected at session start
        injection_format: How to inject skill (summary, full, content)
        audience_config: Agent-type-aware audience configuration (from metadata.gobby)
        internal: Methodology skill loaded by other skills via get_skill; hidden from listings
    """

    name: str
    description: str
    content: str
    version: str | None = None
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: list[str] | None = None
    metadata: dict[str, Any] | None = None
    source_path: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    scripts: list[str] | None = None
    references: list[str] | None = None
    assets: list[str] | None = None
    loaded_files: list[LoadedSkillFile] | None = None
    always_apply: bool = False
    injection_format: str = "summary"
    triggers: list[str] | None = None
    audience_config: SkillAudienceConfig | None = None
    internal: bool = False

    def get_category(self) -> str | None:
        """Get category from top-level or metadata.skillport.category."""
        if not self.metadata:
            return None
        # Check top-level first (from frontmatter)
        result = self.metadata.get("category")
        if result is not None:
            return str(result)
        # Fall back to nested skillport.category
        skillport = self.metadata.get("skillport", {})
        result = skillport.get("category")
        return str(result) if result is not None else None

    def get_tags(self) -> list[str]:
        """Get tags from metadata.skillport.tags."""
        if not self.metadata:
            return []
        skillport = self.metadata.get("skillport", {})
        tags = skillport.get("tags", [])
        if isinstance(tags, list):
            return tags
        if isinstance(tags, str):
            return [tags]
        return []

    def is_always_apply(self) -> bool:
        """Check if this is a core skill (alwaysApply=true).

        Supports both top-level alwaysApply and nested metadata.skillport.alwaysApply.
        Top-level takes precedence.
        """
        if not self.metadata:
            return False
        # Check top-level first (from frontmatter)
        top_level = self.metadata.get("alwaysApply")
        if top_level is not None:
            return bool(top_level)
        # Fall back to nested skillport.alwaysApply
        skillport = self.metadata.get("skillport", {})
        return bool(skillport.get("alwaysApply", False))

    def is_internal(self) -> bool:
        """Check if this is an internal methodology skill (internal=true).

        Internal skills are loaded by other skills via get_skill(name=...) rather
        than surfaced to users; they are hidden from list_skills / search_skills
        listings by default. Supports top-level `internal` and nested
        `metadata.gobby.internal`. Top-level takes precedence.
        """
        if self.internal:
            return True
        if not self.metadata:
            return False
        top_level = self.metadata.get("internal")
        if top_level is not None:
            return bool(top_level)
        gobby_meta = self.metadata.get("gobby", {})
        if isinstance(gobby_meta, dict):
            return bool(gobby_meta.get("internal", False))
        return False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        result = {
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "version": self.version,
            "license": self.license,
            "compatibility": self.compatibility,
            "allowed_tools": self.allowed_tools,
            "metadata": self.metadata,
            "source_path": self.source_path,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "scripts": self.scripts,
            "references": self.references,
            "assets": self.assets,
            "always_apply": self.always_apply,
            "injection_format": self.injection_format,
            "triggers": self.triggers,
            "internal": self.internal,
        }
        if self.audience_config:
            result["audience_config"] = {
                "audience": self.audience_config.audience,
                "depth": self.audience_config.depth,
                "steps": self.audience_config.steps,
                "task_categories": self.audience_config.task_categories,
                "sources": self.audience_config.sources,
                "format_overrides": self.audience_config.format_overrides,
                "priority": self.audience_config.priority,
            }
        return result


class SkillParseError(Exception):
    """Error parsing a skill file."""

    def __init__(self, message: str, path: str | None = None):
        self.path = path
        super().__init__(f"{message}" + (f" in {path}" if path else ""))


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from text.

    Args:
        text: Full text content with frontmatter

    Returns:
        Tuple of (frontmatter dict, content body)

    Raises:
        SkillParseError: If frontmatter is missing or invalid
    """
    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        raise SkillParseError("Missing or invalid YAML frontmatter (must start with ---)")

    frontmatter_yaml = match.group(1)
    content = text[match.end() :].strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_yaml)
    except yaml.YAMLError as e:
        raise SkillParseError(f"Invalid YAML in frontmatter: {e}") from e

    if frontmatter is None:
        frontmatter = {}

    if not isinstance(frontmatter, dict):
        raise SkillParseError("Frontmatter must be a YAML mapping")

    return frontmatter, content


def extract_audience_config(metadata: dict[str, Any] | None) -> SkillAudienceConfig | None:
    """Extract audience config from metadata.gobby namespace.

    Shared helper used by both parse_skill_text() (filesystem path) and
    _db_skill_to_parsed() (DB round-trip path) to avoid duplicating
    the extraction logic.

    Args:
        metadata: Full metadata dict (may contain gobby namespace)

    Returns:
        SkillAudienceConfig if gobby audience keys are present, None otherwise
    """
    if not metadata or not isinstance(metadata, dict):
        return None

    gobby_meta = metadata.get("gobby", {})
    if not isinstance(gobby_meta, dict):
        return None

    audience_keys = (
        "audience",
        "depth",
        "steps",
        "task_categories",
        "sources",
        "format_overrides",
        "priority",
    )
    if not any(k in gobby_meta for k in audience_keys):
        return None

    ac_kwargs: dict[str, Any] = {}
    if "audience" in gobby_meta:
        ac_kwargs["audience"] = str(gobby_meta["audience"])
    if "depth" in gobby_meta:
        raw_depth = gobby_meta["depth"]
        # Support int, list[int], or str range like "0-2"
        if isinstance(raw_depth, int):
            ac_kwargs["depth"] = raw_depth
        elif isinstance(raw_depth, list):
            try:
                ac_kwargs["depth"] = [int(d) for d in raw_depth]
            except (ValueError, TypeError):
                pass  # skip invalid depth list elements
        elif isinstance(raw_depth, str):
            ac_kwargs["depth"] = raw_depth
    if "steps" in gobby_meta:
        raw_steps = gobby_meta["steps"]
        if isinstance(raw_steps, list):
            ac_kwargs["steps"] = [str(s) for s in raw_steps]
    if "task_categories" in gobby_meta:
        raw_cats = gobby_meta["task_categories"]
        if isinstance(raw_cats, list):
            ac_kwargs["task_categories"] = [str(c) for c in raw_cats]
    if "sources" in gobby_meta:
        raw_sources = gobby_meta["sources"]
        if isinstance(raw_sources, list):
            ac_kwargs["sources"] = [str(s) for s in raw_sources]
        elif isinstance(raw_sources, str):
            ac_kwargs["sources"] = [raw_sources]
    if "format_overrides" in gobby_meta:
        raw_fo = gobby_meta["format_overrides"]
        if isinstance(raw_fo, dict):
            ac_kwargs["format_overrides"] = {str(k): str(v) for k, v in raw_fo.items()}
    if "priority" in gobby_meta:
        try:
            ac_kwargs["priority"] = int(gobby_meta["priority"])
        except (ValueError, TypeError):
            pass
    return SkillAudienceConfig(**ac_kwargs)


def parse_skill_text(text: str, source_path: str | None = None) -> ParsedSkill:
    """Parse a skill from text content.

    Args:
        text: Full skill file content (frontmatter + markdown)
        source_path: Optional path for error messages

    Returns:
        ParsedSkill with extracted data

    Raises:
        SkillParseError: If parsing fails or required fields missing
    """
    try:
        frontmatter, content = parse_frontmatter(text)
    except SkillParseError as e:
        # Create a new exception with the path included in the message
        raise SkillParseError(str(e), path=source_path) from e

    # Extract required fields
    name = frontmatter.get("name")
    if not name:
        raise SkillParseError("Missing required field: name", source_path)

    description = frontmatter.get("description")
    if not description:
        raise SkillParseError("Missing required field: description", source_path)

    # Extract optional fields
    license_str = frontmatter.get("license")
    compatibility = frontmatter.get("compatibility")

    # Handle allowed-tools (can be string or list)
    allowed_tools_raw = frontmatter.get("allowed-tools") or frontmatter.get("allowed_tools")
    allowed_tools: list[str] | None = None
    if allowed_tools_raw:
        if isinstance(allowed_tools_raw, str):
            # Single tool or comma-separated
            allowed_tools = [t.strip() for t in allowed_tools_raw.split(",")]
        elif isinstance(allowed_tools_raw, list):
            allowed_tools = [str(t) for t in allowed_tools_raw]

    # Extract metadata (may contain version, skillport, gobby namespaces)
    metadata = frontmatter.get("metadata")

    # Handle top-level alwaysApply and category by including them in metadata
    # This allows both top-level and nested formats to work
    top_level_always_apply = frontmatter.get("alwaysApply")
    top_level_category = frontmatter.get("category")
    top_level_injection_format = frontmatter.get("injectionFormat")
    top_level_internal = frontmatter.get("internal")

    if (
        top_level_always_apply is not None
        or top_level_category is not None
        or top_level_internal is not None
    ):
        if metadata is None:
            metadata = {}
        # Store at top level of metadata (not nested in skillport)
        if top_level_always_apply is not None:
            metadata["alwaysApply"] = top_level_always_apply
        if top_level_category is not None:
            metadata["category"] = top_level_category
        if top_level_internal is not None:
            metadata["internal"] = top_level_internal

    # Version can be at top level or in metadata
    version = frontmatter.get("version")
    if version is None and metadata and isinstance(metadata, dict):
        version = metadata.get("version")

    # Convert version to string if it's a number (e.g., 1.0 parsed as float)
    if version is not None:
        version = str(version)

    # Extract always_apply: check top-level first, then metadata.skillport.alwaysApply
    always_apply = False
    if top_level_always_apply is not None:
        always_apply = bool(top_level_always_apply)
    elif metadata and isinstance(metadata, dict):
        skillport = metadata.get("skillport", {})
        if isinstance(skillport, dict) and skillport.get("alwaysApply"):
            always_apply = bool(skillport["alwaysApply"])

    # Extract injection_format: check top-level first, default to "summary"
    injection_format = "summary"
    if top_level_injection_format is not None:
        injection_format = str(top_level_injection_format)

    # Extract triggers: top-level field (string -> split on comma, list -> use directly)
    triggers_raw = frontmatter.get("triggers")
    triggers: list[str] | None = None
    if triggers_raw:
        if isinstance(triggers_raw, str):
            triggers = [t.strip() for t in triggers_raw.split(",") if t.strip()]
        elif isinstance(triggers_raw, list):
            triggers = [str(t).strip() for t in triggers_raw if str(t).strip()]

    audience_config = extract_audience_config(metadata)

    # Resolve internal: top-level takes precedence, fall back to metadata.gobby.internal
    internal = False
    if top_level_internal is not None:
        internal = bool(top_level_internal)
    elif metadata and isinstance(metadata, dict):
        gobby_meta = metadata.get("gobby", {})
        if isinstance(gobby_meta, dict) and gobby_meta.get("internal") is not None:
            internal = bool(gobby_meta["internal"])

    return ParsedSkill(
        name=name,
        description=description,
        content=content,
        version=version,
        license=license_str,
        compatibility=compatibility,
        allowed_tools=allowed_tools,
        metadata=metadata,
        source_path=source_path,
        always_apply=always_apply,
        injection_format=injection_format,
        triggers=triggers,
        audience_config=audience_config,
        internal=internal,
    )


def parse_skill_file(path: str | Path) -> ParsedSkill:
    """Parse a skill from a file path.

    Args:
        path: Path to SKILL.md file

    Returns:
        ParsedSkill with extracted data

    Raises:
        FileNotFoundError: If file doesn't exist
        SkillParseError: If parsing fails (propagated from parse_skill_text)
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Skill file not found: {path}")

    text = path.read_text(encoding="utf-8")
    return parse_skill_text(text, source_path=str(path))
