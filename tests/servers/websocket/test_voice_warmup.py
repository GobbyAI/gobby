"""Tests for WebSocket voice warmup state."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.voice import VoiceConfig
from gobby.servers.websocket.voice import VoiceMixin

pytestmark = pytest.mark.unit


class DummyVoiceMixin(VoiceMixin):
    def __init__(self, voice_config: VoiceConfig) -> None:
        self.clients: dict = {}
        self.daemon_config = SimpleNamespace(voice=voice_config)
        self._init_voice()


class TestVoiceWarmup:
    @pytest.mark.asyncio
    async def test_start_voice_warmup_is_single_flight(self) -> None:
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

        mixin.start_voice_warmup()
        first_task = mixin._voice_warmup_task
        mixin.start_voice_warmup()

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

    @pytest.mark.asyncio
    async def test_voice_mode_enable_starts_warmup(self) -> None:
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=False))

        mock_tts = MagicMock()
        mock_tts.warmup = AsyncMock()
        mixin._get_tts = MagicMock(return_value=mock_tts)
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
        mock_tts.warmup.assert_awaited_once()
        assert mixin._tts_warmup_status == "ready"

    @pytest.mark.asyncio
    async def test_voice_prepare_can_arm_tts_before_toggle_frame(self) -> None:
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
    async def test_start_voice_warmup_records_failures(self) -> None:
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
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))

        mixin._stt_warmup_status = "ready"
        mixin._tts_warmup_status = "ready"
        mock_stt = MagicMock()
        mock_tts = MagicMock()
        mixin._whisper_stt = mock_stt
        mixin._tts_provider = mock_tts
        mixin._voice_warmup_task = asyncio.create_task(asyncio.sleep(60))
        background_task = asyncio.create_task(asyncio.sleep(60))
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
        mixin = DummyVoiceMixin(VoiceConfig(enabled=True, tts_enabled=True, stt_enabled=True))
        mixin._voice_warmup_task = asyncio.create_task(asyncio.sleep(60))

        await mixin._check_voice_idle()

        assert mixin._voice_warmup_task is None
