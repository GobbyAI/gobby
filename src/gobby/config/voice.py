"""Configuration for the voice chat module."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceConfig(BaseModel):
    """Configuration for voice chat (STT + TTS).

    STT uses local Whisper (faster-whisper) for privacy and low latency.
    TTS uses local Kokoro ONNX for streaming speech synthesis.
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
        description="TTS provider: 'chatterbox' (voice cloning) or 'kokoro' (fixed voices).",
    )
    tts_reference_audio: str = Field(
        default="~/.gobby/voice/reference.wav",
        description="Path to voice clone reference audio (10-20s WAV, for Chatterbox).",
    )
    tts_temperature: float = Field(
        default=0.55,
        ge=0.1,
        le=1.0,
        description="TTS sampling randomness (0.1–1.0, Chatterbox).",
    )
    tts_device: str = Field(
        default="auto",
        description="TTS compute device: 'auto', 'cuda', 'mps', 'cpu'.",
    )
    # --- Kokoro-specific settings (legacy) ---
    tts_voice: str = Field(
        default="af_heart",
        description="Kokoro voice name (e.g. af_heart, af_bella, am_adam, bf_emma).",
    )
    tts_speed: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        description="TTS playback speed multiplier (0.5–2.0, Kokoro).",
    )
    tts_language: str = Field(
        default="en-us",
        description="TTS language code (en-us, en-gb, ja, zh, hi, es, pt-br, it, fr).",
    )
    tts_model_path: str = Field(
        default="~/.gobby/models/kokoro-v1.0.onnx",
        description="Path to the Kokoro ONNX model file.",
    )
    tts_voices_path: str = Field(
        default="~/.gobby/models/voices-v1.0.bin",
        description="Path to the Kokoro voices file.",
    )
    stt_enabled: bool = Field(
        default=True,
        description="Enable speech-to-text (requires enabled=True).",
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
            "SQLite",
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
