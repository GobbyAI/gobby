import logging
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode
from psycopg_pool import PoolTimeout

from gobby.storage.spans import SpanStorage
from gobby.telemetry.span_store import GobbySpanExporter


@pytest.fixture
def mock_storage():
    return MagicMock()


@pytest.fixture
def exporter(mock_storage):
    return GobbySpanExporter(mock_storage)


def _make_span() -> MagicMock:
    span = MagicMock(spec=ReadableSpan)
    span.name = "test-span"
    span.context = SpanContext(trace_id=1, span_id=1, is_remote=False)
    span.parent = None
    span.kind = SpanKind.INTERNAL
    span.start_time = 100
    span.end_time = 200
    span.status = Status(status_code=StatusCode.OK)
    span.attributes = {}
    span.events = []
    return span


def _span_payload() -> dict[str, object]:
    return {
        "span_id": "0000000000000001",
        "trace_id": "00000000000000000000000000000001",
        "parent_span_id": None,
        "name": "test-span",
        "kind": "INTERNAL",
        "start_time_ns": 100,
        "end_time_ns": 200,
        "status": "OK",
        "status_message": None,
        "attributes": {},
        "events": [],
    }


def test_export_spans(exporter, mock_storage):
    # Mock a ReadableSpan
    span = MagicMock(spec=ReadableSpan)
    span.name = "test-span"
    span.context = SpanContext(
        trace_id=0x12345678123456781234567812345678, span_id=0x1234567812345678, is_remote=False
    )
    span.parent = None
    span.kind = SpanKind.INTERNAL
    span.start_time = 1000000
    span.end_time = 2000000
    span.status = Status(status_code=StatusCode.OK, description="All good")
    span.attributes = {"key": "value"}
    span.events = []

    exporter.export([span])

    assert mock_storage.save_spans.called
    saved_spans = mock_storage.save_spans.call_args[0][0]
    assert len(saved_spans) == 1
    assert saved_spans[0]["span_id"] == "1234567812345678"
    assert saved_spans[0]["trace_id"] == "12345678123456781234567812345678"
    assert saved_spans[0]["name"] == "test-span"
    assert saved_spans[0]["status"] == "OK"
    assert saved_spans[0]["attributes"] == {"key": "value"}


def test_export_pool_timeout_logs_warning_without_error(caplog: pytest.LogCaptureFixture) -> None:
    storage = MagicMock()
    storage.save_spans.side_effect = PoolTimeout("couldn't get a connection after 5.00 sec")
    exporter = GobbySpanExporter(storage)

    with caplog.at_level(logging.WARNING, logger="gobby.telemetry.span_store"):
        result = exporter.export([_make_span()])

    assert result is SpanExportResult.FAILURE
    assert "Dropping 1 telemetry spans" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_span_storage_pool_timeout_logs_warning_without_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = MagicMock()
    db.executemany.side_effect = PoolTimeout("couldn't get a connection after 5.00 sec")
    storage = SpanStorage(db)

    with caplog.at_level(logging.WARNING, logger="gobby.storage.spans"):
        with pytest.raises(PoolTimeout):
            storage.save_spans([_span_payload()])

    assert "Dropping 1 telemetry spans" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_broadcast_callback(mock_storage):
    callback = MagicMock()
    exporter = GobbySpanExporter(mock_storage, broadcast_callback=callback)

    span = MagicMock(spec=ReadableSpan)
    span.name = "test-span"
    span.context = SpanContext(trace_id=1, span_id=1, is_remote=False)
    span.parent = None
    span.kind = SpanKind.INTERNAL
    span.start_time = 100
    span.end_time = 200
    span.status = Status(status_code=StatusCode.OK)
    span.attributes = {}
    span.events = []

    exporter.export([span])

    assert callback.called
    event = callback.call_args[0][0]
    assert event["type"] == "trace_event"
    assert event["trace_id"] == "00000000000000000000000000000001"
