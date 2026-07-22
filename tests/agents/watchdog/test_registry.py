import pytest

from gobby.agents.watchdog.registry import WatchdogReaderRegistry

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize("provider", ["agy", "grok", "droid", "qwen", "unknown"])
def test_registry_returns_none_for_uninstalled_readers(provider: str) -> None:
    assert WatchdogReaderRegistry().for_provider(provider) is None
