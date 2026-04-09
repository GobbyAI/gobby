"""Tests for WebSocket voice warmup state."""

from __future__ import annotations

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
        mock_stt._ensure_model = AsyncMock()

        mock_tts = MagicMock()
        mock_tts._ensure_model = AsyncMock()

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

        mock_stt._ensure_model.assert_awaited_once()
        mock_tts._ensure_model.assert_awaited_once()
        assert mixin._stt_warmup_status == "ready"
        assert mixin._tts_warmup_status == "ready"

        status = mixin.get_voice_status()
        assert status["voice_ready"] is True
        assert status["voice_loading"] is False

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
