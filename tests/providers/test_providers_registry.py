"""Provider registry surface tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import gobby.providers as providers
import gobby.providers.registry as registry

pytestmark = pytest.mark.unit


def test_registry_exposes_only_live_provider_helpers() -> None:
    removed = {
        "provider_ids",
        "get_provider_metadata",
        "installed_provider_metadata",
        "provider_status_metadata",
    }

    assert removed.isdisjoint(providers.__all__)
    assert all(not hasattr(providers, name) for name in removed)
    assert all(not hasattr(registry, name) for name in removed)
    assert "installed_only" not in registry.ProviderMetadata.__dataclass_fields__
    assert not hasattr(registry.ProviderMetadata, "path")


def test_provider_metadata_preserves_order_and_live_api_metadata() -> None:
    entries = registry.provider_metadata()

    assert tuple(entry.provider for entry in entries) == (
        "claude",
        "codex",
        "droid",
        "grok",
        "qwen",
        "agy",
    )
    assert {entry.provider: entry.user_directory for entry in entries} == {
        "claude": ".claude",
        "codex": ".codex",
        "droid": ".factory",
        "grok": ".grok",
        "qwen": ".qwen",
        "agy": ".gemini",
    }
    with patch("gobby.providers.registry.shutil.which", return_value="/usr/bin/claude"):
        metadata = entries[0].api_metadata()

    assert metadata["display_name"] == "Claude Code"
    assert metadata["installed"] is True
    assert metadata["supports_web_chat"] is True
    assert "user_directory" not in metadata
