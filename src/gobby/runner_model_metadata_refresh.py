"""Bounded asynchronous refresh for the OpenRouter model metadata cache."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.types.json import Jsonb

from gobby.llm.model_registry import ModelInfo, fetch_models_async, normalize_model_id
from gobby.storage.hub.async_ops import BoundedDBTimeoutError, run_bounded_db
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.providers.capabilities.coverage import CoverageAuditor

logger = logging.getLogger(__name__)

MODEL_METADATA_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
MODEL_METADATA_WRITE_TIMEOUT_SECONDS = 5.0
MODEL_METADATA_DRAIN_TIMEOUT_SECONDS = 7.0


def _require_remaining(cutoff: float) -> float:
    remaining = cutoff - asyncio.get_running_loop().time()
    if remaining <= 0.0:
        raise BoundedDBTimeoutError("model metadata write deadline expired")
    return remaining


def _statement_timeout_ms(cutoff: float) -> int:
    return max(1, math.floor(_require_remaining(cutoff) * 1000.0))


def _metadata_rows(models: list[ModelInfo]) -> list[tuple[object, ...]]:
    by_model: dict[str, ModelInfo] = {}
    for model in models:
        model_key = normalize_model_id(model.id)
        existing = by_model.get(model_key)
        if existing is not None and model.context_length <= existing.context_length:
            continue
        by_model[model_key] = model
    return [
        (
            model_key,
            model.context_length,
            model.max_completion_tokens,
            model.reasoning is not None,
            Jsonb(list(model.reasoning.supported_efforts))
            if model.reasoning is not None and model.reasoning.supported_efforts is not None
            else None,
            model.reasoning.default_effort if model.reasoning is not None else None,
            model.reasoning.default_enabled if model.reasoning is not None else None,
            model.reasoning.mandatory if model.reasoning is not None else None,
            "registry",
        )
        for model_key, model in by_model.items()
    ]


async def _replace_rows(
    connection: psycopg.AsyncConnection[Any],
    cutoff: float,
    rows: list[tuple[object, ...]],
) -> int:
    _require_remaining(cutoff)
    await connection.execute("DELETE FROM model_metadata")

    statement_timeout_ms = _statement_timeout_ms(cutoff)
    await connection.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms}")

    _require_remaining(cutoff)
    placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(rows))
    parameters = tuple(value for row in rows for value in row)
    await connection.execute(
        "INSERT INTO model_metadata "
        "(model, context_length, max_completion_tokens, reasoning_present, "
        "reasoning_supported_efforts, reasoning_default_effort, "
        "reasoning_default_enabled, reasoning_mandatory, source) VALUES "
        f"{placeholders}",
        parameters,
    )
    return len(rows)


async def replace_model_metadata_async(
    database: HubDatabase,
    models: list[ModelInfo],
) -> int:
    """Atomically replace metadata through one dedicated bounded connection."""
    rows = _metadata_rows(models)
    if not rows:
        return 0

    async def work(connection: psycopg.AsyncConnection[Any], cutoff: float) -> int:
        return await _replace_rows(connection, cutoff, rows)

    return await run_bounded_db(
        work,
        conninfo=database.conninfo,
        deadline_seconds=MODEL_METADATA_WRITE_TIMEOUT_SECONDS,
        statement_timeout_remaining=True,
    )


async def refresh_model_metadata_once(
    database: HubDatabase,
    *,
    coverage_auditor: CoverageAuditor | None = None,
) -> bool:
    """Fetch and persist one registry snapshot, retaining cache on failure."""
    models = await fetch_models_async()
    if not models:
        logger.warning("Model metadata refresh returned no models; retaining cached metadata")
        return False
    try:
        inserted = await replace_model_metadata_async(database, models)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Model metadata refresh write failed; retaining cached metadata")
        return False
    logger.debug("Refreshed model metadata cache with %s models", inserted)
    if coverage_auditor is not None:
        try:
            await coverage_auditor.audit_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Provider model metadata coverage audit failed after registry refresh")
    return True


async def model_metadata_refresh_loop(
    database: HubDatabase,
    shutdown_requested: Callable[[], bool],
    *,
    interval_seconds: float = MODEL_METADATA_REFRESH_INTERVAL_SECONDS,
    coverage_auditor: CoverageAuditor | None = None,
) -> None:
    """Refresh metadata every 24 hours until daemon shutdown."""
    while not shutdown_requested():
        await asyncio.sleep(interval_seconds)
        if shutdown_requested():
            return
        await refresh_model_metadata_once(database, coverage_auditor=coverage_auditor)
