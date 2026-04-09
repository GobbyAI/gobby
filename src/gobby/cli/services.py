"""
Service lifecycle utilities for Qdrant, Neo4j, and embedding providers.

Provides status checks for Docker-based services and embedding endpoints
without starting or stopping anything (except optional model auto-load).
"""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


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
            logger.warning(
                "Qdrant health check failed: %s returned %s", healthz_url, resp.status_code
            )
            return False
    except httpx.HTTPError as e:
        logger.warning("Qdrant health check failed: %s unreachable: %s", healthz_url, e)
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
                logger.warning(f"Neo4j health check failed: {url} returned {resp.status_code}")
                return False
            return True
    except httpx.HTTPError as e:
        logger.warning(f"Neo4j health check failed: {url} unreachable: {e}")
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
# Embeddings
# ---------------------------------------------------------------------------


async def is_embedding_healthy(
    model: str,
    api_base: str | None,
    api_key: str | None = None,
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
        )
        return len(result) > 0
    except Exception as e:
        logger.warning(
            f"Embedding health check failed (model={model}, api_base={api_base}): {type(e).__name__}: {e}"
        )
        return False


def _is_lm_studio_endpoint(api_base: str) -> bool:
    """Check if api_base points to a LM Studio endpoint (port 1234)."""
    try:
        parsed = urlparse(api_base)
        return parsed.port == 1234
    except (ValueError, AttributeError):
        return False


def _is_ollama_endpoint(api_base: str) -> bool:
    """Check if api_base points to an Ollama endpoint (port 11434)."""
    try:
        parsed = urlparse(api_base)
        return parsed.port == 11434
    except (ValueError, AttributeError):
        return False


async def try_autoload_embedding_model(model: str, api_base: str | None) -> bool:
    """Attempt to auto-load the embedding model via lms or ollama CLI.

    Called when the health check fails. Returns True if load succeeded.
    Only attempts for local endpoints (api_base matches LM Studio or Ollama ports).
    """
    if not api_base:
        return False

    # LM Studio: try `lms load`
    if _is_lm_studio_endpoint(api_base) and shutil.which("lms"):
        from gobby.cli.installers.embedding import _LMSTUDIO_MODEL_KEY

        try:
            # Run lms load in a thread to avoid blocking the event loop
            # Use the CLI model key, not the API identifier
            result = await asyncio.to_thread(
                subprocess.run,
                ["lms", "load", _LMSTUDIO_MODEL_KEY, "-y"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                logger.info("Auto-loaded embedding model via lms load")
                return True
            logger.warning(f"lms load failed: {result.stderr.strip()}")
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"lms load failed: {e}")

    # Ollama: model is auto-loaded on first request, but we can pull if missing
    if _is_ollama_endpoint(api_base) and shutil.which("ollama"):
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
