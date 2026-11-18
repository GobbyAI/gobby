"""Pins that keep a retained generation past pruning, and lookup of one generation by its ref.

The store is driven through its public entry points against real files in pytest temp
directories, as test_bin_generations.py does. Stored pins are read back from the state file,
so the assertions hold the stored shape and not only the return values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.install import bin_generations as store
from gobby.install.bin_generations import (
    KEEP_GENERATIONS,
    Generation,
    GenerationStoreError,
    list_generations,
    read_state,
    retain_outgoing,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
PIN_FIELDS = {"task_id", "session_id", "set_at", "until"}
LISTED_FIELDS = {"task_id", "session_id", "set_at", "until", "expired"}


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _capture(bin_dir: Path, payload: bytes, moment: datetime) -> Generation:
    """Install payload as the gdaemon binary, then retain the installed set at moment."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "gdaemon").write_bytes(payload)
    generation = retain_outgoing(bin_dir, ["gdaemon"], now=moment)
    assert generation is not None
    return generation


def _stored_pins(bin_dir: Path) -> dict[str, dict[str, Any]]:
    pins: dict[str, dict[str, Any]] = read_state(bin_dir).get("pins", {})
    return pins


# Lookup: a generation by its id or by its sequence number.


def test_find_generation_matches_the_generation_id(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    first = _capture(bin_dir, b"gdaemon-0", NOW)
    _capture(bin_dir, b"gdaemon-1", _at(1))

    found = store.find_generation(bin_dir, first.id)

    assert found.id == first.id
    assert found.seq == 1


def test_find_generation_matches_the_sequence_number(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _capture(bin_dir, b"gdaemon-0", NOW)
    second = _capture(bin_dir, b"gdaemon-1", _at(1))

    found = store.find_generation(bin_dir, "2")

    assert found.id == second.id


def test_find_generation_raises_when_nothing_matches(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _capture(bin_dir, b"gdaemon-0", NOW)

    with pytest.raises(GenerationStoreError, match="no generation"):
        store.find_generation(bin_dir, "9")


# Pinning: the stored record and the refusals.


def test_pin_generation_stores_the_owner_and_both_times(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    until = NOW + timedelta(days=30)

    store.pin_generation(
        bin_dir,
        generation.id,
        until=until,
        task_id="pin-owner-task",
        session_id="pin-owner-session",
        now=NOW,
    )

    pin = _stored_pins(bin_dir)[generation.id]
    assert set(pin) == PIN_FIELDS
    assert pin["task_id"] == "pin-owner-task"
    assert pin["session_id"] == "pin-owner-session"
    assert datetime.fromisoformat(pin["set_at"]) == NOW
    assert datetime.fromisoformat(pin["until"]) == until


def test_pin_generation_without_an_owner_stores_null_task_and_session(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)

    store.pin_generation(bin_dir, generation.id, until=NOW + timedelta(days=1), now=NOW)

    pin = _stored_pins(bin_dir)[generation.id]
    assert pin["task_id"] is None
    assert pin["session_id"] is None


def test_pin_generation_for_a_missing_generation_raises(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _capture(bin_dir, b"gdaemon-0", NOW)

    with pytest.raises(GenerationStoreError, match="no generation"):
        store.pin_generation(
            bin_dir,
            "000009-20261117T120000Z",
            until=NOW + timedelta(days=1),
            now=NOW,
        )


def test_pin_generation_twice_raises_already_pinned(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(bin_dir, generation.id, until=NOW + timedelta(days=1), now=NOW)

    with pytest.raises(GenerationStoreError, match="already pinned"):
        store.pin_generation(bin_dir, generation.id, until=NOW + timedelta(days=2), now=NOW)


def test_pin_generation_with_an_until_not_later_than_now_stores_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)

    with pytest.raises(GenerationStoreError, match="not later than now"):
        store.pin_generation(bin_dir, generation.id, until=NOW, now=NOW)

    assert read_state(bin_dir).get("pins", {}) == {}


# Pruning: a pin protects a generation until its until has passed.


def test_pinned_generation_survives_keep_plus_one_later_captures(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    pinned = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(bin_dir, pinned.id, until=NOW + timedelta(days=30), now=NOW)

    for index in range(1, KEEP_GENERATIONS + 2):
        _capture(bin_dir, f"gdaemon-{index}".encode(), _at(index))

    # Seq 2 falls out of the newest three, and seq 1 stays because its pin has not lapsed.
    assert [g.seq for g in list_generations(bin_dir)] == [1, 3, 4, 5]


def test_pin_lapses_once_until_has_passed_and_the_next_capture_prunes_it(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    pinned = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(bin_dir, pinned.id, until=NOW + timedelta(days=1), now=NOW)
    for index in range(1, KEEP_GENERATIONS + 2):
        _capture(bin_dir, f"gdaemon-{index}".encode(), _at(index))
    assert [g.seq for g in list_generations(bin_dir)] == [1, 3, 4, 5]

    _capture(bin_dir, b"gdaemon-late", NOW + timedelta(days=2))

    assert [g.seq for g in list_generations(bin_dir)] == [4, 5, 6]


# Extending: the until moves and the rest of the pin stays.


def test_extend_pin_moves_until_and_keeps_the_owner_and_set_time(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(
        bin_dir,
        generation.id,
        until=NOW + timedelta(days=1),
        task_id="pin-owner-task",
        session_id="pin-owner-session",
        now=NOW,
    )
    new_until = NOW + timedelta(days=60)

    store.extend_pin(bin_dir, generation.id, until=new_until, now=NOW + timedelta(hours=1))

    pin = _stored_pins(bin_dir)[generation.id]
    assert datetime.fromisoformat(pin["until"]) == new_until
    assert pin["task_id"] == "pin-owner-task"
    assert pin["session_id"] == "pin-owner-session"
    assert datetime.fromisoformat(pin["set_at"]) == NOW


def test_extend_pin_on_an_unpinned_generation_raises(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)

    with pytest.raises(GenerationStoreError, match="not pinned"):
        store.extend_pin(bin_dir, generation.id, until=NOW + timedelta(days=1), now=NOW)


def test_extend_pin_with_an_until_not_later_than_now_raises(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(bin_dir, generation.id, until=NOW + timedelta(days=1), now=NOW)

    with pytest.raises(GenerationStoreError, match="not later than now"):
        store.extend_pin(bin_dir, generation.id, until=NOW + timedelta(hours=1), now=_at(120))


# Unpinning.


def test_unpin_removes_the_pin_and_returns_true(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(bin_dir, generation.id, until=NOW + timedelta(days=1), now=NOW)

    removed = store.unpin_generation(bin_dir, generation.id)

    assert removed is True
    assert generation.id not in _stored_pins(bin_dir)


def test_unpin_of_an_unpinned_generation_returns_false(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)

    assert store.unpin_generation(bin_dir, generation.id) is False


def test_unpin_without_a_store_returns_false_and_creates_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    assert store.unpin_generation(bin_dir, "000001-20261117T120000Z") is False

    assert not bin_dir.exists()


# Listing: the stored pins with an expired flag.


def test_list_pins_reports_the_stored_fields_and_an_expired_flag(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(
        bin_dir,
        generation.id,
        until=NOW + timedelta(days=1),
        task_id="pin-owner-task",
        session_id="pin-owner-session",
        now=NOW,
    )

    pins = store.list_pins(bin_dir, now=NOW)

    assert set(pins) == {generation.id}
    assert set(pins[generation.id]) == LISTED_FIELDS
    assert pins[generation.id]["task_id"] == "pin-owner-task"


def test_list_pins_reports_an_unexpired_pin_before_its_until(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(bin_dir, generation.id, until=NOW + timedelta(days=1), now=NOW)

    pins = store.list_pins(bin_dir, now=NOW)

    assert pins[generation.id]["expired"] is False


def test_list_pins_reports_a_pin_expired_once_its_until_has_passed(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    generation = _capture(bin_dir, b"gdaemon-0", NOW)
    store.pin_generation(bin_dir, generation.id, until=NOW + timedelta(days=1), now=NOW)

    pins = store.list_pins(bin_dir, now=NOW + timedelta(days=2))

    assert pins[generation.id]["expired"] is True


def test_list_pins_without_a_store_is_empty_and_creates_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    assert store.list_pins(bin_dir, now=NOW) == {}

    assert not bin_dir.exists()
