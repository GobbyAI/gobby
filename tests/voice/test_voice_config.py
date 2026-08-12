"""Tests for VoiceConfig and its integration with DaemonConfig."""

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.voice import OpenAICompatibleAudioBindingConfig, VoiceConfig
from gobby.config.voice_secrets import VoiceSecretResolutionError, resolve_voice_binding_api_keys

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
        config = DaemonConfig()
        assert hasattr(config, "voice")
        assert isinstance(config.voice, VoiceConfig)
        assert config.voice.enabled is False

    def test_daemon_config_with_voice(self):
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

    @pytest.mark.parametrize(
        "openai_compatible_audio, match",
        [
            (
                [
                    {
                        "provider": "remote-stt",
                        "url": "http://localhost:8080/v1",
                        "model": "whisper-large-v3",
                    },
                    {
                        "provider": "Remote-STT",
                        "url": "http://localhost:8081/v1",
                        "model": "whisper-large-v3",
                    },
                ],
                "unique case-insensitively",
            ),
            (
                [
                    {
                        "provider": "Whisper",
                        "url": "http://localhost:8080/v1",
                        "model": "whisper-large-v3",
                    }
                ],
                "reserved by built-in audio bindings",
            ),
        ],
    )
    def test_daemon_config_rejects_invalid_audio_provider_ids(
        self,
        openai_compatible_audio: list[dict[str, str]],
        match: str,
    ) -> None:
        with pytest.raises(ValidationError, match=match):
            DaemonConfig(
                voice={
                    "openai_compatible_audio": openai_compatible_audio,
                }
            )

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

    def test_dangling_audio_api_key_reference_fails_closed(self) -> None:
        config = DaemonConfig(
            voice=VoiceConfig(
                openai_compatible_audio=[
                    OpenAICompatibleAudioBindingConfig(
                        provider="remote-stt",
                        url="https://audio.example.test/v1",
                        model="whisper-large-v3",
                        api_key="$secret:MISSING_AUDIO_KEY",
                    )
                ]
            )
        )

        with pytest.raises(VoiceSecretResolutionError, match="MISSING_AUDIO_KEY"):
            resolve_voice_binding_api_keys(config, lambda _name: None)

    def test_openai_compatible_audio_api_key_guidance_requires_secret_reference(self):
        description = OpenAICompatibleAudioBindingConfig.model_fields["api_key"].description

        assert description is not None
        assert "$secret:NAME" in description
        assert "plaintext is only valid after runtime resolution" in description

    @pytest.mark.parametrize(
        "bindings, match",
        [
            (
                [
                    OpenAICompatibleAudioBindingConfig(
                        provider="remote-stt",
                        url="http://localhost:8080/v1",
                        model="whisper-large-v3",
                    ),
                    OpenAICompatibleAudioBindingConfig(
                        provider="Remote-STT",
                        url="http://localhost:8081/v1",
                        model="whisper-large-v3",
                    ),
                ],
                "unique case-insensitively",
            ),
            (
                [
                    OpenAICompatibleAudioBindingConfig(
                        provider="Whisper",
                        url="http://localhost:8080/v1",
                        model="whisper-large-v3",
                    )
                ],
                "reserved by built-in audio bindings",
            ),
            (
                [
                    OpenAICompatibleAudioBindingConfig(
                        provider="   ",
                        url="http://localhost:8080/v1",
                        model="whisper-large-v3",
                    )
                ],
                "provider must not be empty",
            ),
        ],
    )
    def test_openai_compatible_audio_provider_ids_are_validated(
        self,
        bindings: list[OpenAICompatibleAudioBindingConfig],
        match: str,
    ) -> None:
        with pytest.raises(ValidationError, match=match):
            VoiceConfig(openai_compatible_audio=bindings)

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

    def test_mps_memory_limit_default_and_bounds(self):
        assert VoiceConfig().tts_mps_memory_limit_gb == 12.0
        assert VoiceConfig(tts_mps_memory_limit_gb=4.0).tts_mps_memory_limit_gb == 4.0
        with pytest.raises(ValidationError):
            VoiceConfig(tts_mps_memory_limit_gb=0.0)
