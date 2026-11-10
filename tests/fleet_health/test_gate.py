"""Admission: refusal while the fleet is not healthy, and no seat cap while it is."""

from __future__ import annotations

import pytest

from gobby.fleet_health import FLEET_UNHEALTHY, launch_refusal, spawn_refusal
from gobby.fleet_health.state import fleet_state_path
from gobby.paths import get_gobby_home
from tests.fixtures.fleet_state import remove_fleet_state
from tests.fleet_health.support import write_fleet_state

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0


def test_healthy_fleet_admits_every_spawn_and_launch_with_no_seat_cap() -> None:
    for offset in range(25):
        write_fleet_state(at=NOW + offset)
        assert spawn_refusal(NOW + offset) is None
        assert launch_refusal(NOW + offset) is None


def test_admission_keeps_no_ledger() -> None:
    write_fleet_state(at=NOW)
    assert spawn_refusal(NOW) is None
    assert not (get_gobby_home() / "state" / "fleet-admissions.json").exists()


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


def test_a_recovered_fleet_admits_again() -> None:
    write_fleet_state(at=NOW, state="degraded", reasons=("load_per_core:2.6",))
    assert spawn_refusal(NOW) is not None
    write_fleet_state(at=NOW + 30, last_non_healthy=NOW)
    assert spawn_refusal(NOW + 30) is None


def test_missing_state_file_refuses_as_unstable() -> None:
    remove_fleet_state(fleet_state_path())
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
