"""Recall constants: delivery producer tag plus runtime ranking-constant resolution.

Ranking-constant resolution (#17200, epic #17099): the static constants are the
permanent rollback floor — the configured ``temporal_decay_half_life_days``,
``_GRAPH_SYNTHETIC_SIM_DISCOUNT``, and the frozen ``COOCCUR_ALPHA`` /
``COOCCUR_SUPPORT_CAP`` blend coefficients.
``MemoryConfig.use_fitted_recall_constants`` (default off) opts the daemon into
the pooled fitted set from a shipped #17198 gate decision record. A missing,
malformed, or non-shipping (``ship: false``) record is a first-class outcome:
the resolver keeps the static floor and records why, so a reject gate never
activates fitted values even with the flag on. Flipping the flag off is the
one-flag rollback.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig

logger = logging.getLogger(__name__)

MEMORY_RECALL_PRODUCER = "daemon_memory_recall"

# How `MemoryRecallRunner` assembles the query it embeds. It lives here rather
# than in `recall.py` because `recall.py` already imports `recall_signal_log`,
# which needs to read this to stamp the cohort fence; defining it there would
# close an import cycle. This module imports only stdlib.
RECALL_QUERY_CONSTRUCTION_VERSION = "nl-embed-v1"

# Daemon-global pooled location; the gate fit is pooled across projects, so the
# shipped record lives outside any repo checkout.
DEFAULT_DECISION_PATH = "~/.gobby/recall_refit_decision.json"


@dataclass(frozen=True)
class RecallConstants:
    """Effective recall ranking constants plus their provenance."""

    half_life_days: float
    graph_synthetic_discount: float
    cooccur_alpha: float
    cooccur_support_cap: int
    source: str  # "static" | "fitted"
    provenance: str  # "static" | shipped decision_digest
    reason: str | None = None


def static_recall_constants(
    config: MemoryConfig | None = None, reason: str | None = None
) -> RecallConstants:
    """The rollback floor: configured half-life plus the frozen module constants."""
    # Imported lazily so the hook/workflow importers of MEMORY_RECALL_PRODUCER
    # never pull the FalkorDB writer stack.
    from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
    from gobby.memory.services.knowledge_graph.writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

    if config is not None:
        half_life = float(config.temporal_decay_half_life_days)
    else:
        from gobby.config.persistence import MemoryConfig as _MemoryConfig

        half_life = float(_MemoryConfig.model_fields["temporal_decay_half_life_days"].default)
    return RecallConstants(
        half_life_days=half_life,
        graph_synthetic_discount=_GRAPH_SYNTHETIC_SIM_DISCOUNT,
        cooccur_alpha=COOCCUR_ALPHA,
        cooccur_support_cap=COOCCUR_SUPPORT_CAP,
        source="static",
        provenance="static",
        reason=reason,
    )


def decision_record_path(config: MemoryConfig) -> Path:
    """Resolve the gate decision record path (config override or daemon-global default)."""
    raw = config.fitted_recall_decision_path or DEFAULT_DECISION_PATH
    return Path(raw).expanduser()


def _load_decision_record(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read the gate decision record; (record, None) or (None, why it is unusable)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"no gate decision record at {path}"
    except OSError as e:
        return None, f"gate decision record unreadable at {path}: {e}"
    try:
        record = json.loads(text)
    except ValueError as e:
        return None, f"gate decision record malformed at {path}: {e}"
    if not isinstance(record, dict):
        return None, f"gate decision record malformed at {path}: expected a JSON object"
    return record, None


def _fitted_params(record: dict[str, Any]) -> dict[str, float]:
    """Extract and validate ``fitted_params``; raises ValueError on any bad value."""
    raw = record.get("fitted_params")
    if not isinstance(raw, dict):
        raise ValueError("fitted_params missing or not an object")
    values: dict[str, float] = {}
    for key in (
        "half_life_days",
        "graph_synthetic_discount",
        "cooccur_alpha",
        "cooccur_support_cap",
    ):
        raw_value = raw.get(key)
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            raise ValueError(f"fitted_params.{key} missing or non-numeric")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"fitted_params.{key} is not finite")
        values[key] = value
    if values["half_life_days"] <= 0:
        raise ValueError("fitted_params.half_life_days must be > 0")
    if not 0.0 < values["graph_synthetic_discount"] <= 1.0:
        raise ValueError("fitted_params.graph_synthetic_discount must be in (0, 1]")
    if not 0.0 <= values["cooccur_alpha"] <= 1.0:
        raise ValueError("fitted_params.cooccur_alpha must be in [0, 1]")
    if values["cooccur_support_cap"] < 1 or not values["cooccur_support_cap"].is_integer():
        raise ValueError("fitted_params.cooccur_support_cap must be an integer >= 1")
    return values


def resolve_recall_constants(config: MemoryConfig) -> RecallConstants:
    """Resolve the daemon-global effective constants from config and the gate record."""
    if not config.use_fitted_recall_constants:
        return static_recall_constants(config, reason="use_fitted_recall_constants disabled")

    path = decision_record_path(config)
    record, load_error = _load_decision_record(path)
    if record is None:
        logger.warning(
            "use_fitted_recall_constants enabled but %s; keeping static constants", load_error
        )
        return static_recall_constants(config, reason=load_error)

    if not record.get("ship"):
        gate_reasons = record.get("reasons")
        detail = "; ".join(str(r) for r in gate_reasons) if isinstance(gate_reasons, list) else ""
        reason = f"gate decision at {path} did not ship" + (f": {detail}" if detail else "")
        logger.info(
            "use_fitted_recall_constants enabled but the gate rejected; "
            "keeping static constants (%s)",
            reason,
        )
        return static_recall_constants(config, reason=reason)

    try:
        fitted = _fitted_params(record)
    except ValueError as e:
        reason = f"shipped gate decision at {path} has invalid fitted_params: {e}"
        logger.warning("%s; keeping static constants", reason)
        return static_recall_constants(config, reason=reason)

    decision_digest = record.get("decision_digest")
    if not isinstance(decision_digest, str) or not decision_digest.strip():
        reason = f"shipped gate decision at {path} has no decision_digest"
        logger.warning("%s; keeping static constants", reason)
        return static_recall_constants(config, reason=reason)

    logger.info(
        "Applying fitted recall constants from %s (label_source=%s)",
        path,
        record.get("label_source"),
    )
    return RecallConstants(
        half_life_days=fitted["half_life_days"],
        graph_synthetic_discount=fitted["graph_synthetic_discount"],
        cooccur_alpha=fitted["cooccur_alpha"],
        cooccur_support_cap=int(fitted["cooccur_support_cap"]),
        source="fitted",
        provenance=decision_digest,
        reason=None,
    )
