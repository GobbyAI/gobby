"""Daemon-owned text generation execution adapters."""

from __future__ import annotations

from gobby.ai._text_generation_adapters import (
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
from gobby.ai._text_generation_helpers import ONE_SHOT_DIRECTIVE
from gobby.ai._text_generation_service import TextGenerationService

__all__ = [
    "ClaudeTextGenerateAdapter",
    "CodexCLITextGenerateAdapter",
    "DroidCLITextGenerateAdapter",
    "LocalTextGenerateAdapter",
    "ONE_SHOT_DIRECTIVE",
    "TextGenerateAdapter",
    "TextGenerateAdapterFactory",
    "TextGenerateJSONAdapter",
    "TextGenerationRequest",
    "TextGenerationService",
    "build_daemon_text_generation_service",
]
