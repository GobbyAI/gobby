"""Tests for #17200: fitted recall-constant promotion with one-flag rollback.

The resolver consumes a #17198 ``GateDecision.to_record()`` JSON. Synthetic
records exercise every path — no labeled data is required. The static
constants are the permanent rollback floor; a non-shipping (reject) record is
a first-class outcome that keeps static behavior even with the flag on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.recall_constants import (
    DEFAULT_DECISION_PATH,
    RECALL_QUERY_CONSTRUCTION_VERSION,
    RecallConstants,
    decision_record_path,
    resolve_recall_constants,
    static_recall_constants,
)
from gobby.memory.recall_signal_log import _weighting_snapshot
from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
from gobby.memory.services.knowledge_graph.writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

FITTED_PARAMS: dict[str, Any] = {
    "half_life_days": 21.0,
    "graph_synthetic_discount": 0.85,
    "cooccur_alpha": 0.6,
    "cooccur_support_cap": 4,
}


def shipped_record(**overrides: Any) -> dict[str, Any]:
    """A minimal shipped GateDecision.to_record() shape."""
    record: dict[str, Any] = {
        "task": "#17198",
        "label_source": "digest",
        "fitted_params": dict(FITTED_PARAMS),
        "gates": {"sufficient_data": True, "beats_static": True, "guard_ok": True},
        "ship": True,
        "reasons": [],
        "decision_digest": "decision-digest-123",
    }
    record.update(overrides)
    return record


def reject_record() -> dict[str, Any]:
    """The real-world #17198 outcome: insufficient data, no ship."""
    return shipped_record(
        ship=False,
        gates={"sufficient_data": False, "beats_static": False, "guard_ok": True},
        reasons=["insufficient labeled data: 0 train pairs (need 50), 0 holdout pairs (need 20)"],
    )


def write_record(path: Path, record: dict[str, Any]) -> None:
    path.write_text(json.dumps(record), encoding="utf-8")


def config_with(path: Path | None, *, enabled: bool, **kwargs: Any) -> MemoryConfig:
    return MemoryConfig(
        use_fitted_recall_constants=enabled,
        fitted_recall_decision_path=str(path) if path is not None else None,
        **kwargs,
    )


class TestStaticFloor:
    def test_static_constants_match_production_sources(self) -> None:
        constants = static_recall_constants()

        assert constants.source == "static"
        assert constants.provenance == "static"
        assert constants.half_life_days == 30.0
        assert constants.graph_synthetic_discount == _GRAPH_SYNTHETIC_SIM_DISCOUNT
        assert constants.cooccur_alpha == COOCCUR_ALPHA
        assert constants.cooccur_support_cap == COOCCUR_SUPPORT_CAP

    def test_static_honors_configured_half_life(self) -> None:
        config = MemoryConfig(temporal_decay_half_life_days=60.0)

        constants = static_recall_constants(config)

        assert constants.half_life_days == 60.0

    def test_default_decision_path_is_daemon_global(self) -> None:
        path = decision_record_path(MemoryConfig())

        assert path == Path(DEFAULT_DECISION_PATH).expanduser()
        assert "~" not in str(path)

    def test_configured_decision_path_expands_user(self) -> None:
        config = MemoryConfig(fitted_recall_decision_path="~/somewhere/decision.json")

        path = decision_record_path(config)

        assert "~" not in str(path)
        assert str(path).endswith("somewhere/decision.json")


class TestFlagFlip:
    """The validation-criteria flag-flip: fitted-vs-static and the rollback path."""

    def test_flag_off_is_static_even_with_shipped_record(self, tmp_path: Path) -> None:
        record_path = tmp_path / "decision.json"
        write_record(record_path, shipped_record())

        constants = resolve_recall_constants(config_with(record_path, enabled=False))

        assert constants.source == "static"
        assert constants.reason == "use_fitted_recall_constants disabled"
        assert constants.cooccur_alpha == COOCCUR_ALPHA

    def test_flag_on_with_shipped_record_applies_fitted(self, tmp_path: Path) -> None:
        record_path = tmp_path / "decision.json"
        write_record(record_path, shipped_record())

        constants = resolve_recall_constants(config_with(record_path, enabled=True))

        assert constants.source == "fitted"
        assert constants.provenance == "decision-digest-123"
        assert constants.half_life_days == FITTED_PARAMS["half_life_days"]
        assert constants.graph_synthetic_discount == FITTED_PARAMS["graph_synthetic_discount"]
        assert constants.cooccur_alpha == FITTED_PARAMS["cooccur_alpha"]
        assert constants.cooccur_support_cap == FITTED_PARAMS["cooccur_support_cap"]

    def test_rollback_flag_flip_restores_static(self, tmp_path: Path) -> None:
        record_path = tmp_path / "decision.json"
        write_record(record_path, shipped_record())

        fitted = resolve_recall_constants(config_with(record_path, enabled=True))
        rolled_back = resolve_recall_constants(config_with(record_path, enabled=False))

        assert fitted.source == "fitted"
        assert rolled_back.source == "static"
        assert rolled_back.half_life_days == 30.0
        assert rolled_back.cooccur_alpha == COOCCUR_ALPHA
        assert rolled_back.cooccur_support_cap == COOCCUR_SUPPORT_CAP
        assert rolled_back.graph_synthetic_discount == _GRAPH_SYNTHETIC_SIM_DISCOUNT

    def test_fitted_overrides_configured_half_life(self, tmp_path: Path) -> None:
        record_path = tmp_path / "decision.json"
        write_record(record_path, shipped_record())
        config = config_with(record_path, enabled=True, temporal_decay_half_life_days=60.0)

        constants = resolve_recall_constants(config)

        assert constants.half_life_days == FITTED_PARAMS["half_life_days"]


class TestRejectIsFirstClass:
    def test_reject_record_keeps_static_with_reason(self, tmp_path: Path) -> None:
        record_path = tmp_path / "decision.json"
        write_record(record_path, reject_record())

        constants = resolve_recall_constants(config_with(record_path, enabled=True))

        assert constants.source == "static"
        assert constants.reason is not None
        assert "did not ship" in constants.reason
        assert "insufficient labeled data" in constants.reason

    def test_missing_record_keeps_static(self, tmp_path: Path) -> None:
        record_path = tmp_path / "missing.json"

        constants = resolve_recall_constants(config_with(record_path, enabled=True))

        assert constants.source == "static"
        assert constants.reason is not None
        assert "no gate decision record" in constants.reason

    def test_malformed_record_keeps_static(self, tmp_path: Path) -> None:
        record_path = tmp_path / "decision.json"
        record_path.write_text("{not json", encoding="utf-8")

        constants = resolve_recall_constants(config_with(record_path, enabled=True))

        assert constants.source == "static"
        assert constants.reason is not None
        assert "malformed" in constants.reason

    def test_non_object_record_keeps_static(self, tmp_path: Path) -> None:
        record_path = tmp_path / "decision.json"
        record_path.write_text("[1, 2, 3]", encoding="utf-8")

        constants = resolve_recall_constants(config_with(record_path, enabled=True))

        assert constants.source == "static"
        assert constants.reason is not None
        assert "expected a JSON object" in constants.reason

    @pytest.mark.parametrize(
        "bad_params",
        [
            {},
            {**FITTED_PARAMS, "half_life_days": 0.0},
            {**FITTED_PARAMS, "half_life_days": float("nan")},
            {**FITTED_PARAMS, "graph_synthetic_discount": 0.0},
            {**FITTED_PARAMS, "graph_synthetic_discount": 1.5},
            {**FITTED_PARAMS, "cooccur_alpha": -0.1},
            {**FITTED_PARAMS, "cooccur_alpha": 1.1},
            {**FITTED_PARAMS, "cooccur_support_cap": 0},
            {**FITTED_PARAMS, "cooccur_support_cap": 2.5},
            {**FITTED_PARAMS, "cooccur_alpha": "0.5"},
            {**FITTED_PARAMS, "cooccur_alpha": True},
        ],
        ids=[
            "empty",
            "zero-half-life",
            "nan-half-life",
            "zero-discount",
            "discount-above-one",
            "negative-alpha",
            "alpha-above-one",
            "zero-cap",
            "fractional-cap",
            "string-alpha",
            "bool-alpha",
        ],
    )
    def test_shipped_record_with_invalid_params_keeps_static(
        self, tmp_path: Path, bad_params: dict[str, Any]
    ) -> None:
        record_path = tmp_path / "decision.json"
        write_record(record_path, shipped_record(fitted_params=bad_params))

        constants = resolve_recall_constants(config_with(record_path, enabled=True))

        assert constants.source == "static"
        assert constants.reason is not None
        assert "invalid fitted_params" in constants.reason


class TestSignalLogProvenance:
    """Logged weighting snapshots must reflect the EFFECTIVE constants."""

    def test_weighting_snapshot_reports_effective_fitted_values(self) -> None:
        config = MemoryConfig(temporal_decay_half_life_days=60.0)
        constants = RecallConstants(
            half_life_days=21.0,
            graph_synthetic_discount=0.85,
            cooccur_alpha=0.6,
            cooccur_support_cap=4,
            source="fitted",
            provenance="decision-digest-123",
        )

        snapshot = _weighting_snapshot(config, constants)

        assert snapshot["temporal_decay_half_life_days"] == 21.0
        assert snapshot["recall_constants_source"] == "fitted"
        assert snapshot["cooccur_alpha"] == 0.6
        assert snapshot["cooccur_support_cap"] == 4

    def test_weighting_snapshot_without_constants_reads_config(self) -> None:
        config = MemoryConfig(temporal_decay_half_life_days=60.0)

        snapshot = _weighting_snapshot(config)

        assert snapshot["temporal_decay_half_life_days"] == 60.0
        assert "recall_constants_source" not in snapshot

    def test_weighting_snapshot_stamps_the_query_construction_version(self) -> None:
        """4.1.1: every logged request records the era its query was built in.

        The fence is the presence of the key, not its value: rows written before
        this change carry no such key, so `query_construction_version IS NULL`
        selects the pre-v2 era exactly.
        """
        snapshot = _weighting_snapshot(MemoryConfig())

        assert snapshot["query_construction_version"] == RECALL_QUERY_CONSTRUCTION_VERSION


def test_query_construction_version_is_shared_without_an_import_cycle() -> None:
    """2.2.2: the constants module owns the version, so 4.1 can read it from here."""
    from gobby.memory import recall_constants

    assert RECALL_QUERY_CONSTRUCTION_VERSION == "nl-embed-v1"
    source = Path(recall_constants.__file__ or "").read_text(encoding="utf-8")
    assert "from gobby.memory.recall import" not in source
    assert "from gobby.memory.recall_signal_log import" not in source
