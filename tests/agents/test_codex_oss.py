from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.agents.codex_oss import (
    codex_oss_launch_args,
    codex_oss_provider_for_local_endpoint,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("provider", [None, "", "  "])
def test_codex_oss_provider_requires_local_provider(provider: str | None) -> None:
    endpoint = SimpleNamespace(provider=provider)

    with pytest.raises(ValueError, match="requires provider=lmstudio or provider=ollama"):
        codex_oss_provider_for_local_endpoint(endpoint)


def test_codex_oss_provider_rejects_unsupported_provider() -> None:
    endpoint = SimpleNamespace(provider="openai-compatible")

    with pytest.raises(ValueError) as exc_info:
        codex_oss_provider_for_local_endpoint(endpoint)

    message = str(exc_info.value)
    assert "supports provider=lmstudio or provider=ollama" in message
    assert "got provider=openai-compatible" in message


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("LMStudio", "lmstudio"), (" ollama ", "ollama")],
)
def test_codex_oss_provider_normalizes_supported_provider(provider: str, expected: str) -> None:
    endpoint = SimpleNamespace(provider=provider)

    assert codex_oss_provider_for_local_endpoint(endpoint) == expected


def test_codex_oss_launch_args_use_current_global_flags() -> None:
    assert codex_oss_launch_args("LMSTUDIO") == [
        "--oss",
        "--local-provider",
        "lmstudio",
    ]
