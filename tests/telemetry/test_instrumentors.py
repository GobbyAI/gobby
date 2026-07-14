"""Tests for LLM SDK auto-instrumentation."""

from __future__ import annotations

import os
import warnings
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gobby.telemetry.instrumentors import _instrumented, setup_llm_instrumentors

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_instrumented():
    """Reset the instrumented set between tests."""
    _instrumented.clear()
    yield
    _instrumented.clear()


def test_setup_graceful_noop_when_extras_missing():
    """setup_llm_instrumentors is a no-op when instrumentor packages aren't installed."""
    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.side_effect = ImportError("no module")
        setup_llm_instrumentors()

    assert len(_instrumented) == 0


def test_setup_activates_installed_instrumentor(monkeypatch):
    """setup_llm_instrumentors calls .instrument() on available instrumentors."""
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "true")
    mock_instrumentor = MagicMock()
    mock_module = MagicMock()
    mock_module.AnthropicInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:

        def side_effect(name: str) -> MagicMock:
            if name == "opentelemetry.instrumentation.anthropic":
                return mock_module
            raise ImportError(f"no module {name}")

        mock_importlib.import_module.side_effect = side_effect
        setup_llm_instrumentors(providers=["anthropic"])

    mock_module.AnthropicInstrumentor.assert_called_once_with(enrich_token_usage=True)
    mock_instrumentor.instrument.assert_called_once_with()
    assert os.environ["TRACELOOP_TRACE_CONTENT"] == "false"
    assert "anthropic" in _instrumented


def test_capture_content_sets_environment_for_other_instrumentors(monkeypatch):
    """Content capture uses the shared OpenLLMetry environment setting."""
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "false")
    mock_instrumentor = MagicMock()
    mock_module = MagicMock()
    mock_module.OpenAIInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.return_value = mock_module
        setup_llm_instrumentors(capture_content=True, providers=["openai"])

    mock_module.OpenAIInstrumentor.assert_called_once_with()
    mock_instrumentor.instrument.assert_called_once_with()
    assert os.environ["TRACELOOP_TRACE_CONTENT"] == "true"


def test_capture_content_false_omits_anthropic_content_from_spans(monkeypatch):
    """The real Anthropic instrumentor omits prompts and completions by default."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("opentelemetry.trace._TRACER_PROVIDER", provider)
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "true")

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "secret completion"}],
                "model": "claude-3-5-sonnet-20241022",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
            request=request,
        )

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="'asyncio.iscoroutinefunction' is deprecated.*",
                category=DeprecationWarning,
            )
            from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

            setup_llm_instrumentors(providers=["anthropic"])
        client = anthropic.Anthropic(
            api_key="test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handle_request)),
        )
        client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=10,
            messages=[{"role": "user", "content": "secret prompt"}],
        )
        provider.force_flush()

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = dict(spans[0].attributes or {})
        assert attributes["llm.usage.total_tokens"] == 5
        assert all("secret prompt" not in str(value) for value in attributes.values())
        assert all("secret completion" not in str(value) for value in attributes.values())
    finally:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="'asyncio.iscoroutinefunction' is deprecated.*",
                category=DeprecationWarning,
            )
            AnthropicInstrumentor().uninstrument()


def test_idempotent_instrumentation():
    """Calling setup_llm_instrumentors twice doesn't double-instrument."""
    mock_instrumentor = MagicMock()
    mock_module = MagicMock()
    mock_module.AnthropicInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.return_value = mock_module
        setup_llm_instrumentors(providers=["anthropic"])
        setup_llm_instrumentors(providers=["anthropic"])

    assert mock_instrumentor.instrument.call_count == 1


def test_unknown_provider_skipped():
    """Unknown provider names are silently skipped."""
    setup_llm_instrumentors(providers=["nonexistent-provider"])
    assert len(_instrumented) == 0


def test_instrument_exception_handled():
    """If an instrumentor raises during .instrument(), it's caught and logged."""
    mock_instrumentor = MagicMock()
    mock_instrumentor.instrument.side_effect = RuntimeError("broken")
    mock_module = MagicMock()
    mock_module.AnthropicInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.return_value = mock_module
        setup_llm_instrumentors(providers=["anthropic"])

    assert "anthropic" not in _instrumented


def test_selective_providers():
    """Only specified providers are instrumented."""
    call_log: list[str] = []

    def mock_import(name: str) -> MagicMock:
        call_log.append(name)
        raise ImportError(f"no module {name}")

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.side_effect = mock_import
        setup_llm_instrumentors(providers=["openai"])

    assert len(call_log) == 1
    assert "opentelemetry.instrumentation.openai" in call_log
