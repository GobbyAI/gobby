"""Tests for live provider model discovery and cache fallback."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.servers.provider_models import ProviderModelCatalog

pytestmark = pytest.mark.unit


class TestProviderModelCatalog:
    @pytest.mark.asyncio
    async def test_probe_claude_model_records_canonical_id(self, temp_dir: Path) -> None:
        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )
        process = AsyncMock()
        process.communicate = AsyncMock(
            return_value=(
                (
                    json.dumps(
                        {
                            "modelUsage": {
                                "claude-sonnet-4-6-20260410": {"inputTokens": 1, "outputTokens": 1}
                            }
                        }
                    ).encode(),
                    b"",
                )
            )
        )
        process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await catalog._probe_claude_model("sonnet", "Sonnet")

        assert result == {
            "value": "sonnet",
            "label": "Sonnet",
            "canonical_id": "claude-sonnet-4-6-20260410",
            "context_length": 200_000,
            "reasoning": {"supported_efforts": ["low", "medium", "high", "max"]},
        }

    @pytest.mark.asyncio
    async def test_discover_claude_models_keeps_successful_alias_probes(
        self, temp_dir: Path
    ) -> None:
        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )

        async def probe(alias: str, label: str) -> dict[str, str]:
            if alias == "haiku":
                raise RuntimeError("haiku failed")
            return {"value": alias, "label": label, "canonical_id": f"claude-{alias}"}

        with (
            patch(
                "gobby.servers.provider_models.shutil.which", return_value="/usr/local/bin/claude"
            ),
            patch.object(catalog, "_probe_claude_model", side_effect=probe),
        ):
            models = await catalog._discover_claude_models()

        assert models == [
            {"value": "sonnet", "label": "Sonnet", "canonical_id": "claude-sonnet"},
            {"value": "opus", "label": "Opus", "canonical_id": "claude-opus"},
        ]

    @pytest.mark.asyncio
    async def test_refresh_falls_back_to_cached_models_per_provider(self, temp_dir: Path) -> None:
        cache_path = temp_dir / "provider-model-catalog.json"
        catalog = ProviderModelCatalog(config=None, cache_path=cache_path)
        catalog._providers = {
            "codex": {
                "source": "live",
                "cli_version": "0.118.0",
                "error": None,
                "generated_at": "2026-04-10T23:15:00Z",
                "models": [{"value": "gpt-5.4", "label": "gpt-5.4"}],
            }
        }

        async def discover(
            provider: str,
            *,
            codex_client: object | None = None,
        ) -> list[dict[str, str]]:
            if provider == "claude":
                return [{"value": "sonnet", "label": "Sonnet"}]
            if provider == "gemini":
                return [{"value": "gemini-3.1-pro-preview", "label": "gemini-3.1-pro-preview"}]
            raise RuntimeError("codex probe failed")

        with (
            patch.object(catalog, "_discover_provider_models", side_effect=discover),
            patch.object(
                catalog,
                "_get_cli_version",
                new=AsyncMock(side_effect=["1.0.12", "0.37.1", "0.14.3", "0.118.0", "0.106.0"]),
            ),
        ):
            status = await catalog.refresh()

        assert status["claude"]["source"] == "live"
        assert status["gemini"]["source"] == "live"
        assert status["codex"]["source"] == "cache"
        assert status["codex"]["model_count"] == 1
        assert status["codex"]["error"] == "codex probe failed"

        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        assert payload["version"] == 3
        assert payload["providers"]["codex"]["source"] == "cache"
        assert payload["providers"]["codex"]["models"][0]["context_length"] == 200_000

    def test_load_cache_preserves_and_enriches_context_lengths(self, temp_dir: Path) -> None:
        cache_path = temp_dir / "provider-model-catalog.json"
        cache_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "providers": {
                        "codex": {
                            "source": "live",
                            "models": [{"value": "gpt-5.4", "label": "gpt-5.4"}],
                        },
                        "gemini": {
                            "source": "live",
                            "models": [
                                {
                                    "value": "gemini-custom",
                                    "label": "Gemini Custom",
                                    "context_length": 123456,
                                }
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        catalog = ProviderModelCatalog(config=None, cache_path=cache_path)

        codex = catalog.get_provider_snapshot("codex")["models"][0]
        gemini = catalog.get_provider_snapshot("gemini")["models"][0]
        assert codex["context_length"] == 200_000
        assert gemini["context_length"] == 123_456

    def test_load_cache_accepts_version_none(self, temp_dir: Path) -> None:
        cache_path = temp_dir / "provider-model-catalog.json"
        cache_path.write_text(
            json.dumps(
                {
                    "version": None,
                    "providers": {
                        "codex": {
                            "source": "cache",
                            "models": [
                                {
                                    "value": "gpt-5.4",
                                    "label": "GPT-5.4",
                                    "context_length": 123_000,
                                }
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        catalog = ProviderModelCatalog(config=None, cache_path=cache_path)

        model = catalog.get_provider_snapshot("codex")["models"][0]
        assert model["context_length"] == 123_000

    def test_get_context_window_matches_aliases_suffixes_and_droid_core(
        self, temp_dir: Path
    ) -> None:
        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )
        catalog._providers = {
            "claude": {
                "models": [
                    {
                        "value": "sonnet",
                        "canonical_id": "claude-sonnet-4-6-20260410",
                        "context_length": 200_000,
                    }
                ]
            },
            "qwen": {
                "models": [
                    {
                        "value": "qwen3-coder(openai)",
                        "label": "qwen3-coder",
                        "context_length": 262_144,
                    }
                ]
            },
            "droid": {
                "models": [
                    {"value": "glm-5", "label": "Droid Core (GLM-5)", "context_length": 128_000}
                ]
            },
            "codex": {
                "models": [
                    {"value": "gpt-5.5", "context_length": 321_000},
                    {"value": "gpt-5.4", "context_length": 333_000},
                ]
            },
        }

        assert catalog.get_context_window("claude", "sonnet") == 200_000
        assert catalog.get_context_window("claude", "claude-sonnet-4-6-20260410") == 200_000
        assert catalog.get_context_window("claude", "claude-sonnet-4-6-20241022") == 200_000
        assert catalog.get_context_window("qwen", "qwen3-coder(openai)") == 262_144
        assert catalog.get_context_window("qwen", "qwen3-coder") == 262_144
        assert catalog.get_context_window("droid", "gpt-5.5") == 321_000
        assert catalog.get_context_window("droid", "gpt-5.4") == 333_000
        assert catalog.get_context_window("droid", "z-ai/glm-5") == 128_000
        assert catalog.get_context_window("droid", "custom/byok-model") is None

    def test_configured_models_precede_live_snapshot_and_keep_metadata(
        self, temp_dir: Path
    ) -> None:
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                codex=LLMProviderConfig(models="gpt-5.5,gpt-5.4"),
            )
        )
        catalog = ProviderModelCatalog(
            config=config,
            cache_path=temp_dir / "provider-model-catalog.json",
        )
        catalog._providers = {
            "codex": {
                "source": "live",
                "models": [
                    {
                        "value": "gpt-5.5",
                        "label": "GPT-5.5 Live",
                        "context_length": 321_000,
                    },
                    {"value": "gpt-5.4", "label": "GPT-5.4 Live", "context_length": 200_000},
                    {"value": "gpt-5.2", "label": "GPT-5.2 Live", "context_length": 200_000},
                ],
            },
        }

        snapshot = catalog.get_provider_snapshot("codex")
        models = snapshot["models"]

        assert [model["value"] for model in models] == ["gpt-5.5", "gpt-5.4", "gpt-5.2"]
        assert models[0]["label"] == "GPT-5.5 Live"
        assert models[0]["context_length"] == 321_000

    @pytest.mark.asyncio
    async def test_refresh_marks_provider_failed_without_prior_cache(self, temp_dir: Path) -> None:
        cache_path = temp_dir / "provider-model-catalog.json"
        catalog = ProviderModelCatalog(config=None, cache_path=cache_path)

        with (
            patch.object(
                catalog,
                "_discover_provider_models",
                new=AsyncMock(side_effect=FileNotFoundError("gemini CLI not found in PATH")),
            ),
            patch.object(catalog, "_get_cli_version", new=AsyncMock(return_value=None)),
        ):
            status = await catalog.refresh()

        assert status["claude"]["source"] == "failed"
        assert status["gemini"]["source"] == "failed"
        assert status["codex"]["source"] == "failed"

    def test_load_cache_ignores_unsupported_version(self, temp_dir: Path) -> None:
        cache_path = temp_dir / "provider-model-catalog.json"
        cache_path.write_text(
            json.dumps({"version": 99, "providers": {"codex": {"models": [{"value": "gpt-5.4"}]}}}),
            encoding="utf-8",
        )

        catalog = ProviderModelCatalog(config=None, cache_path=cache_path)

        assert catalog.status_snapshot()["codex"]["model_count"] == 0

    @pytest.mark.asyncio
    async def test_discover_qwen_models_merges_acp_and_configured_models(
        self, temp_dir: Path
    ) -> None:
        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )

        with (
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/local/bin/qwen"),
            patch.object(
                catalog,
                "_discover_acp_models",
                new=AsyncMock(
                    return_value=[
                        {"value": "coder-model(qwen-oauth)", "label": "coder-model"},
                        {"value": "gpt-5(openai)", "label": "gpt-5"},
                    ]
                ),
            ),
            patch.object(
                catalog,
                "_discover_qwen_configured_models",
                return_value=[
                    {"value": "gpt-5(openai)", "label": "gpt-5"},
                    {"value": "claude-sonnet-4-5(anthropic)", "label": "claude-sonnet-4-5"},
                ],
            ),
        ):
            models = await catalog._discover_qwen_models()

        assert models == [
            {"value": "coder-model(qwen-oauth)", "label": "coder-model"},
            {"value": "gpt-5(openai)", "label": "gpt-5"},
            {
                "value": "claude-sonnet-4-5(anthropic)",
                "label": "claude-sonnet-4-5",
            },
        ]

    def test_normalize_qwen_model_labels_only_disambiguates_duplicate_base_ids(
        self, temp_dir: Path
    ) -> None:
        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )

        normalized = catalog._normalize_qwen_model_labels(
            [
                {"value": "coder-model(qwen-oauth)", "label": "coder-model"},
                {"value": "gpt-5(openai)", "label": "gpt-5"},
                {"value": "gpt-5(anthropic)", "label": "gpt-5"},
            ]
        )

        assert normalized == [
            {"value": "coder-model(qwen-oauth)", "label": "coder-model"},
            {"value": "gpt-5(openai)", "label": "gpt-5 (openai)"},
            {"value": "gpt-5(anthropic)", "label": "gpt-5 (anthropic)"},
        ]

    @pytest.mark.asyncio
    async def test_discover_qwen_models_can_fall_back_to_settings_catalog(
        self, temp_dir: Path
    ) -> None:
        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )

        with (
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/local/bin/qwen"),
            patch.object(
                catalog,
                "_discover_acp_models",
                new=AsyncMock(side_effect=RuntimeError("ACP auth required")),
            ),
            patch.object(
                catalog,
                "_discover_qwen_configured_models",
                return_value=[{"value": "gpt-5(openai)", "label": "gpt-5"}],
            ),
        ):
            models = await catalog._discover_qwen_models()

        assert models == [{"value": "gpt-5(openai)", "label": "gpt-5"}]

    @pytest.mark.asyncio
    async def test_discover_droid_models_returns_static_catalog(self, temp_dir: Path) -> None:
        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )

        models = await catalog._discover_provider_models("droid")

        assert {model["value"] for model in models} == {
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-6-fast",
            "claude-opus-4-5-20251101",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
            "gpt-5.4",
            "gpt-5.4-fast",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-fast",
            "gpt-5.2",
            "gpt-5.2-codex",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "minimax-m2.7",
            "minimax-m2.5",
            "kimi-k2.6",
            "kimi-k2.5",
            "glm-5.1",
            "glm-5",
            "glm-4.7",
            "gpt-5.1-codex-max",
        }
        assert len(models) == 24

        by_id = {model["value"]: model for model in models}
        assert "xhigh" in by_id["claude-opus-4-7"]["reasoning"]["supported_efforts"]
        assert "max" in by_id["claude-opus-4-7"]["reasoning"]["supported_efforts"]
        assert "minimal" in by_id["gemini-3-flash-preview"]["reasoning"]["supported_efforts"]
        assert by_id["minimax-m2.7"]["reasoning"]["supported_efforts"] == ["high"]
        for model_id in ("glm-5.1", "glm-5", "glm-4.7"):
            assert by_id[model_id].get("reasoning", {}).get("supported_efforts", []) == []

    def test_load_qwen_settings_merges_global_and_project_files(self, temp_dir: Path) -> None:
        global_settings = temp_dir / ".qwen" / "settings.json"
        global_settings.parent.mkdir(parents=True)
        global_settings.write_text(
            json.dumps(
                {
                    "security": {"auth": {"selectedType": "openai"}},
                    "modelProviders": {
                        "openai": [{"id": "gpt-5", "name": "gpt-5"}],
                    },
                }
            ),
            encoding="utf-8",
        )

        project_dir = temp_dir / "project"
        project_settings = project_dir / ".qwen" / "settings.json"
        project_settings.parent.mkdir(parents=True)
        project_settings.write_text(
            json.dumps(
                {
                    "security": {"auth": {"selectedType": "anthropic"}},
                    "modelProviders": {
                        "anthropic": [{"id": "claude-sonnet-4-5", "name": "claude-sonnet-4-5"}],
                    },
                }
            ),
            encoding="utf-8",
        )

        catalog = ProviderModelCatalog(
            config=None, cache_path=temp_dir / "provider-model-catalog.json"
        )

        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.object(Path, "cwd", return_value=project_dir),
        ):
            settings = catalog._load_qwen_settings()

        assert settings["security"]["auth"]["selectedType"] == "anthropic"
        assert settings["modelProviders"]["openai"][0]["id"] == "gpt-5"
        assert settings["modelProviders"]["anthropic"][0]["id"] == "claude-sonnet-4-5"
