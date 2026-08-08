"""Skill storage errors."""


class SkillScopeConflictError(ValueError):
    """Raised when a skill move collides with an existing destination row."""


class DuplicateSkillError(ValueError):
    """Raised when a skill already exists in the requested scope."""


class SkillMetadataValidationError(ValueError):
    """Raised when persisted skill metadata violates its declared contract."""
