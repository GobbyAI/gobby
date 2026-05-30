"""Tests for resolve_context_window in gobby.llm.claude_models.

Verifies the priority order:
1. Config overrides (model substring -> context window)
2. Provider-reported runtime metadata
3. Provider-reported/provider-owned catalog metadata
4. Registry lookup (OpenRouter data via model_costs cache)
5. Static fallback defaults

Note: SDK-reported contextWindow (2nd arg) is deprecated and ignored.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gobby.llm.claude_models import resolve_context_window
from gobby.llm.context_windows import coerce_context_length
from gobby.servers.provider_models import ProviderModelCatalog

pytestmark = pytest.mark.unit


def _mock_lookup(model: str) -> int | None:
    """Mock cost_table.lookup_context_window with test data."""
    data = {
        "claude-opus-4-6": 1_000_000,
        "claude-sonnet-4-6": 200_000,
        "claude-haiku-4-5": 200_000,
        "gpt-4o": 128_000,
        "gpt-5.4": 300_000,
        "qwen3-coder": 262_144,
    }
    # Strip provider prefix
    if "/" in model:
        model = model.split("/", 1)[1]
    # Exact match
    if model in data:
        return data[model]
    # Prefix match
    best_len = 0
    best_val = None
    for key, val in data.items():
        if model.startswith(key) and len(key) > best_len:
            best_len = len(key)
            best_val = val
    return best_val


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, 1),
        (0.5, None),
        (1.5, None),
        (0.0, None),
    ],
)
def test_coerce_context_length_requires_whole_positive_floats(
    value: float,
    expected: int | None,
) -> None:
    assert coerce_context_length(value) == expected


class _FakeCatalog:
    def __init__(self, values: dict[tuple[str | None, str], int]) -> None:
        self.values = values

    def get_context_window(self, provider: str | None, model: str) -> int | None:
        return self.values.get((provider, model))


class _SourceCatalog:
    def __init__(self, values: dict[tuple[str | None, str], tuple[int, str]]) -> None:
        self.values = values

    def get_context_window_with_source(
        self,
        provider: str | None,
        model: str,
    ) -> tuple[int, str] | None:
        return self.values.get((provider, model))


class TestResolveContextWindow:
    """Tests for resolve_context_window()."""

    def test_claude_models_wrapper_emits_deprecation_warning(self) -> None:
        with pytest.warns(DeprecationWarning):
            assert resolve_context_window(None) is None

    def test_claude_models_wrapper_rejects_invalid_model(self) -> None:
        with pytest.raises(TypeError, match="model must be a string or None"):
            resolve_context_window(123)  # type: ignore[arg-type]

    def test_claude_models_wrapper_rejects_invalid_overrides(self) -> None:
        with pytest.raises(TypeError, match="overrides must be a dict or None"):
            resolve_context_window(None, overrides=[])  # type: ignore[arg-type]

    def test_sdk_context_window_ignored(self) -> None:
        """SDK-reported contextWindow (2nd arg) is deprecated and ignored."""
        model_usage = {"contextWindow": 180_000}
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("claude-opus-4-6", model_usage)
        assert result == 1_000_000

    def test_claude_model_family_windows(self) -> None:
        """Claude models return registry-backed context windows."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            assert resolve_context_window("claude-opus-4-6", None) == 1_000_000
            assert resolve_context_window("claude-sonnet-4-6", None) == 200_000
            assert resolve_context_window("claude-haiku-4-5", None) == 200_000

    def test_claude_name_variations(self) -> None:
        """Various Claude model name formats resolve correctly."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            # Prefix match: claude-sonnet-4-6-20241022 matches claude-sonnet-4-6
            assert resolve_context_window("claude-sonnet-4-6-20241022", None) == 200_000

    def test_non_claude_model_uses_registry(self) -> None:
        """Non-Claude models use registry lookup."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-4o", None)
        assert result == 128_000

    def test_registry_miss_non_claude_returns_none(self) -> None:
        """If registry has no data for a non-Claude model, return None."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            result = resolve_context_window("unknown-model-xyz", None)
        assert result is None

    def test_registry_miss_claude_returns_none_without_catalog(self) -> None:
        """Claude no longer has a hardcoded resolver fallback."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            result = resolve_context_window("claude-unknown-model", None)
        assert result is None

    def test_none_model_returns_none(self) -> None:
        """None model returns None."""
        assert resolve_context_window(None, None) is None

    def test_none_model_with_sdk_usage_ignored(self) -> None:
        """None model -- SDK usage (2nd arg) is ignored, returns None."""
        model_usage = {"contextWindow": 200_000}
        result = resolve_context_window(None, model_usage)
        assert result is None

    def test_config_overrides_win_over_registry(self) -> None:
        """Config overrides take precedence over registry data."""
        overrides = {"opus": 500_000}
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("claude-opus-4-6", None, overrides=overrides)
        assert result == 500_000

    def test_config_overrides_win_over_catalog(self) -> None:
        """Config overrides take precedence over provider catalog data."""
        overrides = {"opus": 500_000}
        catalog = _FakeCatalog({("claude", "claude-opus-4-6"): 1_000_000})

        result = resolve_context_window(
            "claude-opus-4-6",
            None,
            overrides=overrides,
            provider="claude",
            catalog=catalog,
        )

        assert result == 500_000

    def test_provider_reported_runtime_metadata_wins(self) -> None:
        """Provider-reported runtime metadata wins over catalog and registry data."""
        catalog = _SourceCatalog({("codex", "gpt-5.4"): (200_000, "static_default")})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window(
                "gpt-5.4",
                {"model_context_window": 258_400},
                provider="codex",
                catalog=catalog,
            )

        assert result == 258_400

    def test_catalog_wins_over_registry(self) -> None:
        """Provider catalog data takes precedence over OpenRouter/model_costs."""
        catalog = _FakeCatalog({("codex", "gpt-4o"): 256_000})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-4o", None, provider="codex", catalog=catalog)

        assert result == 256_000

    def test_static_catalog_source_defers_to_registry(self) -> None:
        """Static catalog values are fallbacks, not authoritative provider metadata."""
        catalog = _SourceCatalog({("codex", "gpt-4o"): (200_000, "static_default")})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-4o", None, provider="codex", catalog=catalog)

        assert result == 128_000

    def test_provider_catalog_source_wins_over_registry(self) -> None:
        """Provider-owned catalog values outrank generic registry data."""
        catalog = _SourceCatalog({("droid", "gpt-5.4"): (200_000, "provider_catalog")})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-5.4", None, provider="droid", catalog=catalog)

        assert result == 200_000

    def test_registry_fills_catalog_gap(self) -> None:
        """OpenRouter/model_costs fills gaps when catalog data is absent."""
        catalog = _FakeCatalog({})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-4o", None, provider="codex", catalog=catalog)

        assert result == 128_000

    def test_config_overrides_partial(self) -> None:
        """Config overrides only affect matched families, others use registry."""
        overrides = {"opus": 500_000}
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            assert resolve_context_window("claude-opus-4-6", None, overrides=overrides) == 500_000
            assert resolve_context_window("claude-sonnet-4-6", None, overrides=overrides) == 200_000

    def test_overrides_win_sdk_ignored(self) -> None:
        """Config overrides win; SDK-reported contextWindow (2nd arg) is ignored."""
        overrides = {"opus": 500_000}
        model_usage = {"contextWindow": 180_000}
        result = resolve_context_window("claude-opus-4-6", model_usage, overrides=overrides)
        assert result == 500_000

    def test_provider_prefix_handled(self) -> None:
        """Provider-prefixed model names work via registry lookup."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("anthropic/claude-sonnet-4-6", None)
        assert result == 200_000

    def test_qwen_auth_suffix_stripped_for_registry_lookup(self) -> None:
        """Qwen auth suffixes are removed before registry fallback lookup."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("qwen3-coder(openai)", None, provider="qwen")
        assert result == 262_144

    def test_legacy_cached_codex_static_value_does_not_block_registry(
        self,
        temp_dir,
    ) -> None:
        """Legacy source-less cached Codex 200k is treated as a static fallback."""
        cache_path = temp_dir / "provider-model-catalog.json"
        cache_path.write_text(
            """
            {
              "version": 4,
              "providers": {
                "codex": {
                  "source": "cache",
                  "models": [
                    {"value": "gpt-5.4", "label": "GPT-5.4", "context_length": 200000}
                  ]
                }
              }
            }
            """,
            encoding="utf-8",
        )
        catalog = ProviderModelCatalog(config=None, cache_path=cache_path)

        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-5.4", None, provider="codex", catalog=catalog)

        assert result == 300_000
