"""Tests for lazy TTS provider loading and public status reasons."""

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.config.voice import VoiceConfig
from gobby.voice import providers
from gobby.voice.tts import TTSProviderStatus

pytestmark = pytest.mark.unit


def _enabled_config(provider: str = "chatterbox") -> VoiceConfig:
    return VoiceConfig(enabled=True, tts_provider=provider)


def test_provider_module_import_failure_has_actionable_public_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_import(_module_name: str) -> Any:
        raise ImportError("runtime dependency unavailable")

    monkeypatch.setattr(providers.importlib, "import_module", fail_import)

    fields = providers.get_tts_provider_status(_enabled_config()).as_status_fields()

    assert fields["tts_available"] is False
    assert fields["tts_reason"] == ("TTS provider module import failed: gobby.voice.tts_chatterbox")


def test_missing_provider_class_has_actionable_public_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(),
    )

    fields = providers.get_tts_provider_status(_enabled_config()).as_status_fields()

    assert fields["tts_available"] is False
    assert fields["tts_reason"] == (
        "TTS provider class missing: gobby.voice.tts_chatterbox.ChatterboxTurboProvider"
    )


def test_unknown_provider_retains_existing_reason_without_importing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_import(_module_name: str) -> Any:
        pytest.fail("unknown providers must not trigger runtime imports")

    monkeypatch.setattr(providers.importlib, "import_module", unexpected_import)

    status = providers.get_tts_provider_status(_enabled_config("unknown"))

    assert status.reason == "Unknown TTS provider: unknown"


def test_successful_provider_retains_factory_status(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = TTSProviderStatus(provider="chatterbox", available=True, reason="ready")

    class FakeProvider:
        def __init__(self, _config: VoiceConfig) -> None:
            pass

        def get_status(self) -> TTSProviderStatus:
            return expected

    monkeypatch.setattr(
        providers.importlib,
        "import_module",
        lambda _module_name: SimpleNamespace(ChatterboxTurboProvider=FakeProvider),
    )

    assert providers.get_tts_provider_status(_enabled_config()) is expected


def test_disabled_tts_status_does_not_import_provider_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_import(_module_name: str) -> Any:
        pytest.fail("disabled TTS must not import provider runtimes")

    monkeypatch.setattr(providers.importlib, "import_module", unexpected_import)

    status = providers.get_tts_status_for_config(VoiceConfig(enabled=True, tts_enabled=False))

    assert status.reason == "TTS disabled in config"


def test_provider_listing_api_is_absent() -> None:
    assert not hasattr(providers, "list_tts_providers")
