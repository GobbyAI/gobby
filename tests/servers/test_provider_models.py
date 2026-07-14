"""Tests for live provider model discovery and cache fallback."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.servers.provider_model_defaults import AGY_MODELS
from gobby.servers.provider_model_discovery import CLAUDE_ALIASES
from gobby.servers.provider_models import (
    ProviderModelCatalog,
    _model_discovery_cwd_path,
    create_provider_model_catalog,
)

pytestmark = pytest.mark.unit


class TestProviderModelCatalog:
    def test_constructor_rejects_legacy_config_argument(self, temp_dir: Path) -> None:
        """ProviderModelCatalog no longer accepts dead daemon config input."""
        with pytest.raises(TypeError, match="config"):
            ProviderModelCatalog(config=None, cache_path=temp_dir / "provider-model-catalog.json")

    def test_factory_accepts_daemon_config_without_constructor_probe(self, temp_dir: Path) -> None:
        """Factory is the daemon-aware entry point; the catalog itself stays config-free."""
        with patch.dict("os.environ", {"GOBBY_HOME": str(temp_dir)}, clear=False):
            catalog = create_provider_model_catalog(DaemonConfig())

        assert isinstance(catalog, ProviderModelCatalog)
        assert catalog.cache_path == temp_dir / "provider-model-catalog.json"

    @pytest.mark.asyncio
    async def test_probe_claude_model_records_canonical_id(self, temp_dir: Path) -> None:
        """Claude probes should preserve the user alias and record canonical IDs."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
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
            "context_length": 1_000_000,
            "context_length_source": "static_default",
            "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh", "max"]},
        }

    @pytest.mark.asyncio
    async def test_probe_claude_model_reports_malformed_final_json(self, temp_dir: Path) -> None:
        """Claude probe failures should include the malformed final output line."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        process = AsyncMock()
        process.communicate = AsyncMock(return_value=(b"warning\nnot-json\n", b""))
        process.returncode = 0

        with (
            patch("asyncio.create_subprocess_exec", return_value=process),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await catalog._probe_claude_model("sonnet", "Sonnet")

        message = str(exc_info.value)
        assert "Claude sonnet" in message
        assert "not-json" in message
        assert "Expecting value" in message

    @pytest.mark.asyncio
    async def test_probe_claude_fable_model_records_context(self, temp_dir: Path) -> None:
        """Claude Fable probes should preserve the alias and report 1M context."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        process = AsyncMock()
        process.communicate = AsyncMock(
            return_value=(
                json.dumps(
                    {"modelUsage": {"claude-fable-5": {"inputTokens": 1, "outputTokens": 1}}}
                ).encode(),
                b"",
            )
        )
        process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await catalog._probe_claude_model("fable", "Fable")

        assert result == {
            "value": "fable",
            "label": "Fable",
            "canonical_id": "claude-fable-5",
            "context_length": 1_000_000,
            "context_length_source": "static_default",
            "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh", "max"]},
        }

    @pytest.mark.asyncio
    async def test_discover_claude_models_keeps_successful_alias_probes(
        self, temp_dir: Path
    ) -> None:
        """Claude discovery should keep successful aliases when one probe fails."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

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
            {"value": "fable", "label": "Fable", "canonical_id": "claude-fable"},
        ]

    def test_claude_aliases_cover_all_current_model_families(self) -> None:
        """Regression guard (task #17775): probed aliases must cover every current
        Claude family. probe_claude_model validates each alias against the real CLI,
        so entries must not be removed because a reviewer or bot does not recognize
        the model name."""
        assert [alias for alias, _ in CLAUDE_ALIASES] == [
            "haiku",
            "sonnet",
            "opus",
            "fable",
        ]

    @pytest.mark.asyncio
    async def test_refresh_falls_back_to_cached_models_per_provider(self, temp_dir: Path) -> None:
        """Refresh should use cached models for only the provider that fails."""
        cache_path = temp_dir / "provider-model-catalog.json"
        catalog = ProviderModelCatalog(cache_path=cache_path)
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
            if provider == "qwen":
                return [{"value": "qwen-live", "label": "Qwen Live"}]
            if provider == "droid":
                return [{"value": "minimax-m2.7", "label": "Droid Core"}]
            raise RuntimeError("codex probe failed")

        with (
            patch.object(catalog, "_discover_provider_models", side_effect=discover),
            patch.object(
                catalog,
                "_discover_grok_models_with_source",
                new=AsyncMock(return_value=([{"value": "grok-build"}], "static")),
            ),
            patch.object(
                catalog,
                "_get_cli_version",
                new=AsyncMock(
                    side_effect=[
                        "1.0.12",
                        "0.118.0",
                        "0.106.0",
                        "0.37.1",
                        "0.1.216",
                        "0.14.3",
                        "1.0.0",
                    ]
                ),
            ),
        ):
            status = await catalog.refresh()

        assert status["claude"]["source"] == "live"
        assert status["qwen"]["source"] == "live"
        assert status["droid"]["source"] == "live"
        assert status["grok"]["source"] == "static"
        assert status["codex"]["source"] == "cache"
        assert "gemini" not in status
        assert status["codex"]["model_count"] == 1
        assert status["codex"]["error"] == "codex probe failed"

        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        assert payload["version"] == 5
        assert payload["providers"]["codex"]["source"] == "cache"
        assert payload["providers"]["codex"]["models"][0]["context_length"] == 258_400
        assert (
            payload["providers"]["codex"]["models"][0]["context_length_source"] == "static_default"
        )

    def test_load_cache_preserves_and_enriches_context_lengths(self, temp_dir: Path) -> None:
        """Cache loading should preserve known lengths and fill missing defaults."""
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
                        "qwen": {
                            "source": "live",
                            "models": [
                                {
                                    "value": "qwen-custom",
                                    "label": "Qwen Custom",
                                    "context_length": 123456,
                                }
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        catalog = ProviderModelCatalog(cache_path=cache_path)

        codex = catalog.get_provider_snapshot("codex")["models"][0]
        qwen = catalog.get_provider_snapshot("qwen")["models"][0]
        assert codex["context_length"] == 258_400
        assert codex["context_length_source"] == "static_default"
        assert qwen["context_length"] == 123_456
        assert qwen["context_length_source"] == "static_default"

    def test_load_cache_accepts_version_none(self, temp_dir: Path) -> None:
        """Cache loading should accept legacy payloads with null version."""
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

        catalog = ProviderModelCatalog(cache_path=cache_path)

        model = catalog.get_provider_snapshot("codex")["models"][0]
        assert model["context_length"] == 123_000
        assert model["context_length_source"] == "static_default"

    def test_get_context_window_matches_aliases_suffixes_and_droid_core(
        self, temp_dir: Path
    ) -> None:
        """Context lookup should match aliases, dated IDs, and Droid core fallbacks."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        catalog._providers = {
            "claude": {
                "models": [
                    {
                        "value": "sonnet",
                        "canonical_id": "claude-sonnet-4-6-20260410",
                        "context_length": 200_000,
                    },
                    {
                        "value": "fable",
                        "canonical_id": "claude-fable-5",
                        "context_length": 1_000_000,
                    },
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
        assert catalog.get_context_window("claude", "fable") == 1_000_000
        assert catalog.get_context_window("claude", "claude-fable-5") == 1_000_000
        assert catalog.get_context_window("claude", "claude-sonnet-4-6-20260410") == 200_000
        assert catalog.get_context_window("claude", "claude-sonnet-4-6-20241022") == 200_000
        assert catalog.get_context_window("qwen", "qwen3-coder(openai)") == 262_144
        assert catalog.get_context_window("qwen", "qwen3-coder") == 262_144
        assert catalog.get_context_window("droid", "gpt-5.5") == 321_000
        assert catalog.get_context_window("droid", "gpt-5.4") == 333_000
        assert catalog.get_context_window("droid", "claude-fable-5") == 1_000_000
        assert catalog.get_context_window("droid", "z-ai/glm-5") == 128_000
        assert catalog.get_context_window("droid", "custom/byok-model") is None

    def test_droid_catalog_precedes_underlying_static_default(self, temp_dir: Path) -> None:
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        catalog._providers = {"droid": {"models": []}, "codex": {"models": []}}

        resolved = catalog.get_context_window_with_source("droid", "gpt-5.4")

        assert resolved is not None
        assert resolved.value == 200_000
        assert resolved.source == "provider_catalog"

    def test_live_snapshot_order_and_metadata_are_preserved(self, temp_dir: Path) -> None:
        """Live discovery owns catalog model order and metadata."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        catalog._providers = {
            "claude": {
                "source": "live",
                "models": [
                    {
                        "value": "opus",
                        "label": "Opus Live",
                        "context_length": 321_000,
                    },
                    {"value": "sonnet", "label": "Sonnet Live", "context_length": 200_000},
                    {"value": "haiku", "label": "Haiku Live", "context_length": 200_000},
                ],
            },
        }

        snapshot = catalog.get_provider_snapshot("claude")
        models = snapshot["models"]

        assert [model["value"] for model in models] == ["opus", "sonnet", "haiku"]
        assert models[0]["label"] == "Opus Live"
        assert models[0]["context_length"] == 321_000

    @pytest.mark.asyncio
    async def test_refresh_uses_static_droid_without_prior_cache(self, temp_dir: Path) -> None:
        """Droid keeps a static catalog when discovery fails before cache exists."""
        cache_path = temp_dir / "provider-model-catalog.json"
        catalog = ProviderModelCatalog(cache_path=cache_path)

        with (
            patch.object(
                catalog,
                "_discover_provider_models",
                new=AsyncMock(side_effect=FileNotFoundError("provider CLI not found in PATH")),
            ),
            patch.object(
                catalog,
                "_discover_grok_models_with_source",
                new=AsyncMock(return_value=([{"value": "grok-build"}], "static")),
            ),
            patch.object(catalog, "_get_cli_version", new=AsyncMock(return_value=None)),
        ):
            status = await catalog.refresh()

        assert status["claude"]["source"] == "failed"
        assert "gemini" not in status
        assert status["droid"]["source"] == "static"
        assert status["droid"]["model_count"] == 26
        assert status["grok"]["source"] == "static"
        assert status["codex"]["source"] == "failed"

        droid = {
            model["value"]: model for model in catalog.get_provider_snapshot("droid")["models"]
        }
        gemini_flash = droid["gemini-3.5-flash"]
        assert gemini_flash["label"] == "Gemini 3.5 Flash"
        assert gemini_flash["context_length"] == 1_048_576
        assert gemini_flash["reasoning"] == {
            "supported_efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
        }

    @pytest.mark.asyncio
    async def test_discover_acp_models_marks_client_as_model_discovery(
        self, temp_dir: Path
    ) -> None:
        """ACP discovery clients should run from the trusted model-discovery cwd."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        client = MagicMock()
        order: list[str] = []

        async def start() -> None:
            order.append("start")

        client.start = AsyncMock(side_effect=start)
        client.stop = AsyncMock()
        client.session_info = {
            "models": {
                "availableModels": [
                    {"modelId": "qwen-test", "name": "Qwen Test"},
                ]
            }
        }

        client_cls = MagicMock(return_value=client)
        client_cls.cli_name = "qwen"
        gobby_home = temp_dir / "gobby-home"
        expected_cwd = (gobby_home / "provider-model-discovery" / "qwen").resolve()

        async def record_trust(_cli: str, _cwd: Path) -> None:
            order.append("trust")

        with (
            patch.dict("os.environ", {"GOBBY_HOME": str(gobby_home)}, clear=False),
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/bin/qwen"),
            patch(
                "gobby.servers.provider_models.authorize_model_discovery_trust",
                new=AsyncMock(side_effect=record_trust),
            ) as authorize_trust,
            patch("gobby.agents.trust.pre_approve_directory") as pre_approve,
        ):
            models = await catalog._discover_acp_models(client_cls=client_cls)

        client_cls.assert_called_once()
        _, kwargs = client_cls.call_args
        assert kwargs["purpose"] == "model-discovery"
        assert Path(kwargs["cwd"]) == expected_cwd
        assert Path(kwargs["cwd"]).is_absolute()
        assert kwargs["request_timeout"] > 30.0
        authorize_trust.assert_awaited_once_with("qwen", expected_cwd)
        pre_approve.assert_not_called()
        assert order == ["trust", "start"]
        assert client_cls.call_count == 1
        assert client_cls.call_args is not None
        client.start.assert_awaited_once()
        assert client.start.await_count == 1
        assert client.start.await_args is not None
        client.stop.assert_awaited_once()
        assert client.stop.await_count == 1
        assert client.stop.await_args is not None
        assert models == [{"value": "qwen-test", "label": "Qwen Test"}]

    @pytest.mark.asyncio
    async def test_discover_codex_models_logs_stop_failure_without_masking_list_error(
        self, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        client = MagicMock()
        client.is_connected = False
        client.start = AsyncMock()
        client.list_models = AsyncMock(side_effect=RuntimeError("list failed"))
        client.stop = AsyncMock(side_effect=RuntimeError("stop failed"))

        with (
            caplog.at_level(logging.ERROR, logger="gobby.servers.provider_model_discovery"),
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/bin/codex"),
            pytest.raises(RuntimeError, match="list failed"),
        ):
            await catalog._discover_codex_models(codex_client=client)

        assert "Failed to stop Codex model discovery client" in caplog.text

    @pytest.mark.asyncio
    async def test_discover_acp_models_logs_stop_failure_without_masking_start_error(
        self, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        client = MagicMock()
        client.start = AsyncMock(side_effect=RuntimeError("start failed"))
        client.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        client_cls = MagicMock(return_value=client)
        client_cls.cli_name = "qwen"

        with (
            caplog.at_level(logging.ERROR, logger="gobby.servers.provider_models"),
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/bin/qwen"),
            patch(
                "gobby.servers.provider_models.authorize_model_discovery_trust",
                new=AsyncMock(),
            ),
            pytest.raises(RuntimeError, match="start failed"),
        ):
            await catalog._discover_acp_models(client_cls=client_cls)

        assert "Failed to stop qwen model discovery client" in caplog.text

    @pytest.mark.asyncio
    async def test_discover_acp_models_removes_created_cwd_when_trust_fails(
        self, temp_dir: Path
    ) -> None:
        """A newly created discovery cwd should be removed when trust fails."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        client_cls = MagicMock()
        client_cls.cli_name = "qwen"
        gobby_home = temp_dir / "gobby-home"
        expected_cwd = (gobby_home / "provider-model-discovery" / "qwen").resolve()

        with (
            patch.dict("os.environ", {"GOBBY_HOME": str(gobby_home)}, clear=False),
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/bin/qwen"),
            patch(
                "gobby.servers.provider_models.authorize_model_discovery_trust",
                new=AsyncMock(side_effect=PermissionError("not trusted")),
            ),
        ):
            with pytest.raises(PermissionError, match="not trusted"):
                await catalog._discover_acp_models(client_cls=client_cls)

        assert not expected_cwd.exists()
        client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_acp_models_logs_cleanup_failure_and_reraises_auth_error(
        self, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Cleanup failures are logged while the authorization error remains primary."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        client_cls = MagicMock()
        client_cls.cli_name = "qwen"
        gobby_home = temp_dir / "gobby-home"

        def fail_rmtree(_path: Path) -> None:
            raise OSError("cleanup failed")

        with (
            caplog.at_level(logging.ERROR, logger="gobby.servers.provider_models"),
            patch.dict("os.environ", {"GOBBY_HOME": str(gobby_home)}, clear=False),
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/bin/qwen"),
            patch(
                "gobby.servers.provider_models.authorize_model_discovery_trust",
                new=AsyncMock(side_effect=PermissionError("not trusted")),
            ),
            patch("gobby.servers.provider_models.shutil.rmtree", side_effect=fail_rmtree),
        ):
            with pytest.raises(PermissionError, match="not trusted") as exc_info:
                await catalog._discover_acp_models(client_cls=client_cls)

        assert exc_info.value.__cause__ is None
        assert "Failed to remove qwen model-discovery cwd" in caplog.text
        client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_acp_models_preserves_existing_cwd_when_trust_fails(
        self, temp_dir: Path
    ) -> None:
        """Existing discovery cwd directories should survive trust failures."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        client_cls = MagicMock()
        client_cls.cli_name = "qwen"
        gobby_home = temp_dir / "gobby-home"
        expected_cwd = (gobby_home / "provider-model-discovery" / "qwen").resolve()
        expected_cwd.mkdir(parents=True)

        with (
            patch.dict("os.environ", {"GOBBY_HOME": str(gobby_home)}, clear=False),
            patch("gobby.servers.provider_models.shutil.which", return_value="/usr/bin/qwen"),
            patch(
                "gobby.servers.provider_models.authorize_model_discovery_trust",
                new=AsyncMock(side_effect=PermissionError("not trusted")),
            ),
        ):
            with pytest.raises(PermissionError, match="not trusted"):
                await catalog._discover_acp_models(client_cls=client_cls)

        assert expected_cwd.exists()
        client_cls.assert_not_called()

    def test_load_cache_ignores_unsupported_version(self, temp_dir: Path) -> None:
        """Unsupported cache versions should be ignored instead of loaded."""
        cache_path = temp_dir / "provider-model-catalog.json"
        cache_path.write_text(
            json.dumps({"version": 99, "providers": {"codex": {"models": [{"value": "gpt-5.4"}]}}}),
            encoding="utf-8",
        )

        catalog = ProviderModelCatalog(cache_path=cache_path)

        assert catalog.status_snapshot()["codex"]["model_count"] == 0

    @pytest.mark.asyncio
    async def test_discover_qwen_models_merges_acp_and_configured_models(
        self, temp_dir: Path
    ) -> None:
        """Qwen discovery should merge ACP models with configured settings models."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

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
            {"value": "coder-model(qwen-oauth)", "label": "Qwen Coder (OAuth)"},
            {"value": "gpt-5(openai)", "label": "gpt-5"},
            {
                "value": "claude-sonnet-4-5(anthropic)",
                "label": "claude-sonnet-4-5",
            },
        ]
        assert len(models) == 3

    def test_normalize_qwen_model_labels_only_disambiguates_duplicate_base_ids(
        self, temp_dir: Path
    ) -> None:
        """Qwen labels should add provider suffixes only for duplicate base IDs."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

        normalized = catalog._normalize_qwen_model_labels(
            [
                {"value": "qwen3-coder(qwen-oauth)", "label": "qwen3-coder"},
                {"value": "gpt-5(openai)", "label": "gpt-5"},
                {"value": "gpt-5(anthropic)", "label": "gpt-5"},
            ]
        )

        assert normalized == [
            {"value": "qwen3-coder(qwen-oauth)", "label": "qwen3-coder"},
            {"value": "gpt-5(openai)", "label": "gpt-5 (openai)"},
            {"value": "gpt-5(anthropic)", "label": "gpt-5 (anthropic)"},
        ]

    def test_normalize_qwen_model_labels_relabels_known_cli_aliases(self, temp_dir: Path) -> None:
        """The opaque qwen-code "coder-model" alias gets a friendly picker label."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

        normalized = catalog._normalize_qwen_model_labels(
            [
                {"value": "coder-model(qwen-oauth)", "label": "coder-model"},
                {"value": "gemma-4-31b-q8-local(openai)", "label": "Gemma 4 31B Q8 (LM Studio)"},
            ]
        )

        assert normalized == [
            {"value": "coder-model(qwen-oauth)", "label": "Qwen Coder (OAuth)"},
            {"value": "gemma-4-31b-q8-local(openai)", "label": "Gemma 4 31B Q8 (LM Studio)"},
        ]

    @pytest.mark.asyncio
    async def test_discover_qwen_models_can_fall_back_to_settings_catalog(
        self, temp_dir: Path
    ) -> None:
        """Qwen discovery should fall back to configured settings when ACP fails."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

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
        assert models[0]["label"] == "gpt-5"

    @pytest.mark.asyncio
    async def test_discover_droid_models_returns_static_catalog(self, temp_dir: Path) -> None:
        """Droid provider discovery should return the bundled static model catalog."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

        models = await catalog._discover_provider_models("droid")

        assert {model["value"] for model in models} == {
            "claude-fable-5",
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
            "gemini-3.5-flash",
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
        assert len(models) == 26

        by_id = {model["value"]: model for model in models}
        assert by_id["claude-fable-5"]["label"] == "Claude Fable 5"
        assert by_id["claude-fable-5"]["context_length"] == 1_000_000
        assert by_id["claude-fable-5"]["context_length_source"] == "provider_catalog"
        assert by_id["claude-fable-5"]["reasoning"] == {
            "supported_efforts": ["off", "low", "medium", "high", "xhigh", "max"],
            "default_effort": "high",
        }
        assert "xhigh" in by_id["claude-opus-4-7"]["reasoning"]["supported_efforts"]
        assert "max" in by_id["claude-opus-4-7"]["reasoning"]["supported_efforts"]
        assert by_id["gemini-3.5-flash"]["context_length"] == 1_048_576
        assert by_id["gemini-3.5-flash"]["reasoning"] == {
            "supported_efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
        }
        assert "minimal" in by_id["gemini-3-flash-preview"]["reasoning"]["supported_efforts"]
        assert by_id["minimax-m2.7"]["reasoning"]["supported_efforts"] == ["high"]
        assert {
            model_id: by_id[model_id]["context_length"]
            for model_id in ("minimax-m2.7", "minimax-m2.5", "kimi-k2.6", "kimi-k2.5")
        } == {
            "minimax-m2.7": 204_800,
            "minimax-m2.5": 204_800,
            "kimi-k2.6": 262_144,
            "kimi-k2.5": 262_144,
        }
        assert all(model.get("context_length") is not None for model in models)
        for model_id in ("glm-5.1", "glm-5", "glm-4.7"):
            assert by_id[model_id].get("reasoning", {}).get("supported_efforts", []) == []

    @pytest.mark.asyncio
    async def test_discover_grok_models_uses_cache_before_static_fallback(
        self, temp_dir: Path
    ) -> None:
        """Grok discovery should fall back to ~/.grok/models_cache.json, then static catalog."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")
        grok_home = temp_dir / ".grok"
        grok_home.mkdir()
        (grok_home / "models_cache.json").write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "modelId": "grok-cache",
                            "name": "Grok Cache",
                            "_meta": {"totalContextTokens": 123456},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("gobby.servers.provider_models.shutil.which", return_value=None),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            source_models, source = await catalog._discover_grok_models_with_source()
            models = await catalog._discover_grok_models()

        assert source == "cache"
        assert source_models == models
        assert models == [
            {
                "value": "grok-cache",
                "label": "Grok Cache",
                "context_length": 123456,
                "context_length_source": "provider_catalog",
            }
        ]

        (grok_home / "models_cache.json").unlink()
        with (
            patch("gobby.servers.provider_models.shutil.which", return_value=None),
            patch.object(Path, "home", return_value=temp_dir),
        ):
            source_models, source = await catalog._discover_grok_models_with_source()
            static_models = await catalog._discover_grok_models()

        assert source == "static"
        assert source_models == static_models
        by_id = {model["value"]: model for model in static_models}
        assert [model["value"] for model in static_models] == [
            "grok-composer-2.5-fast",
            "grok-build",
        ]
        assert by_id["grok-composer-2.5-fast"]["context_length"] == 200_000
        assert by_id["grok-composer-2.5-fast"]["is_default"] is True
        assert by_id["grok-composer-2.5-fast"]["reasoning"]["supported_efforts"] == [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
        assert by_id["grok-build"]["context_length"] == 512_000
        assert by_id["grok-build"]["reasoning"]["supported_efforts"] == [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
        assert static_models[0]["context_length_source"] == "static_default"

    @pytest.mark.asyncio
    async def test_refresh_uses_static_agy_catalog_without_live_discovery(
        self, temp_dir: Path
    ) -> None:
        """AGY should expose static one-shot models while live discovery stays disabled."""
        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

        with (
            patch.object(catalog, "_discover_provider_models", new=AsyncMock(return_value=[])),
            patch.object(
                catalog,
                "_discover_grok_models_with_source",
                new=AsyncMock(return_value=([{"value": "grok-build"}], "static")),
            ),
            patch.object(catalog, "_get_cli_version", new=AsyncMock(return_value=None)),
        ):
            status = await catalog.refresh()

        assert status["agy"]["source"] == "static"
        assert status["agy"]["model_count"] == len(AGY_MODELS)
        assert "machine transport" in (status["agy"]["error"] or "")
        snapshot = catalog.get_provider_snapshot("agy")
        assert [model["value"] for model in snapshot["models"]] == list(AGY_MODELS)

    def test_static_agy_display_strings_match_captured_agy_models_fixture(self) -> None:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/provider_contracts/agy/agy_models_v1.0.10.txt"
        )
        captured = {
            line.strip()
            for line in fixture.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        catalog_displays = {
            display for model in AGY_MODELS.values() for display in model["effort_display"].values()
        }

        assert catalog_displays <= captured

    @pytest.mark.integration
    @pytest.mark.skipif(
        os.environ.get("GOBBY_RUN_AGY_MODELS_LIVE") != "1",
        reason="set GOBBY_RUN_AGY_MODELS_LIVE=1 to check live agy models",
    )
    def test_static_agy_display_strings_exist_in_live_agy_models(self) -> None:
        agy = shutil.which("agy")
        if agy is None:
            pytest.skip("agy CLI not installed")
        version_result = subprocess.run(
            [agy, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version_output = f"{version_result.stdout}\n{version_result.stderr}"
        if re.search(r"(?<![\d.])1\.0\.10(?![\d.])", version_output) is None:
            pytest.skip(f"agy models fixture is pinned to 1.0.10; found {version_output.strip()}")
        result = subprocess.run(
            [agy, "models"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        catalog_displays = {
            display for model in AGY_MODELS.values() for display in model["effort_display"].values()
        }

        assert all(display in result.stdout for display in catalog_displays)

    def test_load_qwen_settings_merges_global_and_project_files(self, temp_dir: Path) -> None:
        """Qwen settings should merge global providers with project overrides."""
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

        catalog = ProviderModelCatalog(cache_path=temp_dir / "provider-model-catalog.json")

        with (
            patch.object(Path, "home", return_value=temp_dir),
            patch.object(Path, "cwd", return_value=project_dir),
        ):
            settings = catalog._load_qwen_settings()

        assert settings["security"]["auth"]["selectedType"] == "anthropic"
        assert settings["modelProviders"]["openai"][0]["id"] == "gpt-5"
        assert settings["modelProviders"]["anthropic"][0]["id"] == "claude-sonnet-4-5"


class TestModelDiscoveryCwdPath:
    """Path-traversal prevention for provider-scoped model-discovery dirs."""

    @pytest.mark.parametrize(
        "provider",
        ["", "   ", ".", "..", " .. ", "/", "\\", "../escape", "/abs/path", "a/b", "a\\b"],
    )
    def test_rejects_traversal_and_empty_inputs(self, provider: str) -> None:
        with pytest.raises(ValueError, match="Invalid provider model-discovery directory"):
            _model_discovery_cwd_path(provider)

    @pytest.mark.parametrize(
        ("provider", "expected_dir"),
        [("qwen", "qwen"), ("Grok", "grok"), ("  QWEN  ", "qwen")],
    )
    def test_accepts_valid_provider_and_normalizes(self, provider: str, expected_dir: str) -> None:
        result = _model_discovery_cwd_path(provider)

        assert result.name == expected_dir
        assert ".." not in result.parts
