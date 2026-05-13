"""Live provider model discovery with JSON cache fallback."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.adapters.acp_client import ACPClient
from gobby.agents.trust import pre_approve_directory
from gobby.config.app import deep_merge
from gobby.servers.provider_model_defaults import DROID_MODEL_CATALOG as _DROID_MODEL_CATALOG

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient
    from gobby.config.app import DaemonConfig

logger = logging.getLogger(__name__)

_PROVIDERS = ("claude", "gemini", "qwen", "codex", "droid")
_CACHE_VERSION = 3
_DEFAULT_CACHE_FILE = "provider-model-catalog.json"
_MODEL_DISCOVERY_CWD_NAME = "provider-model-discovery"
_MODEL_DISCOVERY_REQUEST_TIMEOUT_SECONDS = 90.0
_CLAUDE_ALIASES = (("haiku", "Haiku"), ("sonnet", "Sonnet"), ("opus", "Opus"))
_CLAUDE_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_QWEN_AUTH_TYPES = frozenset({"qwen-oauth", "openai", "anthropic", "gemini", "vertex-ai"})
_KNOWN_PROVIDER_PREFIXES = (
    "anthropic/",
    "openai/",
    "google/",
    "qwen/",
    "z-ai/",
    "moonshotai/",
    "minimax/",
)
_STATIC_CONTEXT_LENGTHS: dict[str, int] = {
    "opus": 1_000_000,
    "sonnet": 200_000,
    "haiku": 200_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-6-fast": 1_000_000,
    "claude-opus-4-5": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-5.5": 200_000,
    "gpt-5.4": 200_000,
    "gpt-5.4-fast": 200_000,
    "gpt-5.4-mini": 200_000,
    "gpt-5.3-codex": 200_000,
    "gpt-5.3-codex-fast": 200_000,
    "gpt-5.3-codex-spark": 200_000,
    "gpt-5.2": 200_000,
    "gpt-5.2-codex": 200_000,
    "gpt-5.1-codex-max": 200_000,
    "gemini-3.1-pro-preview": 1_000_000,
    "gemini-3-flash-preview": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "qwen3-coder": 262_144,
    "qwen3-coder-plus": 262_144,
    "qwen3-coder-flash": 262_144,
}


def _coerce_context_length(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.replace("_", ""))
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _strip_known_provider_prefix(value: str) -> str:
    normalized = value.strip()
    lower = normalized.lower()
    for prefix in _KNOWN_PROVIDER_PREFIXES:
        if lower.startswith(prefix):
            return normalized.split("/", 1)[1]
    return normalized


def _strip_qwen_auth_suffix(value: str) -> str:
    trimmed = value.strip()
    close_idx = trimmed.rfind(")")
    open_idx = trimmed.rfind("(")
    if open_idx >= 0 and close_idx == len(trimmed) - 1 and open_idx < close_idx:
        model_id = trimmed[:open_idx].strip()
        auth_type = trimmed[open_idx + 1 : close_idx].strip()
        if model_id and auth_type in _QWEN_AUTH_TYPES:
            return model_id
    return trimmed


def _normalize_model_lookup_id(value: str) -> str:
    return _strip_qwen_auth_suffix(_strip_known_provider_prefix(value)).lower()


def _context_key_allowed_for_provider(provider: str | None, key: str) -> bool:
    if key in {"opus", "sonnet", "haiku"}:
        return provider in {None, "claude", "droid"}
    if key.startswith("qwen3-coder"):
        return provider in {None, "qwen"}
    return True


def context_length_for_model(provider: str | None, model: str | None) -> int | None:
    """Return a static catalog context length for known shipped models."""
    if not model:
        return None

    normalized_provider = provider.strip().lower() if isinstance(provider, str) else None
    normalized_model = _normalize_model_lookup_id(model)
    exact = _STATIC_CONTEXT_LENGTHS.get(normalized_model)
    if exact is not None and _context_key_allowed_for_provider(
        normalized_provider, normalized_model
    ):
        return exact

    best_len = 0
    best_value: int | None = None
    for key, value in _STATIC_CONTEXT_LENGTHS.items():
        if not _context_key_allowed_for_provider(normalized_provider, key):
            continue
        if normalized_model.startswith(key) and len(key) > best_len:
            best_len = len(key)
            best_value = value
    return best_value


def _extract_context_length(model: dict[str, Any]) -> int | None:
    for key in (
        "context_length",
        "contextLength",
        "contextWindow",
        "inputTokenLimit",
        "maxInputTokens",
    ):
        context_length = _coerce_context_length(model.get(key))
        if context_length is not None:
            return context_length

    top_provider = model.get("top_provider")
    if isinstance(top_provider, dict):
        return _coerce_context_length(top_provider.get("context_length"))
    return None


def _model_identifiers(model: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    for key in ("value", "canonical_id", "id", "model"):
        value = model.get(key)
        if isinstance(value, str) and value.strip():
            identifiers.append(value)
    match_identifiers = model.get("match_identifiers")
    if isinstance(match_identifiers, list):
        identifiers.extend(str(value) for value in match_identifiers if str(value).strip())
    return identifiers


def _normalize_model_entry(provider: str, model: dict[str, Any]) -> dict[str, Any]:
    entry = copy.deepcopy(model)
    context_length = _extract_context_length(entry)
    if context_length is None:
        for identifier in _model_identifiers(entry):
            context_length = context_length_for_model(provider, identifier)
            if context_length is not None:
                break
    if context_length is not None:
        entry["context_length"] = context_length
    return entry


def with_context_lengths(provider: str, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return provider model entries enriched with known context lengths."""
    return [_normalize_model_entry(provider, model) for model in models if isinstance(model, dict)]


def _cached_models(provider: str, models: Any) -> list[dict[str, Any]]:
    if not isinstance(models, list):
        return []
    # _cached_models keeps cached lists consistent: if every entry already has
    # _extract_context_length metadata, return it; otherwise enrich all entries
    # with with_context_lengths.
    if all(isinstance(model, dict) and _extract_context_length(model) for model in models):
        return copy.deepcopy(models)
    return with_context_lengths(provider, models)


DROID_MODEL_CATALOG: list[dict[str, Any]] = with_context_lengths("droid", _DROID_MODEL_CATALOG)


def _gobby_home() -> Path:
    raw = os.environ.get("GOBBY_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".gobby"


def _model_discovery_cwd() -> Path:
    cwd = _gobby_home() / _MODEL_DISCOVERY_CWD_NAME
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd.resolve()


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
    by_value: dict[str, dict[str, Any]] = {}

    for item in [*primary, *secondary]:
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        if value in by_value:
            existing = by_value[value]
            for key, field_value in item.items():
                if key not in existing:
                    existing[key] = copy.deepcopy(field_value)
            continue
        entry = copy.deepcopy(item)
        by_value[value] = entry
        merged.append(entry)

    return merged


def _configured_models_for_provider(
    config: DaemonConfig | None, provider: str
) -> list[dict[str, Any]]:
    providers = getattr(config, "llm_providers", None)
    provider_config = getattr(providers, provider, None) if providers is not None else None
    fields_set = getattr(providers, "model_fields_set", None)
    if fields_set is not None and provider not in fields_set:
        return []
    if provider_config is None or not hasattr(provider_config, "get_models_list"):
        return []
    return [{"value": model} for model in provider_config.get_models_list()]


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
        if payload.get("version") not in (None, 2, _CACHE_VERSION):
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
                "models": _cached_models(provider, models),
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
        if isinstance(models, list):
            models = _merge_models(_configured_models_for_provider(self._config, provider), models)
        else:
            models = _configured_models_for_provider(self._config, provider)
        return {
            "source": entry.get("source", "failed"),
            "cli_version": entry.get("cli_version"),
            "error": entry.get("error"),
            "models": with_context_lengths(provider, models),
            "generated_at": entry.get("generated_at") or self._generated_at,
        }

    def get_context_window(self, provider: str | None, model: str | None) -> int | None:
        """Resolve context length from provider catalog metadata."""
        if not model:
            return None

        normalized_provider = provider.strip().lower() if isinstance(provider, str) else None
        if not normalized_provider:
            for candidate_provider in _PROVIDERS:
                context_length = self._get_provider_context_window(candidate_provider, model)
                if context_length is not None:
                    return context_length
            return None

        if normalized_provider == "droid":
            context_length = self._get_provider_context_window(
                normalized_provider, model, include_static=False
            )
            if context_length is not None:
                return context_length
            for underlying_provider in self._droid_underlying_providers(model):
                context_length = self._get_provider_context_window(underlying_provider, model)
                if context_length is not None:
                    return context_length
            return self._get_provider_context_window(normalized_provider, model)

        return self._get_provider_context_window(normalized_provider, model)

    def _get_provider_context_window(
        self, provider: str, model: str, *, include_static: bool = True
    ) -> int | None:
        target = _normalize_model_lookup_id(model)
        entry = self._providers.get(provider, {})
        models = entry.get("models")
        best_len = 0
        best_context: int | None = None

        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                match = self._match_candidate_and_context(provider, item, target)
                if match is None:
                    continue
                candidate_len, context_length = match
                if candidate_len > best_len:
                    best_len = candidate_len
                    best_context = context_length

        if best_context is not None:
            return best_context
        return context_length_for_model(provider, model) if include_static else None

    def _match_candidate_and_context(
        self, provider: str, item: dict[str, Any], target: str
    ) -> tuple[int, int] | None:
        context_length = _extract_context_length(item)
        if context_length is None:
            for identifier in _model_identifiers(item):
                context_length = context_length_for_model(provider, identifier)
                if context_length is not None:
                    break
        if context_length is None:
            return None
        candidate_len = 0
        for candidate in self._entry_lookup_candidates(provider, item):
            if (
                target == candidate
                or target.startswith(candidate)
                or self._alias_matches(provider, candidate, target)
            ):
                candidate_len = max(candidate_len, len(candidate))
        if not candidate_len:
            return None
        return candidate_len, context_length

    def _entry_lookup_candidates(self, provider: str, model: dict[str, Any]) -> set[str]:
        candidates = {
            normalized
            for identifier in _model_identifiers(model)
            if (normalized := _normalize_model_lookup_id(identifier))
        }
        if provider == "claude":
            for candidate in tuple(candidates):
                if candidate.startswith("claude-opus"):
                    candidates.add("opus")
                elif candidate.startswith("claude-sonnet"):
                    candidates.add("sonnet")
                elif candidate.startswith("claude-haiku"):
                    candidates.add("haiku")
        return candidates

    def _alias_matches(self, provider: str, candidate: str, target: str) -> bool:
        families = {"opus", "sonnet", "haiku"}
        return provider in {"claude", "droid"} and candidate in families and candidate in target

    @staticmethod
    def _droid_underlying_providers(model: str) -> tuple[str, ...]:
        normalized = _normalize_model_lookup_id(model)
        if normalized.startswith("claude-") or any(
            family in normalized for family in ("opus", "sonnet", "haiku")
        ):
            return ("claude",)
        if normalized.startswith(("gpt-", "o1-", "o3-", "o4-")):
            return ("codex",)
        if normalized.startswith("gemini-"):
            return ("gemini",)
        return ()

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
                discovered_models = await self._discover_provider_models(
                    provider, codex_client=codex_client
                )
                models = with_context_lengths(provider, discovered_models)
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
                        "models": with_context_lengths(
                            provider,
                            previous.get("models", []),
                        ),
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
        if provider == "droid":
            return copy.deepcopy(DROID_MODEL_CATALOG)
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
            context_length = _extract_context_length(item)
            if context_length is not None:
                entry["context_length"] = context_length
            reasoning = _extract_reasoning(item)
            if reasoning:
                entry["reasoning"] = reasoning
            models.append(entry)

        return models

    async def _discover_gemini_models(self) -> list[dict[str, Any]]:
        from gobby.adapters.gemini_acp_client import GeminiACPClient

        return await self._discover_acp_models(client_cls=GeminiACPClient)

    async def _discover_qwen_models(self) -> list[dict[str, Any]]:
        from gobby.adapters.qwen_acp_client import QwenACPClient

        if not shutil.which(QwenACPClient.cli_name):
            raise FileNotFoundError("qwen CLI not found in PATH")

        acp_error: Exception | None = None
        try:
            acp_models = await self._discover_acp_models(client_cls=QwenACPClient)
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
        base_id_counts = Counter(
            model_id
            for model in models
            if (model_id := _split_qwen_model_value(str(model.get("value") or ""))[0])
        )
        if not any(count > 1 for count in base_id_counts.values()):
            return models

        normalized: list[dict[str, Any]] = []
        for model in models:
            entry = copy.deepcopy(model)
            value = str(entry.get("value") or "")
            model_id, auth_type = _split_qwen_model_value(value)
            if not auth_type or base_id_counts[model_id] <= 1:
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
        client_cls: type[ACPClient],
    ) -> list[dict[str, Any]]:
        if not shutil.which(client_cls.cli_name):
            raise FileNotFoundError(f"{client_cls.cli_name} CLI not found in PATH")

        cwd = _model_discovery_cwd()
        # Tracked by gobby-#14568: replace this temporary process-wide pre-approval
        # with provider-scoped model-discovery authorization.
        pre_approve_directory(client_cls.cli_name, cwd)
        client = client_cls(
            cwd=os.fspath(cwd),
            purpose="model-discovery",
            request_timeout=_MODEL_DISCOVERY_REQUEST_TIMEOUT_SECONDS,
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
            context_length = _extract_context_length(item)
            if context_length is not None:
                entry["context_length"] = context_length
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
            "context_length": context_length_for_model("claude", str(canonical_id)),
            "reasoning": {"supported_efforts": list(_CLAUDE_REASONING_EFFORTS)},
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
