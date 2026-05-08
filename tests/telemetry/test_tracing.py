"""
Tests for tracing utilities.
"""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from gobby.telemetry.tracing import (
    add_span_attributes,
    create_span,
    current_span,
    record_exception,
    traced,
)
from tests._timing import drain_asyncio_tasks


@pytest.fixture
def tracer_provider(monkeypatch):
    """Fixture to provide a TracerProvider with an InMemorySpanExporter."""
    from unittest.mock import MagicMock, patch

    from gobby.telemetry.config import TelemetrySettings

    # Mock get_app_context to enable tracing
    mock_ctx = MagicMock()
    mock_ctx.config.telemetry = TelemetrySettings(traces_enabled=True)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: mock_ctx)

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    with patch("opentelemetry.trace.get_tracer_provider", return_value=provider):
        # We also need to patch the global tracer if it was already fetched
        # But traced() calls trace.get_tracer() which calls get_tracer_provider()
        yield provider, exporter


def test_traced_sync(tracer_provider):
    """Test @traced decorator on a synchronous function."""
    _, exporter = tracer_provider

    @traced(name="sync_func", attributes={"attr1": "val1"})
    def my_func(a, b):
        return a + b

    result = my_func(1, 2)
    assert result == 3

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "sync_func"
    assert spans[0].attributes["attr1"] == "val1"


@pytest.mark.asyncio
async def test_traced_async(tracer_provider):
    """Test @traced decorator on an asynchronous function."""
    _, exporter = tracer_provider

    @traced(name="async_func")
    async def my_async_func(a, b):
        await drain_asyncio_tasks()
        return a + b

    result = await my_async_func(1, 2)
    assert result == 3

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "async_func"


def test_traced_default_name(tracer_provider):
    """Test @traced decorator uses function name by default."""
    _, exporter = tracer_provider

    @traced()
    def my_func_name():
        pass

    my_func_name()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "my_func_name"


def test_traced_exception(tracer_provider):
    """Test @traced decorator records exceptions."""
    _, exporter = tracer_provider

    @traced()
    def fail_func():
        raise ValueError("test error")

    with pytest.raises(ValueError, match="test error"):
        fail_func()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert "test error" in spans[0].status.description
    assert len(spans[0].events) == 1
    assert spans[0].events[0].name == "exception"


def test_nested_spans(tracer_provider):
    """Test nested spans created with @traced."""
    _, exporter = tracer_provider

    @traced(name="outer")
    def outer():
        inner()

    @traced(name="inner")
    def inner():
        pass

    outer()

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    # Spans are finished in reverse order (inner then outer)
    assert spans[0].name == "inner"
    assert spans[1].name == "outer"
    assert spans[0].parent.span_id == spans[1].context.span_id


def test_create_span_context_manager(tracer_provider):
    """Test create_span helper."""
    _, exporter = tracer_provider

    with create_span("manual_span", attributes={"foo": "bar"}):
        add_span_attributes(baz="qux")
        active_span = current_span()
        assert active_span is not None
        assert active_span.name == "manual_span"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "manual_span"
    assert spans[0].attributes["foo"] == "bar"
    assert spans[0].attributes["baz"] == "qux"


def test_record_exception_helper(tracer_provider):
    """Test record_exception helper."""
    _, exporter = tracer_provider

    with create_span("test_span"):
        try:
            raise RuntimeError("manual error")
        except RuntimeError as e:
            record_exception(e)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert "manual error" in spans[0].status.description


def test_current_span_none(tracer_provider):
    """Test current_span returns None when no span is active."""
    assert current_span() is None


def test_traced_no_op_when_disabled(tracer_provider, monkeypatch):
    """Test @traced is a no-op when traces_enabled is False."""
    _, exporter = tracer_provider
    from unittest.mock import MagicMock

    from gobby.telemetry.config import TelemetrySettings

    # Mock get_app_context to disable tracing
    mock_ctx = MagicMock()
    mock_ctx.config.telemetry = TelemetrySettings(traces_enabled=False)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: mock_ctx)

    @traced(name="disabled_func")
    def my_disabled_func():
        pass

    my_disabled_func()

    spans = exporter.get_finished_spans()
    assert len(spans) == 0
