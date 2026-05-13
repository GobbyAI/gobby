"""Skill validation per Agent Skills specification.

This module provides validation functions for skill fields following
the Agent Skills spec (agentskills.io) constraints:

- name: max 64 chars, lowercase + hyphens only, no leading/trailing/consecutive hyphens
- description: max 1024 chars, non-empty
- compatibility: max 500 chars (optional)
- tags: list of strings
- version: semver pattern
- category: lowercase alphanumeric + hyphens
"""

import re
from dataclasses import dataclass, field
from typing import Any

# Constants for validation limits
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500
MAX_TAG_LENGTH = 64

# Regex patterns
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# SemVer 2.0.0 compliant pattern: no leading zeros in numeric identifiers
# Prerelease and build metadata allow alphanumeric, hyphens, and dots
_SEMVER_NUM = r"(?:0|[1-9]\d*)"  # 0 or non-zero-prefixed number
SEMVER_PATTERN = re.compile(
    rf"^{_SEMVER_NUM}\.{_SEMVER_NUM}\.{_SEMVER_NUM}(-[a-zA-Z0-9.-]+)?(\+[a-zA-Z0-9.-]+)?$"
)
CATEGORY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


@dataclass
class ValidationResult:
    """Result of a validation check.

    Attributes:
        valid: Whether the validation passed
        errors: List of error messages if validation failed
        warnings: List of warning messages (non-fatal issues)
    """

    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        """Add an error and mark result as invalid."""
        self.errors.append(message)
        self.valid = False

    def add_warning(self, message: str) -> None:
        """Add a warning (does not affect validity)."""
        self.warnings.append(message)

    def merge(self, other: "ValidationResult") -> None:
        """Merge another result into this one."""
        if not other.valid:
            self.valid = False
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def validate_skill_name(name: str | None) -> ValidationResult:
    """Validate a skill name per Agent Skills spec.

    Requirements:
    - Required (non-empty)
    - Max 64 characters
    - Lowercase letters, numbers, and hyphens only
    - Must start with a letter
    - No leading, trailing, or consecutive hyphens
    - No uppercase letters

    Args:
        name: The skill name to validate

    Returns:
        ValidationResult with any errors
    """
    result = ValidationResult()

    if name is None or name == "":
        result.add_error("Skill name is required")
        return result

    # Check for uppercase letters
    if name != name.lower():
        result.add_error("Skill name must be lowercase")

    # Check length
    if len(name) > MAX_NAME_LENGTH:
        result.add_error(f"Skill name exceeds maximum length of {MAX_NAME_LENGTH} characters")

    # Check for leading hyphen
    if name.startswith("-"):
        result.add_error("Skill name cannot start with a hyphen")

    # Check for trailing hyphen
    if name.endswith("-"):
        result.add_error("Skill name cannot end with a hyphen")

    # Check for consecutive hyphens
    if "--" in name:
        result.add_error("Skill name cannot contain consecutive hyphens")

    # Check overall pattern (lowercase alphanumeric with single hyphens)
    if not NAME_PATTERN.match(name):
        # Only add this if we haven't already identified the specific issue
        if result.valid:
            result.add_error(
                "Skill name must contain only lowercase letters, numbers, and single hyphens"
            )

    return result


def validate_skill_description(description: str | None) -> ValidationResult:
    """Validate a skill description per Agent Skills spec.

    Requirements:
    - Required (non-empty)
    - Max 1024 characters

    Args:
        description: The skill description to validate

    Returns:
        ValidationResult with any errors
    """
    result = ValidationResult()

    if description is None or description.strip() == "":
        result.add_error("Skill description is required")
        return result

    if len(description) > MAX_DESCRIPTION_LENGTH:
        result.add_error(
            f"Skill description exceeds maximum length of {MAX_DESCRIPTION_LENGTH} characters"
        )

    return result


def validate_skill_compatibility(compatibility: str | None) -> ValidationResult:
    """Validate a skill compatibility string per Agent Skills spec.

    Requirements:
    - Optional (can be None or empty)
    - Max 500 characters if provided

    Args:
        compatibility: The compatibility string to validate

    Returns:
        ValidationResult with any errors
    """
    result = ValidationResult()

    if compatibility is None or compatibility == "":
        # Compatibility is optional
        return result

    if len(compatibility) > MAX_COMPATIBILITY_LENGTH:
        result.add_error(
            f"Skill compatibility exceeds maximum length of {MAX_COMPATIBILITY_LENGTH} characters"
        )

    return result


def validate_skill_tags(tags: object) -> ValidationResult:
    """Validate skill tags.

    Requirements:
    - Optional (can be None or empty list)
    - Must be a list of strings
    - Each tag max 64 characters
    - No empty tags

    Args:
        tags: The tags list to validate

    Returns:
        ValidationResult with any errors
    """
    result = ValidationResult()

    if tags is None:
        return result

    if not isinstance(tags, list):
        result.add_error("Tags must be a list")
        return result

    for i, tag in enumerate(tags):
        if not isinstance(tag, str):
            result.add_error(f"Tag at index {i} must be a string")
            continue

        if tag.strip() == "":
            result.add_error(f"Tag at index {i} cannot be empty")
            continue

        if len(tag) > MAX_TAG_LENGTH:
            result.add_error(
                f"Tag '{tag[:20]}...' exceeds maximum length of {MAX_TAG_LENGTH} characters"
            )

    return result


def validate_skill_version(version: str | None) -> ValidationResult:
    """Validate a skill version string.

    Requirements:
    - Optional (can be None)
    - Must follow semver 2.0.0 pattern: MAJOR.MINOR.PATCH[-prerelease][+build]

    Args:
        version: The version string to validate

    Returns:
        ValidationResult with any errors
    """
    result = ValidationResult()

    if version is None or version == "":
        return result

    if not SEMVER_PATTERN.match(version):
        result.add_error(
            f"Version '{version}' does not follow semver pattern (e.g., '1.0.0', '2.1.3', '1.0.0-beta')"
        )

    return result


def validate_skill_category(category: str | None) -> ValidationResult:
    """Validate a skill category.

    Requirements:
    - Optional (can be None)
    - Must be lowercase alphanumeric + hyphens
    - Must start with a letter

    Args:
        category: The category to validate

    Returns:
        ValidationResult with any errors
    """
    result = ValidationResult()

    if category is None or category == "":
        return result

    if not CATEGORY_PATTERN.match(category):
        result.add_error(
            "Category must be lowercase letters, numbers, and hyphens, starting with a letter"
        )

    return result


class SkillValidator:
    """Validates a complete skill against the Agent Skills specification.

    This class combines all field validators to provide comprehensive
    skill validation. It can validate either a ParsedSkill object or
    raw field values.

    Example usage:
        ```python
        from gobby.skills.parser import parse_skill_file
        from gobby.skills.validator import SkillValidator

        skill = parse_skill_file("SKILL.md")
        validator = SkillValidator()
        result = validator.validate(skill)

        if not result.valid:
            for error in result.errors:
                print(f"Error: {error}")
        ```
    """

    def validate(
        self,
        skill: Any = None,
        *,
        name: str | None = None,
        description: str | None = None,
        compatibility: str | None = None,
        tags: list[str] | None = None,
        version: str | None = None,
        category: str | None = None,
    ) -> ValidationResult:
        """Validate a skill against the Agent Skills specification.

        Can accept either a ParsedSkill object or individual field values.
        If a skill object is provided, its fields take precedence.

        Args:
            skill: A ParsedSkill object to validate (optional)
            name: Skill name (required if no skill object)
            description: Skill description (required if no skill object)
            compatibility: Compatibility notes (optional)
            tags: List of tags (optional)
            version: Version string (optional)
            category: Category string (optional)

        Returns:
            ValidationResult with all errors and warnings
        """
        result = ValidationResult()

        # Extract fields from skill object if provided
        if skill is not None:
            name = getattr(skill, "name", name)
            description = getattr(skill, "description", description)
            compatibility = getattr(skill, "compatibility", compatibility)
            version = getattr(skill, "version", version)

            # Extract tags and category from metadata if available
            metadata = getattr(skill, "metadata", None)
            if metadata and isinstance(metadata, dict):
                skillport = metadata.get("skillport", {})
                if tags is None:
                    tags = skillport.get("tags")
                if category is None:
                    category = skillport.get("category")

        # Validate required fields
        result.merge(validate_skill_name(name))
        result.merge(validate_skill_description(description))

        # Validate optional fields
        result.merge(validate_skill_compatibility(compatibility))
        result.merge(validate_skill_tags(tags))
        result.merge(validate_skill_version(version))
        result.merge(validate_skill_category(category))

        return result

    def validate_parsed_skill(self, skill: Any) -> ValidationResult:
        """Validate a ParsedSkill object.

        This is a convenience method that wraps validate() for
        ParsedSkill objects specifically.

        Args:
            skill: A ParsedSkill object

        Returns:
            ValidationResult with all errors and warnings
        """
        return self.validate(skill=skill)
