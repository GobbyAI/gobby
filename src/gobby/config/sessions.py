"""
Session configuration module.

Contains session-related Pydantic config models:
- SessionSummaryConfig: Session summary (handoff) generation settings
- MessageTrackingConfig: Session message tracking settings
- SessionLifecycleConfig: Session lifecycle management settings
- SessionFeedbackConfig: Gobby-experience survey capture settings

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from gobby.config.feature_base import FeatureDefaultConfig, FeatureProfile

__all__ = [
    "ChatHistoryConfig",
    "SessionSummaryConfig",
    "MessageTrackingConfig",
    "SessionLifecycleConfig",
    "SessionFeedbackConfig",
    "FeedbackReviewConfig",
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


class SessionSummaryConfig(FeatureDefaultConfig):
    """Session summary generation configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based session summary generation",
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
Always include these two mandatory sections using the exact headings:

## Current State
State what is working, broken, in progress, or complete. When the session's work is
complete, say so explicitly.

## Next Steps
List clear continuation actions. When implementation is complete, state the remaining
handoff action, such as committing, linking the commit, validating, or closing the task.

Optional sections may describe what was accomplished, files changed, technical decisions,
or problems encountered. Omit optional sections that have no relevant information.

Be concise. Focus on what the next agent needs to know to continue effectively.""",
        description="Prompt template for session summary (use placeholders: {transcript_summary}, {git_status}, {file_changes}, {session_tasks})",
    )
    summary_file_path: str = Field(
        default=".gobby/session_summaries",
        description="Directory path for session summary markdown files",
    )


class MemoryUsefulnessConfig(FeatureDefaultConfig):
    """Turn-end memory-usefulness judge configuration (#17195).

    Routes the de-biased usefulness judge (contract §4). Configure candidates
    to a model family different from the coding agents whose transcripts are
    judged; the resolved candidate is recorded as judge_model on every label
    row. Enablement lives on memory.shadow_relevance_judging.
    """

    timeout: int = Field(
        default=30,
        gt=0,
        description="Timeout in seconds for usefulness-judge LLM calls (default 30s).",
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
    workflow_audit_retention_days: int = Field(
        default=7,
        description="Days to retain workflow audit rows before maintenance prunes them",
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
        "workflow_audit_retention_days",
    )
    @classmethod
    def validate_positive(cls, v: int) -> int:
        """Validate value is positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v


class FeedbackReviewConfig(FeatureDefaultConfig):
    """Nightly review loop over unreviewed session_feedback rows."""

    enabled: bool = Field(
        default=True,
        description="Enable the scheduled session-feedback review job",
    )
    schedule_cron: str = Field(
        default="0 3 * * *",
        description="Cron expression for the system feedback-review job",
    )
    prompt_path: str = Field(
        default="feedback/review",
        description="Prompt template path for the feedback distill pass",
    )
    max_tokens: int = Field(
        default=8192,
        ge=1,
        description="Maximum tokens for feedback distill responses",
    )
    max_rows_per_run: int = Field(
        default=200,
        ge=1,
        description="Maximum unreviewed feedback rows batched into one run",
    )
    max_tasks_per_run: int = Field(
        default=10,
        ge=0,
        description="Maximum tasks one run may file into the gobby project",
    )
    profile: FeatureProfile = Field(
        default=FeatureProfile.MID,
        description="Provider-agnostic capability profile for feedback review",
    )


class SessionFeedbackConfig(BaseModel):
    """Gobby-experience survey capture (session_feedback table)."""

    survey: Literal["all", "gobby", "off"] = Field(
        default="all",
        description=(
            "Who gets the session-feedback survey gates. "
            "all: every project. "
            "gobby: only projects named gobby. "
            "off: no prompts; gobby-sessions:feedback remains callable."
        ),
    )
    review: FeedbackReviewConfig = Field(
        default_factory=FeedbackReviewConfig,
        description="Scheduled review loop that distills unreviewed feedback rows",
    )
