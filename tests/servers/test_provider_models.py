"""Tests for live provider model discovery and cache fallback."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

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
                new=AsyncMock(side_effect=["1.0.12", "0.37.1", "0.118.0"]),
            ),
        ):
            status = await catalog.refresh()

        assert status["claude"]["source"] == "live"
        assert status["gemini"]["source"] == "live"
        assert status["codex"]["source"] == "cache"
        assert status["codex"]["model_count"] == 1
        assert status["codex"]["error"] == "codex probe failed"

        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["providers"]["codex"]["source"] == "cache"

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
