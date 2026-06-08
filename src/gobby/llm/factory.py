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
        config: Daemon configuration.

    Returns:
        LLMService instance backed by feature routing.

    Example:
        service = create_llm_service(config)
        summary = await service.call_feature(config.session_summary, "Summarize this session.")
    """
    return LLMService(config)
