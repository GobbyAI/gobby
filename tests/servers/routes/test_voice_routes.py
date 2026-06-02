"""Tests for voice API routes with real config objects."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gobby.servers.routes.voice as voice_module
from gobby.ai.audio import AudioCapabilityOutput, AudioSegment
from gobby.config.app import DaemonConfig
from gobby.config.voice import OpenAICompatibleAudioBindingConfig, VoiceConfig
from gobby.servers.routes.voice import create_voice_router

pytestmark = pytest.mark.unit


class TestVoiceRoutes:
    """Tests for voice endpoints using real VoiceConfig objects."""

    @pytest.fixture
    def voice_config(self) -> VoiceConfig:
        """Create a real VoiceConfig with defaults."""
        return VoiceConfig()

    @pytest.fixture
    def server_with_voice(self, voice_config: VoiceConfig) -> MagicMock:
        """Server with real VoiceConfig attached."""
        server = MagicMock()
        config = DaemonConfig(voice=voice_config)
        server.config = config
        server.services.websocket_server = None
        server.websocket_server = None
        return server

    @pytest.fixture
    def client(self, server_with_voice: MagicMock) -> TestClient:
        app = FastAPI()
        router = create_voice_router(server_with_voice)
        app.include_router(router)
        return TestClient(app)

    # -----------------------------------------------------------------
    # GET /api/voice/status
    # -----------------------------------------------------------------

    def test_status_voice_disabled_by_default(self, client: TestClient) -> None:
        """VoiceConfig defaults: enabled=False."""
        response = client.get("/api/voice/status")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["stt_available"] is False
        assert data["stt_reason"] == "Voice not enabled in config"
        assert data["stt_enabled"] is True
        assert data["whisper_model"] == "base"
        assert data["voice_ready"] is False
        assert data["voice_loading"] is False
        assert data["stt_warmup_status"] == "idle"
        assert data["tts_warmup_status"] == "idle"
        assert data["transcription_enabled"] is False
        assert data["translation_enabled"] is False
        assert data["tts_backend_kind"] == "embedded"
        assert data["tts_capabilities"]["supports_reference_audio"] is False

    def test_status_no_config(self, client: TestClient, server_with_voice: MagicMock) -> None:
        """When server.config is None."""
        server_with_voice.config = None
        response = client.get("/api/voice/status")
        data = response.json()
        assert data["enabled"] is False
        assert data["stt_available"] is False
        assert data["reason"] == "Voice config not found"
        assert data["voice_ready"] is False
        assert data["voice_loading"] is False
        assert data["transcription_enabled"] is False
        assert data["translation_enabled"] is False

    def test_status_no_voice_attr(self, client: TestClient, server_with_voice: MagicMock) -> None:
        """When config exists but has no voice attribute."""
        # Remove the voice attribute from config
        config_obj = MagicMock(spec=[])  # spec=[] means no attributes
        server_with_voice.config = config_obj
        response = client.get("/api/voice/status")
        data = response.json()
        assert data["enabled"] is False
        assert data["reason"] == "Voice config not found"

    def test_status_voice_enabled_no_whisper(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Voice enabled but faster-whisper not installed."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        with patch.dict("sys.modules", {"faster_whisper": None}):
            response = client.get("/api/voice/status")
        data = response.json()
        assert data["enabled"] is True
        assert data["stt_available"] is False
        assert "faster-whisper" in data["stt_reason"]

    def test_status_voice_enabled_with_whisper(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Voice enabled and faster-whisper is available."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        mock_whisper = MagicMock()
        with patch.dict("sys.modules", {"faster_whisper": mock_whisper}):
            response = client.get("/api/voice/status")
        data = response.json()
        assert data["enabled"] is True
        assert data["stt_available"] is True
        assert data["stt_reason"] == ""
        assert data["stt_warmup_status"] == "idle"
        assert data["transcription_enabled"] is True
        assert data["translation_enabled"] is True
        assert data["tts_backend_kind"] == "embedded"

    def test_status_prefers_websocket_voice_status(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Live warmup state should come from the WebSocket voice subsystem when present."""
        mock_ws = MagicMock()
        mock_ws.get_voice_status.return_value = {
            "enabled": True,
            "stt_enabled": True,
            "stt_available": True,
            "stt_reason": "",
            "whisper_model": "base",
            "stt_warmup_status": "ready",
            "stt_warmup_error": "",
            "tts_enabled": True,
            "tts_provider": "chatterbox",
            "tts_available": True,
            "tts_reason": "",
            "tts_backend_kind": "embedded",
            "tts_capabilities": {
                "supports_reference_audio": True,
                "supports_reference_text": False,
                "supports_streaming": False,
                "supports_voice_cloning": True,
            },
            "tts_warmup_status": "loading",
            "tts_warmup_error": "",
            "voice_ready": False,
            "voice_loading": True,
        }
        server_with_voice.services.websocket_server = mock_ws

        response = client.get("/api/voice/status")

        assert response.status_code == 200
        data = response.json()
        assert data["stt_warmup_status"] == "ready"
        assert data["tts_warmup_status"] == "loading"
        assert data["transcription_enabled"] is False
        assert data["translation_enabled"] is False
        assert data["voice_loading"] is True

    def test_status_forwards_scoped_voice_targets(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.get_voice_status.return_value = {
            "enabled": True,
            "stt_enabled": True,
            "stt_available": True,
            "stt_reason": "",
            "whisper_model": "base",
            "stt_warmup_status": "ready",
            "stt_warmup_error": "",
            "tts_enabled": True,
            "tts_warmup_status": "loading",
            "tts_warmup_error": "",
            "voice_ready": True,
            "voice_loading": False,
        }
        server_with_voice.services.websocket_server = mock_ws

        response = client.get("/api/voice/status?want_stt=true&want_tts=false")

        assert response.status_code == 200
        mock_ws.get_voice_status.assert_called_once_with(want_stt=True, want_tts=False)
        data = response.json()
        assert data["voice_ready"] is True
        assert data["voice_loading"] is False

    def test_status_forwards_partial_scoped_voice_targets(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.get_voice_status.return_value = {
            "enabled": True,
            "stt_enabled": True,
            "stt_available": True,
            "stt_reason": "",
            "whisper_model": "base",
            "stt_warmup_status": "ready",
            "stt_warmup_error": "",
            "tts_enabled": True,
            "tts_warmup_status": "idle",
            "tts_warmup_error": "",
            "voice_ready": True,
            "voice_loading": False,
        }
        server_with_voice.services.websocket_server = mock_ws

        response = client.get("/api/voice/status?want_stt=true")

        assert response.status_code == 200
        mock_ws.get_voice_status.assert_called_once_with(want_stt=True, want_tts=None)

    def test_status_stt_disabled(self, client: TestClient, server_with_voice: MagicMock) -> None:
        """STT unavailable when stt_enabled=False."""
        server_with_voice.config.voice = VoiceConfig(enabled=True, stt_enabled=False)
        response = client.get("/api/voice/status")
        data = response.json()
        assert data["stt_available"] is False
        assert data["stt_reason"] == "STT disabled in config"
        assert data["transcription_enabled"] is False
        assert data["translation_enabled"] is False

    def test_status_custom_whisper_model(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Custom whisper model size is reflected in status."""
        server_with_voice.config.voice = VoiceConfig(enabled=False, whisper_model_size="small")
        response = client.get("/api/voice/status")
        data = response.json()
        assert data["whisper_model"] == "small"

    def test_status_advertises_remote_audio_capability_flags(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        server_with_voice.config.voice = VoiceConfig(
            enabled=False,
            openai_compatible_audio=[
                OpenAICompatibleAudioBindingConfig(
                    provider="remote-stt",
                    url="http://localhost:8080/v1",
                    model="whisper-large-v3",
                    transcription_enabled=True,
                    translation_enabled=False,
                )
            ],
        )

        response = client.get("/api/voice/status")

        data = response.json()
        assert data["transcription_enabled"] is True
        assert data["translation_enabled"] is False

    def test_status_reuses_audio_registry_until_config_changes(
        self,
        client: TestClient,
        server_with_voice: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server_with_voice.config.voice = VoiceConfig(
            enabled=True,
            stt_enabled=False,
            openai_compatible_audio=[
                OpenAICompatibleAudioBindingConfig(
                    provider="remote-stt",
                    url="http://localhost:8080/v1",
                    model="whisper-large-v3",
                )
            ],
        )
        calls: list[str] = []

        def build_registry(config: DaemonConfig) -> voice_module.AICapabilityRegistry:
            calls.append(config.voice.whisper_model_size)
            return voice_module.AICapabilityRegistry()

        monkeypatch.setattr(voice_module, "_AUDIO_REGISTRY_CACHE", {})
        monkeypatch.setattr(voice_module, "build_daemon_ai_capability_registry", build_registry)

        first = client.get("/api/voice/status")
        second = client.get("/api/voice/status")
        server_with_voice.config.voice.whisper_model_size = "small"
        third = client.get("/api/voice/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 200
        assert calls == ["base", "small"]

    def test_status_reports_missing_chatterbox_reference_audio(
        self, client: TestClient, server_with_voice: MagicMock, tmp_path: Path
    ) -> None:
        server_with_voice.config.voice = VoiceConfig(
            enabled=True,
            tts_enabled=True,
            tts_provider="chatterbox",
            tts_reference_audio=str(tmp_path / "missing-reference.wav"),
        )

        with patch.dict(
            "sys.modules",
            {"faster_whisper": MagicMock(), "chatterbox": MagicMock()},
        ):
            response = client.get("/api/voice/status")

        data = response.json()
        assert data["tts_provider"] == "chatterbox"
        assert data["tts_available"] is False
        assert "reference audio not found" in data["tts_reason"]

    # -----------------------------------------------------------------
    # POST /api/voice/transcribe
    # -----------------------------------------------------------------

    def test_transcribe_voice_disabled(self, client: TestClient) -> None:
        """Transcribe returns error when voice is disabled."""
        response = client.post(
            "/api/voice/transcribe",
            files={"file": ("test.webm", b"fake audio data", "audio/webm")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "Voice not enabled"
        assert data["text"] == ""

    def test_transcribe_no_config(self, client: TestClient, server_with_voice: MagicMock) -> None:
        """Transcribe returns error when config is None."""
        server_with_voice.config = None
        response = client.post(
            "/api/voice/transcribe",
            files={"file": ("test.webm", b"fake audio data", "audio/webm")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "Voice not enabled"
        assert data["text"] == ""

    def test_transcribe_no_voice_attr(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Transcribe returns error when config has no voice attribute."""
        config_obj = MagicMock(spec=[])
        server_with_voice.config = config_obj
        response = client.post(
            "/api/voice/transcribe",
            files={"file": ("test.webm", b"audio", "audio/webm")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "Voice not enabled"

    def test_transcribe_stt_disabled(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Transcribe returns error when stt_enabled=False."""
        server_with_voice.config.voice = VoiceConfig(enabled=True, stt_enabled=False)
        response = client.post(
            "/api/voice/transcribe",
            files={"file": ("test.webm", b"fake audio data", "audio/webm")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "STT disabled in config"
        assert data["text"] == ""

    def test_transcribe_stt_not_available(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Transcribe returns error when STT is not available."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        mock_stt = MagicMock()
        mock_stt.return_value.is_available = False

        with patch("gobby.voice.stt.WhisperSTT", mock_stt):
            response = client.post(
                "/api/voice/transcribe",
                files={"file": ("test.webm", b"audio data", "audio/webm")},
            )
        assert response.status_code == 200
        data = response.json()
        assert "faster-whisper" in data["error"]
        assert data["text"] == ""

    def test_transcribe_success(self, client: TestClient, server_with_voice: MagicMock) -> None:
        """Successful transcription returns text and metadata."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        mock_stt_instance = MagicMock()
        mock_stt_instance.is_available = True
        mock_stt_instance.transcribe_verbose = AsyncMock(
            return_value=AudioCapabilityOutput(
                text="Hello world",
                segments=(AudioSegment(start=0.0, end=1.25, text="Hello world"),),
                language="en",
                task="transcribe",
            )
        )
        mock_stt_cls = MagicMock(return_value=mock_stt_instance)

        with patch("gobby.voice.stt.WhisperSTT", mock_stt_cls):
            response = client.post(
                "/api/voice/transcribe",
                files={"file": ("test.webm", b"audio data here", "audio/webm")},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["text"] == "Hello world"
        assert data["segments"] == [{"text": "Hello world", "start": 0.0, "end": 1.25}]
        assert data["language"] == "en"
        assert data["task"] == "transcribe"
        assert data["bytes"] == len(b"audio data here")
        assert data["content_type"] == "audio/webm"
        assert data["capability"] == "audio_transcribe"
        assert data["provider"] == "whisper"
        assert data["model"] == "base"

    def test_transcribe_selects_openai_compatible_provider(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        server_with_voice.config.voice = VoiceConfig(
            enabled=True,
            openai_compatible_audio=[
                OpenAICompatibleAudioBindingConfig(
                    provider="remote-stt",
                    url="http://localhost:8080/v1",
                    model="whisper-large-v3",
                )
            ],
        )

        with patch(
            "gobby.ai.audio.OpenAICompatibleAudioAdapter.transcribe",
            new_callable=AsyncMock,
            return_value=AudioCapabilityOutput(
                text="Remote transcript",
                segments=(AudioSegment(start=0.0, end=0.75, text="Remote transcript"),),
                language="en",
                task="transcribe",
            ),
        ) as transcribe:
            response = client.post(
                "/api/voice/transcribe",
                data={"provider": "remote-stt"},
                files={"file": ("test.webm", b"audio data here", "audio/webm")},
            )

        assert response.status_code == 200
        transcribe.assert_awaited_once()
        data = response.json()
        assert data["text"] == "Remote transcript"
        assert data["segments"] == [{"text": "Remote transcript", "start": 0.0, "end": 0.75}]
        assert data["language"] == "en"
        assert data["task"] == "transcribe"
        assert data["capability"] == "audio_transcribe"
        assert data["provider"] == "remote-stt"
        assert data["model"] == "whisper-large-v3"

    def test_transcribe_endpoint_can_select_audio_translate(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        server_with_voice.config.voice = VoiceConfig(
            enabled=True,
            openai_compatible_audio=[
                OpenAICompatibleAudioBindingConfig(
                    provider="remote-stt",
                    url="http://localhost:8080/v1",
                    model="whisper-large-v3",
                )
            ],
        )

        with patch(
            "gobby.ai.audio.OpenAICompatibleAudioAdapter.translate",
            new_callable=AsyncMock,
            return_value=AudioCapabilityOutput(text="Remote translation", task="translate"),
        ) as translate:
            response = client.post(
                "/api/voice/transcribe",
                data={"capability": "audio_translate", "provider": "remote-stt"},
                files={"file": ("test.webm", b"audio data here", "audio/webm")},
            )

        assert response.status_code == 200
        translate.assert_awaited_once()
        data = response.json()
        assert data["text"] == "Remote translation"
        assert data["segments"] == []
        assert data["language"] is None
        assert data["task"] == "translate"
        assert data["capability"] == "audio_translate"
        assert data["provider"] == "remote-stt"
        assert data["model"] == "whisper-large-v3"

    def test_transcribe_provider_lacks_capability_returns_structured_error(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        server_with_voice.config.voice = VoiceConfig(
            enabled=True,
            openai_compatible_audio=[
                OpenAICompatibleAudioBindingConfig(
                    provider="remote-stt",
                    url="http://localhost:8080/v1",
                    model="whisper-large-v3",
                    translation_enabled=False,
                )
            ],
        )

        response = client.post(
            "/api/voice/transcribe",
            data={"capability": "audio_translate", "provider": "remote-stt"},
            files={"file": ("test.webm", b"audio data here", "audio/webm")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == "capability_unavailable"
        assert data["capability"] == "audio_translate"
        assert data["provider"] == "remote-stt"
        assert data["model"] is None
        assert "audio_translate is disabled" in data["reason"]
        assert data["text"] == ""

    def test_transcribe_default_content_type(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """When no content_type is provided, defaults to audio/webm."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        mock_stt_instance = MagicMock()
        mock_stt_instance.is_available = True
        mock_stt_instance.transcribe = AsyncMock(return_value="Transcribed text")
        mock_stt_cls = MagicMock(return_value=mock_stt_instance)

        with patch("gobby.voice.stt.WhisperSTT", mock_stt_cls):
            # Send without explicit content_type
            response = client.post(
                "/api/voice/transcribe",
                files={"file": ("test.wav", b"wav data")},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["text"] == "Transcribed text"

    def test_transcribe_error_during_transcription(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Transcription error is caught and returned."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        mock_stt_instance = MagicMock()
        mock_stt_instance.is_available = True
        mock_stt_instance.transcribe = AsyncMock(side_effect=RuntimeError("Model crashed"))
        mock_stt_cls = MagicMock(return_value=mock_stt_instance)

        with patch("gobby.voice.stt.WhisperSTT", mock_stt_cls):
            response = client.post(
                "/api/voice/transcribe",
                files={"file": ("test.webm", b"audio", "audio/webm")},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "Transcription failed"
        assert data["text"] == ""

    def test_transcribe_validation_error_returns_message(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Expected transcription rejections keep their user-facing message."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        mock_stt_instance = MagicMock()
        mock_stt_instance.is_available = True
        mock_stt_instance.transcribe = AsyncMock(side_effect=ValueError("Unsupported audio"))
        mock_stt_cls = MagicMock(return_value=mock_stt_instance)

        with patch("gobby.voice.stt.WhisperSTT", mock_stt_cls):
            response = client.post(
                "/api/voice/transcribe",
                files={"file": ("test.webm", b"audio", "audio/webm")},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "Unsupported audio"
        assert data["text"] == ""

    def test_transcribe_timeout_returns_timeout_message(
        self, client: TestClient, server_with_voice: MagicMock
    ) -> None:
        """Transcription timeouts return a stable message."""
        server_with_voice.config.voice = VoiceConfig(enabled=True)
        mock_stt_instance = MagicMock()
        mock_stt_instance.is_available = True
        mock_stt_instance.transcribe = AsyncMock(side_effect=TimeoutError)
        mock_stt_cls = MagicMock(return_value=mock_stt_instance)

        with patch("gobby.voice.stt.WhisperSTT", mock_stt_cls):
            response = client.post(
                "/api/voice/transcribe",
                files={"file": ("test.webm", b"audio", "audio/webm")},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "Transcription timed out"
        assert data["text"] == ""
