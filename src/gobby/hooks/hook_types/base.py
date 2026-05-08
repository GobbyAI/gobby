"""Pydantic base classes shared by every hook input and output model."""

from pydantic import BaseModel, ConfigDict, Field


class HookInput(BaseModel):
    """
    Base class for all hook input models.

    Provides common fields and configuration for hook inputs.
    All hook-specific input models should inherit from this base.
    """

    model_config = ConfigDict(
        extra="allow",  # Allow extra fields for future extensibility
        validate_assignment=True,  # Validate on attribute assignment
        str_strip_whitespace=True,  # Strip whitespace from strings
    )


class HookOutput(BaseModel):
    """
    Base class for all hook output models.

    Provides common fields for hook responses.
    All hook-specific output models should inherit from this base.
    """

    status: str = Field(default="success", description="Execution status (success/error/queued)")
    message: str | None = Field(default=None, description="Optional message or error details")

    model_config = ConfigDict(
        extra="allow",  # Allow extra fields for future extensibility
        validate_assignment=True,
        populate_by_name=True,
    )
