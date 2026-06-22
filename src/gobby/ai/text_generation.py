"""Daemon-owned text generation execution adapters."""

from __future__ import annotations

from gobby.ai._text_generation_adapters import (
    AgyCLITextGenerateAdapter,
    ClaudeTextGenerateAdapter,
    CodexCLITextGenerateAdapter,
    DroidCLITextGenerateAdapter,
    LocalTextGenerateAdapter,
)
from gobby.ai._text_generation_builder import build_daemon_text_generation_service
from gobby.ai._text_generation_contracts import (
    TextGenerateAdapter,
    TextGenerateAdapterFactory,
    TextGenerateJSONAdapter,
    TextGenerationRequest,
)
from gobby.ai._text_generation_helpers import (
    ONE_SHOT_DIRECTIVE,
    FeatureGenerationUnavailableError,
    is_feature_generation_infrastructure_error,
)
from gobby.ai._text_generation_service import TextGenerationService

__all__ = [
    "AgyCLITextGenerateAdapter",
    "ClaudeTextGenerateAdapter",
    "CodexCLITextGenerateAdapter",
    "DroidCLITextGenerateAdapter",
    "FeatureGenerationUnavailableError",
    "LocalTextGenerateAdapter",
    "ONE_SHOT_DIRECTIVE",
    "TextGenerateAdapter",
    "TextGenerateAdapterFactory",
    "TextGenerateJSONAdapter",
    "TextGenerationRequest",
    "TextGenerationService",
    "build_daemon_text_generation_service",
    "is_feature_generation_infrastructure_error",
]
