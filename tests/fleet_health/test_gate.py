"""Admission: refusal while unhealthy, the per-window ramp cap, and the admission ledger."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.fleet_health import (
    FLEET_LEDGER_UNREADABLE,
    FLEET_RAMP_CAPPED,
    FLEET_UNHEALTHY,
    launch_refusal,
    spawn_refusal,
)
from gobby.fleet_health.ledger import ledger_path, read_admissions, write_admissions
from gobby.fleet_health.state import fleet_state_path
from tests.fleet_health.support import write_fleet_state

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0


def test_healthy_fleet_admits_a_spawn_and_records_it() -> None:
    write_fleet_state(at=NOW)
    assert spawn_refusal(NOW) is None
    assert read_admissions(ledger_path()) == [NOW]


def test_healthy_fleet_admits_a_launch_without_recording_a_seat() -> None:
    write_fleet_state(at=NOW)
    assert launch_refusal(NOW) is None
    assert read_admissions(ledger_path()) == []


@pytest.mark.parametrize("state", ["degraded", "unstable"])
def test_non_healthy_fleet_refuses_spawns_and_launches_with_reasons(state: str) -> None:
    write_fleet_state(at=NOW, state=state, reasons=("probe_slow:1500ms",))
    for refusal in (spawn_refusal(NOW), launch_refusal(NOW)):
        assert refusal is not None
        assert refusal.code == FLEET_UNHEALTHY
        result = refusal.as_result()
        assert result["success"] is False
        assert result["error_code"] == "fleet_unhealthy"
        assert result["fleet"] == {"state": state, "reasons": ["probe_slow:1500ms"]}
    assert read_admissions(ledger_path()) == []


def test_missing_state_file_refuses_as_unstable() -> None:
    fleet_state_path().unlink(missing_ok=True)
    refusal = spawn_refusal(NOW)
    assert refusal is not None
    assert refusal.code == FLEET_UNHEALTHY
    assert refusal.state == "unstable"
    assert refusal.reasons == ("state_file_missing",)


def test_stale_state_refuses_as_unstable() -> None:
    write_fleet_state(at=NOW - 300)
    refusal = spawn_refusal(NOW)
    assert refusal is not None
    assert refusal.state == "unstable"
    assert refusal.reasons[0].startswith("state_file_stale")


def test_spawns_are_capped_per_ramp_window() -> None:
    write_fleet_state(at=NOW, max_new_seats=3)
    for offset in range(3):
        write_fleet_state(at=NOW + offset, max_new_seats=3)
        assert spawn_refusal(NOW + offset) is None
    write_fleet_state(at=NOW + 3, max_new_seats=3)
    refusal = spawn_refusal(NOW + 3)
    assert refusal is not None
    assert refusal.code == FLEET_RAMP_CAPPED
    assert read_admissions(ledger_path()) == [NOW, NOW + 1, NOW + 2]


def test_a_launch_cannot_start_while_the_ramp_has_no_room() -> None:
    write_fleet_state(at=NOW, max_new_seats=1)
    assert spawn_refusal(NOW) is None
    write_fleet_state(at=NOW + 1, max_new_seats=1)
    refusal = launch_refusal(NOW + 1)
    assert refusal is not None
    assert refusal.code == FLEET_RAMP_CAPPED
    assert read_admissions(ledger_path()) == [NOW]


def test_admissions_age_out_of_the_window() -> None:
    write_fleet_state(at=NOW, max_new_seats=1, ramp_window_seconds=900)
    assert spawn_refusal(NOW) is None
    write_fleet_state(at=NOW + 899, max_new_seats=1, ramp_window_seconds=900)
    assert spawn_refusal(NOW + 899) is not None
    write_fleet_state(at=NOW + 901, max_new_seats=1, ramp_window_seconds=900)
    assert spawn_refusal(NOW + 901) is None


def test_a_flap_does_not_reset_the_ramp() -> None:
    write_fleet_state(at=NOW, max_new_seats=3)
    for offset in range(3):
        write_fleet_state(at=NOW + offset, max_new_seats=3)
        assert spawn_refusal(NOW + offset) is None
    degraded = NOW + 30
    write_fleet_state(
        at=degraded, state="degraded", reasons=("load_per_core:2.6",), max_new_seats=3
    )
    unhealthy = spawn_refusal(degraded)
    assert unhealthy is not None
    assert unhealthy.code == FLEET_UNHEALTHY
    recovered = NOW + 60
    write_fleet_state(at=recovered, max_new_seats=3, last_non_healthy=degraded)
    capped = spawn_refusal(recovered)
    assert capped is not None
    assert capped.code == FLEET_RAMP_CAPPED
    assert read_admissions(ledger_path()) == [NOW, NOW + 1, NOW + 2]


def test_a_nested_launch_checks_health_but_leaves_the_ramp_to_its_seats() -> None:
    write_fleet_state(at=NOW, max_new_seats=1)
    assert spawn_refusal(NOW) is None
    write_fleet_state(at=NOW + 1, max_new_seats=1)
    assert launch_refusal(NOW + 1, ramp=False) is None
    write_fleet_state(at=NOW + 2, state="degraded", max_new_seats=1, last_non_healthy=NOW + 2)
    refusal = launch_refusal(NOW + 2, ramp=False)
    assert refusal is not None
    assert refusal.code == FLEET_UNHEALTHY


def test_recording_prunes_admissions_outside_the_window() -> None:
    write_fleet_state(at=NOW, max_new_seats=3)
    write_admissions(ledger_path(), [NOW - 5000.0])
    assert spawn_refusal(NOW) is None
    assert read_admissions(ledger_path()) == [NOW]


def test_an_unreadable_ledger_refuses_rather_than_guessing_the_ramp() -> None:
    write_fleet_state(at=NOW)
    ledger_path().write_text("not json", encoding="utf-8")
    refusal = spawn_refusal(NOW)
    assert refusal is not None
    assert refusal.code == FLEET_LEDGER_UNREADABLE


def test_a_ledger_that_cannot_be_locked_refuses_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_fleet_state(at=NOW)
    ledger = tmp_path / "fleet-admissions.json"
    # A directory where the lock file belongs makes the lock unopenable. The ledger is
    # redirected to a temp dir so the shared test home is left unchanged.
    (tmp_path / "fleet-admissions.json.lock").mkdir()
    monkeypatch.setattr("gobby.fleet_health.gate.ledger_path", lambda: ledger)
    refusal = spawn_refusal(NOW)
    assert refusal is not None
    assert refusal.code == FLEET_LEDGER_UNREADABLE
