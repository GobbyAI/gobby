import pytest

from gobby.agents.watchdog.models import KNOWN_WATCHDOG_PROVIDERS
from gobby.agents.watchdog.registry import _READERS, WatchdogReaderRegistry

pytestmark = pytest.mark.unit


def test_registry_ids_match_known_watchdog_providers() -> None:
    assert frozenset(_READERS) == KNOWN_WATCHDOG_PROVIDERS
    for provider_id, reader in _READERS.items():
        assert reader is not None
        assert reader.provider_id == provider_id


def test_registry_normalizes_codex_and_returns_reader() -> None:
    reader = WatchdogReaderRegistry().for_provider(" CodEx ")

    assert reader is not None
    assert reader.provider_id == "codex"
    assert reader.capacity_pane_message is not None
    assert reader.supports_reasoning_interrupt is True


def test_registry_normalizes_claude_and_returns_reader() -> None:
    reader = WatchdogReaderRegistry().for_provider(" CLAUDE ")

    assert reader is not None
    assert reader.provider_id == "claude"
    assert reader.capacity_pane_message is None
    assert reader.supports_reasoning_interrupt is False


def test_registry_normalizes_grok_and_returns_reader() -> None:
    reader = WatchdogReaderRegistry().for_provider(" GroK ")

    assert reader is not None
    assert reader.provider_id == "grok"
    assert reader.capacity_pane_message is None
    assert reader.supports_reasoning_interrupt is False


@pytest.mark.parametrize("provider", ["droid", "qwen"])
def test_registry_returns_diagnostics_only_readers(provider: str) -> None:
    reader = WatchdogReaderRegistry().for_provider(provider.upper())

    assert reader is not None
    assert reader.provider_id == provider
    assert reader.capacity_pane_message is None
    assert reader.supports_reasoning_interrupt is False


def test_registry_returns_agy_reader() -> None:
    reader = WatchdogReaderRegistry().for_provider(" AGY ")

    assert reader is not None
    assert reader.provider_id == "agy"
    assert reader.capacity_pane_message is None
    assert reader.supports_reasoning_interrupt is False


def test_registry_returns_none_for_uninstalled_readers() -> None:
    assert WatchdogReaderRegistry().for_provider("unknown") is None

