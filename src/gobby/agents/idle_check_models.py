"""State models used by the agent idle-check watchdog."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _CodexTranscriptEventSummary:
    line_num: int
    timestamp: str | None
    event_type: str
    payload_type: str

    def to_log_dict(self) -> dict[str, object]:
        return {
            "line_num": self.line_num,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload_type": self.payload_type,
        }


@dataclass(frozen=True, slots=True)
class _CodexTranscriptSnapshot:
    response_items: tuple[_CodexTranscriptEventSummary, ...]
    lifecycle_event: _CodexTranscriptEventSummary | None
    task_started_event: _CodexTranscriptEventSummary | None
    capacity_error_event: _CodexTranscriptEventSummary | None
    latest_model_output_line_num: int | None
    last_malformed_line_num: int | None = None

    @property
    def latest_response_payload_type(self) -> str | None:
        if not self.response_items:
            return None
        return self.response_items[-1].payload_type

    @property
    def has_conclusive_task_complete(self) -> bool:
        event = self.lifecycle_event
        if event is None or event.payload_type != "task_complete":
            return False
        return self.last_malformed_line_num is None

    @property
    def has_conclusive_capacity_error(self) -> bool:
        started = self.task_started_event
        error = self.capacity_error_event
        completed = self.lifecycle_event
        if started is None or error is None or completed is None:
            return False
        if completed.payload_type != "task_complete":
            return False
        if not started.line_num < error.line_num < completed.line_num:
            return False
        return self.last_malformed_line_num is None

    def to_log_dict(self) -> dict[str, object]:
        return {
            "response_items": [item.to_log_dict() for item in self.response_items],
            "lifecycle_event": (
                self.lifecycle_event.to_log_dict() if self.lifecycle_event is not None else None
            ),
            "capacity_error_event": (
                self.capacity_error_event.to_log_dict()
                if self.capacity_error_event is not None
                else None
            ),
        }


@dataclass(slots=True)
class _CodexCapacityRecoveryState:
    transcript_path: str
    last_error_line_num: int | None = None
    successful_reprompts: int = 0
