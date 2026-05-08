"""Tests for LLM SDK auto-instrumentation."""

from __future__ import annotations

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


def test_setup_activates_installed_instrumentor():
    """setup_llm_instrumentors calls .instrument() on available instrumentors."""
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

    mock_instrumentor.instrument.assert_called_once_with(
        enrich_token_usage=True,
        capture_content=False,
    )
    assert "anthropic" in _instrumented


def test_capture_content_flag_propagation():
    """capture_content=True is passed through to the instrumentor."""
    mock_instrumentor = MagicMock()
    mock_module = MagicMock()
    mock_module.OpenAIInstrumentor.return_value = mock_instrumentor

    with patch("gobby.telemetry.instrumentors.importlib") as mock_importlib:
        mock_importlib.import_module.return_value = mock_module
        setup_llm_instrumentors(capture_content=True, providers=["openai"])

    mock_instrumentor.instrument.assert_called_once_with(
        enrich_token_usage=True,
        capture_content=True,
    )
    assert mock_instrumentor.instrument.call_count == 1
    assert mock_instrumentor.instrument.call_args is not None


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
