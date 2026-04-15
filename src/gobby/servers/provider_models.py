"""Live provider model discovery with JSON cache fallback."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.config.app import deep_merge

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient
    from gobby.config.app import DaemonConfig

logger = logging.getLogger(__name__)

_PROVIDERS = ("claude", "gemini", "qwen", "codex")
_CACHE_VERSION = 1
_DEFAULT_CACHE_FILE = "provider-model-catalog.json"
_CLAUDE_ALIASES: tuple[tuple[str, str], ...] = (
    ("haiku", "Haiku"),
    ("sonnet", "Sonnet"),
    ("opus", "Opus"),
)
_QWEN_AUTH_TYPES = frozenset({"qwen-oauth", "openai", "anthropic", "gemini", "vertex-ai"})


def _gobby_home() -> Path:
    raw = os.environ.get("GOBBY_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".gobby"


def _short_error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:240]


def _extract_reasoning(model: dict[str, Any]) -> dict[str, Any] | None:
    supported: list[str] = []

    supported_reasoning = model.get("supportedReasoningEfforts")
    if isinstance(supported_reasoning, list):
        supported = [
            str(item.get("reasoningEffort"))
            for item in supported_reasoning
            if isinstance(item, dict) and item.get("reasoningEffort")
        ]
    elif isinstance(model.get("reasoningEfforts"), list):
        supported = [str(item) for item in model["reasoningEfforts"] if item]

    default_effort = model.get("defaultReasoningEffort") or model.get("defaultReasoningMode")
    if default_effort is None and not supported:
        return None

    result: dict[str, Any] = {"supported_efforts": supported}
    if default_effort is not None:
        result["default_effort"] = str(default_effort)
    return result


def _format_qwen_model_value(model_id: str, auth_type: str | None) -> str:
    if not auth_type:
        return model_id
    return f"{model_id}({auth_type})"


def _split_qwen_model_value(value: str) -> tuple[str, str | None]:
    trimmed = value.strip()
    close_idx = trimmed.rfind(")")
    open_idx = trimmed.rfind("(")
    if open_idx >= 0 and close_idx == len(trimmed) - 1 and open_idx < close_idx:
        model_id = trimmed[:open_idx].strip()
        auth_type = trimmed[open_idx + 1 : close_idx].strip()
        if model_id and auth_type in _QWEN_AUTH_TYPES:
            return model_id, auth_type
    return trimmed, None


def _merge_models(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_values: set[str] = set()

    for item in [*primary, *secondary]:
        value = str(item.get("value") or "").strip()
        if not value or value in seen_values:
            continue
        seen_values.add(value)
        merged.append(copy.deepcopy(item))

    return merged


class ProviderModelCatalog:
    """Caches provider model discovery for route and status consumers."""

    def __init__(
        self,
        config: DaemonConfig | None,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self._config = config
        self._cache_path = cache_path or (_gobby_home() / _DEFAULT_CACHE_FILE)
        self._providers: dict[str, dict[str, Any]] = {}
        self._generated_at: str | None = None
        self.load_cache()

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def load_cache(self) -> None:
        """Load the last-good snapshot from disk."""
        self._providers = {}
        self._generated_at = None

        if not self._cache_path.exists():
            return

        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read provider model cache %s: %s", self._cache_path, exc)
            return

        if not isinstance(payload, dict):
            return
        if payload.get("version") not in (None, _CACHE_VERSION):
            logger.warning(
                "Ignoring unsupported provider model cache version: %s", payload.get("version")
            )
            return

        providers = payload.get("providers")
        if not isinstance(providers, dict):
            return

        normalized: dict[str, dict[str, Any]] = {}
        for provider in _PROVIDERS:
            entry = providers.get(provider)
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            normalized[provider] = {
                "source": str(entry.get("source") or "cache"),
                "cli_version": entry.get("cli_version"),
                "error": entry.get("error"),
                "models": copy.deepcopy(models) if isinstance(models, list) else [],
                "generated_at": entry.get("generated_at"),
            }

        self._providers = normalized
        generated_at = payload.get("generated_at")
        self._generated_at = str(generated_at) if generated_at else None

    def _write_cache(self) -> None:
        payload = {
            "version": _CACHE_VERSION,
            "generated_at": self._generated_at,
            "providers": self._providers,
        }

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._cache_path.with_suffix(".tmp")

        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temp_path.replace(self._cache_path)
            self._cache_path.chmod(0o600)
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

    def get_provider_snapshot(self, provider: str) -> dict[str, Any]:
        """Return a single provider snapshot for routing/status."""
        entry = self._providers.get(provider, {})
        models = entry.get("models")
        return {
            "source": entry.get("source", "failed"),
            "cli_version": entry.get("cli_version"),
            "error": entry.get("error"),
            "models": copy.deepcopy(models) if isinstance(models, list) else [],
            "generated_at": entry.get("generated_at") or self._generated_at,
        }

    def status_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a compact health/status view for each provider catalog."""
        snapshot: dict[str, dict[str, Any]] = {}
        for provider in _PROVIDERS:
            entry = self.get_provider_snapshot(provider)
            snapshot[provider] = {
                "source": entry["source"],
                "cli_version": entry["cli_version"],
                "error": entry["error"],
                "model_count": len(entry["models"]),
                "generated_at": entry["generated_at"],
            }
        return snapshot

    async def refresh(
        self,
        *,
        codex_client: CodexAppServerClient | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Refresh provider catalogs, falling back to last-good cache per provider."""
        old = copy.deepcopy(self._providers)
        generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        results: dict[str, dict[str, Any]] = {}
        for provider in _PROVIDERS:
            previous = old.get(provider)
            cli_version = await self._get_cli_version(provider)
            try:
                models = await self._discover_provider_models(provider, codex_client=codex_client)
                results[provider] = {
                    "source": "live",
                    "cli_version": cli_version or (previous or {}).get("cli_version"),
                    "error": None,
                    "models": models,
                    "generated_at": generated_at,
                }
            except Exception as exc:
                error = _short_error(exc)
                if previous and previous.get("models"):
                    results[provider] = {
                        "source": "cache",
                        "cli_version": cli_version or previous.get("cli_version"),
                        "error": error,
                        "models": copy.deepcopy(previous.get("models", [])),
                        "generated_at": previous.get("generated_at") or self._generated_at,
                    }
                else:
                    results[provider] = {
                        "source": "failed",
                        "cli_version": cli_version,
                        "error": error,
                        "models": [],
                        "generated_at": generated_at,
                    }

        self._providers = results
        self._generated_at = generated_at
        self._write_cache()
        return self.status_snapshot()

    async def _discover_provider_models(
        self,
        provider: str,
        *,
        codex_client: CodexAppServerClient | None = None,
    ) -> list[dict[str, Any]]:
        if provider == "claude":
            return await self._discover_claude_models()
        if provider == "gemini":
            return await self._discover_gemini_models()
        if provider == "qwen":
            return await self._discover_qwen_models()
        if provider == "codex":
            return await self._discover_codex_models(codex_client=codex_client)
        raise ValueError(f"Unknown provider: {provider}")

    async def _discover_codex_models(
        self,
        *,
        codex_client: CodexAppServerClient | None = None,
    ) -> list[dict[str, Any]]:
        if not shutil.which("codex"):
            raise FileNotFoundError("codex CLI not found in PATH")

        client = codex_client
        owns_client = False
        if client is None or not client.is_connected:
            from gobby.adapters.codex_impl.client import CodexAppServerClient

            client = CodexAppServerClient()
            owns_client = True
            await client.start()

        assert client is not None
        try:
            raw_models = await client.list_models(include_hidden=True)
        finally:
            if owns_client:
                await client.stop()

        models: list[dict[str, Any]] = []
        for item in raw_models:
            model_id = str(item.get("model") or item.get("id") or "").strip()
            if not model_id:
                continue
            entry: dict[str, Any] = {
                "value": model_id,
                "label": str(item.get("displayName") or model_id),
                "hidden": bool(item.get("hidden", False)),
                "is_default": bool(item.get("isDefault", False)),
            }
            reasoning = _extract_reasoning(item)
            if reasoning:
                entry["reasoning"] = reasoning
            models.append(entry)

        return models

    async def _discover_gemini_models(self) -> list[dict[str, Any]]:
        return await self._discover_acp_models(provider="gemini", display_name="Gemini")

    async def _discover_qwen_models(self) -> list[dict[str, Any]]:
        if not shutil.which("qwen"):
            raise FileNotFoundError("qwen CLI not found in PATH")

        acp_error: Exception | None = None
        try:
            acp_models = await self._discover_acp_models(
                provider="qwen",
                display_name="Qwen",
                prompt_timeout_env="GOBBY_QWEN_ACP_PROMPT_TIMEOUT_SECONDS",
            )
        except Exception as exc:
            acp_models = []
            acp_error = exc

        models = _merge_models(acp_models, self._discover_qwen_configured_models())
        if models:
            return self._normalize_qwen_model_labels(models)
        if acp_error is not None:
            raise acp_error
        return []

    def _discover_qwen_configured_models(self) -> list[dict[str, Any]]:
        settings = self._load_qwen_settings()
        model_providers = settings.get("modelProviders")
        if not isinstance(model_providers, dict):
            return []

        models: list[dict[str, Any]] = []
        for auth_type, configured_models in model_providers.items():
            if auth_type not in _QWEN_AUTH_TYPES or auth_type == "qwen-oauth":
                continue
            if not isinstance(configured_models, list):
                continue
            for configured_model in configured_models:
                if not isinstance(configured_model, dict):
                    continue
                model_id = str(configured_model.get("id") or "").strip()
                if not model_id:
                    continue
                entry: dict[str, Any] = {
                    "value": _format_qwen_model_value(model_id, auth_type),
                    "label": str(configured_model.get("name") or model_id),
                }
                description = configured_model.get("description")
                if isinstance(description, str) and description.strip():
                    entry["description"] = description.strip()
                models.append(entry)
        return models

    def _load_qwen_settings(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        seen_paths: set[Path] = set()
        settings_paths = [
            Path.home() / ".qwen" / "settings.json",
            Path.cwd() / ".qwen" / "settings.json",
        ]

        for settings_path in settings_paths:
            if settings_path in seen_paths or not settings_path.exists():
                continue
            seen_paths.add(settings_path)
            try:
                payload = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read Qwen settings from %s: %s", settings_path, exc)
                continue
            if not isinstance(payload, dict):
                continue
            deep_merge(merged, payload)

        return merged

    def _normalize_qwen_model_labels(
        self,
        models: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        auth_types = {
            auth_type
            for model in models
            if (auth_type := _split_qwen_model_value(str(model.get("value") or ""))[1])
        }
        if len(auth_types) < 2:
            return models

        normalized: list[dict[str, Any]] = []
        for model in models:
            entry = copy.deepcopy(model)
            value = str(entry.get("value") or "")
            _model_id, auth_type = _split_qwen_model_value(value)
            if not auth_type:
                normalized.append(entry)
                continue
            label = str(entry.get("label") or value)
            if f"({auth_type})" not in label:
                entry["label"] = f"{label} ({auth_type})"
            normalized.append(entry)
        return normalized

    async def _discover_acp_models(
        self,
        *,
        provider: str,
        display_name: str,
        prompt_timeout_env: str = "GOBBY_GEMINI_ACP_PROMPT_TIMEOUT_SECONDS",
    ) -> list[dict[str, Any]]:
        if not shutil.which(provider):
            raise FileNotFoundError(f"{provider} CLI not found in PATH")

        from gobby.adapters.gemini_acp_client import GeminiACPClient

        client = GeminiACPClient(
            cli_name=provider,
            display_name=display_name,
            prompt_timeout_env=prompt_timeout_env,
        )
        await client.start()
        try:
            session_info = client.session_info
        finally:
            await client.stop()

        raw_models = (
            session_info.get("models", {}).get("availableModels", [])
            if isinstance(session_info, dict)
            else []
        )
        models: list[dict[str, Any]] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("modelId") or "").strip()
            if not model_id:
                continue
            entry: dict[str, Any] = {
                "value": model_id,
                "label": str(item.get("name") or model_id),
            }
            reasoning = _extract_reasoning(item)
            if reasoning:
                entry["reasoning"] = reasoning
            models.append(entry)
        return models

    async def _discover_claude_models(self) -> list[dict[str, Any]]:
        if not shutil.which("claude"):
            raise FileNotFoundError("claude CLI not found in PATH")

        probes = await asyncio.gather(
            *[self._probe_claude_model(alias, label) for alias, label in _CLAUDE_ALIASES],
            return_exceptions=True,
        )

        models: list[dict[str, Any]] = []
        errors: list[str] = []
        for probe in probes:
            if isinstance(probe, Exception):
                errors.append(_short_error(probe))
                continue
            if isinstance(probe, dict):
                models.append(probe)

        if not models:
            raise RuntimeError("; ".join(errors) or "Claude model probes failed")
        return models

    async def _probe_claude_model(self, alias: str, label: str) -> dict[str, Any]:
        env = os.environ.copy()
        env["GOBBY_HOOKS_DISABLED"] = "1"
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--print",
            "--output-format",
            "json",
            "--model",
            alias,
            "reply with ok",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45.0)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"Claude probe timed out for {alias}") from exc

        if proc.returncode != 0:
            error = stderr.decode().strip() or stdout.decode().strip() or "probe failed"
            raise RuntimeError(f"Claude {alias}: {error}")

        lines = [line for line in stdout.decode().splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(f"Claude {alias}: empty response")

        payload = json.loads(lines[-1])
        model_usage = payload.get("modelUsage")
        if not isinstance(model_usage, dict) or not model_usage:
            raise RuntimeError(f"Claude {alias}: missing modelUsage")

        canonical_id = next(iter(model_usage))
        return {
            "value": alias,
            "label": label,
            "canonical_id": str(canonical_id),
        }

    async def _get_cli_version(self, provider: str) -> str | None:
        if not shutil.which(provider):
            return None

        proc = await asyncio.create_subprocess_exec(
            provider,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None

        if proc.returncode != 0:
            return None
        output = stdout.decode().strip() or stderr.decode().strip()
        return output or None
