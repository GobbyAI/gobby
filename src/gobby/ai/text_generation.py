"""Daemon-owned text generation execution adapters."""

from __future__ import annotations

from gobby.ai._text_generation_adapters import (
    ACPTextGenerateAdapter,
    ClaudeTextGenerateAdapter,
    CodexAppServerTextGenerateAdapter,
    DroidCLITextGenerateAdapter,
    LocalTextGenerateAdapter,
)
from gobby.ai._text_generation_builder import build_daemon_text_generation_service
from gobby.ai._text_generation_contracts import (
    ACPClientFactory,
    ACPClientLike,
    ACPStreamEventLike,
    CodexAppServerClientFactory,
    CodexAppServerClientLike,
    CodexAppServerClientProvider,
    TextGenerateAdapter,
    TextGenerateAdapterFactory,
    TextGenerateJSONAdapter,
    TextGenerationRequest,
)
from gobby.ai._text_generation_helpers import ONE_SHOT_DIRECTIVE
from gobby.ai._text_generation_service import TextGenerationService

__all__ = [
    "ACPClientFactory",
    "ACPClientLike",
    "ACPStreamEventLike",
    "ACPTextGenerateAdapter",
    "ClaudeTextGenerateAdapter",
    "CodexAppServerClientFactory",
    "CodexAppServerClientLike",
    "CodexAppServerClientProvider",
    "CodexAppServerTextGenerateAdapter",
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
