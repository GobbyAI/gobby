"""Tests for WebSocket voice warmup state."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.voice import VoiceConfig
from gobby.servers.websocket.voice import VoiceMixin
from gobby.voice.tts import TTSProviderStatus
from tests._timing import wait_forever

pytestmark = pytest.mark.unit


class DummyVoiceMixin(VoiceMixin):
    def __init__(self, voice_config: VoiceConfig) -> None:
        self.clients: dict = {}
        self.daemon_config = SimpleNamespace(voice=voice_config)
        self._init_voice()
        self._handle_chat_message = AsyncMock()

    async def _ensure_stt_deps(self, voice_config: VoiceConfig) -> bool:
        return True

    async def _ensure_tts_deps(self, voice_config: VoiceConfig) -> bool:
        return True


class TestVoiceWarmup:
    @pytest.mark.asyncio
    async def test_start_voice_warmup_is_single_flight(self) -> None:
        """Warmup is single-flight and logs one trigger while the task is in flight."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))

        mock_stt = MagicMock()
        mock_stt.is_available = True
        mock_stt.warmup = AsyncMock()

        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()

        mixin._get_stt = MagicMock(return_value=mock_stt)
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_stt_availability = MagicMock(return_value=(True, ""))
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))

        assert mixin.start_voice_warmup() is True
        first_task = mixin._voice_warmup_task
        assert mixin.start_voice_warmup() is False

        assert first_task is not None
        assert mixin._voice_warmup_task is first_task

        await first_task

        mock_stt.warmup.assert_awaited_once()
        mock_tts.warmup.assert_awaited_once()
        assert mixin._stt_warmup_status == "ready"
        assert mixin._tts_warmup_status == "ready"

        status = mixin.get_voice_status()
        assert status["voice_ready"] is True
        assert status["voice_loading"] is False
        assert mixin._voice_warmup_task is None
        assert mixin.start_voice_warmup() is False

    @pytest.mark.asyncio
    async def test_tts_warmup_waits_for_deps_before_provider_lookup(self) -> None:
        """TTS warmup waits for dependency setup before resolving and warming the provider."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))
        order: list[str] = []

        async def ensure_deps(_voice_config: VoiceConfig) -> bool:
            order.append("deps")
            return True

        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()

        def get_tts() -> MagicMock:
            order.append("provider")
            return mock_tts

        mixin._ensure_tts_deps = AsyncMock(side_effect=ensure_deps)
        mixin._get_tts = MagicMock(side_effect=get_tts)
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))

        assert mixin.start_voice_warmup(want_stt=False, want_tts=True) is True
        assert mixin._voice_warmup_task is not None
        await mixin._voice_warmup_task

        assert order == ["deps", "provider"]
        assert mixin._tts_deps_checked is True
        mock_tts.warmup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stt_warmup_waits_for_deps_before_model_lookup(self) -> None:
        """STT warmup waits for dependency setup before resolving and warming the model."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=False, stt_enabled=True))
        order: list[str] = []

        async def ensure_deps(_voice_config: VoiceConfig) -> bool:
            order.append("deps")
            return True

        mock_stt = MagicMock()
        mock_stt.is_available = True
        mock_stt.warmup = AsyncMock()

        def get_stt() -> MagicMock:
            order.append("stt")
            return mock_stt

        mixin._ensure_stt_deps = AsyncMock(side_effect=ensure_deps)
        mixin._get_stt = MagicMock(side_effect=get_stt)
        mixin._get_stt_availability = MagicMock(return_value=(True, ""))

        assert mixin.start_voice_warmup(want_stt=True, want_tts=False) is True
        assert mixin._voice_warmup_task is not None
        await mixin._voice_warmup_task

        assert order == ["deps", "stt"]
        assert mixin._stt_deps_checked is True
        mock_stt.warmup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tts_warmup_records_dependency_failure_without_provider_lookup(self) -> None:
        """TTS dependency failure should skip provider lookup and record the error."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))
        mixin._ensure_tts_deps = AsyncMock(return_value=False)
        mixin._get_tts = MagicMock()
        mixin._get_tts_availability = MagicMock(return_value=(False, "chatterbox missing"))

        assert mixin.start_voice_warmup(want_stt=False, want_tts=True) is True
        assert mixin._voice_warmup_task is not None
        await mixin._voice_warmup_task

        mixin._get_tts.assert_not_called()
        assert mixin._tts_warmup_status == "error"
        assert mixin._tts_warmup_error == "chatterbox missing"

    @pytest.mark.asyncio
    async def test_voice_prepare_logs_warmup_trigger_only_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Repeated prepare frames share the in-flight warmup and emit one trigger log."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))

        async def warm_forever() -> None:
            await wait_forever()

        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock(side_effect=warm_forever)
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))
        websocket = SimpleNamespace(send=AsyncMock())

        with caplog.at_level(logging.INFO, logger="gobby.servers.websocket.voice"):
            await mixin._handle_voice_prepare(
                websocket,
                {"conversation_id": "conv-1", "tts_enabled": True},
            )
            await mixin._handle_voice_prepare(
                websocket,
                {"conversation_id": "conv-1", "tts_enabled": True},
            )

        warmup_logs = [
            record
            for record in caplog.records
            if record.levelno == logging.INFO
            and record.message == "Voice model warmup triggered by client"
        ]
        assert len(warmup_logs) == 1
        assert mixin._voice_warmup_task is not None

        await mixin.stop_voice_warmup()

    @pytest.mark.asyncio
    async def test_voice_mode_enable_starts_tts_warmup_only(self) -> None:
        """Voice-mode enable should warm TTS without warming STT."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))

        mock_stt = MagicMock()
        mock_stt.is_available = True
        mock_stt.warmup = AsyncMock()
        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()
        mixin._get_stt = MagicMock(return_value=mock_stt)
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_stt_availability = MagicMock(return_value=(True, ""))
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_mode_toggle(
            websocket,
            {"conversation_id": "conv-1", "enabled": True},
        )

        assert mixin._voice_enabled["conv-1"] is True
        assert mixin._voice_warmup_task is not None
        payload = json.loads(websocket.send.await_args.args[0])
        assert payload["status"] == "voice_mode_on"
        assert payload["tts_warmup_status"] == "loading"
        assert payload["voice_loading"] is True

        await mixin._voice_warmup_task
        mock_stt.warmup.assert_not_awaited()
        mock_tts.warmup.assert_awaited_once()
        assert mixin._stt_warmup_status == "idle"
        assert mixin._tts_warmup_status == "ready"

    @pytest.mark.asyncio
    async def test_voice_prepare_can_arm_tts_before_toggle_frame(self) -> None:
        """Voice prepare should arm TTS before the toggle frame arrives."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))

        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_prepare(
            websocket,
            {"conversation_id": "conv-1", "tts_enabled": True},
        )

        assert mixin._voice_enabled["conv-1"] is True
        payload = json.loads(websocket.send.await_args.args[0])
        assert payload["status"] == "preparing"
        assert payload["voice_loading"] is True

        assert mixin._voice_warmup_task is not None
        await mixin._voice_warmup_task

    @pytest.mark.asyncio
    async def test_voice_prepare_stt_only_does_not_warm_tts(self) -> None:
        """STT-only prepare should leave TTS idle."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))

        mock_stt = MagicMock()
        mock_stt.is_available = True
        mock_stt.warmup = AsyncMock()
        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()
        mixin._get_stt = MagicMock(return_value=mock_stt)
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_stt_availability = MagicMock(return_value=(True, ""))
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_prepare(
            websocket,
            {"conversation_id": "conv-1", "stt_enabled": True, "tts_enabled": False},
        )

        assert "conv-1" not in mixin._voice_enabled
        payload = json.loads(websocket.send.await_args.args[0])
        assert payload["status"] == "preparing"
        assert payload["voice_loading"] is True
        assert payload["tts_warmup_status"] == "idle"

        assert mixin._voice_warmup_task is not None
        await mixin._voice_warmup_task
        mock_stt.warmup.assert_awaited_once()
        mock_tts.warmup.assert_not_awaited()
        assert mixin._stt_warmup_status == "ready"
        assert mixin._tts_warmup_status == "idle"

    @pytest.mark.asyncio
    async def test_attached_voice_transcription_is_sent_to_terminal_session(self) -> None:
        """STT from an attached web client should relay into the attached tmux pane."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, stt_enabled=True, tts_enabled=False))
        websocket = MagicMock()
        websocket.send = AsyncMock()
        mixin.clients = {websocket: {"attached_session_id": "term-voice"}}
        mixin._send_error = AsyncMock()

        stt = MagicMock()
        stt.transcribe = AsyncMock(return_value="run the focused tests")
        mixin._get_stt = MagicMock(return_value=stt)

        target_session = MagicMock()
        target_session.session_type = "terminal"
        target_session.terminal_context = {"tmux_pane": "%21"}
        target_session.metadata = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=target_session)
        session_manager.db = MagicMock()
        mixin.session_manager = session_manager

        inter_message = MagicMock()
        inter_message.id = "msg-voice"
        inter_msg_manager = MagicMock()
        inter_msg_manager.create_message.return_value = inter_message

        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with (
            patch(
                "gobby.storage.inter_session_messages.InterSessionMessageManager",
                return_value=inter_msg_manager,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.get_tmux_manager_for_context",
                return_value=tmux_manager,
            ),
        ):
            await mixin._handle_voice_audio(
                websocket,
                {
                    "conversation_id": "term-voice",
                    "audio_data": base64.b64encode(b"audio").decode("ascii"),
                    "mime_type": "audio/webm",
                    "request_id": "voice-req-1",
                },
            )

        tmux_manager.send_keys.assert_awaited_once_with("%21", "run the focused tests\n")
        inter_msg_manager.create_message.assert_called_once()
        mixin._handle_chat_message.assert_not_awaited()
        sent_payloads = [json.loads(call.args[0]) for call in websocket.send.await_args_list]
        assert len(sent_payloads) >= 1
        assert any(
            payload["type"] == "voice_transcription"
            and payload["conversation_id"] == "term-voice"
            and payload["request_id"] == "voice-req-1"
            and payload["text"] == "run the focused tests"
            for payload in sent_payloads
        )

    @pytest.mark.asyncio
    async def test_voice_prepare_tts_only_does_not_warm_stt(self) -> None:
        """TTS-only prepare should leave STT idle."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))

        mock_stt = MagicMock()
        mock_stt.is_available = True
        mock_stt.warmup = AsyncMock()
        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()
        mixin._get_stt = MagicMock(return_value=mock_stt)
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_stt_availability = MagicMock(return_value=(True, ""))
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_prepare(
            websocket,
            {"conversation_id": "conv-1", "stt_enabled": False, "tts_enabled": True},
        )

        assert mixin._voice_enabled["conv-1"] is True
        assert mixin._voice_warmup_task is not None
        await mixin._voice_warmup_task

        mock_stt.warmup.assert_not_awaited()
        mock_tts.warmup.assert_awaited_once()
        assert mixin._stt_warmup_status == "idle"
        assert mixin._tts_warmup_status == "ready"

    @pytest.mark.asyncio
    async def test_voice_prepare_without_targets_preserves_config_warmup(self) -> None:
        """Prepare without explicit targets should use configured warmup targets."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))

        mock_stt = MagicMock()
        mock_stt.is_available = True
        mock_stt.warmup = AsyncMock()
        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()
        mixin._get_stt = MagicMock(return_value=mock_stt)
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_stt_availability = MagicMock(return_value=(True, ""))
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_prepare(websocket, {"conversation_id": "conv-1"})

        assert mixin._voice_warmup_task is not None
        await mixin._voice_warmup_task

        mock_stt.warmup.assert_awaited_once()
        mock_tts.warmup.assert_awaited_once()
        assert mixin._stt_warmup_status == "ready"
        assert mixin._tts_warmup_status == "ready"

    def test_scoped_status_ignores_unrequested_tts_state(self) -> None:
        """Scoped STT status should ignore unrequested TTS loading state."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))
        mixin._stt_warmup_status = "ready"
        mixin._tts_warmup_status = "loading"
        mixin._tts_warmup_error = "reference audio invalid"
        mixin._get_stt_availability = MagicMock(return_value=(True, ""))

        scoped = mixin.get_voice_status(want_stt=True, want_tts=False)
        assert scoped["voice_ready"] is True
        assert scoped["voice_loading"] is False
        assert scoped["tts_warmup_error"] == ""

        global_status = mixin.get_voice_status()
        assert global_status["voice_ready"] is False
        assert global_status["voice_loading"] is True
        assert global_status["tts_warmup_error"] == "reference audio invalid"

    def test_status_reports_loaded_tts_provider_details(self) -> None:
        """Status should include details from an already loaded TTS provider."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))
        provider = MagicMock()
        provider.get_status.return_value = TTSProviderStatus(
            provider="chatterbox",
            available=True,
            details={"tts_runtime_primed": True},
        )
        mixin._tts_provider = provider
        mixin._tts_warmup_status = "ready"

        status = mixin.get_voice_status(want_stt=False, want_tts=True)

        provider.get_status.assert_called_once()
        assert status["tts_runtime_primed"] is True

    @pytest.mark.asyncio
    async def test_start_voice_warmup_records_failures(self) -> None:
        """Missing warmup providers should record unavailable status details."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))
        mixin._get_tts = MagicMock(return_value=None)
        mixin._get_tts_availability = MagicMock(return_value=(False, "chatterbox missing"))

        mixin.start_voice_warmup()
        assert mixin._voice_warmup_task is not None

        await mixin._voice_warmup_task

        assert mixin._tts_warmup_status == "error"
        assert mixin._tts_warmup_error == "chatterbox missing"

        status = mixin.get_voice_status()
        assert status["voice_ready"] is False
        assert status["tts_warmup_status"] == "error"
        assert status["tts_warmup_error"] == "chatterbox missing"

    @pytest.mark.asyncio
    async def test_start_voice_warmup_records_provider_warmup_failure(self) -> None:
        """Provider warmup exceptions should be exposed in voice status."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))

        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock(side_effect=RuntimeError("reference audio invalid"))
        mixin._get_tts = MagicMock(return_value=mock_tts)
        mixin._get_tts_availability = MagicMock(return_value=(True, ""))

        mixin.start_voice_warmup()
        assert mixin._voice_warmup_task is not None

        await mixin._voice_warmup_task

        assert mixin._tts_warmup_status == "error"
        assert mixin._tts_warmup_error == "reference audio invalid"

    @pytest.mark.asyncio
    async def test_cleanup_voice_unloads_models_and_cancels_tasks(self) -> None:
        """Voice cleanup should unload providers and cancel background tasks."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))

        mixin._stt_warmup_status = "ready"
        mixin._tts_warmup_status = "ready"
        mock_stt = MagicMock()
        mock_tts = MagicMock()
        mixin._whisper_stt = mock_stt
        mixin._tts_provider = mock_tts
        mixin._voice_warmup_task = asyncio.create_task(wait_forever())
        background_task = asyncio.create_task(wait_forever())
        mixin._background_tasks.add(background_task)
        mixin._voice_enabled["conv-1"] = True

        await mixin.cleanup_voice()

        assert mixin._voice_warmup_task is None
        assert background_task.cancelled()
        mock_stt.unload.assert_called_once()
        mock_tts.unload.assert_called_once()
        assert mixin._whisper_stt is None
        assert mixin._tts_provider is None
        assert mixin._background_tasks == set()
        assert mixin._voice_enabled == {}
        assert mixin._stt_warmup_status == "idle"
        assert mixin._tts_warmup_status == "idle"

        # Idempotent shutdown should remain safe after state is cleared.
        await mixin.cleanup_voice()

    @pytest.mark.asyncio
    async def test_check_voice_idle_cancels_inflight_warmup(self) -> None:
        """Idle checks should cancel an in-flight warmup task."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))
        mixin._voice_warmup_task = asyncio.create_task(wait_forever())

        await mixin._check_voice_idle()

        assert mixin._voice_warmup_task is None

    @pytest.mark.asyncio
    async def test_voice_audio_forwards_project_id_to_chat_message(self) -> None:
        """Voice audio should forward project_id into the chat message payload."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, stt_enabled=True))
        stt = MagicMock()
        stt.transcribe = AsyncMock(return_value="hello from voice")
        mixin._get_stt = MagicMock(return_value=stt)
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_audio(
            websocket,
            {
                "conversation_id": "conv-voice",
                "audio_data": "YXVkaW8=",
                "mime_type": "audio/wav",
                "request_id": "req-1",
                "project_id": "project-123",
            },
        )

        mixin._handle_chat_message.assert_awaited_once()
        payloads = [json.loads(call.args[0]) for call in websocket.send.await_args_list]
        assert payloads[-1]["text"] == "hello from voice"
        chat_data = mixin._handle_chat_message.await_args.args[1]
        assert chat_data == {
            "type": "chat_message",
            "content": "hello from voice",
            "conversation_id": "conv-voice",
            "request_id": "req-1",
            "project_id": "project-123",
        }

    @pytest.mark.asyncio
    async def test_voice_audio_without_project_id_preserves_chat_fallback(self) -> None:
        """Voice audio without project_id should keep the chat fallback payload unchanged."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, stt_enabled=True))
        stt = MagicMock()
        stt.transcribe = AsyncMock(return_value="hello from voice")
        mixin._get_stt = MagicMock(return_value=stt)
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_audio(
            websocket,
            {
                "conversation_id": "conv-voice",
                "audio_data": "YXVkaW8=",
                "mime_type": "audio/wav",
                "request_id": "req-1",
            },
        )

        mixin._handle_chat_message.assert_awaited_once()
        chat_data = mixin._handle_chat_message.await_args.args[1]
        assert "project_id" not in chat_data
        assert chat_data["conversation_id"] == "conv-voice"

    @pytest.mark.asyncio
    async def test_voice_audio_timeout_sends_error_with_request_id(
        self,
    ) -> None:
        """Hung STT transcription should time out without forwarding chat."""
        mixin = DummyVoiceMixin(
            VoiceConfig(
                enabled=True,
                stt_enabled=True,
                transcription_timeout_seconds=0.01,
            )
        )
        stt = MagicMock()

        async def transcribe_forever(_audio_bytes: bytes, _mime_type: str) -> str:
            await wait_forever()
            return ""

        stt.transcribe = AsyncMock(side_effect=transcribe_forever)
        mixin._get_stt = MagicMock(return_value=stt)
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_audio(
            websocket,
            {
                "conversation_id": "conv-timeout",
                "audio_data": "YXVkaW8=",
                "mime_type": "audio/wav",
                "request_id": "req-timeout",
            },
        )

        payloads = [json.loads(call.args[0]) for call in websocket.send.await_args_list]
        assert [payload["status"] for payload in payloads] == ["transcribing", "error"]
        assert payloads[-1] == {
            "type": "voice_status",
            "conversation_id": "conv-timeout",
            "status": "error",
            "request_id": "req-timeout",
            "error": "Speech-to-text timed out",
        }
        mixin._handle_chat_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_voice_audio_no_audio_error_includes_request_id(self) -> None:
        """No-audio errors should echo the client request_id."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, stt_enabled=True))
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_audio(
            websocket,
            {
                "conversation_id": "conv-empty-audio",
                "audio_data": "",
                "mime_type": "audio/wav",
                "request_id": "req-no-audio",
            },
        )

        payload = json.loads(websocket.send.await_args.args[0])
        assert payload == {
            "type": "voice_status",
            "conversation_id": "conv-empty-audio",
            "status": "error",
            "request_id": "req-no-audio",
            "error": "No audio data provided",
        }

    @pytest.mark.asyncio
    async def test_voice_audio_stt_unavailable_error_includes_request_id(self) -> None:
        """STT-unavailable errors should echo the client request_id."""
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, stt_enabled=True))
        mixin._get_stt = MagicMock(return_value=None)
        websocket = SimpleNamespace(send=AsyncMock())

        await mixin._handle_voice_audio(
            websocket,
            {
                "conversation_id": "conv-no-stt",
                "audio_data": "YXVkaW8=",
                "mime_type": "audio/wav",
                "request_id": "req-no-stt",
            },
        )

        payload = json.loads(websocket.send.await_args.args[0])
        assert payload["type"] == "voice_status"
        assert payload["conversation_id"] == "conv-no-stt"
        assert payload["status"] == "error"
        assert payload["request_id"] == "req-no-stt"
        assert "Speech-to-text" in payload["error"]
