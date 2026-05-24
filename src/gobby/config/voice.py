"""Configuration for the voice chat module."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceConfig(BaseModel):
    """Configuration for voice chat (STT + TTS).

    STT uses local Whisper (faster-whisper) for privacy and low latency.
    TTS routes through a pluggable provider backend.
    """

    enabled: bool = Field(
        default=False,
        description="Enable voice chat features (master switch).",
    )

    # --- TTS settings ---
    tts_enabled: bool = Field(
        default=True,
        description="Enable text-to-speech output in voice mode (requires enabled=True).",
    )
    tts_provider: str = Field(
        default="chatterbox",
        description="TTS provider id. Currently supported: 'chatterbox' (voice cloning).",
    )
    tts_reference_audio: str = Field(
        default="~/.gobby/voice/reference.wav",
        description="Path to voice clone reference audio (10-20s WAV for supported providers).",
    )
    tts_reference_text: str | None = Field(
        default=None,
        description=(
            "Optional transcript of the reference audio. Providers that support "
            "higher-fidelity cloning can use it; others ignore it."
        ),
    )
    tts_temperature: float = Field(
        default=0.55,
        ge=0.1,
        le=1.0,
        description="TTS sampling randomness (0.1–1.0, Chatterbox).",
    )
    tts_chatterbox_max_generation_tokens: int = Field(
        default=1000,
        ge=1,
        le=1000,
        description=(
            "Maximum Turbo speech tokens to generate per utterance. Gobby "
            "defaults to the upstream 1000-token cap and relies on clause-level "
            "chunking to bound individual synthesis calls."
        ),
    )
    tts_clause_max_chars: int = Field(
        default=180,
        ge=80,
        le=400,
        description=(
            "Maximum text characters per TTS synthesis chunk before splitting on "
            "clause boundaries or whitespace."
        ),
    )
    tts_device: str = Field(
        default="auto",
        description=(
            "Requested TTS compute device: 'auto', 'cuda', 'mps', 'cpu'. "
            "Providers may ignore explicit selection if the upstream runtime does not support it."
        ),
    )
    stt_enabled: bool = Field(
        default=True,
        description="Enable speech-to-text (requires enabled=True).",
    )
    transcription_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Maximum seconds to wait for a single speech-to-text transcription.",
    )
    whisper_model_size: str = Field(
        default="base",
        description="Whisper model size: tiny, base, small, medium.",
    )
    whisper_device: str = Field(
        default="auto",
        description="Device for Whisper inference: auto, cpu, cuda.",
    )
    whisper_compute_type: str = Field(
        default="int8",
        description="Compute type for Whisper: int8, float16, float32.",
    )
    whisper_prompt: str = Field(
        default="Gobby",
        description="Initial prompt for Whisper STT to bias vocabulary (e.g. proper nouns).",
    )
    whisper_vocabulary: list[str] = Field(
        default_factory=lambda: [
            # Gobby-specific
            "Gobby",
            "MCP",
            "worktree",
            # Common dev terms Whisper struggles with
            "Kubernetes",
            "PostgreSQL",
            "FastAPI",
            "Pydantic",
            "TypeScript",
            "GraphQL",
            "WebSocket",
            "Redis",
            "MongoDB",
            "OAuth",
            "JWT",
            "REST",
            "gRPC",
            "YAML",
            "JSON",
            "Docker",
            "Terraform",
            "GitHub",
            "GitLab",
            "Claude",
            "Anthropic",
            "Gemini",
            "Codex",
            "npm",
            "pip",
            "pytest",
            "ESLint",
            "webpack",
            "Vite",
        ],
        description="Custom vocabulary terms to bias Whisper STT recognition (proper nouns, technical terms). Pre-loaded with common dev terms.",
    )
