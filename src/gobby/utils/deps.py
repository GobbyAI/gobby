"""External dependency and CLI version detection.

Provides version info for the operational health dashboard (gobby status).
Each function returns None if the tool is not installed/available.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # nosec B404 # needed for version detection
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

from gobby.utils.native_bin import local_native_bin_path, resolve_native_bin

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


def _extract_version_token(output: str) -> str | None:
    """Extract the trailing version token from a ``--version`` style output."""
    parts = output.split()
    return parts[-1] if parts else output


def _get_native_binary_version(binary_name: str, stamp_name: str | None = None) -> str | None:
    """Get a managed native binary version from a stamp file or resolved CLI."""
    if stamp_name:
        stamped_version = _read_version_stamp(stamp_name)
        if stamped_version:
            return stamped_version

    binary_path = resolve_native_bin(binary_name)
    if not binary_path:
        return None

    output = _run_cmd([binary_path, "--version"])
    if output:
        return _extract_version_token(output)
    return None


def get_gcode_version() -> str | None:
    """Get gcode version from stamp file or CLI."""
    return _get_native_binary_version("gcode", ".gcode-version")


def get_gsqz_version() -> str | None:
    """Get gsqz version from stamp file or CLI."""
    return _get_native_binary_version("gsqz", ".gsqz-version")


def get_ghook_version() -> str | None:
    """Get ghook version from stamp file or CLI."""
    return _get_native_binary_version("ghook", ".ghook-version")


def get_gloc_version() -> str | None:
    """Get gloc version from stamp file or CLI."""
    return _get_native_binary_version("gloc", ".gloc-version")


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


def get_gemini_cli_version() -> str | None:
    """Get Gemini CLI version."""
    output = _run_cmd(["gemini", "--version"])
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

    # Gemini: ~/.gemini/settings.json
    gemini_settings = Path.home() / ".gemini" / "settings.json"
    result["gemini"] = _check_hooks_in_file(gemini_settings)

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
    return Path.home() / ".factory" / "hooks" / "hooks.json"


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
        running = "running" in output.lower()
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
            running = result.returncode == 0 and "running" in combined
        except Exception:
            pass
    return {"running": running}


def _normalize_embedding_provider(provider: Any) -> str | None:
    """Normalize a configured embeddings provider value."""
    if not isinstance(provider, str):
        return None
    normalized = provider.strip().lower()
    if normalized in {"lmstudio", "ollama", "openai", "none"}:
        return normalized
    return None


def _infer_embedding_provider_from_api_base(api_base: Any) -> str | None:
    """Infer a local embeddings provider from the configured API base."""
    if not isinstance(api_base, str):
        return None
    api_base_lower = api_base.lower()
    if ":1234" in api_base_lower:
        return "lmstudio"
    if ":11434" in api_base_lower:
        return "ollama"
    return None


def _detect_openai(db: HubDatabase | None = None) -> str | None:
    """Detect OpenAI embeddings from stored or environment credentials."""
    if db is not None:
        try:
            from gobby.storage.secrets import SecretStore

            if SecretStore(db).get("openai_api_key"):
                return "openai"
        except (OSError, RuntimeError, ValueError):
            logger.debug("Failed to read openai_api_key from SecretStore", exc_info=True)

    return "openai" if os.environ.get("OPENAI_API_KEY") else None


def _infer_from_env_or_none(dim: Any, db: HubDatabase | None = None) -> str | None:
    """Return the env-backed OpenAI provider, or explicit disabled state, or None."""
    normalized_dim = dim
    if isinstance(dim, str):
        stripped = dim.strip()
        if stripped:
            try:
                normalized_dim = int(stripped)
            except ValueError:
                normalized_dim = stripped
    if normalized_dim == 0:
        return "none"
    return _detect_openai(db)


def get_configured_embedding_provider() -> str | None:
    """Get the configured embeddings provider from persisted config."""
    try:
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.hub.runtime import runtime_hub_database

        with runtime_hub_database(apply_migrations=False) as db:
            store = ConfigStore(db)
            api_base = store.get("embeddings.api_base")
            dim = store.get("embeddings.dim")

            if api_base == "":
                return _infer_from_env_or_none(dim=dim, db=db)

            provider = _infer_embedding_provider_from_api_base(api_base)
            if provider is not None:
                return provider

            if api_base is None:
                return _infer_from_env_or_none(dim=dim, db=db)
    except Exception:
        logger.debug(
            "Failed to resolve configured embeddings provider from persisted config", exc_info=True
        )
    return _detect_openai()


# ---------------------------------------------------------------------------
# Config cross-reference (detect mismatches)
# ---------------------------------------------------------------------------


def check_config_mismatches(config: Any) -> list[dict[str, str]]:
    """Cross-reference config against installed binaries.

    Returns list of {"subsystem": ..., "error": ...} for each mismatch.
    """
    issues: list[dict[str, str]] = []

    # LLM providers vs CLIs
    providers = config.llm_providers
    if providers.claude and not shutil.which("claude"):
        issues.append(
            {
                "subsystem": "Claude Code",
                "error": "provider configured but claude CLI not in PATH",
            }
        )
    if providers.codex and not shutil.which("codex"):
        issues.append(
            {
                "subsystem": "Codex",
                "error": "provider configured but codex CLI not in PATH",
            }
        )
    if providers.gemini and not shutil.which("gemini"):
        issues.append(
            {
                "subsystem": "Gemini",
                "error": "provider configured but gemini CLI not in PATH",
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


def collect_all_deps() -> dict[str, Any]:
    """Collect all dependency info for status display.

    Returns structured dict with all sections.
    """

    def _local_binary_path(name: str) -> str | None:
        path = local_native_bin_path(name)
        return str(path) if path.exists() else None

    return {
        "gobby": {
            "gobby": get_gobby_version(),
            "gcode": get_gcode_version(),
            "gcode_path": _local_binary_path("gcode"),
            "gsqz": get_gsqz_version(),
            "gsqz_path": _local_binary_path("gsqz"),
            "ghook": get_ghook_version(),
            "ghook_path": _local_binary_path("ghook"),
            "gloc": get_gloc_version(),
            "gloc_path": _local_binary_path("gloc"),
        },
        "coding_clis": {
            "claude": get_claude_code_version(),
            "gemini": get_gemini_cli_version(),
            "codex": get_codex_cli_version(),
            "droid": get_droid_cli_version(),
            "qwen": get_qwen_cli_version(),
            "hooks": get_coding_cli_hooks_status(),
        },
        "dependencies": {
            "tmux": get_tmux_version(),
            "docker": get_docker_version(),
            "docker_running": get_docker_running(),
            "git": get_git_version(),
            "node": get_node_version(),
            "tailscale": get_tailscale_info(),
            "embeddings_provider": get_configured_embedding_provider(),
            "ollama": get_ollama_info(),
            "lmstudio": get_lmstudio_info(),
        },
    }
