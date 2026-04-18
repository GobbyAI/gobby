"""
LLM SDK auto-instrumentation via OpenLLMetry.

Activates OpenTelemetry instrumentors for Anthropic and OpenAI SDKs.
Each LLM call automatically emits a child span with gen_ai.* semantic convention attributes.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

# Map provider names to their instrumentor module and class
_INSTRUMENTOR_MAP: dict[str, tuple[str, str]] = {
    "anthropic": (
        "opentelemetry.instrumentation.anthropic",
        "AnthropicInstrumentor",
    ),
    "openai": (
        "opentelemetry.instrumentation.openai",
        "OpenAIInstrumentor",
    ),
}

_instrumented: set[str] = set()


def setup_llm_instrumentors(
    capture_content: bool = False,
    providers: list[str] | None = None,
) -> None:
    """
    Activate OpenLLMetry instrumentors for LLM SDKs.

    Must be called before any LLM client instantiation for best results.
    Graceful no-op if the instrumentor packages are not installed.

    Args:
        capture_content: Whether to capture prompt/completion content in spans.
        providers: List of provider names to instrument. Defaults to all known.
    """
    target_providers = providers or list(_INSTRUMENTOR_MAP.keys())

    for provider in target_providers:
        if provider in _instrumented:
            continue

        entry = _INSTRUMENTOR_MAP.get(provider)
        if not entry:
            logger.debug(f"Unknown LLM provider for instrumentation: {provider}")
            continue

        module_path, class_name = entry
        try:
            mod = importlib.import_module(module_path)
            instrumentor_cls = getattr(mod, class_name)
            instrumentor_cls().instrument(enrich_token_usage=True, capture_content=capture_content)
            _instrumented.add(provider)
            logger.info(f"Activated LLM instrumentor for {provider}")
        except ImportError:
            logger.debug(
                f"LLM instrumentor for {provider} not available (install 'llm-tracing' extra)",
            )
        except Exception:
            logger.warning(f"Failed to activate LLM instrumentor for {provider}", exc_info=True)
