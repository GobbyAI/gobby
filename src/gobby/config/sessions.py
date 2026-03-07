"""
Session configuration module.

Contains session-related Pydantic config models:
- ContextInjectionConfig: Subagent context injection settings
- SessionSummaryConfig: Session summary generation settings
- DigestConfig: Rolling digest and title generation settings
- MessageTrackingConfig: Session message tracking settings
- SessionLifecycleConfig: Session lifecycle management settings

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "ChatHistoryConfig",
    "ContextInjectionConfig",
    "DigestConfig",
    "SessionSummaryConfig",
    "SessionTitleConfig",
    "MessageTrackingConfig",
    "SessionLifecycleConfig",
]


class ChatHistoryConfig(BaseModel):
    """Configuration for chat history injection on session recreation.

    Controls how much prior conversation context is loaded and injected
    when a ChatSession is recreated after a disconnect.
    """

    max_message_chars: int = Field(
        default=2000,
        gt=0,
        description="Maximum characters per individual history message before truncation.",
    )
    max_total_chars: int = Field(
        default=30_000,
        gt=0,
        description="Maximum total characters of combined history context to inject.",
    )


class ContextInjectionConfig(BaseModel):
    """Context injection configuration for subagent spawning.

    Controls how context is resolved and injected into subagent prompts.
    """

    enabled: bool = Field(
        default=True,
        description="Enable context injection for subagents",
    )
    default_source: str = Field(
        default="summary_markdown",
        description="Default context source when not specified. "
        "Options: summary_markdown, compact_markdown, session_id:<id>, "
        "transcript:<n>, file:<path>",
    )
    max_file_size: int = Field(
        default=51200,
        description="Maximum file size in bytes for file: source (default: 50KB)",
    )
    max_content_size: int = Field(
        default=51200,
        description="Maximum content size in bytes for all sources (default: 50KB)",
    )
    max_transcript_messages: int = Field(
        default=100,
        description="Maximum number of messages for transcript: source",
    )
    truncation_suffix: str = Field(
        default="\n\n[truncated: {bytes} bytes remaining]",
        description="Suffix template appended when content is truncated",
    )
    context_template: str | None = Field(
        default=None,
        description="Custom template for context injection. "
        "Use {{ context }} and {{ prompt }} placeholders. "
        "If None, uses the default template.",
    )

    @field_validator("max_file_size", "max_content_size", "max_transcript_messages")
    @classmethod
    def validate_positive(cls, v: int) -> int:
        """Validate value is positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v


class SessionSummaryConfig(BaseModel):
    """Session summary generation configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based session summary generation",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for session summary",
    )
    model: str = Field(
        default="sonnet",
        description="Model to use for session summary generation",
    )
    prompt: str = Field(
        default="""Generate a concise session summary for handoff to another agent or future session.

## Session Context
Transcript Summary:
{transcript_summary}

Git Status:
{git_status}

File Changes:
{file_changes}

{session_tasks}

## Instructions
Create a summary with these sections:
1. **What was accomplished** - Key completions in 2-3 bullet points
2. **Current state** - What's in progress or pending
3. **Next steps** - Clear actionable items for continuation

Be concise. Focus on what the next agent needs to know to continue effectively.""",
        description="Prompt template for session summary (use placeholders: {transcript_summary}, {git_status}, {file_changes}, {session_tasks})",
    )
    summary_file_path: str = Field(
        default="~/.gobby/session_summaries",
        description="Directory path for session summary markdown files",
    )


class DigestConfig(BaseModel):
    """Rolling digest and title generation configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable background digest and title generation",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for digest generation",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for digest generation",
    )


class SessionTitleConfig(BaseModel):
    """Configuration for session title synthesis LLM calls."""

    provider: str = Field(
        default="claude",
        description="LLM provider to use for title synthesis",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for title synthesis (fast/cheap recommended)",
    )
    timeout: int = Field(
        default=30,
        gt=0,
        description="Timeout in seconds for the LLM call (default 30s to accommodate CLI subprocess cold start)",
    )


class MessageTrackingConfig(BaseModel):
    """Configuration for session message tracking."""

    enabled: bool = Field(
        default=True,
        description="Enable session message tracking",
    )
    poll_interval: float = Field(
        default=5.0,
        description="Polling interval in seconds for transcript updates",
    )
    debounce_delay: float = Field(
        default=1.0,
        description="Debounce delay in seconds for message processing",
    )
    max_message_length: int = Field(
        default=10000,
        description="Maximum length of a single message content",
    )
    broadcast_enabled: bool = Field(
        default=True,
        description="Enable broadcasting message events",
    )

    @field_validator("poll_interval", "debounce_delay")
    @classmethod
    def validate_positive(cls, v: float) -> float:
        """Validate value is positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v


class SessionLifecycleConfig(BaseModel):
    """Configuration for session lifecycle management.

    Handles:
    - Pausing active sessions with no recent activity
    - Expiring stale sessions (active/paused for too long)
    - Background transcript processing for expired sessions
    """

    active_session_pause_minutes: int = Field(
        default=30,
        description="Minutes of inactivity before active sessions are marked paused",
    )
    stale_session_timeout_hours: int = Field(
        default=24,
        description="Hours after which inactive sessions are marked expired",
    )
    expire_check_interval_minutes: int = Field(
        default=60,
        description="How often to check for stale sessions (minutes)",
    )
    transcript_processing_interval_minutes: int = Field(
        default=5,
        description="How often to process pending transcripts (minutes)",
    )
    transcript_processing_batch_size: int = Field(
        default=10,
        description="Maximum sessions to process per batch",
    )
    transcript_archive_dir: str = Field(
        default="~/.gobby/session_transcripts",
        description="Directory for gzip-compressed transcript backups",
    )

    @field_validator(
        "active_session_pause_minutes",
        "stale_session_timeout_hours",
        "expire_check_interval_minutes",
        "transcript_processing_interval_minutes",
        "transcript_processing_batch_size",
    )
    @classmethod
    def validate_positive(cls, v: int) -> int:
        """Validate value is positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v
