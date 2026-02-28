"""Tests for VoiceConfig and its integration with DaemonConfig."""

from gobby.config.voice import VoiceConfig


class TestVoiceConfig:
    def test_defaults(self):
        config = VoiceConfig()
        assert config.enabled is False
        assert config.stt_enabled is True
        assert config.whisper_model_size == "base"
        assert config.whisper_device == "auto"
        assert config.whisper_compute_type == "int8"
        assert config.whisper_prompt == "Gobby"

    def test_custom_values(self):
        config = VoiceConfig(
            enabled=True,
            whisper_model_size="small",
        )
        assert config.enabled is True
        assert config.whisper_model_size == "small"

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

        config = DaemonConfig(voice={"enabled": True, "whisper_model_size": "medium"})
        assert config.voice.enabled is True
        assert config.voice.whisper_model_size == "medium"
