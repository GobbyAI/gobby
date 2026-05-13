"""Tests for skill validation functions."""

import pytest

from gobby.skills.parser import ParsedSkill
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

pytestmark = pytest.mark.unit


class TestValidationResult:
    """Tests for ValidationResult class."""

    def test_default_is_valid(self) -> None:
        """Test that default result is valid."""
        result = ValidationResult()
        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []

    def test_add_error_marks_invalid(self) -> None:
        """Test that adding an error marks result invalid."""
        result = ValidationResult()
        result.add_error("Test error")
        assert result.valid is False
        assert "Test error" in result.errors

    def test_add_warning_keeps_valid(self) -> None:
        """Test that warnings don't affect validity."""
        result = ValidationResult()
        result.add_warning("Test warning")
        assert result.valid is True
        assert "Test warning" in result.warnings

    def test_merge_combines_results(self) -> None:
        """Test merging two results."""
        r1 = ValidationResult()
        r1.add_error("Error 1")
        r1.add_warning("Warning 1")

        r2 = ValidationResult()
        r2.add_error("Error 2")

        r1.merge(r2)
        assert r1.valid is False
        assert len(r1.errors) == 2
        assert "Error 1" in r1.errors
        assert "Error 2" in r1.errors

    def test_to_dict(self) -> None:
        """Test dictionary conversion."""
        result = ValidationResult()
        result.add_error("Error")
        result.add_warning("Warning")

        d = result.to_dict()
        assert d["valid"] is False
        assert "Error" in d["errors"]
        assert "Warning" in d["warnings"]


class TestValidateSkillName:
    """Tests for validate_skill_name function."""

    def test_valid_simple_name(self) -> None:
        """Test valid simple names."""
        assert validate_skill_name("commit-message").valid is True
        assert validate_skill_name("git").valid is True
        assert validate_skill_name("code-review").valid is True
        assert validate_skill_name("my-skill-name").valid is True

    def test_valid_name_with_numbers(self) -> None:
        """Test valid names with numbers."""
        assert validate_skill_name("skill2").valid is True
        assert validate_skill_name("my-skill-v2").valid is True
        assert validate_skill_name("pr123-reviewer").valid is True

    def test_rejects_empty_name(self) -> None:
        """Test that empty names are rejected."""
        result = validate_skill_name("")
        assert result.valid is False
        assert any("required" in e.lower() for e in result.errors)

    def test_rejects_none_name(self) -> None:
        """Test that None names are rejected."""
        result = validate_skill_name(None)
        assert result.valid is False
        assert any("required" in e.lower() for e in result.errors)

    def test_rejects_uppercase(self) -> None:
        """Test that uppercase letters are rejected."""
        result = validate_skill_name("CommitMessage")
        assert result.valid is False
        assert any("lowercase" in e.lower() for e in result.errors)

        result = validate_skill_name("SKILL")
        assert result.valid is False

        result = validate_skill_name("mySkill")
        assert result.valid is False

    def test_rejects_leading_hyphen(self) -> None:
        """Test that leading hyphens are rejected."""
        result = validate_skill_name("-skill")
        assert result.valid is False
        assert any("start with" in e.lower() for e in result.errors)

    def test_rejects_trailing_hyphen(self) -> None:
        """Test that trailing hyphens are rejected."""
        result = validate_skill_name("skill-")
        assert result.valid is False
        assert any("end with" in e.lower() for e in result.errors)

    def test_rejects_consecutive_hyphens(self) -> None:
        """Test that consecutive hyphens are rejected."""
        result = validate_skill_name("my--skill")
        assert result.valid is False
        assert any("consecutive" in e.lower() for e in result.errors)

        result = validate_skill_name("skill---name")
        assert result.valid is False

    def test_rejects_too_long_name(self) -> None:
        """Test that names over 64 chars are rejected."""
        long_name = "a" * 65
        result = validate_skill_name(long_name)
        assert result.valid is False
        assert any("64" in e for e in result.errors)

    def test_accepts_max_length_name(self) -> None:
        """Test that 64-char names are accepted."""
        max_name = "a" * 64
        assert validate_skill_name(max_name).valid is True

    def test_rejects_special_characters(self) -> None:
        """Test that special characters are rejected."""
        assert validate_skill_name("my_skill").valid is False
        assert validate_skill_name("my.skill").valid is False
        assert validate_skill_name("my skill").valid is False
        assert validate_skill_name("my@skill").valid is False

    def test_rejects_starting_with_number(self) -> None:
        """Test that names starting with numbers are rejected."""
        result = validate_skill_name("2skill")
        assert result.valid is False


class TestValidateSkillDescription:
    """Tests for validate_skill_description function."""

    def test_valid_description(self) -> None:
        """Test valid descriptions."""
        assert validate_skill_description("A simple skill").valid is True
        assert validate_skill_description("Generate commit messages").valid is True

    def test_rejects_empty_description(self) -> None:
        """Test that empty descriptions are rejected."""
        result = validate_skill_description("")
        assert result.valid is False
        assert any("required" in e.lower() for e in result.errors)

    def test_rejects_whitespace_only(self) -> None:
        """Test that whitespace-only descriptions are rejected."""
        result = validate_skill_description("   ")
        assert result.valid is False

        result = validate_skill_description("\t\n")
        assert result.valid is False

    def test_rejects_none_description(self) -> None:
        """Test that None descriptions are rejected."""
        result = validate_skill_description(None)
        assert result.valid is False

    def test_rejects_too_long_description(self) -> None:
        """Test that descriptions over 1024 chars are rejected."""
        long_desc = "a" * 1025
        result = validate_skill_description(long_desc)
        assert result.valid is False
        assert any("1024" in e for e in result.errors)

    def test_accepts_max_length_description(self) -> None:
        """Test that 1024-char descriptions are accepted."""
        max_desc = "a" * 1024
        assert validate_skill_description(max_desc).valid is True


class TestValidateSkillCompatibility:
    """Tests for validate_skill_compatibility function."""

    def test_valid_compatibility(self) -> None:
        """Test valid compatibility strings."""
        assert validate_skill_compatibility("Requires Python 3.11+").valid is True
        assert validate_skill_compatibility("Works with git CLI").valid is True

    def test_accepts_empty_compatibility(self) -> None:
        """Test that empty compatibility is accepted (optional field)."""
        assert validate_skill_compatibility("").valid is True
        assert validate_skill_compatibility(None).valid is True

    def test_rejects_too_long_compatibility(self) -> None:
        """Test that compatibility over 500 chars is rejected."""
        long_compat = "a" * 501
        result = validate_skill_compatibility(long_compat)
        assert result.valid is False
        assert any("500" in e for e in result.errors)

    def test_accepts_max_length_compatibility(self) -> None:
        """Test that 500-char compatibility is accepted."""
        max_compat = "a" * 500
        assert validate_skill_compatibility(max_compat).valid is True


class TestValidateSkillTags:
    """Tests for validate_skill_tags function."""

    def test_valid_tags(self) -> None:
        """Test valid tag lists."""
        assert validate_skill_tags(["git", "commits"]).valid is True
        assert validate_skill_tags(["code-review", "quality"]).valid is True

    def test_accepts_empty_tags(self) -> None:
        """Test that empty/None tags are accepted (optional)."""
        assert validate_skill_tags([]).valid is True
        assert validate_skill_tags(None).valid is True

    def test_rejects_non_list(self) -> None:
        """Test that non-list values are rejected."""
        result = validate_skill_tags("git")
        assert result.valid is False
        assert any("list" in e.lower() for e in result.errors)

    def test_rejects_non_string_tags(self) -> None:
        """Test that non-string tags are rejected."""
        result = validate_skill_tags([123, "valid"])
        assert result.valid is False
        assert any("string" in e.lower() for e in result.errors)

    def test_rejects_empty_string_tags(self) -> None:
        """Test that empty string tags are rejected."""
        result = validate_skill_tags(["valid", ""])
        assert result.valid is False
        assert any("empty" in e.lower() for e in result.errors)

    def test_rejects_too_long_tags(self) -> None:
        """Test that tags over 64 chars are rejected."""
        long_tag = "a" * 65
        result = validate_skill_tags([long_tag])
        assert result.valid is False
        assert any("64" in e for e in result.errors)


class TestValidateSkillVersion:
    """Tests for validate_skill_version function."""

    def test_valid_versions(self) -> None:
        """Test valid version strings (SemVer 2.0.0 MAJOR.MINOR.PATCH required)."""
        assert validate_skill_version("1.0.0").valid is True
        assert validate_skill_version("2.1.3").valid is True
        assert validate_skill_version("1.0.0-beta").valid is True
        assert validate_skill_version("1.0.0-alpha.1").valid is True
        assert validate_skill_version("1.0.0+build123").valid is True

    def test_accepts_empty_version(self) -> None:
        """Test that empty/None versions are accepted (optional)."""
        assert validate_skill_version("").valid is True
        assert validate_skill_version(None).valid is True

    def test_rejects_invalid_versions(self) -> None:
        """Test that invalid version strings are rejected."""
        assert validate_skill_version("v1.0").valid is False
        assert validate_skill_version("1").valid is False
        assert validate_skill_version("latest").valid is False
        assert validate_skill_version("1.0.0.0").valid is False


class TestValidateSkillCategory:
    """Tests for validate_skill_category function."""

    def test_valid_categories(self) -> None:
        """Test valid category strings."""
        assert validate_skill_category("git").valid is True
        assert validate_skill_category("code-review").valid is True
        assert validate_skill_category("ci-cd").valid is True
        assert validate_skill_category("testing").valid is True

    def test_accepts_empty_category(self) -> None:
        """Test that empty/None categories are accepted (optional)."""
        assert validate_skill_category("").valid is True
        assert validate_skill_category(None).valid is True

    def test_rejects_uppercase_category(self) -> None:
        """Test that uppercase categories are rejected."""
        result = validate_skill_category("Git")
        assert result.valid is False

    def test_rejects_invalid_category_chars(self) -> None:
        """Test that invalid characters are rejected."""
        assert validate_skill_category("code_review").valid is False
        assert validate_skill_category("code.review").valid is False
        assert validate_skill_category("code review").valid is False

    def test_rejects_starting_with_number(self) -> None:
        """Test that categories starting with numbers are rejected."""
        result = validate_skill_category("2git")
        assert result.valid is False


class TestSkillValidator:
    """Tests for SkillValidator class."""

    def test_validate_valid_skill_fields(self) -> None:
        """Test validating valid skill fields."""
        validator = SkillValidator()
        result = validator.validate(
            name="my-skill",
            description="A valid skill description",
        )
        assert result.valid is True
        assert len(result.errors) == 0

    def test_validate_all_optional_fields(self) -> None:
        """Test validating with all optional fields."""
        validator = SkillValidator()
        result = validator.validate(
            name="full-skill",
            description="A fully specified skill",
            compatibility="Requires Python 3.11+",
            tags=["git", "workflow"],
            version="1.0.0",
            category="git",
        )
        assert result.valid is True
        assert len(result.errors) == 0

    def test_validate_invalid_name(self) -> None:
        """Test that invalid name is caught."""
        validator = SkillValidator()
        result = validator.validate(
            name="InvalidName",  # Uppercase not allowed
            description="Valid description",
        )
        assert result.valid is False
        assert any("lowercase" in e.lower() for e in result.errors)

    def test_validate_missing_description(self) -> None:
        """Test that missing description is caught."""
        validator = SkillValidator()
        result = validator.validate(
            name="valid-name",
            description="",
        )
        assert result.valid is False
        assert any("description" in e.lower() for e in result.errors)

    def test_validate_multiple_errors(self) -> None:
        """Test that multiple errors are collected."""
        validator = SkillValidator()
        result = validator.validate(
            name="",  # Invalid - empty
            description="",  # Invalid - empty
            version="invalid",  # Invalid - not semver
            category="Invalid",  # Invalid - uppercase
        )
        assert result.valid is False
        assert len(result.errors) >= 4

    def test_validate_parsed_skill(self) -> None:
        """Test validating a ParsedSkill object."""
        skill = ParsedSkill(
            name="test-skill",
            description="A test skill",
            content="# Test\n\nContent here.",
            version="1.0.0",
        )
        validator = SkillValidator()
        result = validator.validate(skill)
        assert result.valid is True

    def test_validate_parsed_skill_with_metadata(self) -> None:
        """Test validating a ParsedSkill with metadata."""
        skill = ParsedSkill(
            name="test-skill",
            description="A test skill",
            content="Content",
            metadata={
                "skillport": {
                    "category": "git",
                    "tags": ["git", "commits"],
                }
            },
        )
        validator = SkillValidator()
        result = validator.validate(skill)
        assert result.valid is True

    def test_validate_parsed_skill_invalid_metadata(self) -> None:
        """Test validating a ParsedSkill with invalid metadata."""
        skill = ParsedSkill(
            name="test-skill",
            description="A test skill",
            content="Content",
            metadata={
                "skillport": {
                    "category": "Invalid",  # Uppercase not allowed
                    "tags": ["git", 123],  # Non-string tag
                }
            },
        )
        validator = SkillValidator()
        result = validator.validate(skill)
        assert result.valid is False
        assert len(result.errors) >= 2

    def test_validate_parsed_skill_method(self) -> None:
        """Test validate_parsed_skill convenience method."""
        skill = ParsedSkill(
            name="my-skill",
            description="Description",
            content="Content",
        )
        validator = SkillValidator()
        result = validator.validate_parsed_skill(skill)
        assert result.valid is True

    def test_validate_skill_overrides_kwargs(self) -> None:
        """Test that skill object values take precedence over kwargs."""
        skill = ParsedSkill(
            name="skill-name",  # This should be used
            description="Skill description",
            content="Content",
        )
        validator = SkillValidator()
        result = validator.validate(
            skill,
            name="different-name",  # Should be ignored
            description="Different description",  # Should be ignored
        )
        assert result.valid is True
        # The skill's name is used, not the kwarg

    def test_validate_invalid_tags_in_metadata(self) -> None:
        """Test that invalid tags in metadata are caught."""
        skill = ParsedSkill(
            name="test-skill",
            description="Description",
            content="Content",
            metadata={
                "skillport": {
                    "tags": ["", "valid"],  # Empty tag not allowed
                }
            },
        )
        validator = SkillValidator()
        result = validator.validate(skill)
        assert result.valid is False
        assert any("empty" in e.lower() for e in result.errors)

    def test_validate_invalid_version(self) -> None:
        """Test that invalid version is caught."""
        validator = SkillValidator()
        result = validator.validate(
            name="valid-name",
            description="Valid description",
            version="not-a-version",
        )
        assert result.valid is False
        assert any("semver" in e.lower() for e in result.errors)

    def test_validate_compatibility_too_long(self) -> None:
        """Test that overly long compatibility is caught."""
        validator = SkillValidator()
        result = validator.validate(
            name="valid-name",
            description="Valid description",
            compatibility="x" * 501,  # Max is 500
        )
        assert result.valid is False
        assert any("500" in e for e in result.errors)
