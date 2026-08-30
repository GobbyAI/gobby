"""Live provider model discovery helpers."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tomllib
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from gobby.adapters.acp_client import ACPClient
from gobby.llm.context_windows import (
    CONTEXT_LENGTH_SOURCE_KEY,
    extract_context_length_candidate,
)

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

Which = Callable[[str], str | None]
DeepMerge = Callable[[dict[str, Any], dict[str, Any]], None]
TrustAuthorizer = Callable[[str, Path], Awaitable[object]]
ModelDiscoveryCwd = Callable[[str], Awaitable[tuple[Path, bool]]]
CleanupTree = Callable[[Path], object]
ACPDiscoverer = Callable[[type[ACPClient]], Awaitable[list[dict[str, Any]]]]
ContextLengthResolver = Callable[[str | None, str | None], int | None]

MODEL_DISCOVERY_REQUEST_TIMEOUT_SECONDS = 90.0
logger = logging.getLogger(__name__)
# Each alias is validated live by probe_claude_model (`claude --print --model <alias>`),
# which is the source of truth for whether a model exists. Do not remove entries because
# a reviewer or bot does not recognize the model name.
CLAUDE_ALIASES = (
    ("haiku", "Haiku"),
    ("sonnet", "Sonnet"),
    ("opus", "Opus"),
    ("fable", "Fable"),
)
CLAUDE_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
QWEN_AUTH_TYPES = frozenset({"qwen-oauth", "openai", "anthropic", "gemini", "vertex-ai"})
_LOCAL_ENDPOINT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# qwen-code reports its built-in Qwen OAuth coder alias as the opaque id "coder-model"
# with no friendly name; relabel known aliases for the model picker.
QWEN_ALIAS_LABELS = {"coder-model": "Qwen Coder (OAuth)"}


def extract_reasoning(model: dict[str, Any]) -> dict[str, Any] | None:
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


def format_qwen_model_value(model_id: str, auth_type: str | None) -> str:
    if not auth_type:
        return model_id
    return f"{model_id}({auth_type})"


def split_qwen_model_value(value: str) -> tuple[str, str | None]:
    trimmed = value.strip()
    close_idx = trimmed.rfind(")")
    open_idx = trimmed.rfind("(")
    if open_idx >= 0 and close_idx == len(trimmed) - 1 and open_idx < close_idx:
        model_id = trimmed[:open_idx].strip()
        auth_type = trimmed[open_idx + 1 : close_idx].strip()
        if model_id and auth_type in QWEN_AUTH_TYPES:
            return model_id, auth_type
    return trimmed, None


def merge_models(
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


async def discover_codex_models(
    *,
    codex_client: CodexAppServerClient | None = None,
    which: Which,
) -> list[dict[str, Any]]:
    if not which("codex"):
        raise FileNotFoundError("codex CLI not found in PATH")

    client = codex_client
    owns_client = False
    if client is None:
        from gobby.adapters.codex_impl.client import CodexAppServerClient

        client = CodexAppServerClient()
        owns_client = True
    started_client = False

    assert client is not None
    try:
        if not client.is_connected:
            await client.start()
            started_client = True
        raw_models = await client.list_models(include_hidden=True)
    finally:
        if owns_client or started_client:
            try:
                await client.stop()
            except Exception as exc:
                logger.exception("Failed to stop Codex model discovery client: %s", exc)

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
        context = extract_context_length_candidate(item, source_if_missing="provider_reported")
        if context is not None:
            entry["context_length"] = context.value
            entry[CONTEXT_LENGTH_SOURCE_KEY] = context.source
        reasoning = extract_reasoning(item)
        if reasoning:
            entry["reasoning"] = reasoning
        models.append(entry)

    return models


async def discover_grok_models_with_source(
    *,
    client_cls: type[ACPClient],
    acp_discoverer: ACPDiscoverer,
    which: Which,
    models_from_cache: Callable[[], list[dict[str, Any]]],
    static_models: Callable[[], list[dict[str, Any]]],
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], str]:
    if not which(client_cls.cli_name):
        try:
            cached = models_from_cache()
        except Exception:
            cached = []
        if cached:
            return cached, "cache"
        return static_models(), "static"

    acp_error: Exception | None = None
    try:
        return await acp_discoverer(client_cls), "live"
    except Exception as exc:
        acp_error = exc

    try:
        cached = models_from_cache()
    except Exception:
        cached = []
    if cached:
        return cached, "cache"
    if acp_error is not None:
        logger.debug("Grok ACP model discovery failed; using static fallback: %s", acp_error)
    return static_models(), "static"


async def discover_qwen_models(
    *,
    client_cls: type[ACPClient],
    acp_discoverer: ACPDiscoverer,
    configured_model_discoverer: Callable[[], list[dict[str, Any]]],
    label_normalizer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    which: Which,
) -> list[dict[str, Any]]:
    if not which(client_cls.cli_name):
        raise FileNotFoundError("qwen CLI not found in PATH")

    acp_error: Exception | None = None
    try:
        acp_models = await acp_discoverer(client_cls)
    except Exception as exc:
        acp_models = []
        acp_error = exc

    models = merge_models(acp_models, configured_model_discoverer())
    if models:
        return label_normalizer(models)
    if acp_error is not None:
        raise acp_error
    return []


def discover_qwen_configured_models(settings: dict[str, Any]) -> list[dict[str, Any]]:
    model_providers = settings.get("modelProviders")
    if not isinstance(model_providers, dict):
        return []

    models: list[dict[str, Any]] = []
    for auth_type, configured_models in model_providers.items():
        if auth_type not in QWEN_AUTH_TYPES or auth_type == "qwen-oauth":
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
                "value": format_qwen_model_value(model_id, auth_type),
                "label": str(configured_model.get("name") or model_id),
            }
            description = configured_model.get("description")
            if isinstance(description, str) and description.strip():
                entry["description"] = description.strip()
            models.append(entry)
    return models


def qwen_local_model_values(settings: dict[str, Any]) -> frozenset[str]:
    """Return configured Qwen model identities backed by loopback endpoints."""
    model_providers = settings.get("modelProviders")
    if not isinstance(model_providers, dict):
        return frozenset()

    models: set[str] = set()
    for auth_type, configured_models in model_providers.items():
        if auth_type not in QWEN_AUTH_TYPES or auth_type == "qwen-oauth":
            continue
        if not isinstance(configured_models, list):
            continue
        for configured_model in configured_models:
            if not isinstance(configured_model, dict):
                continue
            model_id = str(configured_model.get("id") or "").strip()
            base_url = configured_model.get("baseUrl")
            if not model_id or not isinstance(base_url, str):
                continue
            if is_loopback_model_endpoint(base_url):
                models.add(format_qwen_model_value(model_id, auth_type))
    return frozenset(models)


def codex_uses_loopback_model_endpoint(config: Mapping[str, Any]) -> bool:
    """Return whether Codex's active model provider uses a loopback endpoint."""
    provider_id = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(provider_id, str) or not isinstance(providers, Mapping):
        return False
    provider = providers.get(provider_id)
    return isinstance(provider, Mapping) and is_loopback_model_endpoint(provider.get("base_url"))


def claude_uses_loopback_model_endpoint(
    settings: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether Claude's effective Anthropic endpoint is loopback-backed."""
    effective_environment = os.environ if environment is None else environment
    base_url = effective_environment.get("ANTHROPIC_BASE_URL")
    if base_url is None:
        configured_environment = settings.get("env")
        if isinstance(configured_environment, Mapping):
            base_url = configured_environment.get("ANTHROPIC_BASE_URL")
    return is_loopback_model_endpoint(base_url)


def is_loopback_model_endpoint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return False
    return hostname is not None and hostname.casefold() in _LOCAL_ENDPOINT_HOSTS


def load_codex_config(*, logger: logging.Logger) -> dict[str, Any]:
    configured_home = os.environ.get("CODEX_HOME")
    config_dir = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    config_path = config_dir / "config.toml"
    if not config_path.exists():
        return {}
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Failed to read Codex config from %s: %s", config_path, exc)
        return {}


def load_claude_settings(
    *,
    deep_merge: DeepMerge,
    logger: logging.Logger,
) -> dict[str, Any]:
    configured_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(configured_dir).expanduser() if configured_dir else Path.home() / ".claude"
    return _load_merged_json_settings(
        (
            config_dir / "settings.json",
            Path.cwd() / ".claude" / "settings.json",
            Path.cwd() / ".claude" / "settings.local.json",
        ),
        provider="Claude",
        deep_merge=deep_merge,
        logger=logger,
    )


def load_qwen_settings(
    *,
    deep_merge: DeepMerge,
    logger: logging.Logger,
) -> dict[str, Any]:
    return _load_merged_json_settings(
        (
            Path.home() / ".qwen" / "settings.json",
            Path.cwd() / ".qwen" / "settings.json",
        ),
        provider="Qwen",
        deep_merge=deep_merge,
        logger=logger,
    )


def _load_merged_json_settings(
    settings_paths: Sequence[Path],
    *,
    provider: str,
    deep_merge: DeepMerge,
    logger: logging.Logger,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    seen_paths: set[Path] = set()

    for settings_path in settings_paths:
        if settings_path in seen_paths or not settings_path.exists():
            continue
        seen_paths.add(settings_path)
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read %s settings from %s: %s", provider, settings_path, exc)
            continue
        if not isinstance(payload, dict):
            continue
        deep_merge(merged, payload)

    return merged


def normalize_qwen_model_labels(
    models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    relabeled: list[dict[str, Any]] = []
    for model in models:
        value = str(model.get("value") or "")
        model_id, _ = split_qwen_model_value(value)
        alias_label = QWEN_ALIAS_LABELS.get(model_id)
        if alias_label and str(model.get("label") or "") in ("", model_id, value):
            entry = copy.deepcopy(model)
            entry["label"] = alias_label
            relabeled.append(entry)
        else:
            relabeled.append(model)
    models = relabeled

    base_id_counts = Counter(
        model_id
        for model in models
        if (model_id := split_qwen_model_value(str(model.get("value") or ""))[0])
    )
    if not any(count > 1 for count in base_id_counts.values()):
        return models

    normalized: list[dict[str, Any]] = []
    for model in models:
        entry = copy.deepcopy(model)
        value = str(entry.get("value") or "")
        model_id, auth_type = split_qwen_model_value(value)
        if not auth_type or base_id_counts[model_id] <= 1:
            normalized.append(entry)
            continue
        label = str(entry.get("label") or value)
        if f"({auth_type})" not in label:
            entry["label"] = f"{label} ({auth_type})"
        normalized.append(entry)
    return normalized


async def discover_acp_models(
    *,
    client_cls: type[ACPClient],
    which: Which,
    model_discovery_cwd: ModelDiscoveryCwd,
    authorize_trust: TrustAuthorizer,
    cleanup_tree: CleanupTree,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    if not which(client_cls.cli_name):
        raise FileNotFoundError(f"{client_cls.cli_name} CLI not found in PATH")

    cwd, created_cwd = await model_discovery_cwd(client_cls.cli_name)
    try:
        await authorize_trust(client_cls.cli_name, cwd)
    except Exception:
        if created_cwd:
            try:
                await asyncio.to_thread(cleanup_tree, cwd)
            except Exception as cleanup_exc:
                logger.exception(
                    "Failed to remove %s model-discovery cwd %s after authorization failure: %s",
                    client_cls.cli_name,
                    cwd,
                    cleanup_exc,
                )
        raise
    client = client_cls(
        cwd=os.fspath(cwd),
        purpose="model-discovery",
        request_timeout=MODEL_DISCOVERY_REQUEST_TIMEOUT_SECONDS,
    )
    try:
        await client.start()
        session_info = client.session_info
    finally:
        try:
            await client.stop()
        except Exception as exc:
            logger.exception(
                "Failed to stop %s model discovery client: %s",
                client_cls.cli_name,
                exc,
            )

    return parse_acp_models(client_cls, session_info)


def parse_acp_models(
    client_cls: type[ACPClient],
    session_info: dict[str, Any],
) -> list[dict[str, Any]]:
    if client_cls.cli_name == "grok":
        from gobby.servers.provider_models_grok import models_from_acp_session

        return models_from_acp_session(session_info)

    models_info = session_info.get("models")
    raw_models = models_info.get("availableModels", []) if isinstance(models_info, dict) else []
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
        context = extract_context_length_candidate(item, source_if_missing="provider_reported")
        if context is not None:
            entry["context_length"] = context.value
            entry[CONTEXT_LENGTH_SOURCE_KEY] = context.source
        reasoning = extract_reasoning(item)
        if reasoning:
            entry["reasoning"] = reasoning
        models.append(entry)
    return models


async def discover_claude_models(
    *,
    probe_model: Callable[[str, str], Awaitable[dict[str, Any]]],
    short_error: Callable[[BaseException], str],
) -> list[dict[str, Any]]:
    probes = await asyncio.gather(
        *[probe_model(alias, label) for alias, label in CLAUDE_ALIASES],
        return_exceptions=True,
    )

    models: list[dict[str, Any]] = []
    errors: list[str] = []
    for probe in probes:
        if isinstance(probe, Exception):
            errors.append(short_error(probe))
            continue
        if isinstance(probe, dict):
            models.append(probe)

    if not models:
        raise RuntimeError("; ".join(errors) or "Claude model probes failed")
    return models


async def probe_claude_model(
    alias: str,
    label: str,
    *,
    context_length_resolver: ContextLengthResolver,
) -> dict[str, Any]:
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
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            logger.warning("Claude probe process did not exit promptly after kill")
        raise TimeoutError(f"Claude probe timed out for {alias}") from exc

    if proc.returncode != 0:
        error = stderr.decode().strip() or stdout.decode().strip() or "probe failed"
        raise RuntimeError(f"Claude {alias}: {error}")

    lines = [line for line in stdout.decode().splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"Claude {alias}: empty response")

    final_line = lines[-1]
    try:
        payload = json.loads(final_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Claude {alias}: failed to parse final JSON line {final_line!r}: {exc}"
        ) from exc
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        raise RuntimeError(f"Claude {alias}: missing modelUsage")

    canonical_id = next(iter(model_usage))
    result: dict[str, Any] = {
        "value": alias,
        "label": label,
        "canonical_id": str(canonical_id),
        "reasoning": {"supported_efforts": list(CLAUDE_REASONING_EFFORTS)},
    }
    context_length = context_length_resolver("claude", str(canonical_id))
    if context_length is not None:
        result["context_length"] = context_length
        result["context_length_source"] = "registry"
    return result


async def get_cli_version(provider: str, *, which: Which) -> str | None:
    executable = which(provider)
    if not executable:
        return None

    if provider == "grok":
        args = [executable, "version"]
    else:
        args = [executable, "--version"]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            logger.warning("%s version probe did not exit promptly after kill", provider)
        return None

    if proc.returncode != 0:
        return None
    output = stdout.decode().strip() or stderr.decode().strip()
    return output or None
