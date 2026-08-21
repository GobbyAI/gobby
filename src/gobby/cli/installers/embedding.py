"""Embedding provider installer for gobby install.

Configures an embedding provider (LM Studio, Ollama, or OpenAI) and persists
the settings to config_store so the daemon picks them up on next start.

For local providers, ensures the model is downloaded and loaded.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

from gobby.agents.local_model import LocalModelError
from gobby.ai.embeddings import EmbeddingGenerationError, EmbeddingService

if TYPE_CHECKING:
    from gobby.storage.config_store import ConfigStore

logger = logging.getLogger(__name__)

_DEFAULT_EMBEDDING_DIM = 768
_LMSTUDIO_MODEL_ID = "text-embedding-nomic-embed-text-v1.5@f16"
_GENERIC_OPENAI_COMPATIBLE_MODEL = "nomic-embed-text"

# Provider configuration table
_PROVIDER_CONFIG: dict[str, dict[str, Any]] = {
    "lmstudio": {
        "model": _LMSTUDIO_MODEL_ID,
        "api_base": "http://localhost:1234/v1",
        "dim": _DEFAULT_EMBEDDING_DIM,
    },
    "ollama": {
        "model": "nomic-embed-text",
        "api_base": "http://localhost:11434/v1",
        "dim": _DEFAULT_EMBEDDING_DIM,
    },
    "openai": {
        "model": "text-embedding-3-small",
        "api_base": None,  # uses OpenAI default
        "dim": 1536,
    },
    "openai-compatible": {
        "model": _GENERIC_OPENAI_COMPATIBLE_MODEL,
        "api_base": None,
        "dim": _DEFAULT_EMBEDDING_DIM,
    },
    "vllm": {
        # Operator-started server: the served id is resolved live from
        # /v1/models and the api_base is always explicit, so neither has a
        # bundled default. The dim is probed, never defaulted.
        "model": None,
        "api_base": None,
        "dim": None,
    },
    "none": {
        "model": None,
        "api_base": None,
        "dim": 0,
    },
}

# LM Studio model key to download (nomic-embed-text-v1.5 GGUF)
_LMSTUDIO_MODEL_KEY = "nomic-embed-text-v1.5"


def _resolve_vllm_served_model_sync(api_base: str, api_key: str | None) -> str:
    """Resolve the single served id from the vLLM server, or raise LocalModelError."""
    from gobby.agents.local_model import select_vllm_served_model, vllm_served_model_ids

    async def _resolve() -> str:
        served = await vllm_served_model_ids(api_base, api_key)
        return select_vllm_served_model("auto", served, api_base=api_base)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_resolve())
    raise LocalModelError("Cannot resolve the vLLM served model from inside an event loop")


# Ollama model name
_OLLAMA_MODEL_NAME = "nomic-embed-text"


def install_embedding(
    provider: str,
    embedding_api_key: str | None = None,
    *,
    catalog_key: str | None = None,
    model_override: str | None = None,
    api_base_override: str | None = None,
    dim_override: int | None = None,
) -> dict[str, Any]:
    """Set up an embedding provider and persist config to config_store.

    Args:
        provider: One of "lmstudio", "ollama", "openai", "openai-compatible", "none"
        embedding_api_key: Required when provider="openai"; optional for any
            embedding endpoint that requires authentication.
        catalog_key: Embedding catalog key (e.g. "qwen3-8b-q8"). When provided,
            resolves model/dim/query_prefix from the catalog and uses
            provider-specific refs for pulling. Overrides take precedence.
        model_override: Override the provider's default model id
        api_base_override: Override the provider's default ``api_base`` URL
        dim_override: Override the embedding dimension. When omitted and the
            user supplied ``model_override`` or ``api_base_override``, the
            installer probes ``/v1/embeddings`` to detect the dim automatically.

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

    if provider == "openai" and not embedding_api_key:
        return {"success": False, "error": "Embedding API key required for openai provider"}
    if provider == "openai-compatible" and not api_base_override:
        return {
            "success": False,
            "error": "Custom OpenAI-compatible embedding provider requires --embedding-url",
        }
    if provider == "vllm" and not api_base_override:
        return {
            "success": False,
            "error": "vLLM embedding provider requires --embedding-url",
        }

    # Provider-specific setup: ensure model is downloaded and loaded.
    # Skip bundled local setup when the user points at their own model or endpoint.
    # When catalog_key is provided, use catalog refs for pulling.
    catalog_spec = None
    if catalog_key is not None:
        from gobby.ai.embedding_catalog import catalog_model_for_provider, get_spec

        catalog_spec = get_spec(catalog_key)
        if catalog_spec is None:
            return {"success": False, "error": f"Unknown embedding catalog key: {catalog_key}"}

    setup_result: dict[str, Any]
    if provider == "lmstudio" and model_override is None and api_base_override is None:
        if catalog_spec is not None:
            setup_result = _setup_lmstudio(
                model_id=catalog_spec.lmstudio_ref,
                gguf_repo=catalog_spec.gguf_repo,
                gguf_filename=catalog_spec.gguf_filename,
                search_keyword=catalog_spec.gguf_filename,
            )
        else:
            setup_result = _setup_lmstudio()
    elif provider == "ollama" and model_override is None and api_base_override is None:
        if catalog_spec is not None:
            setup_result = _setup_ollama(ollama_tag=catalog_spec.ollama_tag)
        else:
            setup_result = _setup_ollama()
    else:
        setup_result = {"success": True}

    if not setup_result["success"]:
        return setup_result

    cfg = _PROVIDER_CONFIG[provider]
    # When catalog_key is provided, use catalog values as defaults (overrides still win)
    if catalog_spec is not None:
        catalog_model = catalog_model_for_provider(catalog_spec, provider)
        model = model_override if model_override is not None else catalog_model
        if model is None and provider == "vllm" and api_base_override:
            try:
                model = _resolve_vllm_served_model_sync(api_base_override, embedding_api_key)
            except LocalModelError as exc:
                return {"success": False, "error": str(exc)}
        if model is None:
            return {
                "success": False,
                "error": (
                    f"Provider {provider!r} has no bundled catalog model reference; "
                    "pass --embedding-model"
                ),
            }
        api_base = api_base_override if api_base_override is not None else cfg["api_base"]
        if dim_override is not None:
            dim = dim_override
        elif model_override is not None or api_base_override is not None:
            probed = _probe_embedding_dim(model=model, api_base=api_base, api_key=embedding_api_key)
            if probed is None:
                return {"success": False, "error": _dim_probe_error(model, api_base)}
            if provider == "vllm" and probed != catalog_spec.dim:
                return {
                    "success": False,
                    "error": (
                        f"vLLM served model {model!r} probes dim {probed}, but catalog "
                        f"key {catalog_key!r} expects dim {catalog_spec.dim}"
                    ),
                }
            dim = probed
        else:
            dim = catalog_spec.dim
    else:
        model = model_override if model_override is not None else cfg["model"]
        if model is None and provider == "vllm" and api_base_override:
            try:
                model = _resolve_vllm_served_model_sync(api_base_override, embedding_api_key)
            except LocalModelError as exc:
                return {"success": False, "error": str(exc)}
        if model is None:
            return {
                "success": False,
                "error": f"Provider {provider!r} requires --embedding-model",
            }
        api_base = api_base_override if api_base_override is not None else cfg["api_base"]

        if dim_override is not None:
            dim = dim_override
        elif model_override is not None or api_base_override is not None:
            probed = _probe_embedding_dim(model=model, api_base=api_base, api_key=embedding_api_key)
            if probed is not None:
                dim = probed
            use_provider_default_fallback = (
                api_base_override is not None and model_override is None and cfg["dim"] is not None
            )
            if probed is None and use_provider_default_fallback:
                dim = cfg["dim"]
                logger.warning(
                    "Embedding dim probe failed for provider-default model %s at %s; "
                    "falling back to default dim %s before health check",
                    model,
                    api_base,
                    dim,
                )
            elif probed is None:
                return {"success": False, "error": _dim_probe_error(model, api_base)}
        else:
            dim = cfg["dim"]

    # Health check before persisting
    health_ok = _health_check_embedding(
        model=model,
        api_base=api_base,
        api_key=embedding_api_key,
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

    # Semantic smoke test: verify related > unrelated + norm ≈ 1.0
    smoke_ok = _semantic_smoke_test(
        model=model,
        api_base=api_base,
        api_key=embedding_api_key,
        query_prefix=catalog_spec.query_prefix if catalog_spec else None,
    )
    if not smoke_ok:
        return {
            "success": False,
            "error": (
                f"Embedding semantic smoke test failed for {provider} "
                f"(model={model}). The model may have wrong pooling/EOS/normalization."
            ),
        }

    # Persist to config_store (and SecretStore for OpenAI key)
    query_prefix = catalog_spec.query_prefix if catalog_spec else None
    resolved_catalog_key = catalog_key if catalog_key else None
    try:
        _persist_embedding_config(
            model=model,
            api_base=api_base,
            dim=dim,
            provider=provider,
            embedding_api_key=embedding_api_key,
            query_prefix=query_prefix,
            catalog_key=resolved_catalog_key,
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
        "catalog_key": resolved_catalog_key,
    }


def _setup_lmstudio(
    *,
    model_id: str = _LMSTUDIO_MODEL_ID,
    gguf_repo: str | None = None,
    gguf_filename: str | None = None,
    search_keyword: str | None = None,
) -> dict[str, Any]:
    """Ensure LM Studio has the embedding model loaded. Uses lms CLI.

    Args:
        model_id: LM Studio model identifier for ``lms load`` (e.g.
            ``text-embedding-nomic-embed-text-v1.5@f16``).
        gguf_repo: HuggingFace GGUF repo for ``lms get`` (e.g.
            ``nomic-ai/nomic-embed-text-v1.5-GGUF``). When None, uses
            the legacy hardcoded model key.
        gguf_filename: GGUF filename within the repo (e.g.
            ``nomic-embed-text-v1.5.f16.gguf``). Used with ``lms get --gguf``.
        search_keyword: Exact requested model identifier or filename to match
            in ``lms ls`` output for the on-disk check. When None, derives from
            the model_id.

    Steps:
    1. Check `lms` is on PATH
    2. Check `lms server status` — start if not running
    3. Check `lms ps` for the model — if loaded, done
    4. Check `lms ls` for the model — if on disk, `lms load` it
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

    # 3. Check if model is already loaded
    try:
        ps_result = subprocess.run(["lms", "ps"], capture_output=True, text=True, timeout=10)
        if ps_result.returncode == 0 and model_id.lower() in ps_result.stdout.lower():
            return {"success": True, "action": "already_loaded"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"lms ps failed: {e}"}

    # 4. Check if on disk
    try:
        ls_result = subprocess.run(["lms", "ls"], capture_output=True, text=True, timeout=15)
        # Match by the requested model identifier or filename. Family-level
        # matches can skip downloading the specific catalog artifact.
        if search_keyword is not None:
            keyword = search_keyword.lower()
        else:
            # Derive from model_id: use the part before @, after last /
            base = model_id.split("@")[0].split("/")[-1].lower()
            # Use a short keyword (e.g. "nomic" from "text-embedding-nomic-embed-text-v1.5")
            keyword = "nomic" if "nomic" in base else base[:20]
        ls_output = ls_result.stdout.lower()
        on_disk = ls_result.returncode == 0 and keyword in ls_output
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"lms ls failed: {e}"}

    # 5. Download if needed
    if not on_disk:
        try:
            if gguf_repo is not None and gguf_filename is not None:
                get_cmd = ["lms", "get", gguf_repo, "--gguf", gguf_filename, "-y"]
            else:
                get_cmd = ["lms", "get", _LMSTUDIO_MODEL_KEY, "--gguf", "-y"]
            get_result = subprocess.run(
                get_cmd,
                capture_output=True,
                text=True,
                timeout=600,
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
            ["lms", "load", model_id, "-y"],
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


def _setup_ollama(*, ollama_tag: str = _OLLAMA_MODEL_NAME) -> dict[str, Any]:
    """Ensure Ollama has the embedding model. Uses ollama CLI.

    Args:
        ollama_tag: Ollama model tag for ``ollama pull`` (e.g.
            ``nomic-embed-text`` or ``qwen3-embedding:8b-q8_0``).

    Steps:
    1. Check `ollama` is on PATH
    2. Check `ollama list` for the model — if present, done
    3. Otherwise `ollama pull <ollama_tag>`
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
        if list_result.returncode == 0 and ollama_tag in list_result.stdout:
            return {"success": True, "action": "already_pulled"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"ollama list failed: {e}"}

    # Pull the model
    try:
        pull_result = subprocess.run(
            ["ollama", "pull", ollama_tag],
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
    embedding_api_key: str | None = None,
    query_prefix: str | None = None,
    catalog_key: str | None = None,
) -> None:
    """Write embedding config to the canonical embedding namespace via ConfigStore.

    Sets the model, API base, vector dimension, query prefix, and catalog key.

    For the "none" provider, writes null/zero values to disable semantic search.
    If embedding_api_key is provided, stores it encrypted.
    """
    from gobby.config.embedding_keys import (
        AI_EMBEDDING_API_BASE_KEY,
        AI_EMBEDDING_CATALOG_KEY,
        AI_EMBEDDING_DIM_KEY,
        AI_EMBEDDING_MODEL_KEY,
        AI_EMBEDDING_QUERY_PREFIX_KEY,
    )
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.hub.runtime import runtime_hub_database
    from gobby.storage.secrets import SecretStore

    with runtime_hub_database(apply_migrations=False) as db:
        store = ConfigStore(db)

        entries: dict[str, Any]
        if provider == "none":
            entries = {
                AI_EMBEDDING_MODEL_KEY: None,
                AI_EMBEDDING_API_BASE_KEY: None,
                AI_EMBEDDING_DIM_KEY: 0,
                AI_EMBEDDING_QUERY_PREFIX_KEY: None,
                AI_EMBEDDING_CATALOG_KEY: None,
            }
        else:
            entries = {
                AI_EMBEDDING_MODEL_KEY: model,
                AI_EMBEDDING_API_BASE_KEY: api_base,
                AI_EMBEDDING_DIM_KEY: dim,
                AI_EMBEDDING_QUERY_PREFIX_KEY: query_prefix,
                AI_EMBEDDING_CATALOG_KEY: catalog_key,
            }

        secret_store = SecretStore(db)
        store.set_embedding_bootstrap_values(
            entries,
            secret_store=secret_store,
            plaintext_api_key=embedding_api_key,
            managed_collections_exist=lambda: _managed_embedding_collections_exist(store),
        )


def _managed_embedding_collections_exist(config_store: ConfigStore) -> bool:
    """Prove whether any managed active or staged vector collection already exists."""
    import asyncio

    from gobby.memory.collection_names import CollectionNameResolver
    from gobby.memory.vectorstore import VectorStore
    from gobby.storage.config_mutations import EmbeddingConfigMutationBlocked

    async def _inspect() -> bool:
        snapshot = config_store.read_snapshot()
        config = config_store.repository.runtime_candidate(
            dict(snapshot.overrides), snapshot.secret_bindings
        )
        vector_store = VectorStore(
            url=config.databases.qdrant.url,
            api_key=config.databases.qdrant.api_key,
            embedding_dim=config.embeddings.dim,
        )
        try:
            names = CollectionNameResolver()
            for collection_name in await vector_store.list_collection_names():
                parsed = names.parse_physical_name(collection_name)
                kind = parsed[0] if parsed is not None else collection_name
                if kind in names.kinds:
                    return True
            return False
        finally:
            await vector_store.close()

    try:
        return asyncio.run(_inspect())
    except Exception as exc:
        logger.exception(
            "Failed to inspect managed embedding collections before configuration mutation",
            extra={"operation": "managed_embedding_collection_inspection"},
        )
        raise EmbeddingConfigMutationBlocked(
            "Unable to prove a first embedding bootstrap because managed collection state "
            "could not be inspected"
        ) from exc


def _probe_embedding_dim(
    model: str,
    api_base: str | None,
    api_key: str | None = None,
) -> int | None:
    """Send a 1-token probe and return ``len(embedding)``, or ``None`` on error.

    Used to auto-detect the dim when the user supplied a custom model or
    endpoint without passing ``--embedding-dim``.
    """

    async def _probe() -> int | None:
        try:
            service = EmbeddingService(model=model, api_base=api_base, api_key=api_key)
            result = await service.generate_embedding(
                "x",
                max_retries=1,
            )
            if not result:
                raise EmbeddingGenerationError("Embedding API returned empty probe result")
            return len(result)
        except EmbeddingGenerationError as e:
            logger.warning("Embedding dim probe failed: %s", e)
            return None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_probe())
    logger.warning("Cannot run dim probe: already in event loop")
    return None


def _dim_probe_error(model: str, api_base: str | None) -> str:
    return (
        f"Could not probe embedding dim from {api_base or 'default endpoint'} "
        f"for model {model}. Pass --embedding-dim explicitly."
    )


def _health_check_embedding(
    model: str,
    api_base: str | None,
    api_key: str | None = None,
    expected_dim: int | None = None,
) -> bool:
    """Fire a single test embedding. Returns True on success."""

    async def _check() -> bool:
        try:
            service = EmbeddingService(
                model=model,
                api_base=api_base,
                api_key=api_key,
                dim=expected_dim,
            )
            result = await service.generate_embedding(
                "gobby health check",
                max_retries=1,
            )
            return len(result) > 0
        except Exception as e:
            logger.warning("Embedding health check failed: %s", e)
            return False

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_check())
    logger.warning("Cannot run health check: already in event loop")
    return False


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _vector_norm(v: list[float]) -> float:
    """Compute L2 norm of a vector."""
    return math.sqrt(sum(x * x for x in v))


def _semantic_smoke_test(
    model: str,
    api_base: str | None,
    api_key: str | None = None,
    query_prefix: str | None = None,
    *,
    margin: float = 0.1,
) -> bool:
    """Verify the embedding model produces semantically meaningful vectors.

    Embeds a query, a related document, and an unrelated document, then asserts:
    1. ``sim(query, related) > sim(query, unrelated) + margin``
    2. Vector norm ≈ 1.0 for each embedding (catches wrong normalization)

    Returns True if both checks pass, False otherwise.
    """

    async def _test() -> bool:
        try:
            service = EmbeddingService(
                model=model,
                api_base=api_base,
                api_key=api_key,
                query_prefix=query_prefix,
            )
            query_vec = await service.generate_embedding(
                "How do I connect to PostgreSQL?",
                max_retries=1,
                is_query=True,
            )
            related_vec = await service.generate_embedding(
                "Database connection configuration and setup guide",
                max_retries=1,
                is_query=False,
            )
            unrelated_vec = await service.generate_embedding(
                "The weather is sunny today",
                max_retries=1,
                is_query=False,
            )

            if not query_vec or not related_vec or not unrelated_vec:
                logger.warning("Semantic smoke test: empty embedding returned")
                return False

            sim_related = _cosine_similarity(query_vec, related_vec)
            sim_unrelated = _cosine_similarity(query_vec, unrelated_vec)

            if sim_related <= sim_unrelated + margin:
                logger.warning(
                    "Semantic smoke test failed: sim(related)=%.4f <= sim(unrelated)=%.4f + margin=%.4f",
                    sim_related,
                    sim_unrelated,
                    margin,
                )
                return False

            for label, vec in [
                ("query", query_vec),
                ("related", related_vec),
                ("unrelated", unrelated_vec),
            ]:
                norm = _vector_norm(vec)
                if abs(norm - 1.0) > 0.1:
                    logger.warning(
                        "Semantic smoke test: %s vector norm=%.4f (expected ~1.0)", label, norm
                    )
                    return False

            logger.debug(
                "Semantic smoke test passed: sim(related)=%.4f, sim(unrelated)=%.4f",
                sim_related,
                sim_unrelated,
            )
            return True
        except Exception as e:
            logger.warning("Semantic smoke test failed: %s", e)
            return False

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_test())
    logger.warning("Cannot run semantic smoke test: already in event loop")
    return False
