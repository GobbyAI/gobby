"""Embedding provider installer for gobby install.

Configures an embedding provider (LM Studio, Ollama, or OpenAI) and persists
the settings to config_store so the daemon picks them up on next start.

For local providers, ensures the model is downloaded and loaded.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Provider configuration table
_PROVIDER_CONFIG: dict[str, dict[str, Any]] = {
    "lmstudio": {
        "model": "text-embedding-nomic-embed-text-v1.5@q8_0",
        "api_base": "http://localhost:1234/v1",
        "dim": 768,
    },
    "ollama": {
        "model": "nomic-embed-text",
        "api_base": "http://localhost:11434/v1",
        "dim": 768,
    },
    "openai": {
        "model": "text-embedding-3-small",
        "api_base": None,  # uses OpenAI default
        "dim": 1536,
    },
    "none": {
        "model": None,
        "api_base": None,
        "dim": 0,
    },
}

# LM Studio model key to download (nomic-embed-text-v1.5 GGUF)
_LMSTUDIO_MODEL_KEY = "nomic-embed-text-v1.5"
# Ollama model name
_OLLAMA_MODEL_NAME = "nomic-embed-text"


def install_embedding(
    provider: str,
    openai_api_key: str | None = None,
) -> dict[str, Any]:
    """Set up an embedding provider and persist config to config_store.

    Args:
        provider: One of "lmstudio", "ollama", "openai", "none"
        openai_api_key: Required when provider="openai"

    Returns:
        Dict with success status and details:
            {"success": True, "provider": str, "model": str, "dim": int,
             "api_base": str | None, "health_check": bool}
        or on failure:
            {"success": False, "error": str}
    """
    if provider not in _PROVIDER_CONFIG:
        return {"success": False, "error": f"Unknown provider: {provider}"}

    if provider == "none":
        _persist_embedding_config(model=None, api_base=None, dim=0, provider="none")
        return {
            "success": True,
            "provider": "none",
            "model": None,
            "dim": 0,
            "api_base": None,
            "health_check": False,
            "skipped": True,
        }

    if provider == "openai" and not openai_api_key:
        return {"success": False, "error": "OpenAI API key required for openai provider"}

    # Provider-specific setup: ensure model is downloaded and loaded
    setup_result: dict[str, Any]
    if provider == "lmstudio":
        setup_result = _setup_lmstudio()
    elif provider == "ollama":
        setup_result = _setup_ollama()
    else:  # openai
        setup_result = {"success": True}

    if not setup_result["success"]:
        return setup_result

    cfg = _PROVIDER_CONFIG[provider]
    model = cfg["model"]
    api_base = cfg["api_base"]
    dim = cfg["dim"]

    # Health check before persisting
    health_ok = _health_check_embedding(
        model=model,
        api_base=api_base,
        api_key=openai_api_key,
        expected_dim=dim,
    )
    if not health_ok:
        return {
            "success": False,
            "error": (
                f"Embedding health check failed for {provider} "
                f"(model={model}, api_base={api_base or 'default'})"
            ),
        }

    # Persist to config_store (and SecretStore for OpenAI key)
    try:
        _persist_embedding_config(
            model=model,
            api_base=api_base,
            dim=dim,
            provider=provider,
            openai_api_key=openai_api_key,
        )
    except Exception as e:
        return {"success": False, "error": f"Failed to persist config: {e}"}

    return {
        "success": True,
        "provider": provider,
        "model": model,
        "dim": dim,
        "api_base": api_base,
        "health_check": True,
    }


def _setup_lmstudio() -> dict[str, Any]:
    """Ensure LM Studio has nomic loaded. Uses lms CLI.

    Steps:
    1. Check `lms` is on PATH
    2. Check `lms server status` — start if not running
    3. Check `lms ps` for nomic — if loaded, done
    4. Check `lms ls` for nomic — if on disk, `lms load` it
    5. If not on disk, `lms get` then `lms load`
    """
    if not shutil.which("lms"):
        return {
            "success": False,
            "error": (
                "lms CLI not found. Install LM Studio from https://lmstudio.ai and "
                "run `lms bootstrap` to add it to PATH."
            ),
        }

    # 2. Ensure server is running
    try:
        result = subprocess.run(
            ["lms", "server", "status"], capture_output=True, text=True, timeout=10
        )
        combined = (result.stdout + result.stderr).lower()
        if result.returncode != 0 or "running" not in combined:
            # Try to start it
            start_result = subprocess.run(
                ["lms", "server", "start"], capture_output=True, text=True, timeout=30
            )
            if start_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Failed to start LM Studio server: {start_result.stderr.strip()}",
                }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"lms server check failed: {e}"}

    # 3. Check if nomic is already loaded
    try:
        ps_result = subprocess.run(["lms", "ps"], capture_output=True, text=True, timeout=10)
        if ps_result.returncode == 0 and "nomic" in ps_result.stdout.lower():
            return {"success": True, "action": "already_loaded"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"lms ps failed: {e}"}

    # 4. Check if on disk
    try:
        ls_result = subprocess.run(["lms", "ls"], capture_output=True, text=True, timeout=15)
        on_disk = ls_result.returncode == 0 and "nomic" in ls_result.stdout.lower()
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"lms ls failed: {e}"}

    # 5. Download if needed
    if not on_disk:
        try:
            get_result = subprocess.run(
                ["lms", "get", _LMSTUDIO_MODEL_KEY, "--gguf", "-y"],
                capture_output=True,
                text=True,
                timeout=600,  # model is ~146MB, allow time for download
            )
            if get_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"lms get failed: {get_result.stderr.strip() or get_result.stdout.strip()}",
                }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "lms get timed out (10 min)"}
        except OSError as e:
            return {"success": False, "error": f"lms get failed: {e}"}

    # Load the model
    try:
        load_result = subprocess.run(
            ["lms", "load", _LMSTUDIO_MODEL_KEY, "-y"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if load_result.returncode != 0:
            return {
                "success": False,
                "error": f"lms load failed: {load_result.stderr.strip() or load_result.stdout.strip()}",
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "lms load timed out"}
    except OSError as e:
        return {"success": False, "error": f"lms load failed: {e}"}

    return {"success": True, "action": "loaded"}


def _setup_ollama() -> dict[str, Any]:
    """Ensure Ollama has nomic-embed-text. Uses ollama CLI.

    Steps:
    1. Check `ollama` is on PATH
    2. Check `ollama list` for nomic-embed-text — if present, done
    3. Otherwise `ollama pull nomic-embed-text`
    """
    if not shutil.which("ollama"):
        return {
            "success": False,
            "error": (
                "ollama not found. Install Ollama from https://ollama.com and ensure it is on PATH."
            ),
        }

    # Check if model is already pulled
    try:
        list_result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
        if list_result.returncode == 0 and _OLLAMA_MODEL_NAME in list_result.stdout:
            return {"success": True, "action": "already_pulled"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"ollama list failed: {e}"}

    # Pull the model (nomic-embed-text is ~274MB)
    try:
        pull_result = subprocess.run(
            ["ollama", "pull", _OLLAMA_MODEL_NAME],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if pull_result.returncode != 0:
            return {
                "success": False,
                "error": f"ollama pull failed: {pull_result.stderr.strip() or pull_result.stdout.strip()}",
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "ollama pull timed out (10 min)"}
    except OSError as e:
        return {"success": False, "error": f"ollama pull failed: {e}"}

    return {"success": True, "action": "pulled"}


def _persist_embedding_config(
    model: str | None,
    api_base: str | None,
    dim: int,
    provider: str,
    openai_api_key: str | None = None,
) -> None:
    """Write embedding config to the unified embeddings.* namespace via ConfigStore.

    Sets: embeddings.model, embeddings.api_base, embeddings.dim

    For the "none" provider, writes null/zero values to disable semantic search.
    If openai_api_key is provided, stores it in SecretStore.
    """
    from gobby.config.app import load_config
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.database import LocalDatabase
    from gobby.storage.secrets import SecretStore

    config = load_config()
    db_path = Path(config.database_path).expanduser()
    with LocalDatabase(db_path) as db:
        store = ConfigStore(db)

        entries: dict[str, Any]
        if provider == "none":
            entries = {
                "embeddings.model": None,
                "embeddings.api_base": None,
                "embeddings.dim": 0,
            }
        else:
            entries = {
                "embeddings.model": model,
                "embeddings.api_base": api_base,
                "embeddings.dim": dim,
            }

        store.set_many(entries, source="install")

        # Store OpenAI API key in SecretStore
        if openai_api_key:
            secret_store = SecretStore(db)
            secret_store.set(
                name="openai_api_key",
                plaintext_value=openai_api_key,
                category="llm",
                description="OpenAI API key for embeddings (set by gobby install)",
            )


def _health_check_embedding(
    model: str,
    api_base: str | None,
    api_key: str | None = None,
    expected_dim: int | None = None,
) -> bool:
    """Fire a single test embedding. Returns True on success."""
    from gobby.search.embeddings import generate_embedding

    async def _check() -> bool:
        try:
            result = await generate_embedding(
                "gobby health check",
                model=model,
                api_base=api_base,
                api_key=api_key,
                max_retries=1,
                expected_dim=expected_dim,
            )
            return len(result) > 0
        except Exception as e:
            logger.warning(f"Embedding health check failed: {e}")
            return False

    try:
        return asyncio.run(_check())
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            logger.warning("Cannot run health check: already in event loop")
            return False
        raise
