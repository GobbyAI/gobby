"""Tests for resolve_context_window in gobby.llm.context_windows.

Verifies the priority order:
1. Config overrides (model substring -> context window)
2. Provider-reported runtime metadata
3. Provider-reported/provider-owned catalog metadata
4. Registry lookup (OpenRouter data via model_costs cache)
5. Static fallback defaults

Note: SDK-reported contextWindow (2nd arg) is deprecated and ignored.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.llm.context_windows import (
    coerce_context_length,
    provider_catalog_context_length_for_model,
    resolve_context_window,
    resolve_context_window_with_source,
    static_context_length_for_model,
)
from gobby.servers.provider_models import ProviderModelCatalog
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("model", "provider", "expected"),
    [
        ("sonnet", None, 1_000_000),
        ("sonnet", "claude", 1_000_000),
        ("claude-sonnet-4-6", None, 1_000_000),
        ("claude-sonnet-4-6-20260410", "claude", 1_000_000),
        ("anthropic/claude-sonnet-4-6", "claude", 1_000_000),
        ("claude/claude-sonnet-4-6", "claude", 1_000_000),
        ("claude-sonnet-4-5", "claude", 200_000),
        ("anthropic/claude-sonnet-4-5-20250929", "claude", 200_000),
    ],
)
def test_sonnet_static_context_windows(
    model: str,
    provider: str | None,
    expected: int,
) -> None:
    assert static_context_length_for_model(provider, model) == expected


def test_droid_sonnet_context_window_remains_provider_owned() -> None:
    assert provider_catalog_context_length_for_model("droid", "claude-sonnet-4-6") == 200_000


def test_sonnet_static_window_drives_context_pressure_ratio() -> None:
    context_window = static_context_length_for_model("claude", "claude-sonnet-4-6")

    assert ContextUsageSnapshot.calculate_ratio(200_000, context_window) == 0.2


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


def test_db_failure_degrades_to_static_context_window() -> None:
    db = MagicMock()
    db.fetchone.side_effect = psycopg.OperationalError("database unavailable")
    app_context = SimpleNamespace(database=db, provider_model_catalog=None)

    with patch("gobby.app_context.get_app_context", return_value=app_context):
        result = resolve_context_window_with_source("gpt-5.4")

    assert result is not None
    assert result.value == 258_400
    assert result.source == "static_default"


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


class _BareSourceCatalog:
    def __init__(self, value: object) -> None:
        self.value = value

    def get_context_window_with_source(
        self,
        provider: str | None,
        model: str,
    ) -> object:
        return self.value


class TestResolveContextWindow:
    """Tests for resolve_context_window()."""

    def test_rejects_invalid_model(self) -> None:
        with pytest.raises(TypeError, match="model must be a string or None"):
            resolve_context_window(123)  # type: ignore[arg-type]

    def test_rejects_invalid_overrides(self) -> None:
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
            assert resolve_context_window("claude-fable-5", None) == 1_000_000

    def test_claude_name_variations(self) -> None:
        """Various Claude model name formats resolve correctly."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            # Prefix match: claude-sonnet-4-6-20241022 matches claude-sonnet-4-6
            assert resolve_context_window("claude-sonnet-4-6-20241022", None) == 200_000

    @pytest.mark.parametrize(
        ("model", "provider", "expected"),
        [
            # Bare aliases resolve through their current family defaults.
            ("opus", None, 1_000_000),
            ("opus", "claude", 1_000_000),
            ("sonnet", None, 1_000_000),
            ("haiku", None, 200_000),
            ("fable", None, 1_000_000),
            ("fable", "claude", 1_000_000),
            ("claude-fable-5", None, 1_000_000),
            ("claude-fable-5", "claude", 1_000_000),
            ("claude-fable-5", "droid", 1_000_000),
            # Long-form Opus not enumerated in the static table — the
            # claude-opus-4-8 regression — resolves via the family-substring
            # fallback rather than falling through to a smaller default.
            ("claude-opus-4-8", None, 1_000_000),
            ("claude-opus-4-8", "claude", 1_000_000),
            # 1M-context marker suffixes normalize to the same family window.
            ("claude-opus-4-8[1m]", None, 1_000_000),
            ("claude-opus-4-8[1m]", "claude", 1_000_000),
            ("claude-opus-4-8-context-1m", "claude", 1_000_000),
            # Future dated Opus versions resolve without a per-version table.
            ("claude-opus-4-9", "claude", 1_000_000),
            # Current Sonnet is 1M; Haiku stays at 200k.
            ("claude-sonnet-4-6", "claude", 1_000_000),
            ("claude-haiku-4-5", "claude", 200_000),
        ],
    )
    def test_claude_family_substring_fallback(
        self, model: str, provider: str | None, expected: int
    ) -> None:
        """Long-form Claude IDs resolve to their family window via static fallback."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            assert resolve_context_window(model, provider=provider) == expected

    @pytest.mark.parametrize(
        ("model", "provider", "expected"),
        [
            ("sonnet[1m]", None, 1_000_000),
            ("haiku[1m]", "claude", 1_000_000),
            ("claude-sonnet-4-6[1m]", "claude", 1_000_000),
            ("anthropic/claude-haiku-4-5[1m]", "claude", 1_000_000),
            ("claude-opus-4-8[1m]", "claude", 1_000_000),
            ("sonnet", None, 1_000_000),
            ("haiku", "claude", 200_000),
            ("anthropic/claude-sonnet-4-6", "claude", 1_000_000),
        ],
    )
    def test_one_million_marker_floors_base_window_only(
        self, model: str, provider: str | None, expected: int
    ) -> None:
        """The marker raises sub-1M aliases and prefixes without changing unmarked models."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            assert resolve_context_window(model, provider=provider) == expected

    @pytest.mark.parametrize(
        ("kwargs", "registry_value", "expected_source"),
        [
            ({"overrides": {"sonnet": 200_000}}, None, "override"),
            ({"provider_reported_context_window": 200_000}, None, "provider_reported"),
            (
                {
                    "catalog": _SourceCatalog(
                        {(None, "claude-sonnet-4-6[1m]"): (200_000, "provider_catalog")}
                    )
                },
                None,
                "provider_catalog",
            ),
            ({"catalog": _FakeCatalog({})}, 200_000, "registry"),
            ({"catalog": _FakeCatalog({})}, None, "static_default"),
        ],
    )
    def test_one_million_marker_floors_all_sources_without_changing_attribution(
        self,
        kwargs: dict[str, object],
        registry_value: int | None,
        expected_source: str,
    ) -> None:
        """Every winning source honors the marker and retains its attribution."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=registry_value):
            result = resolve_context_window_with_source("claude-sonnet-4-6[1m]", **kwargs)

        assert result is not None
        assert result.value == 1_000_000
        assert result.source == expected_source

    def test_grok_composer_static_fallback_uses_current_window(self) -> None:
        """Grok Composer 2.5 Fast has a 200k static fallback."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            assert resolve_context_window("grok-composer-2.5-fast", provider="grok") == 200_000

    @pytest.mark.parametrize(
        "registry_value",
        [
            pytest.param(None, id="null"),
            pytest.param(0, id="zero"),
            pytest.param(-1, id="negative"),
            pytest.param(True, id="bool"),
            pytest.param("128000", id="string"),
            pytest.param(128000.0, id="float"),
        ],
    )
    def test_invalid_registry_window_defers_to_static_fallback(
        self,
        registry_value: object,
    ) -> None:
        with patch(
            "gobby.llm.model_registry.lookup_context_window",
            return_value=registry_value,
        ):
            result = resolve_context_window_with_source(
                "grok-composer-2.5-fast",
                provider="grok",
            )

        assert result is not None
        assert result.value == 200_000
        assert result.source == "static_default"

    def test_positive_registry_window_keeps_registry_source(self) -> None:
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=300_000):
            result = resolve_context_window_with_source(
                "grok-composer-2.5-fast",
                provider="grok",
            )

        assert result is not None
        assert result.value == 300_000
        assert result.source == "registry"

    def test_family_fallback_scoped_to_claude_providers(self) -> None:
        """Claude family keys stay scoped to Claude-compatible providers."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            assert resolve_context_window("claude-opus-4-8", provider="openai") is None
            assert resolve_context_window("claude-sonnet-4-6", provider="openai") is None
            assert resolve_context_window("fable", provider="local") is None
            assert resolve_context_window("claude-fable-5", provider="openai") is None

    def test_empty_override_key_is_ignored(self) -> None:
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            result = resolve_context_window_with_source(
                "grok-composer-2.5-fast",
                overrides={"": 123_000},
                provider="grok",
            )

        assert result is not None
        assert result.value == 200_000
        assert result.source == "static_default"

    def test_family_fallback_ignores_unknown_claude_model(self) -> None:
        """A Claude id with no family token still returns None (no false 200k)."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            assert resolve_context_window("claude-unknown-model", None) is None

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

    def test_provider_reported_runtime_metadata_accepts_camel_alias(self) -> None:
        """Runtime metadata accepts modelContextWindow alias."""
        catalog = _SourceCatalog({("codex", "gpt-5.4"): (200_000, "static_default")})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window(
                "gpt-5.4",
                {"modelContextWindow": 258_400},
                provider="codex",
                catalog=catalog,
            )

        assert result == 258_400

    def test_legacy_catalog_defers_to_registry(self) -> None:
        """Source-less legacy catalog data is a fallback, not provider metadata."""
        catalog = _FakeCatalog({("codex", "gpt-4o"): 256_000})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-4o", None, provider="codex", catalog=catalog)

        assert result == 128_000

    def test_bare_catalog_int_must_be_positive(self) -> None:
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            assert (
                resolve_context_window(
                    "unknown-model-xyz",
                    None,
                    provider="codex",
                    catalog=_BareSourceCatalog(123_000),
                )
                == 123_000
            )
            assert (
                resolve_context_window(
                    "unknown-model-xyz",
                    None,
                    provider="codex",
                    catalog=_BareSourceCatalog(0),
                )
                is None
            )

    def test_static_catalog_source_defers_to_registry(self) -> None:
        """Static catalog values are fallbacks, not authoritative provider metadata."""
        catalog = _SourceCatalog({("codex", "gpt-4o"): (200_000, "static_default")})
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-4o", None, provider="codex", catalog=catalog)

        assert result == 128_000

    def test_registry_catalog_source_remains_an_eligible_fallback(self) -> None:
        catalog = _SourceCatalog({("codex", "unknown-model"): (200_000, "registry")})
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            result = resolve_context_window_with_source(
                "unknown-model",
                provider="codex",
                catalog=catalog,
            )

        assert result is not None
        assert result.value == 200_000
        assert result.source == "registry"

    def test_legacy_catalog_value_defers_to_registry(self) -> None:
        catalog = _FakeCatalog({("codex", "gpt-4o"): 200_000})
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=128_000):
            result = resolve_context_window_with_source(
                "gpt-4o",
                provider="codex",
                catalog=catalog,
            )

        assert result is not None
        assert result.value == 128_000
        assert result.source == "registry"

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

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("anthropic/claude-sonnet-4-6", 200_000),
            ("claude/claude-sonnet-4-6", 200_000),
            ("gemini/gemini-3.5-flash", 1_048_576),
            ("codex/gpt-5.3-codex", 258_400),
            ("grok/grok-composer-2.5-fast", 200_000),
        ],
    )
    def test_provider_prefix_handled(self, model: str, expected: int) -> None:
        """Provider-prefixed model names work via registry and static lookup."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window(model, None)
        assert result == expected

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
        catalog = ProviderModelCatalog(cache_path=cache_path)

        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-5.4", None, provider="codex", catalog=catalog)

        assert result == 300_000
