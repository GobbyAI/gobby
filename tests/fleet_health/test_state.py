"""The fleet state file: parsing, schema, staleness, and what counts as unstable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.fleet_health.state import FleetState, read_fleet_state

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "state": "healthy",
        "sampled_at_unix": NOW,
        "last_non_healthy_at_unix": None,
        "reasons": [],
        "thresholds_source": "defaults",
        "watcher_pid": 1,
    }
    document.update(overrides)
    return document


def _write(path: Path, document: Any) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_fresh_healthy_file_admits(tmp_path: Path) -> None:
    state = read_fleet_state(_write(tmp_path / "fleet.json", _document()), NOW)
    assert state == FleetState("healthy", (), None)


def test_a_file_from_an_older_watcher_with_a_ramp_block_still_reads(tmp_path: Path) -> None:
    document = _document(ramp={"max_new_seats": 3, "ramp_window_seconds": 900})
    state = read_fleet_state(_write(tmp_path / "fleet.json", document), NOW)
    assert state == FleetState("healthy", (), None)


def test_degraded_file_carries_its_reasons(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "fleet.json",
        _document(state="degraded", reasons=["load_per_core:2.6"]),
    )
    state = read_fleet_state(path, NOW)
    assert state.state == "degraded"
    assert state.reasons == ("load_per_core:2.6",)


def test_missing_file_is_unstable(tmp_path: Path) -> None:
    state = read_fleet_state(tmp_path / "absent.json", NOW)
    assert state.state == "unstable"
    assert state.reasons == ("state_file_missing",)


@pytest.mark.parametrize(("age", "state"), [(90.0, "healthy"), (91.0, "unstable")])
def test_a_state_older_than_ninety_seconds_is_stale(tmp_path: Path, age: float, state: str) -> None:
    path = _write(tmp_path / "fleet.json", _document(sampled_at_unix=NOW - age))
    result = read_fleet_state(path, NOW)
    assert result.state == state
    if state == "unstable":
        assert result.reasons[0].startswith("state_file_stale")


def test_a_sample_stamped_far_in_the_future_is_stale(tmp_path: Path) -> None:
    path = _write(tmp_path / "fleet.json", _document(sampled_at_unix=NOW + 600))
    result = read_fleet_state(path, NOW)
    assert result.state == "unstable"
    assert result.reasons[0].startswith("state_file_stale")


def test_malformed_json_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "fleet.json"
    path.write_text("{not json", encoding="utf-8")
    result = read_fleet_state(path, NOW)
    assert result.state == "unstable"
    assert result.reasons[0].startswith("state_file_unreadable")


def test_an_invalid_field_is_named_in_the_reason(tmp_path: Path) -> None:
    path = _write(tmp_path / "fleet.json", _document(state="bogus"))
    assert read_fleet_state(path, NOW).reasons == ("state_file_unreadable: invalid field(s) state",)


def test_an_unknown_schema_version_is_unreadable(tmp_path: Path) -> None:
    path = _write(tmp_path / "fleet.json", _document(schema_version=2))
    assert read_fleet_state(path, NOW).reasons[0].startswith("state_file_unreadable")
