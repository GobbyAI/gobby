from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind

if TYPE_CHECKING:
    from gobby.storage.spans import SpanStorage

logger = logging.getLogger(__name__)


class GobbySpanExporter(SpanExporter):
    """
    Custom OpenTelemetry SpanExporter that persists spans to the hub database and
    broadcasts trace events via WebSocket.
    """

    def __init__(
        self,
        storage: SpanStorage,
        broadcast_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.storage = storage
        self.broadcast_callback = broadcast_callback

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export spans to storage and broadcast."""
        try:
            span_dicts = [self._span_to_dict(span) for span in spans]
            self.storage.save_spans(span_dicts)

            if self.broadcast_callback:
                for span_dict in span_dicts:
                    # We broadcast each span as a trace_event
                    # Real-time UI can then append it to the trace tree
                    self.broadcast_callback(
                        {
                            "type": "trace_event",
                            "span": span_dict,
                            "trace_id": span_dict["trace_id"],
                        }
                    )

            return SpanExportResult.SUCCESS
        except Exception:
            logger.error("Error exporting spans", exc_info=True)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Shutdown the exporter."""
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush spans."""
        return True

    def _span_to_dict(self, span: ReadableSpan) -> dict[str, Any]:
        """Convert OTel ReadableSpan to a dictionary for storage."""
        attributes = dict(span.attributes) if span.attributes else {}

        # Extract events
        events = []
        if span.events:
            for event in span.events:
                events.append(
                    {
                        "name": event.name,
                        "timestamp": event.timestamp,
                        "attributes": dict(event.attributes) if event.attributes else {},
                    }
                )

        status = span.status
        # Use .name if available (StatusCode enum), otherwise fallback to string representation
        status_code = (
            getattr(status.status_code, "name", str(status.status_code)) if status else "UNSET"
        )
        status_message = status.description if status else None

        # parent_span_context can be None or have span_id=0
        parent_span_id = None
        if span.parent and span.parent.span_id != 0:
            parent_span_id = f"{span.parent.span_id:016x}"

        return {
            "span_id": f"{span.context.span_id:016x}",
            "trace_id": f"{span.context.trace_id:032x}",
            "parent_span_id": parent_span_id,
            "name": span.name,
            "kind": span.kind.name if isinstance(span.kind, SpanKind) else str(span.kind),
            "start_time_ns": span.start_time,
            "end_time_ns": span.end_time,
            "status": status_code,
            "status_message": status_message,
            "attributes": attributes,
            "events": events,
        }
