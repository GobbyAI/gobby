from collections.abc import Iterator
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from gobby.telemetry.context import (
    extract_from_env,
    get_trace_id,
    inject_into_env,
    set_trace_context,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def setup_otel() -> Iterator[None]:
    provider = TracerProvider()

    # Patch to avoid "Overriding not allowed" if already set
    with patch("opentelemetry.trace.get_tracer_provider", return_value=provider):
        # We also need to patch set_tracer_provider just in case
        with patch("opentelemetry.trace.set_tracer_provider"):
            yield


def test_get_trace_id_no_active_span() -> None:
    assert get_trace_id() is None


def test_get_trace_id_with_active_span() -> None:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test-span") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        assert get_trace_id() == trace_id


@pytest.mark.asyncio
async def test_get_trace_id_async_boundary() -> None:
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("parent"):
        parent_trace_id = get_trace_id()
        assert parent_trace_id is not None

        async def sub_task() -> str | None:
            return get_trace_id()

        child_trace_id = await sub_task()
        assert child_trace_id == parent_trace_id


def test_inject_into_env() -> None:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test"):
        env = inject_into_env({})
        assert "traceparent" in env
        # W3C traceparent format: 00-<trace_id>-<span_id>-<flags>
        assert env["traceparent"].startswith("00-")


def test_extract_from_env() -> None:
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    span_id = "b7ad6b7169203331"
    traceparent = f"00-{trace_id}-{span_id}-01"

    env = {"traceparent": traceparent}
    context = extract_from_env(env)

    assert context is not None
    span_context = trace.get_current_span(context).get_span_context()
    assert format(span_context.trace_id, "032x") == trace_id
    assert format(span_context.span_id, "016x") == span_id


def test_inject_extract_roundtrip() -> None:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("parent") as parent_span:
        env = inject_into_env({})

        context = extract_from_env(env)
        assert context is not None

        span_context = trace.get_current_span(context).get_span_context()
        assert span_context.trace_id == parent_span.get_span_context().trace_id
        assert span_context.span_id == parent_span.get_span_context().span_id


def test_set_trace_context() -> None:
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    span_id = "b7ad6b7169203331"

    context = set_trace_context(trace_id, span_id)
    assert context is not None

    span_context = trace.get_current_span(context).get_span_context()
    assert format(span_context.trace_id, "032x") == trace_id
    assert format(span_context.span_id, "016x") == span_id
