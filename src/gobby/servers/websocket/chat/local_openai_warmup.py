"""Warm up local OpenAI-compatible backends before Qwen web-chat starts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from gobby.config.app import deep_merge

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_NOISE_TOKENS = {"local", "lmstudio", "openai"}
_QWEN_SETTINGS_PATH = Path.home() / ".qwen" / "settings.json"


class LocalOpenAIModelWarmupError(RuntimeError):
    """Raised when a local OpenAI-compatible backend cannot be prepared."""


@dataclass(slots=True, frozen=True)
class LocalOpenAIModelTarget:
    """A local OpenAI-compatible model target resolved from Qwen settings."""

    backend: Literal["lm_studio", "ollama"]
    request_model: str
    base_url: str
    api_key: str | None = None


def _settings_paths(project_path: str | None) -> list[Path]:
    paths = [_QWEN_SETTINGS_PATH]
    if project_path:
        paths.append(Path(project_path) / ".qwen" / "settings.json")
    return paths


def _load_qwen_settings(project_path: str | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    seen_paths: set[Path] = set()

    for settings_path in _settings_paths(project_path):
        if settings_path in seen_paths or not settings_path.exists():
            continue
        seen_paths.add(settings_path)
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read %s: %s", settings_path, exc)
            continue
        if isinstance(payload, dict):
            deep_merge(merged, payload)

    return merged


def _split_qwen_model_value(value: str) -> tuple[str, str | None]:
    trimmed = value.strip()
    close_idx = trimmed.rfind(")")
    open_idx = trimmed.rfind("(")
    if open_idx >= 0 and close_idx == len(trimmed) - 1 and open_idx < close_idx:
        model_id = trimmed[:open_idx].strip()
        auth_type = trimmed[open_idx + 1 : close_idx].strip()
        if model_id and auth_type:
            return model_id, auth_type
    return trimmed, None


def _model_identifier_signatures(value: str) -> tuple[str, str]:
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    if not tokens:
        return "", ""

    full_tokens = [token for token in tokens if token not in _NOISE_TOKENS]
    if not full_tokens:
        full_tokens = tokens

    core_tokens = [
        token
        for token in full_tokens
        if not re.fullmatch(r"(?:q\d+[a-z0-9]*|f16|bf16|fp16|gguf|mlx)", token)
    ]
    if not core_tokens:
        core_tokens = full_tokens

    return "".join(full_tokens), "".join(core_tokens)


def _candidate_match_score(target_value: str, candidate_value: str) -> int:
    target_full, target_core = _model_identifier_signatures(target_value)
    candidate_full, candidate_core = _model_identifier_signatures(candidate_value)

    if not target_full or not candidate_full:
        return 0
    if candidate_full == target_full:
        return 120
    if candidate_core == target_core:
        return 110
    if candidate_full.endswith(target_full) or target_full.endswith(candidate_full):
        return 100
    if candidate_core.endswith(target_core) or target_core.endswith(candidate_core):
        return 90
    if target_full in candidate_full or candidate_full in target_full:
        return 80
    if target_core in candidate_core or candidate_core in target_core:
        return 70
    return 0


def _extract_base_url(model_config: dict[str, Any]) -> str | None:
    for key in ("baseUrl", "baseURL", "apiBase", "base_url", "endpoint", "url", "host"):
        value = model_config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_local_backend(base_url: str) -> Literal["lm_studio", "ollama"] | None:
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return None

    if (parsed.hostname or "").lower() not in _LOCAL_HOSTS:
        return None
    if parsed.port == 1234:
        return "lm_studio"
    if parsed.port == 11434:
        return "ollama"
    return None


def resolve_qwen_local_openai_target(
    model_value: str | None,
    *,
    project_path: str | None,
) -> LocalOpenAIModelTarget | None:
    """Resolve a Qwen `(...openai)` model to a local backend target."""
    settings = _load_qwen_settings(project_path)

    raw_model = (
        model_value.strip() if isinstance(model_value, str) and model_value.strip() else None
    )
    auth_type: str | None = None
    request_model: str | None = None

    if raw_model:
        request_model, auth_type = _split_qwen_model_value(raw_model)
    else:
        model_config = settings.get("model")
        if isinstance(model_config, dict):
            default_name = model_config.get("name")
            if isinstance(default_name, str) and default_name.strip():
                request_model = default_name.strip()

    if auth_type is None:
        auth = settings.get("security", {}).get("auth", {})
        if isinstance(auth, dict):
            selected_type = auth.get("selectedType")
            if isinstance(selected_type, str) and selected_type.strip():
                auth_type = selected_type.strip()

    if auth_type != "openai" or not request_model:
        return None

    providers = settings.get("modelProviders")
    if not isinstance(providers, dict):
        return None
    configured_models = providers.get("openai")
    if not isinstance(configured_models, list):
        return None

    match = next(
        (
            entry
            for entry in configured_models
            if isinstance(entry, dict) and str(entry.get("id") or "").strip() == request_model
        ),
        None,
    )
    if not isinstance(match, dict):
        return None

    base_url = _extract_base_url(match)
    if not base_url:
        return None

    backend = _resolve_local_backend(base_url)
    if backend is None:
        return None

    api_key: str | None = None
    env_key = match.get("envKey")
    env_block = settings.get("env")
    if isinstance(env_key, str) and env_key.strip() and isinstance(env_block, dict):
        env_value = env_block.get(env_key)
        if isinstance(env_value, str) and env_value.strip():
            api_key = env_value.strip()

    return LocalOpenAIModelTarget(
        backend=backend,
        request_model=request_model,
        base_url=base_url,
        api_key=api_key,
    )


def _base_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _base_origin(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _select_lm_studio_model(
    requested_model: str,
    models: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best_match: dict[str, Any] | None = None
    best_score = 0
    ambiguous = False

    for model in models:
        if not isinstance(model, dict):
            continue

        candidate_strings: list[str] = []
        for key in ("key", "display_name", "selected_variant"):
            value = model.get(key)
            if isinstance(value, str) and value.strip():
                candidate_strings.append(value.strip())

        variants = model.get("variants")
        if isinstance(variants, list):
            candidate_strings.extend(
                str(item).strip() for item in variants if isinstance(item, str) and item.strip()
            )

        loaded_instances = model.get("loaded_instances")
        if isinstance(loaded_instances, list):
            for instance in loaded_instances:
                if not isinstance(instance, dict):
                    continue
                value = instance.get("id")
                if isinstance(value, str) and value.strip():
                    candidate_strings.append(value.strip())

        score = max(
            (_candidate_match_score(requested_model, candidate) for candidate in candidate_strings),
            default=0,
        )
        if score == 0:
            continue
        if score > best_score:
            best_match = model
            best_score = score
            ambiguous = False
        elif score == best_score:
            ambiguous = True

    if ambiguous:
        return None
    return best_match


async def _prepare_lm_studio_model(
    client: httpx.AsyncClient,
    target: LocalOpenAIModelTarget,
) -> None:
    headers = _base_headers(target.api_key)
    origin = _base_origin(target.base_url)
    list_response = await client.get(f"{origin}/api/v1/models", headers=headers, timeout=15.0)
    list_response.raise_for_status()

    payload = list_response.json()
    models = payload.get("models")
    if not isinstance(models, list):
        raise LocalOpenAIModelWarmupError(
            f"LM Studio at {target.base_url} returned an invalid model catalog."
        )

    matched = _select_lm_studio_model(target.request_model, models)
    if matched is None:
        raise LocalOpenAIModelWarmupError(
            "Unable to map Qwen model "
            f"'{target.request_model}' to a local LM Studio model at {target.base_url}. "
            "Update the Qwen model id to match LM Studio, or enable Just-In-Time loading."
        )

    loaded_instances = matched.get("loaded_instances")
    if isinstance(loaded_instances, list) and loaded_instances:
        return

    model_key = matched.get("key")
    if not isinstance(model_key, str) or not model_key.strip():
        raise LocalOpenAIModelWarmupError(
            f"LM Studio model mapping for '{target.request_model}' is missing a loadable key."
        )

    load_response = await client.post(
        f"{origin}/api/v1/models/load",
        headers=headers,
        json={"model": model_key},
        timeout=300.0,
    )
    try:
        load_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = load_response.text.strip()
        suffix = f" {detail}" if detail else ""
        raise LocalOpenAIModelWarmupError(
            "Failed to load LM Studio model "
            f"'{target.request_model}' via {target.base_url}: "
            f"{exc.response.status_code}.{suffix} "
            "Load it manually in LM Studio or enable Just-In-Time loading."
        ) from exc


def _ollama_running_model_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    models = payload.get("models")
    if not isinstance(models, list):
        return names

    for model in models:
        if not isinstance(model, dict):
            continue
        for key in ("model", "name"):
            value = model.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return names


async def _prepare_ollama_model(
    client: httpx.AsyncClient,
    target: LocalOpenAIModelTarget,
) -> None:
    origin = _base_origin(target.base_url)
    ps_response = await client.get(f"{origin}/api/ps", timeout=10.0)
    ps_response.raise_for_status()

    running = _ollama_running_model_names(ps_response.json())
    if target.request_model in running:
        return

    preload_response = await client.post(
        f"{origin}/api/generate",
        json={"model": target.request_model, "keep_alive": -1, "stream": False},
        timeout=300.0,
    )
    try:
        preload_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = preload_response.text.strip()
        suffix = f" {detail}" if detail else ""
        raise LocalOpenAIModelWarmupError(
            "Failed to preload Ollama model "
            f"'{target.request_model}' via {target.base_url}: "
            f"{exc.response.status_code}.{suffix} "
            f"Pull it with `ollama pull {target.request_model}` or update the Qwen model config."
        ) from exc


async def ensure_qwen_local_openai_model_ready(
    model_value: str | None,
    *,
    project_path: str | None,
) -> None:
    """Warm the local backend for a Qwen OpenAI-compatible model when needed."""
    target = resolve_qwen_local_openai_target(model_value, project_path=project_path)
    if target is None:
        return

    async with httpx.AsyncClient() as client:
        try:
            if target.backend == "lm_studio":
                await _prepare_lm_studio_model(client, target)
            else:
                await _prepare_ollama_model(client, target)
        except LocalOpenAIModelWarmupError:
            raise
        except httpx.ConnectError as exc:
            raise LocalOpenAIModelWarmupError(
                f"Cannot reach local {target.backend.replace('_', ' ')} endpoint at {target.base_url}."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LocalOpenAIModelWarmupError(
                f"Timed out preparing local model '{target.request_model}' at {target.base_url}."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LocalOpenAIModelWarmupError(
                "Local model warmup failed with "
                f"{exc.response.status_code} at {target.base_url}: {exc.response.text.strip()}"
            ) from exc
