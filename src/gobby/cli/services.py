"""
Service lifecycle utilities for Qdrant, Neo4j, FalkorDB, and embedding providers.

Provides status checks for Docker-based services plus local embedding
readiness helpers for managed local dependencies such as LM Studio.
"""

import asyncio
import inspect
import ipaddress
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_LM_STUDIO_STATUS_TIMEOUT = 10
_LM_STUDIO_START_TIMEOUT = 30
_LM_STUDIO_READINESS_TIMEOUT = 20.0
_LM_STUDIO_READINESS_INITIAL_DELAY = 0.25
_LM_STUDIO_READINESS_MAX_DELAY = 2.0
_last_local_embedding_service_failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


def is_qdrant_installed(*, gobby_home: Path | None = None) -> bool:
    """Check if Qdrant service is installed.

    Checks for the unified Docker Compose file (which always includes Qdrant).
    """
    home = gobby_home or Path("~/.gobby").expanduser()
    compose = home / "services" / "docker-compose.yml"
    return compose.exists()


async def is_qdrant_healthy(url: str | None) -> bool:
    """Check if a Qdrant instance is reachable and healthy.

    Sends a GET request to /healthz with a short timeout.
    Returns False if URL is None or unreachable.
    """
    if not url:
        return False
    healthz_url = f"{url.rstrip('/')}/healthz"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(healthz_url, timeout=5)
            if resp.status_code == 200:
                return True
            logger.debug(
                "Qdrant health check failed: %s returned %s", healthz_url, resp.status_code
            )
            return False
    except httpx.HTTPError as e:
        logger.debug(
            "Qdrant health check failed: %s unreachable: %s: %s",
            healthz_url,
            type(e).__name__,
            e,
        )
        return False


async def get_qdrant_status(
    *,
    gobby_home: Path | None = None,
    qdrant_url: str | None = None,
) -> dict[str, Any]:
    """Get comprehensive Qdrant status.

    Returns dict with:
        installed: bool - service files exist
        healthy: bool - API is reachable
        url: str | None - configured URL
    """
    installed = is_qdrant_installed(gobby_home=gobby_home)
    healthy = await is_qdrant_healthy(qdrant_url) if installed else False

    return {
        "installed": installed,
        "healthy": healthy,
        "url": qdrant_url,
    }


# ---------------------------------------------------------------------------
# Neo4j
# ---------------------------------------------------------------------------


def is_neo4j_installed(*, gobby_home: Path | None = None) -> bool:
    """Check if Neo4j services are installed locally.

    Checks for the presence of ~/.gobby/services/neo4j/ directory.
    """
    home = gobby_home or Path("~/.gobby").expanduser()
    return (home / "services" / "neo4j").exists()


async def is_neo4j_healthy(url: str | None) -> bool:
    """Check if a Neo4j instance is reachable and healthy.

    Sends a GET request to the Neo4j HTTP endpoint with a short timeout.
    Returns False if URL is None, unreachable, or returns 5xx.
    """
    if not url:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5)
            if resp.status_code >= 500:
                logger.debug("Neo4j health check failed: %s returned %s", url, resp.status_code)
                return False
            return True
    except httpx.HTTPError as e:
        logger.debug(
            "Neo4j health check failed: %s unreachable: %s: %s",
            url,
            type(e).__name__,
            e,
        )
        return False


async def get_neo4j_status(
    *,
    gobby_home: Path | None = None,
    neo4j_url: str | None = None,
) -> dict[str, Any]:
    """Get comprehensive Neo4j status.

    Returns dict with:
        installed: bool - service directory exists
        healthy: bool - API is reachable
        url: str | None - configured URL
    """
    installed = is_neo4j_installed(gobby_home=gobby_home)
    healthy = await is_neo4j_healthy(neo4j_url) if installed else False

    return {
        "installed": installed,
        "healthy": healthy,
        "url": neo4j_url,
    }


# ---------------------------------------------------------------------------
# FalkorDB
# ---------------------------------------------------------------------------


def is_falkordb_installed(
    *,
    db: Any | None = None,
    gobby_home: Path | None = None,
) -> bool:
    """Check whether FalkorDB connection keys were recorded in config_store."""
    owned_db: Any | None = None
    if db is None:
        from gobby.cli.utils import get_gobby_home
        from gobby.config.bootstrap import load_bootstrap
        from gobby.storage.database import LocalDatabase

        home = gobby_home if gobby_home is not None else get_gobby_home()
        bootstrap_file = home / "bootstrap.yaml"
        if bootstrap_file.exists():
            db_path = Path(load_bootstrap(str(bootstrap_file)).database_path).expanduser()
        else:
            db_path = home / "gobby-hub.db"
        db = LocalDatabase(db_path)
        owned_db = db

    from gobby.storage.config_store import ConfigStore

    try:
        store = ConfigStore(db)
        return (
            store.get("databases.falkordb.host") is not None
            and store.get("databases.falkordb.port") is not None
        )
    finally:
        if owned_db is not None:
            owned_db.close()


async def is_falkordb_healthy(
    host: str | None,
    port: int | None,
    password: str | None,
) -> bool:
    """Check if FalkorDB responds to Redis PING."""
    if not host or not port:
        return False

    import redis.asyncio as redis

    client: Any | None = None
    try:
        client = redis.Redis(host=host, port=port, password=password, socket_timeout=5)
        result = client.ping()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except Exception as exc:
        logger.debug(
            "FalkorDB health check failed: %s:%s unreachable: %s: %s",
            host,
            port,
            type(exc).__name__,
            exc,
        )
        return False
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def get_falkordb_status(
    *,
    db: Any | None = None,
    gobby_home: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Get FalkorDB install and runtime health status."""
    installed = is_falkordb_installed(db=db, gobby_home=gobby_home)
    healthy = await is_falkordb_healthy(host, port, password) if installed else False

    return {
        "installed": installed,
        "healthy": healthy,
        "url": f"redis://{host}:{port}" if host and port else None,
    }


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


async def is_embedding_healthy(
    model: str,
    api_base: str | None,
    api_key: str | None = None,
    expected_dim: int | None = None,
) -> bool:
    """Check if the embedding endpoint is reachable.

    Sends a single short embedding request with max_retries=1. Returns False
    on any exception. Logs a warning on failure.
    """
    from gobby.search.embeddings import generate_embedding

    try:
        result = await generate_embedding(
            "health",
            model=model,
            api_base=api_base,
            api_key=api_key,
            max_retries=1,
            expected_dim=expected_dim,
        )
        return len(result) > 0
    except Exception as e:
        logger.warning(
            f"Embedding health check failed (model={model}, api_base={api_base}): {type(e).__name__}: {e}"
        )
        return False


def get_local_embedding_service_failure_reason() -> str | None:
    """Return the last local embedding readiness failure, if any."""
    return _last_local_embedding_service_failure_reason


def _set_local_embedding_service_failure_reason(reason: str | None) -> None:
    global _last_local_embedding_service_failure_reason
    _last_local_embedding_service_failure_reason = reason


def _is_loopback_host(hostname: str | None) -> bool:
    """Return True when a hostname resolves to loopback without doing DNS."""
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_lm_studio_endpoint(api_base: str) -> bool:
    """Check if api_base points to a loopback LM Studio endpoint (port 1234)."""
    try:
        parsed = urlparse(api_base)
        return parsed.port == 1234 and _is_loopback_host(parsed.hostname)
    except (ValueError, AttributeError):
        return False


def _is_ollama_endpoint(api_base: str) -> bool:
    """Check if api_base points to a loopback Ollama endpoint (port 11434)."""
    try:
        parsed = urlparse(api_base)
        return parsed.port == 11434 and _is_loopback_host(parsed.hostname)
    except (ValueError, AttributeError):
        return False


async def _run_cli_command(
    command: list[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a CLI command off the event loop and capture text output."""
    return await asyncio.to_thread(
        subprocess.run,
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    """Return the most useful stderr/stdout snippet for a failed command."""
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"


def _lm_studio_model_loaded(ps_output: str, *, model: str, cli_model_key: str) -> bool:
    """Return True when lms ps output already contains the embedding model."""
    output = ps_output.lower()
    candidates = {
        model.lower(),
        cli_model_key.lower(),
        model.lower().removeprefix("text-embedding-"),
    }
    return any(candidate and candidate in output for candidate in candidates)


def _lm_studio_load_identifier(model: str, *, fallback_model_key: str) -> str:
    """Return the identifier LM Studio should load for a configured embedding model."""
    if model.startswith("text-embedding-") and "@" in model:
        return model
    return fallback_model_key


async def _wait_for_embedding_models_ready(api_base: str, api_key: str | None) -> bool:
    """Poll the OpenAI-compatible /models route until the endpoint answers."""
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{api_base.rstrip('/')}/models"
    deadline = time.monotonic() + _LM_STUDIO_READINESS_TIMEOUT
    delay = _LM_STUDIO_READINESS_INITIAL_DELAY

    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(url, headers=headers, timeout=5.0)
                if 200 <= response.status_code < 300:
                    return True
            except httpx.HTTPError:
                pass

            if time.monotonic() >= deadline:
                return False

            await asyncio.sleep(delay)
            delay = min(delay * 2, _LM_STUDIO_READINESS_MAX_DELAY)


async def ensure_local_embedding_service_ready(
    model: str,
    api_base: str | None,
    api_key: str | None = None,
    expected_dim: int | None = None,
) -> bool:
    """Ensure a local embedding backend is ready to serve requests."""
    _set_local_embedding_service_failure_reason(None)

    if not api_base:
        return False

    if _is_lm_studio_endpoint(api_base):
        if not shutil.which("lms"):
            _set_local_embedding_service_failure_reason("LM Studio CLI not found on PATH")
            return False

        try:
            status_result = await _run_cli_command(
                ["lms", "server", "status"],
                timeout=_LM_STUDIO_STATUS_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            _set_local_embedding_service_failure_reason(f"LM Studio server status failed: {exc}")
            return False

        combined_status = (status_result.stdout + status_result.stderr).lower()
        server_running = status_result.returncode == 0 and "running" in combined_status
        if not server_running:
            try:
                start_result = await _run_cli_command(
                    ["lms", "server", "start"],
                    timeout=_LM_STUDIO_START_TIMEOUT,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                _set_local_embedding_service_failure_reason(f"LM Studio server start failed: {exc}")
                return False
            if start_result.returncode != 0:
                _set_local_embedding_service_failure_reason(
                    f"LM Studio server start failed: {_command_error(start_result)}"
                )
                return False

        if not await _wait_for_embedding_models_ready(api_base, api_key):
            _set_local_embedding_service_failure_reason(
                f"LM Studio readiness timed out at {api_base}"
            )
            return False

        if await is_embedding_healthy(
            model=model,
            api_base=api_base,
            api_key=api_key,
            expected_dim=expected_dim,
        ):
            return True

        if not await try_autoload_embedding_model(model, api_base):
            _set_local_embedding_service_failure_reason(f"LM Studio model load failed for {model}")
            return False

        if not await is_embedding_healthy(
            model=model,
            api_base=api_base,
            api_key=api_key,
            expected_dim=expected_dim,
        ):
            _set_local_embedding_service_failure_reason(
                f"LM Studio final health check failed at {api_base}"
            )
            return False
        return True

    healthy = await is_embedding_healthy(
        model=model,
        api_base=api_base,
        api_key=api_key,
        expected_dim=expected_dim,
    )
    if healthy or not _is_ollama_endpoint(api_base):
        return healthy

    if not await try_autoload_embedding_model(model, api_base):
        return False

    return await is_embedding_healthy(
        model=model,
        api_base=api_base,
        api_key=api_key,
        expected_dim=expected_dim,
    )


async def try_autoload_embedding_model(model: str, api_base: str | None) -> bool:
    """Attempt to auto-load the embedding model via lms or ollama CLI.

    Called when the health check fails. Returns True if load succeeded.
    Only attempts for loopback endpoints (LM Studio/Ollama default local ports).
    """
    if not api_base:
        return False

    if _is_ollama_endpoint(api_base):
        if shutil.which("ollama"):
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["ollama", "pull", model],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0:
                    logger.info(f"Auto-pulled embedding model via ollama pull {model}")
                    return True
                logger.warning(f"ollama pull failed: {result.stderr.strip()}")
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning(f"ollama pull failed: {e}")
        return False

    # LM Studio: try `lms load`
    if _is_lm_studio_endpoint(api_base) and shutil.which("lms"):
        from gobby.cli.installers.embedding import _LMSTUDIO_MODEL_KEY

        load_identifier = _lm_studio_load_identifier(
            model,
            fallback_model_key=_LMSTUDIO_MODEL_KEY,
        )
        try:
            ps_result = await _run_cli_command(["lms", "ps"], timeout=_LM_STUDIO_STATUS_TIMEOUT)
            if ps_result.returncode == 0 and _lm_studio_model_loaded(
                ps_result.stdout + ps_result.stderr,
                model=model,
                cli_model_key=load_identifier,
            ):
                logger.debug("LM Studio embedding model already loaded")
                return True

            # Run lms load off the event loop, using the exact configured LM Studio id when present.
            result = await _run_cli_command(
                ["lms", "load", load_identifier, "-y"],
                timeout=120,
            )
            if result.returncode == 0:
                logger.info("Auto-loaded embedding model via lms load")
                return True
            logger.warning(f"lms load failed: {result.stderr.strip()}")
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"lms load failed: {e}")

    return False
