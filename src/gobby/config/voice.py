"""Configuration for the voice chat module."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceConfig(BaseModel):
    """Configuration for voice chat (STT + TTS).

    STT uses local Whisper (faster-whisper) for privacy and low latency.
    TTS routes through a configurable provider such as Chatterbox, Kokoro,
    or VoxCPM.
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
        description=(
            "TTS provider: 'chatterbox' (voice cloning), 'kokoro' (fixed voices), "
            "or 'voxcpm' (voice cloning with optional reference_text)."
        ),
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
    # --- VoxCPM-specific settings ---
    tts_voxcpm_model: str = Field(
        default="openbmb/VoxCPM2",
        description="VoxCPM model id or local model directory.",
    )
    tts_voxcpm_cfg_value: float = Field(
        default=2.0,
        ge=0.1,
        le=10.0,
        description="VoxCPM guidance scale (recommended 1.0-3.0).",
    )
    tts_voxcpm_inference_timesteps: int = Field(
        default=10,
        ge=1,
        le=100,
        description="VoxCPM inference timesteps.",
    )
    tts_voxcpm_load_denoiser: bool = Field(
        default=False,
        description="Load VoxCPM's optional denoiser pipeline during model initialization.",
    )
    tts_voxcpm_denoise: bool = Field(
        default=False,
        description="Denoise prompt/reference audio before VoxCPM synthesis when available.",
    )
    tts_voxcpm_local_files_only: bool = Field(
        default=False,
        description="Only use local model files for VoxCPM; do not download from HuggingFace.",
    )
    tts_voxcpm_optimize: bool = Field(
        default=True,
        description="Enable VoxCPM runtime optimization/warmup behavior.",
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
