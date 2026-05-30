"""Tests for VoiceConfig and its integration with DaemonConfig."""

import pytest
from pydantic import ValidationError

from gobby.config.voice import OpenAICompatibleAudioBindingConfig, VoiceConfig

pytestmark = pytest.mark.unit


class TestVoiceConfig:
    def test_defaults(self):
        config = VoiceConfig()
        assert config.enabled is False
        assert config.tts_provider == "chatterbox"
        assert config.tts_chatterbox_max_generation_tokens == 1000
        assert config.tts_clause_max_chars == 180
        assert config.tts_reference_text is None
        assert config.stt_enabled is True
        assert config.transcription_timeout_seconds == 120.0
        assert config.whisper_model_size == "base"
        assert config.whisper_device == "auto"
        assert config.whisper_compute_type == "int8"
        assert config.whisper_prompt == "Gobby"
        assert config.openai_compatible_audio == []

    def test_custom_values(self):
        config = VoiceConfig(
            enabled=True,
            whisper_model_size="small",
            tts_chatterbox_max_generation_tokens=144,
            tts_clause_max_chars=220,
        )
        assert config.enabled is True
        assert config.whisper_model_size == "small"
        assert config.tts_chatterbox_max_generation_tokens == 144
        assert config.tts_clause_max_chars == 220

    def test_stt_only(self):
        config = VoiceConfig(enabled=True, stt_enabled=True)
        assert config.enabled is True
        assert config.stt_enabled is True

    def test_stt_disabled(self):
        config = VoiceConfig(enabled=True, stt_enabled=False)
        assert config.enabled is True
        assert config.stt_enabled is False

    def test_daemon_config_integration(self):
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert hasattr(config, "voice")
        assert isinstance(config.voice, VoiceConfig)
        assert config.voice.enabled is False

    def test_daemon_config_with_voice(self):
        from gobby.config.app import DaemonConfig

        config = DaemonConfig(
            voice={
                "enabled": True,
                "whisper_model_size": "medium",
                "tts_chatterbox_max_generation_tokens": 144,
                "tts_clause_max_chars": 220,
            }
        )
        assert config.voice.enabled is True
        assert config.voice.whisper_model_size == "medium"
        assert config.voice.tts_chatterbox_max_generation_tokens == 144
        assert config.voice.tts_clause_max_chars == 220

    def test_openai_compatible_audio_binding(self):
        config = VoiceConfig(
            openai_compatible_audio=[
                OpenAICompatibleAudioBindingConfig(
                    provider="remote-stt",
                    url="http://localhost:8080/v1",
                    model="whisper-large-v3",
                    api_key="$secret:REMOTE_STT_API_KEY",
                    translation_enabled=False,
                    timeout_seconds=30.0,
                )
            ]
        )

        binding = config.openai_compatible_audio[0]
        assert binding.provider == "remote-stt"
        assert binding.url == "http://localhost:8080/v1"
        assert binding.model == "whisper-large-v3"
        assert binding.api_key == "$secret:REMOTE_STT_API_KEY"
        assert binding.transcription_enabled is True
        assert binding.translation_enabled is False
        assert binding.timeout_seconds == 30.0

    @pytest.mark.parametrize("value", [0, 1001])
    def test_generation_token_bounds_validation(self, value: int):
        with pytest.raises(ValidationError):
            VoiceConfig(tts_chatterbox_max_generation_tokens=value)

    @pytest.mark.parametrize("value", [79, 401])
    def test_clause_max_chars_bounds_validation(self, value: int):
        with pytest.raises(ValidationError):
            VoiceConfig(tts_clause_max_chars=value)

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_transcription_timeout_bounds_validation(self, value: float):
        with pytest.raises(ValidationError):
            VoiceConfig(transcription_timeout_seconds=value)
