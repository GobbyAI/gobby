"""Live provider model discovery with JSON cache fallback."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.adapters.acp_client import ACPClient
from gobby.agents.trust import authorize_model_discovery_trust
from gobby.config.app import DaemonConfig, deep_merge
from gobby.llm.context_windows import (
    CONTEXT_LENGTH_SOURCE_KEY,
    ContextLengthCandidate,
    ContextLengthSource,
    ResolvedContextWindow,
    extract_context_length_candidate,
    normalize_model_lookup_id,
    provider_catalog_context_length_for_model,
    static_context_length_for_model,
)
from gobby.paths import get_gobby_home
from gobby.providers import provider_metadata
from gobby.servers.provider_model_defaults import AGY_MODELS as _AGY_MODELS
from gobby.servers.provider_model_defaults import DROID_MODEL_CATALOG as _DROID_MODEL_CATALOG
from gobby.servers.provider_model_discovery import (
    discover_acp_models as _discover_acp_models_impl,
)
from gobby.servers.provider_model_discovery import (
    discover_claude_models as _discover_claude_models_impl,
)
from gobby.servers.provider_model_discovery import (
    discover_codex_models as _discover_codex_models_impl,
)
from gobby.servers.provider_model_discovery import (
    discover_grok_models_with_source as _discover_grok_models_with_source_impl,
)
from gobby.servers.provider_model_discovery import (
    discover_qwen_configured_models as _discover_qwen_configured_models_impl,
)
from gobby.servers.provider_model_discovery import (
    discover_qwen_models as _discover_qwen_models_impl,
)
from gobby.servers.provider_model_discovery import (
    get_cli_version as _get_cli_version_impl,
)
from gobby.servers.provider_model_discovery import (
    load_qwen_settings as _load_qwen_settings_impl,
)
from gobby.servers.provider_model_discovery import (
    normalize_qwen_model_labels as _normalize_qwen_model_labels_impl,
)
from gobby.servers.provider_model_discovery import (
    probe_claude_model as _probe_claude_model_impl,
)
from gobby.servers.provider_models_grok import models_from_cache as grok_models_from_cache
from gobby.servers.provider_models_grok import static_models as grok_static_models

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

logger = logging.getLogger(__name__)

_PROVIDER_METADATA = {entry.provider: entry for entry in provider_metadata()}
_PROVIDERS = tuple(_PROVIDER_METADATA)
_CACHE_VERSION = 5
_DEFAULT_CACHE_FILE = "provider-model-catalog.json"
_MODEL_DISCOVERY_CWD_NAME = "provider-model-discovery"


def context_length_for_model(provider: str | None, model: str | None) -> int | None:
    """Return a static catalog context length for known shipped models."""
    return static_context_length_for_model(provider, model)


def _model_identifiers(model: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    for key in ("value", "canonical_id", "id", "model", "context_lookup_key"):
        value = model.get(key)
        if isinstance(value, str) and value.strip():
            identifiers.append(value)
    match_identifiers = model.get("match_identifiers")
    if isinstance(match_identifiers, list):
        identifiers.extend(str(value) for value in match_identifiers if str(value).strip())
    return identifiers


def _fallback_context_candidate(
    provider: str,
    identifier: str,
) -> ContextLengthCandidate | None:
    provider_catalog = provider_catalog_context_length_for_model(provider, identifier)
    if provider_catalog is not None:
        return ContextLengthCandidate(provider_catalog, "provider_catalog")
    static_default = static_context_length_for_model(provider, identifier)
    if static_default is not None:
        return ContextLengthCandidate(static_default, "static_default")
    return None


def _normalize_model_entry(
    provider: str,
    model: dict[str, Any],
    *,
    source_if_missing: ContextLengthSource | None = None,
) -> dict[str, Any]:
    entry = copy.deepcopy(model)
    candidate = extract_context_length_candidate(entry, source_if_missing=source_if_missing)
    if candidate is None:
        for identifier in _model_identifiers(entry):
            candidate = _fallback_context_candidate(provider, identifier)
            if candidate is not None:
                break
    if candidate is not None:
        entry["context_length"] = candidate.value
        entry[CONTEXT_LENGTH_SOURCE_KEY] = candidate.source
    return entry


def with_context_lengths(
    provider: str,
    models: list[dict[str, Any]],
    *,
    source_if_missing: ContextLengthSource | None = None,
) -> list[dict[str, Any]]:
    """Return provider model entries enriched with known context lengths."""
    return [
        _normalize_model_entry(provider, model, source_if_missing=source_if_missing)
        for model in models
        if isinstance(model, dict)
    ]


def _cached_models(provider: str, models: Any) -> list[dict[str, Any]]:
    if not isinstance(models, list):
        return []
    legacy_source: ContextLengthSource = (
        "provider_catalog" if provider == "droid" else "static_default"
    )
    return with_context_lengths(provider, models, source_if_missing=legacy_source)


DROID_MODEL_CATALOG: list[dict[str, Any]] = with_context_lengths("droid", _DROID_MODEL_CATALOG)
# Public route catalog: drop the internal effort->display map. Clients pick a
# model + reasoning_effort; the daemon composes the AGY --model string. The
# source _AGY_MODELS keeps effort_display for the adapter/gate and drift test.
AGY_MODEL_CATALOG: list[dict[str, Any]] = with_context_lengths(
    "agy",
    [
        {key: value for key, value in model.items() if key != "effort_display"}
        for model in _AGY_MODELS.values()
    ],
)


def _static_provider_models(provider: str) -> list[dict[str, Any]]:
    if provider == "droid":
        return copy.deepcopy(DROID_MODEL_CATALOG)
    if provider == "agy":
        return copy.deepcopy(AGY_MODEL_CATALOG)
    return []


def _model_discovery_cwd_path(provider: str) -> Path:
    provider_dir = provider.strip().lower()
    if (
        not provider_dir
        or provider_dir in {".", ".."}
        or "/" in provider_dir
        or "\\" in provider_dir
    ):
        raise ValueError(f"Invalid provider model-discovery directory: {provider!r}")
    return get_gobby_home() / _MODEL_DISCOVERY_CWD_NAME / provider_dir


async def _model_discovery_cwd(provider: str) -> tuple[Path, bool]:
    cwd = _model_discovery_cwd_path(provider)
    created = False
    try:
        await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=False)
        created = True
    except FileExistsError:
        if not await asyncio.to_thread(cwd.is_dir):
            raise
    return cwd.resolve(), created


def _short_error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:240]


def create_provider_model_catalog(
    daemon_config: DaemonConfig | None = None,
) -> ProviderModelCatalog:
    """Create the provider model catalog.

    ``daemon_config`` is accepted as a reserved daemon-aware extension point for callers that
    already construct the catalog from daemon configuration paths.
    """
    return ProviderModelCatalog()


class ProviderModelCatalog:
    """Caches provider model discovery for route and status consumers."""

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self._cache_path = cache_path or (get_gobby_home() / _DEFAULT_CACHE_FILE)
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
        if payload.get("version") not in (None, 2, 3, 4, _CACHE_VERSION):
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
        if not isinstance(models, list):
            models = []
        return {
            "source": entry.get("source", "failed"),
            "cli_version": entry.get("cli_version"),
            "error": entry.get("error"),
            "models": with_context_lengths(provider, models),
            "generated_at": entry.get("generated_at") or self._generated_at,
        }

    def get_context_window(self, provider: str | None, model: str | None) -> int | None:
        """Resolve context length from provider catalog metadata."""
        resolved = self.get_context_window_with_source(provider, model)
        return resolved.value if resolved else None

    def get_context_window_with_source(
        self,
        provider: str | None,
        model: str | None,
    ) -> ResolvedContextWindow | None:
        """Resolve context length and source from provider catalog metadata."""
        if not model:
            return None

        normalized_provider = provider.strip().lower() if isinstance(provider, str) else None
        if not normalized_provider:
            for candidate_provider in _PROVIDERS:
                resolved = self._get_provider_context_window(candidate_provider, model)
                if resolved is not None:
                    return resolved
            return None

        if normalized_provider == "droid":
            resolved = self._get_provider_context_window(
                normalized_provider, model, include_static=False
            )
            if resolved is not None:
                return resolved
            for underlying_provider in self._droid_underlying_providers(model):
                resolved = self._get_provider_context_window(underlying_provider, model)
                if resolved is not None:
                    return resolved
            return self._get_provider_context_window(normalized_provider, model)

        return self._get_provider_context_window(normalized_provider, model)

    def _get_provider_context_window(
        self, provider: str, model: str, *, include_static: bool = True
    ) -> ResolvedContextWindow | None:
        target = normalize_model_lookup_id(model)
        entry = self._providers.get(provider, {})
        models = entry.get("models")
        best_len = 0
        best_context: ContextLengthCandidate | None = None

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
            return ResolvedContextWindow(best_context.value, best_context.source)
        if not include_static:
            return None
        fallback = _fallback_context_candidate(provider, model)
        return ResolvedContextWindow(fallback.value, fallback.source) if fallback else None

    def _match_candidate_and_context(
        self, provider: str, item: dict[str, Any], target: str
    ) -> tuple[int, ContextLengthCandidate] | None:
        context_length = extract_context_length_candidate(item)
        if context_length is None:
            for identifier in _model_identifiers(item):
                context_length = _fallback_context_candidate(provider, identifier)
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
            if (normalized := normalize_model_lookup_id(identifier))
        }
        if provider == "claude":
            for candidate in tuple(candidates):
                if candidate.startswith("claude-opus"):
                    candidates.add("opus")
                elif candidate.startswith("claude-sonnet"):
                    candidates.add("sonnet")
                elif candidate.startswith("claude-haiku"):
                    candidates.add("haiku")
                elif candidate.startswith("claude-fable"):
                    candidates.add("fable")
        return candidates

    def _alias_matches(self, provider: str, candidate: str, target: str) -> bool:
        families = {"opus", "sonnet", "haiku", "fable"}
        return provider in {"claude", "droid"} and candidate in families and candidate in target

    @staticmethod
    def _droid_underlying_providers(model: str) -> tuple[str, ...]:
        normalized = normalize_model_lookup_id(model)
        if normalized.startswith("claude-") or any(
            family in normalized for family in ("opus", "sonnet", "haiku", "fable")
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
            metadata = _PROVIDER_METADATA[provider]
            if not metadata.live_model_discovery:
                static_models = _static_provider_models(provider)
                results[provider] = {
                    "source": "static" if static_models else "unsupported",
                    "cli_version": cli_version or (previous or {}).get("cli_version"),
                    "error": metadata.unavailable_reason,
                    "models": static_models,
                    "generated_at": generated_at,
                }
                continue
            try:
                source = "live"
                if provider == "grok":
                    discovered_models, source = await self._discover_grok_models_with_source()
                else:
                    discovered_models = await self._discover_provider_models(
                        provider, codex_client=codex_client
                    )
                models = with_context_lengths(provider, discovered_models)
                results[provider] = {
                    "source": source,
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
                    static_models = _static_provider_models(provider)
                    if static_models:
                        results[provider] = {
                            "source": "static",
                            "cli_version": cli_version,
                            "error": error,
                            "models": static_models,
                            "generated_at": generated_at,
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
        if provider == "grok":
            return await self._discover_grok_models()
        if provider == "qwen":
            return await self._discover_qwen_models()
        if provider == "codex":
            return await self._discover_codex_models(codex_client=codex_client)
        if provider == "droid":
            return _static_provider_models(provider)
        if provider == "agy":
            return _static_provider_models(provider)
        raise ValueError(f"Unknown provider: {provider}")

    async def _discover_codex_models(
        self,
        *,
        codex_client: CodexAppServerClient | None = None,
    ) -> list[dict[str, Any]]:
        return await _discover_codex_models_impl(codex_client=codex_client, which=shutil.which)

    async def _discover_grok_models(self) -> list[dict[str, Any]]:
        models, _source = await self._discover_grok_models_with_source()
        return models

    async def _discover_grok_models_with_source(self) -> tuple[list[dict[str, Any]], str]:
        from gobby.adapters.grok_acp_client import GrokACPClient

        return await _discover_grok_models_with_source_impl(
            client_cls=GrokACPClient,
            acp_discoverer=lambda client_cls: self._discover_acp_models(client_cls=client_cls),
            which=shutil.which,
            models_from_cache=grok_models_from_cache,
            static_models=grok_static_models,
            logger=logger,
        )

    async def _discover_qwen_models(self) -> list[dict[str, Any]]:
        from gobby.adapters.qwen_acp_client import QwenACPClient

        return await _discover_qwen_models_impl(
            client_cls=QwenACPClient,
            acp_discoverer=lambda client_cls: self._discover_acp_models(client_cls=client_cls),
            configured_model_discoverer=self._discover_qwen_configured_models,
            label_normalizer=self._normalize_qwen_model_labels,
            which=shutil.which,
        )

    def _discover_qwen_configured_models(self) -> list[dict[str, Any]]:
        return _discover_qwen_configured_models_impl(self._load_qwen_settings())

    def _load_qwen_settings(self) -> dict[str, Any]:
        return _load_qwen_settings_impl(deep_merge=deep_merge, logger=logger)

    def _normalize_qwen_model_labels(
        self,
        models: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return _normalize_qwen_model_labels_impl(models)

    async def _discover_acp_models(
        self,
        *,
        client_cls: type[ACPClient],
    ) -> list[dict[str, Any]]:
        return await _discover_acp_models_impl(
            client_cls=client_cls,
            which=shutil.which,
            model_discovery_cwd=_model_discovery_cwd,
            authorize_trust=authorize_model_discovery_trust,
            cleanup_tree=shutil.rmtree,
            logger=logger,
        )

    async def _discover_claude_models(self) -> list[dict[str, Any]]:
        if not shutil.which("claude"):
            raise FileNotFoundError("claude CLI not found in PATH")

        return await _discover_claude_models_impl(
            probe_model=self._probe_claude_model,
            short_error=_short_error,
        )

    async def _probe_claude_model(self, alias: str, label: str) -> dict[str, Any]:
        return await _probe_claude_model_impl(
            alias,
            label,
            context_length_resolver=context_length_for_model,
        )

    async def _get_cli_version(self, provider: str) -> str | None:
        return await _get_cli_version_impl(provider, which=shutil.which)
