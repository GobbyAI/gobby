"""
Factory for creating LLM service.

Provides factory function for creating LLMService with multi-provider support.
"""

import logging

from gobby.config.app import DaemonConfig
from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)


def create_llm_service(config: DaemonConfig) -> LLMService:
    """
    Create an LLM service for multi-provider support.

    Args:
        config: Client configuration with llm_providers.

    Returns:
        LLMService instance with access to all configured providers.

    Raises:
        ValueError: If config doesn't have llm_providers set.

    Example:
        service = create_llm_service(config)
        provider, model, prompt = service.get_provider_for_feature(
            config.session_summary
        )
    """
    return LLMService(config)
