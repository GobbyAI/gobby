"""External dependency and CLI version detection.

Provides version info for the operational health dashboard (gobby status).
Each function returns None if the tool is not installed/available.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess  # nosec B404 # needed for version detection
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import psycopg

from gobby.config.bootstrap import BootstrapConfigError
from gobby.install.version_probe import probe_native_bin_version
from gobby.utils.dependency_requirements import collect_dependency_report
from gobby.utils.native_bin import local_native_bin_path, resolve_native_bin

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def _run_cmd(args: list[str], timeout: int = 5) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(  # nosec B603 # hardcoded commands
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Gobby CLIs
# ---------------------------------------------------------------------------


def get_gobby_version() -> str | None:
    """Get gobby package version from metadata."""
    from gobby.utils.version import get_version

    try:
        return get_version()
    except Exception:
        return None


def _read_version_stamp(stamp_name: str) -> str | None:
    """Read a version stamp from ``~/.gobby/bin`` when present."""
    stamp = Path.home() / ".gobby" / "bin" / stamp_name
    if not stamp.exists():
        return None
    try:
        value = stamp.read_text().strip()
    except OSError:
        return None
    return value or None


def _get_native_binary_version(binary_name: str, stamp_name: str | None = None) -> str | None:
    """Get a managed native binary version, preferring the resolved CLI over stamps."""

    binary_path = resolve_native_bin(binary_name)
    if binary_path:
        version = probe_native_bin_version(binary_path)
        if version:
            return version

    return _read_version_stamp(stamp_name) if stamp_name else None


def get_gcode_version() -> str | None:
    """Get gcode version from stamp file or CLI."""
    return _get_native_binary_version("gcode", ".gcode-version")


def get_ghook_version() -> str | None:
    """Get ghook version from stamp file or CLI."""
    return _get_native_binary_version("ghook", ".ghook-version")


def get_gwiki_version() -> str | None:
    """Get gwiki version from stamp file or CLI."""
    return _get_native_binary_version("gwiki", ".gwiki-version")


def get_impeccable_version() -> str | None:
    """Return the version of a fully verified managed Impeccable install."""
    from gobby.cli.install_setup_impeccable import (
        ImpeccableInstallError,
        inspect_impeccable_installation,
    )
    from gobby.utils.dependency_requirements import IMPECCABLE_RELEASE

    try:
        pointer = inspect_impeccable_installation()
    except ImpeccableInstallError:
        return None
    return IMPECCABLE_RELEASE.version if pointer is not None else None


# ---------------------------------------------------------------------------
# Coding CLIs
# ---------------------------------------------------------------------------


def get_claude_code_version() -> str | None:
    """Get Claude Code CLI version."""
    output = _run_cmd(["claude", "--version"])
    if output:
        # Various formats: "claude 1.0.12", "1.0.12", etc.
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_grok_cli_version() -> str | None:
    """Get Grok CLI version."""
    output = _run_cmd(["grok", "version"])
    if output:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_codex_cli_version() -> str | None:
    """Get Codex CLI version."""
    output = _run_cmd(["codex", "--version"])
    if output:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_qwen_cli_version() -> str | None:
    """Get Qwen CLI version."""
    output = _run_cmd(["qwen", "--version"])
    if output:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_agy_cli_version() -> str | None:
    """Get AGY CLI version."""
    output = _run_cmd(["agy", "--version"])
    if output:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_droid_cli_version() -> str | None:
    """Get Factory Droid CLI version."""
    output = _run_cmd(["droid", "--version"])
    if output:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_coding_cli_hooks_status() -> dict[str, bool]:
    """Check which coding CLIs have gobby hooks installed.

    Returns dict mapping CLI name to whether hooks are installed.
    Detects by checking for the ``--gobby-owned`` marker in config.
    """
    result: dict[str, bool] = {}

    # Claude Code: ~/.claude/settings.json
    claude_settings = Path.home() / ".claude" / "settings.json"
    result["claude"] = _check_hooks_in_file(claude_settings)

    # Grok: ~/.grok/hooks/gobby.json
    grok_hooks = Path.home() / ".grok" / "hooks" / "gobby.json"
    result["grok"] = _check_hooks_in_file(grok_hooks)

    # AGY: no supported hook transport
    result["agy"] = False

    # Codex: ~/.codex/hooks.json
    codex_hooks = Path.home() / ".codex" / "hooks.json"
    result["codex"] = _check_hooks_in_file(codex_hooks)

    # Qwen: ~/.qwen/settings.json
    qwen_settings = Path.home() / ".qwen" / "settings.json"
    result["qwen"] = _check_hooks_in_file(qwen_settings)

    # Factory Droid: ~/.factory/hooks/hooks.json
    droid_hooks = _droid_hooks_file()
    result["droid"] = _check_hooks_in_file(droid_hooks)

    return result


def _droid_hooks_file() -> Path:
    """Return the Droid hooks path used by status and test overrides."""
    if override := os.environ.get("GOBBY_DROID_HOOKS_FILE"):
        return Path(override).expanduser()
    if hooks_dir := os.environ.get("GOBBY_HOOKS_DIR"):
        return Path(hooks_dir).expanduser() / "hooks.json"
    return Path.home() / ".factory" / "hooks.json"


def _check_hooks_in_file(path: Path) -> bool:
    """Check if a settings/hooks file references a Gobby-managed hook command."""
    if not path.exists():
        return False
    try:
        from gobby.cli.installers.hook_commands import is_gobby_hook_command

        content = path.read_text()
        return is_gobby_hook_command(content)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# External dependencies
# ---------------------------------------------------------------------------


def get_tmux_version() -> str | None:
    """Get tmux version."""
    output = _run_cmd(["tmux", "-V"])
    if output:
        # "tmux 3.4" → "3.4"
        match = re.search(r"(\d+\.\d+[a-z]?)", output)
        return match.group(1) if match else output
    return None


def get_docker_version() -> str | None:
    """Get Docker version."""
    output = _run_cmd(["docker", "--version"])
    if output:
        # "Docker version 27.1.1, build ..." → "27.1.1"
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_docker_running() -> bool:
    """Check if Docker daemon is running."""
    return _run_cmd(["docker", "info"], timeout=3) is not None


def get_git_version() -> str | None:
    """Get git version."""
    output = _run_cmd(["git", "--version"])
    if output:
        # "git version 2.44.0" → "2.44.0"
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else output
    return None


def get_node_version() -> str | None:
    """Get Node.js version."""
    output = _run_cmd(["node", "--version"])
    if output:
        # "v22.1.0" → "22.1.0"
        return output.lstrip("v")
    return None


def get_tailscale_info() -> dict[str, Any] | None:
    """Get Tailscale status including serve config.

    Returns dict with:
        version: Tailscale version string
        hostname: Tailnet hostname (e.g. "machine.tail1234.ts.net")
        serving: dict of port → backend mappings if tailscale serve is active
        funnel: whether funnel is enabled for any port
    Or None if tailscale is not installed.
    """
    if not shutil.which("tailscale"):
        return None

    import json

    info: dict[str, Any] = {"version": None, "hostname": None, "serving": {}, "funnel": False}

    # Version
    version_output = _run_cmd(["tailscale", "version"])
    if version_output:
        match = re.search(r"(\d+\.\d+\.\d+)", version_output.split("\n")[0])
        info["version"] = match.group(1) if match else version_output.split("\n")[0]

    # Status (hostname)
    status_output = _run_cmd(["tailscale", "status", "--json"])
    if status_output:
        try:
            status = json.loads(status_output)
            dns_name = status.get("Self", {}).get("DNSName", "")
            info["hostname"] = dns_name.rstrip(".") if dns_name else None
        except (json.JSONDecodeError, KeyError):
            pass

    # Serve status
    serve_output = _run_cmd(["tailscale", "serve", "status", "--json"])
    if serve_output:
        try:
            serve = json.loads(serve_output)
            # Parse serve config — structure varies by version
            web = serve.get("Web", serve.get("web", {}))
            for addr, handlers in web.items():
                if isinstance(handlers, dict):
                    info["serving"][addr] = handlers
            # Check funnel
            allow_funnel = serve.get("AllowFunnel", serve.get("allowFunnel", {}))
            if allow_funnel and any(allow_funnel.values()):
                info["funnel"] = True
        except (json.JSONDecodeError, KeyError):
            pass

    return info


def get_ollama_info() -> dict[str, Any] | None:
    """Get Ollama version and status."""
    if not shutil.which("ollama"):
        return None
    version = _run_cmd(["ollama", "--version"])
    ver_str = None
    if version:
        match = re.search(r"(\d+\.\d+\.\d+)", version)
        ver_str = match.group(1) if match else version
    running = _run_cmd(["ollama", "list"], timeout=3) is not None
    return {"version": ver_str, "running": running}


def get_lmstudio_info() -> dict[str, Any] | None:
    """Get LM Studio status."""
    if not shutil.which("lms"):
        return None
    output = _run_cmd(["lms", "server", "status"])
    # lms writes status to stderr, but _run_cmd captures stdout
    # Check if running based on output or fallback
    running = False
    if output:
        normalized_output = output.lower()
        running = "running" in normalized_output and "not running" not in normalized_output
    else:
        # Try with stderr too
        try:
            result = subprocess.run(  # nosec B603
                ["lms", "server", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            combined = (result.stdout + result.stderr).lower()
            running = (
                result.returncode == 0 and "running" in combined and "not running" not in combined
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.debug("Failed to determine LM Studio server status", exc_info=True)
    return {"running": running}


def _normalize_embedding_provider(provider: Any) -> str | None:
    """Normalize a configured embeddings provider value."""
    if not isinstance(provider, str):
        return None
    normalized = provider.strip().lower()
    if normalized in {"lmstudio", "ollama", "openai", "none"}:
        return normalized
    return None


def _strip_config_string(value: Any) -> Any:
    """Strip configured string values while preserving non-string typed values."""
    if isinstance(value, str):
        return value.strip()
    return value


async def fingerprint_embedding_server(
    api_base: str,
    api_key: str | None = None,
    *,
    timeout: float = 1.5,
) -> str | None:
    """Identify a local embedding server by probing its origin.

    Ollama answers ``GET /api/tags``; LM Studio answers ``GET /api/v1/models``;
    vLLM ``/v1/models`` entries carry ``owned_by: "vllm"``; any other
    ``/v1/models`` 200 is generic openai-compatible. Unreachable or
    unidentifiable servers return ``None``.
    """
    parsed = urlparse(api_base.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for url, provider in (
            (f"{origin}/api/tags", "ollama"),
            (f"{origin}/api/v1/models", "lmstudio"),
        ):
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                return provider
        try:
            response = await client.get(f"{origin}/v1/models")
        except httpx.HTTPError:
            return None
    if response.status_code != 200:
        return None
    try:
        payload: Any = response.json()
    except ValueError:
        return "openai-compatible"
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and any(
        isinstance(entry, dict) and entry.get("owned_by") == "vllm" for entry in data
    ):
        return "vllm"
    return "openai-compatible"


def fingerprint_embedding_server_sync(
    api_base: str,
    api_key: str | None = None,
    *,
    timeout: float = 1.5,
) -> str | None:
    """Sync wrapper over :func:`fingerprint_embedding_server` for CLI callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fingerprint_embedding_server(api_base, api_key, timeout=timeout))
    logger.warning("Cannot fingerprint embedding server: already in an event loop")
    return None


def _is_openai_cloud_api_base(api_base: Any) -> bool:
    """Return whether an API base targets OpenAI or Azure OpenAI cloud."""
    normalized_api_base = _strip_config_string(api_base)
    if not isinstance(normalized_api_base, str):
        return False
    hostname = urlparse(normalized_api_base).hostname
    if hostname is None:
        return False
    hostname = hostname.lower()
    return hostname == "api.openai.com" or hostname.endswith(
        (".openai.azure.com", ".services.ai.azure.com")
    )


def _infer_from_config_or_none(*, dim: Any, api_key: Any, model: Any, api_base: Any) -> str | None:
    """Infer provider from configured embedding values, or explicit disabled state."""
    normalized_dim = _strip_config_string(dim)
    normalized_api_key = _strip_config_string(api_key)
    normalized_model = _strip_config_string(model)
    normalized_api_base = _strip_config_string(api_base)
    dim_int = normalized_dim if isinstance(normalized_dim, int) else None
    if isinstance(normalized_dim, str) and normalized_dim:
        try:
            dim_int = int(normalized_dim)
        except ValueError:
            pass
    if dim_int == 0:
        return "none"
    if normalized_api_key:
        if _is_openai_cloud_api_base(normalized_api_base):
            return "openai"
        if normalized_api_base not in (None, ""):
            return None
        from gobby.ai.embeddings import is_openai_cloud_embedding_model

        if isinstance(normalized_model, str) and is_openai_cloud_embedding_model(normalized_model):
            return "openai"
    return None


def get_configured_embedding_provider(
    db: HubDatabase,
    *,
    raise_storage_errors: bool = False,
) -> str | None:
    """Get the configured embeddings provider from persisted config."""
    try:
        from gobby.config.embedding_keys import (
            AI_EMBEDDING_API_BASE_KEY,
            AI_EMBEDDING_API_KEY_KEY,
            AI_EMBEDDING_DIM_KEY,
            AI_EMBEDDING_MODEL_KEY,
        )
        from gobby.storage.config_repository import ConfigRepository

        snapshot = ConfigRepository(db).read()
        values = snapshot.values
        model = _strip_config_string(values.get(AI_EMBEDDING_MODEL_KEY))
        api_base = _strip_config_string(values.get(AI_EMBEDDING_API_BASE_KEY))
        api_key_binding = snapshot.secret_bindings.get(AI_EMBEDDING_API_KEY_KEY)
        api_key = _strip_config_string(
            api_key_binding.plaintext if api_key_binding is not None else None
        )
        dim = _strip_config_string(values.get(AI_EMBEDDING_DIM_KEY))

        inferred_from_config = _infer_from_config_or_none(
            dim=dim, api_key=api_key, model=model, api_base=api_base
        )
        if inferred_from_config == "none":
            return inferred_from_config

        if isinstance(api_base, str) and api_base and not _is_openai_cloud_api_base(api_base):
            provider = fingerprint_embedding_server_sync(
                api_base,
                api_key if isinstance(api_key, str) and api_key else None,
            )
            if provider is not None:
                return provider
            return "openai-compatible"

        return inferred_from_config
    except (psycopg.Error, BootstrapConfigError, RuntimeError, OSError):
        logger.debug(
            "Failed to resolve configured embeddings provider from persisted config", exc_info=True
        )
        if raise_storage_errors:
            raise
    return None


# ---------------------------------------------------------------------------
# Config cross-reference (detect mismatches)
# ---------------------------------------------------------------------------


def check_config_mismatches(config: Any) -> list[dict[str, str]]:
    """Cross-reference config against installed binaries.

    Returns list of {"subsystem": ..., "error": ...} for each mismatch.
    """
    issues: list[dict[str, str]] = []

    # Chat candidates vs CLIs
    chat_candidates = getattr(getattr(config, "chat", None), "candidates", ())
    uses_claude_chat = any(
        candidate.partition("/")[0] == "claude"
        for candidate in chat_candidates
        if isinstance(candidate, str)
    )
    if uses_claude_chat and not shutil.which("claude"):
        issues.append(
            {
                "subsystem": "Claude Code",
                "error": "chat candidates include Claude but claude CLI not in PATH",
            }
        )
    # Embedding provider vs local tools
    emb = config.embeddings
    if emb.api_base:
        api_base_lower = emb.api_base.lower()
        if ":1234" in api_base_lower and not shutil.which("lms"):
            issues.append(
                {
                    "subsystem": "LM Studio",
                    "error": f"embeddings configured at {emb.api_base} but lms CLI not installed",
                }
            )
        if ":11434" in api_base_lower and not shutil.which("ollama"):
            issues.append(
                {
                    "subsystem": "Ollama",
                    "error": f"embeddings configured at {emb.api_base} but ollama not installed",
                }
            )

    return issues


# ---------------------------------------------------------------------------
# Aggregate helper
# ---------------------------------------------------------------------------


def collect_all_deps(db: HubDatabase, *, managed_services: bool) -> dict[str, Any]:
    """Collect all dependency info for status display.

    Returns structured dict with all sections.
    """

    def _local_binary_path(name: str) -> str | None:
        path = local_native_bin_path(name)
        return str(path) if path.exists() else None

    try:
        embeddings_provider: str | dict[str, str] | None = get_configured_embedding_provider(
            db, raise_storage_errors=True
        )
    except Exception as exc:
        logger.debug("Failed to probe embeddings provider for status", exc_info=True)
        embeddings_provider = {"status": "degraded", "error": type(exc).__name__}

    dependency_payload = collect_dependency_report(
        managed_services=managed_services,
        include_srt=True,
    ).to_payload()
    return {
        "gobby": {
            "gobby": get_gobby_version(),
            "gcode": get_gcode_version(),
            "gcode_path": _local_binary_path("gcode"),
            "ghook": get_ghook_version(),
            "ghook_path": _local_binary_path("ghook"),
            "gwiki": get_gwiki_version(),
            "gwiki_path": _local_binary_path("gwiki"),
            "impeccable": get_impeccable_version(),
        },
        "coding_clis": {
            "claude": get_claude_code_version(),
            "grok": get_grok_cli_version(),
            "codex": get_codex_cli_version(),
            "droid": get_droid_cli_version(),
            "qwen": get_qwen_cli_version(),
            "agy": get_agy_cli_version(),
            "hooks": get_coding_cli_hooks_status(),
        },
        **dependency_payload,
        "integrations": {
            "tailscale": get_tailscale_info(),
            "embeddings_provider": embeddings_provider,
            "ollama": get_ollama_info(),
            "lmstudio": get_lmstudio_info(),
        },
    }
