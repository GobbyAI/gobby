"""Structurally redacted models emitted by transcript watchdog readers."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

ActivityKind = Literal["reasoning", "message", "tool", "user_input", "other"]
TurnEventKind = Literal["started", "completed", "aborted"]
ProviderErrorKind = Literal["capacity", "api_error", "retry"]

WATCHDOG_TAIL_LIMIT = 8

KNOWN_ACTIVITY_KINDS = frozenset({"reasoning", "message", "tool", "user_input", "other"})
KNOWN_TURN_EVENT_KINDS = frozenset({"started", "completed", "aborted"})
KNOWN_PROVIDER_ERROR_KINDS = frozenset({"capacity", "api_error", "retry"})
KNOWN_ERROR_REASONS = frozenset({"server_overloaded", "api_error", "retrying"})
KNOWN_WATCHDOG_PROVIDERS = frozenset({"agy", "claude", "codex", "droid", "grok", "qwen"})

# Readers may retain only these structural labels. Raw content never enters a model.
KNOWN_EVENT_TYPES = frozenset(
    {
        "assistant",
        "event_msg",
        "message",
        "response_item",
        "session_end",
        "session_start",
        "session_update",
        "system",
        "tool_result",
        "user",
    }
)
KNOWN_PAYLOAD_TYPES = frozenset(
    {
        "agent_message",
        "agent_message_chunk",
        "agent_reasoning",
        "agent_thought_chunk",
        "api_error",
        "compaction_state",
        "context_compacted",
        "custom_tool_call",
        "custom_tool_call_output",
        "error",
        "function_call",
        "function_call_output",
        "message",
        "reasoning",
        "retry_state",
        "session_end",
        "session_start",
        "snapshot",
        "task_complete",
        "task_started",
        "telemetry",
        "text",
        "thinking",
        "todo_state",
        "tool_call",
        "tool_call_update",
        "tool_result",
        "tool_use",
        "turn_aborted",
        "turn_completed",
        "turn_duration",
        "user_message_chunk",
        "web_search_call",
    }
)


def _normalize_timestamp(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_line_num(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a non-bool int")
    return value


def _optional_line_num(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_line_num(value, field_name)


@dataclass(frozen=True, slots=True)
class TranscriptEventSummary:
    """A content-free summary of one structurally relevant transcript record."""

    line_num: int
    timestamp: datetime | None
    event_type: str
    payload_type: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_num", _require_line_num(self.line_num, "line_num"))
        object.__setattr__(self, "timestamp", _normalize_timestamp(self.timestamp))
        object.__setattr__(
            self,
            "event_type",
            self.event_type
            if isinstance(self.event_type, str) and self.event_type in KNOWN_EVENT_TYPES
            else "other",
        )
        object.__setattr__(
            self,
            "payload_type",
            self.payload_type
            if isinstance(self.payload_type, str) and self.payload_type in KNOWN_PAYLOAD_TYPES
            else "other",
        )

    def to_log_dict(self) -> dict[str, object]:
        return {
            "line_num": self.line_num,
            "timestamp": self.timestamp.isoformat() if self.timestamp is not None else None,
            "event_type": self.event_type,
            "payload_type": self.payload_type,
        }


def _optional_event(value: object, field_name: str) -> TranscriptEventSummary | None:
    if value is None:
        return None
    if not isinstance(value, TranscriptEventSummary):
        raise TypeError(f"{field_name} must be a TranscriptEventSummary or None")
    return value


@dataclass(frozen=True, slots=True)
class WatchdogTranscriptSnapshot:
    """Provider-neutral transcript signals safe to serialize into diagnostics."""

    provider: str
    tail: tuple[TranscriptEventSummary, ...] = ()
    turn_started_event: TranscriptEventSummary | None = None
    latest_turn_event: TranscriptEventSummary | None = None
    latest_turn_kind: TurnEventKind | None = None
    provider_error_event: TranscriptEventSummary | None = None
    provider_error_kind: ProviderErrorKind | None = None
    provider_error_reason: str | None = None
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None
    last_malformed_line_num: int | None = None

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower() if isinstance(self.provider, str) else ""
        object.__setattr__(
            self,
            "provider",
            provider if provider in KNOWN_WATCHDOG_PROVIDERS else "unknown",
        )
        if not isinstance(self.tail, tuple):
            raise TypeError("tail must be a tuple of TranscriptEventSummary instances")
        if not all(isinstance(item, TranscriptEventSummary) for item in self.tail):
            raise TypeError("tail must contain only TranscriptEventSummary instances")
        object.__setattr__(self, "tail", self.tail[-WATCHDOG_TAIL_LIMIT:])
        object.__setattr__(
            self,
            "turn_started_event",
            _optional_event(self.turn_started_event, "turn_started_event"),
        )
        object.__setattr__(
            self,
            "latest_turn_event",
            _optional_event(self.latest_turn_event, "latest_turn_event"),
        )
        object.__setattr__(
            self,
            "latest_turn_kind",
            self.latest_turn_kind
            if isinstance(self.latest_turn_kind, str)
            and self.latest_turn_kind in KNOWN_TURN_EVENT_KINDS
            else None,
        )
        object.__setattr__(
            self,
            "provider_error_event",
            _optional_event(self.provider_error_event, "provider_error_event"),
        )
        object.__setattr__(
            self,
            "provider_error_kind",
            self.provider_error_kind
            if isinstance(self.provider_error_kind, str)
            and self.provider_error_kind in KNOWN_PROVIDER_ERROR_KINDS
            else None,
        )
        object.__setattr__(
            self,
            "provider_error_reason",
            self.provider_error_reason
            if isinstance(self.provider_error_reason, str)
            and self.provider_error_reason in KNOWN_ERROR_REASONS
            else None,
        )
        object.__setattr__(
            self,
            "latest_activity_kind",
            self.latest_activity_kind
            if isinstance(self.latest_activity_kind, str)
            and self.latest_activity_kind in KNOWN_ACTIVITY_KINDS
            else None,
        )
        object.__setattr__(
            self,
            "latest_model_output_line_num",
            _optional_line_num(
                self.latest_model_output_line_num,
                "latest_model_output_line_num",
            ),
        )
        object.__setattr__(
            self,
            "last_malformed_line_num",
            _optional_line_num(self.last_malformed_line_num, "last_malformed_line_num"),
        )

    @property
    def has_conclusive_turn_completed(self) -> bool:
        event = self.latest_turn_event
        return (
            self.latest_turn_kind == "completed"
            and event is not None
            and event.timestamp is not None
            and (
                self.latest_model_output_line_num is None
                or self.latest_model_output_line_num <= event.line_num
            )
            and self.last_malformed_line_num is None
        )

    @property
    def has_conclusive_capacity_error(self) -> bool:
        started = self.turn_started_event
        error = self.provider_error_event
        completed = self.latest_turn_event
        if self.provider_error_kind != "capacity":
            return False
        if started is None or error is None or completed is None:
            return False
        if self.latest_turn_kind != "completed":
            return False
        if not started.line_num < error.line_num < completed.line_num:
            return False
        return self.last_malformed_line_num is None

    def to_log_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "tail": [item.to_log_dict() for item in self.tail],
            "turn_started_event": (
                self.turn_started_event.to_log_dict()
                if self.turn_started_event is not None
                else None
            ),
            "latest_turn_event": (
                self.latest_turn_event.to_log_dict() if self.latest_turn_event is not None else None
            ),
            "latest_turn_kind": self.latest_turn_kind,
            "provider_error_event": (
                self.provider_error_event.to_log_dict()
                if self.provider_error_event is not None
                else None
            ),
            "provider_error_kind": self.provider_error_kind,
            "provider_error_reason": self.provider_error_reason,
            "latest_activity_kind": self.latest_activity_kind,
            "latest_model_output_line_num": self.latest_model_output_line_num,
            "last_malformed_line_num": self.last_malformed_line_num,
        }


@dataclass(slots=True)
class CapacityRecoveryState:
    transcript_path: str
    last_error_line_num: int | None = None
    successful_reprompts: int = 0


@dataclass(slots=True)
class CompletedTurnRecoveryState:
    workflow_fingerprint: str | None = None
    last_completion_identity: tuple[str, int, datetime] | None = None
    successful_reprompts: int = 0
