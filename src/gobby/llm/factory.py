"""
Factory for creating LLM service.

Provides factory function for creating LLMService with multi-provider support.
"""

import logging

from gobby.ai import CodexAppServerClientProvider, build_daemon_text_generation_service
from gobby.config.app import DaemonConfig
from gobby.llm.service import LLMService, TextGenerationDependency

logger = logging.getLogger(__name__)


def create_llm_service(
    config: DaemonConfig,
    *,
    text_generation: TextGenerationDependency | None = None,
    codex_client_provider: CodexAppServerClientProvider | None = None,
) -> LLMService:
    """
    Create an LLM service for multi-provider support.

    Args:
        config: Daemon configuration.

    Returns:
        LLMService instance backed by feature routing.

    Example:
        service = create_llm_service(config)
        summary = await service.call_feature(config.session_summary, "Summarize this session.")
    """
    return LLMService(
        config,
        text_generation
        or build_daemon_text_generation_service(
            config,
            codex_client_provider=codex_client_provider,
        ),
    )
