"""Tests for LLM SDK auto-instrumentation."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

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
    mock_module.OpenAIInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:

        def side_effect(name: str) -> MagicMock:
            if name == "opentelemetry.instrumentation.openai":
                return mock_module
            raise ImportError(f"no module {name}")

        mock_importlib.import_module.side_effect = side_effect
        setup_llm_instrumentors(providers=["openai"])

    mock_module.OpenAIInstrumentor.assert_called_once_with()
    mock_instrumentor.instrument.assert_called_once_with()
    assert os.environ["TRACELOOP_TRACE_CONTENT"] == "false"
    assert "openai" in _instrumented


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


def test_idempotent_instrumentation():
    """Calling setup_llm_instrumentors twice doesn't double-instrument."""
    mock_instrumentor = MagicMock()
    mock_module = MagicMock()
    mock_module.OpenAIInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.return_value = mock_module
        setup_llm_instrumentors(providers=["openai"])
        setup_llm_instrumentors(providers=["openai"])

    assert mock_instrumentor.instrument.call_count == 1


def test_unknown_provider_skipped():
    """Unknown provider names are silently skipped."""
    setup_llm_instrumentors(providers=["nonexistent-provider"])
    assert len(_instrumented) == 0


def test_anthropic_provider_is_skipped() -> None:
    """Anthropic SDK calls are not instrumented when the SDK is not a runtime dependency."""
    with patch("gobby.telemetry.instrumentors.importlib.import_module") as mock_import:
        setup_llm_instrumentors(providers=["anthropic"])

    mock_import.assert_not_called()
    assert "anthropic" not in _instrumented


def test_instrument_exception_handled():
    """If an instrumentor raises during .instrument(), it's caught and logged."""
    mock_instrumentor = MagicMock()
    mock_instrumentor.instrument.side_effect = RuntimeError("broken")
    mock_module = MagicMock()
    mock_module.OpenAIInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.return_value = mock_module
        setup_llm_instrumentors(providers=["openai"])

    assert "openai" not in _instrumented


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
