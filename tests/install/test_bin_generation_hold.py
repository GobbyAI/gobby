"""Hold, burn and promotion-guard behavior of the managed binary generation store.

The store is driven through its public entry points against real files in pytest temp
directories. The seeding helpers follow test_bin_generations.py.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.install import bin_generations
from gobby.install.bin_generations import (
    GENERATIONS_DIR,
    KEEP_GENERATIONS,
    MANAGED_BINARIES,
    STATE_FILE,
    Generation,
    GenerationStoreError,
    list_generations,
    read_state,
    record_promotion,
    retain_outgoing,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
FIRST_ID = "000001-20261117T120000Z"
REFUSAL_PREFIX = "promotion refused: "
LOCK_BUSY = "^generation store is busy"
STORED_HOLD_KEYS = {"task_id", "session_id", "token", "set_at", "expires_at", "first_seq"}
STATUS_KEYS = {"task_id", "session_id", "set_at", "expires_at", "first_seq", "expired"}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _set_binary(bin_dir: Path, name: str, payload: bytes) -> None:
    """Replace an installed binary's bytes in place, as a later install would."""
    _write(bin_dir / name, payload, 0o755)


def _seed(bin_dir: Path, name: str, payload: bytes) -> None:
    """Install a managed binary with the receipt files a real promotion leaves beside it."""
    _set_binary(bin_dir, name, payload)
    receipt = {
        "artifact_sha256": _sha(payload),
        "source_identity": f"src-{name}",
        "version": "1.0.0",
    }
    _write(bin_dir / f".{name}-release-identity.json", _json_bytes(receipt), 0o600)
    _write(bin_dir / f".{name}-version", b"1.0.0\n", 0o644)
    _write(bin_dir / f".{name}-install.json", _json_bytes({"installed_version": "1.0.0"}), 0o644)
    _write(bin_dir / f".{name}-source-sha256", f"{_sha(payload)}\n".encode(), 0o644)


def _state_path(bin_dir: Path) -> Path:
    return bin_dir / GENERATIONS_DIR / STATE_FILE


def _update_state(bin_dir: Path, **changes: Any) -> None:
    state = read_state(bin_dir)
    state.update(changes)
    _state_path(bin_dir).parent.mkdir(parents=True, exist_ok=True)
    _state_path(bin_dir).write_text(json.dumps(state, sort_keys=True))


def _hold(first_seq: int) -> dict[str, Any]:
    return {
        "task_id": "task-1",
        "session_id": "session-1",
        "token": "token-1",
        "set_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "first_seq": first_seq,
    }


def _hold_with_unreadable_expiry(kind: str) -> dict[str, Any]:
    hold = _hold(first_seq=1)
    if kind == "missing":
        del hold["expires_at"]
    else:
        hold["expires_at"] = "not a time"
    return hold


def _pin(until: datetime) -> dict[str, Any]:
    return {
        "task_id": None,
        "session_id": None,
        "set_at": NOW.isoformat(),
        "until": until.isoformat(),
    }


def _capture(bin_dir: Path, members: Iterable[str], minute: int = 0) -> Generation:
    """Retain the installed members at a fixed time offset, expecting a new generation."""
    generation = retain_outgoing(bin_dir, members, now=NOW + timedelta(minutes=minute))
    assert generation is not None
    return generation


def _set_hold(
    bin_dir: Path,
    *,
    now: datetime = NOW,
    hours: int = 1,
    task_id: str = "task-1",
    session_id: str = "session-1",
) -> str:
    return bin_generations.set_hold(
        bin_dir,
        task_id=task_id,
        session_id=session_id,
        expires_at=now + timedelta(hours=hours),
        now=now,
    )


def _promote_gterm_repeatedly(bin_dir: Path) -> None:
    """Simulate KEEP_GENERATIONS + 1 gterm promotions, each installing new bytes first."""
    for index in range(1, KEEP_GENERATIONS + 2):
        _set_binary(bin_dir, "gterm", f"gterm-{index}".encode())
        _capture(bin_dir, ["gterm"], minute=index)


def _retain_gdaemon_from_another_thread(bin_dir: Path, minute: int) -> Generation | None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            retain_outgoing, bin_dir, ["gdaemon"], now=NOW + timedelta(minutes=minute)
        )
        return pending.result(timeout=10)


@pytest.fixture
def short_lock_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.install.bin_generations._LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr("gobby.install.bin_generations._LOCK_POLL_SECONDS", 0.01)


# set_hold: the token, the stored hold and the first retained sequence.


def test_set_hold_returns_a_nonempty_token_that_is_stored(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    token = _set_hold(bin_dir)

    assert isinstance(token, str)
    assert token
    assert read_state(bin_dir)["hold"]["token"] == token


def test_set_hold_tokens_differ_between_stores(tmp_path: Path) -> None:
    first = _set_hold(tmp_path / "first")
    second = _set_hold(tmp_path / "second")

    assert first != second


def test_set_hold_stores_identity_times_and_first_seq(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    expires_at = NOW + timedelta(hours=2)

    bin_generations.set_hold(
        bin_dir,
        task_id="task-7",
        session_id="session-7",
        expires_at=expires_at,
        now=NOW,
    )

    hold = read_state(bin_dir)["hold"]
    assert set(hold) == STORED_HOLD_KEYS
    assert hold["task_id"] == "task-7"
    assert hold["session_id"] == "session-7"
    assert datetime.fromisoformat(hold["set_at"]) == NOW
    assert datetime.fromisoformat(hold["expires_at"]) == expires_at
    assert hold["first_seq"] == 1


def test_set_hold_retains_the_installed_set_like_retain_outgoing(tmp_path: Path) -> None:
    held_dir = tmp_path / "held"
    retained_dir = tmp_path / "retained"
    for bin_dir in (held_dir, retained_dir):
        _seed(bin_dir, "gdaemon", b"gdaemon-0")
        _seed(bin_dir, "gterm", b"gterm-0")

    _set_hold(held_dir)
    retain_outgoing(retained_dir, MANAGED_BINARIES, now=NOW)

    held = [generation.manifest for generation in list_generations(held_dir)]
    retained = [generation.manifest for generation in list_generations(retained_dir)]
    assert len(held) == 1
    assert held == retained


def test_set_hold_with_nothing_installed_starts_at_the_next_sequence(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _update_state(bin_dir, next_seq=7)

    _set_hold(bin_dir)

    assert read_state(bin_dir)["hold"]["first_seq"] == 7


def test_set_hold_on_a_fresh_store_starts_at_sequence_one(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    _set_hold(bin_dir)

    assert read_state(bin_dir)["hold"]["first_seq"] == 1


def test_set_hold_first_seq_is_the_lowest_matching_generation_across_members(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _seed(bin_dir, "gterm", b"gterm-0")
    _capture(bin_dir, ["gdaemon", "gterm"], minute=0)
    (bin_dir / "gdaemon").unlink()
    _set_binary(bin_dir, "gterm", b"gterm-1")
    _capture(bin_dir, ["gterm"], minute=1)
    _set_binary(bin_dir, "gdaemon", b"gdaemon-0")

    _set_hold(bin_dir, now=NOW + timedelta(minutes=2))

    # gdaemon's installed bytes match only seq 1; gterm's match seq 2. The lowest one wins.
    assert read_state(bin_dir)["hold"]["first_seq"] == 1


def test_set_hold_keeps_the_state_file_private(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    record_promotion(bin_dir, ["gcode"], now=NOW)

    _set_hold(bin_dir)

    assert stat.S_IMODE(_state_path(bin_dir).stat().st_mode) == 0o600


def test_set_hold_keeps_the_other_state_keys(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    pins = {"generation-x": _pin(NOW + timedelta(days=1))}
    burned = {"gdaemon": {"1.0.0": {"burned_at": NOW.isoformat(), "generation": None}}}
    _update_state(bin_dir, next_seq=7, pins=pins, burned=burned)

    _set_hold(bin_dir)

    state = read_state(bin_dir)
    assert state["next_seq"] == 7
    assert state["pins"] == pins
    assert state["burned"] == burned


def test_set_hold_refuses_to_replace_an_unexpired_hold(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    first = _set_hold(bin_dir, now=NOW, hours=1, task_id="task-1")

    with pytest.raises(GenerationStoreError, match="hold is already set"):
        _set_hold(bin_dir, now=NOW + timedelta(minutes=30), hours=1, task_id="task-2")

    assert read_state(bin_dir)["hold"]["token"] == first


def test_set_hold_replaces_an_expired_hold(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    first = _set_hold(bin_dir, now=NOW, hours=1, task_id="task-1")

    second = _set_hold(bin_dir, now=NOW + timedelta(hours=2), hours=1, task_id="task-2")

    assert second != first
    assert read_state(bin_dir)["hold"]["token"] == second
    status = bin_generations.hold_status(bin_dir, now=NOW + timedelta(hours=2))
    assert status is not None
    assert status["task_id"] == "task-2"


def test_set_hold_rejects_an_expiry_not_after_now(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    with pytest.raises(GenerationStoreError, match="expir"):
        bin_generations.set_hold(
            bin_dir,
            task_id="task-1",
            session_id="session-1",
            expires_at=NOW,
            now=NOW,
        )

    assert read_state(bin_dir).get("hold") is None


# extend_hold: only the stored token, and only with a later expiry.


def test_extend_hold_updates_the_expiry(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    token = _set_hold(bin_dir, now=NOW, hours=1)
    new_expiry = NOW + timedelta(hours=5)

    bin_generations.extend_hold(
        bin_dir, token, expires_at=new_expiry, now=NOW + timedelta(minutes=30)
    )

    hold = read_state(bin_dir)["hold"]
    assert datetime.fromisoformat(hold["expires_at"]) == new_expiry


def test_extend_hold_without_a_stored_hold_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GenerationStoreError, match="hold"):
        bin_generations.extend_hold(
            tmp_path / "bin",
            "any-token",
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )


def test_extend_hold_with_another_token_is_refused_and_keeps_the_expiry(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir, now=NOW, hours=1)
    original = read_state(bin_dir)["hold"]["expires_at"]

    with pytest.raises(GenerationStoreError, match="hold"):
        bin_generations.extend_hold(
            bin_dir,
            "wrong-token",
            expires_at=NOW + timedelta(hours=5),
            now=NOW + timedelta(minutes=30),
        )

    assert read_state(bin_dir)["hold"]["expires_at"] == original


def test_extend_hold_rejects_an_expiry_not_after_now(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    token = _set_hold(bin_dir, now=NOW, hours=1)
    original = read_state(bin_dir)["hold"]["expires_at"]
    later = NOW + timedelta(minutes=30)

    with pytest.raises(GenerationStoreError, match="expir"):
        bin_generations.extend_hold(bin_dir, token, expires_at=later, now=later)

    assert read_state(bin_dir)["hold"]["expires_at"] == original


def test_extend_hold_with_the_token_revives_an_expired_hold(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    token = _set_hold(bin_dir, now=NOW, hours=1)
    later = NOW + timedelta(hours=2)

    bin_generations.extend_hold(bin_dir, token, expires_at=NOW + timedelta(hours=4), now=later)

    status = bin_generations.hold_status(bin_dir, now=later)
    assert status is not None
    assert status["expired"] is False
    assert datetime.fromisoformat(status["expires_at"]) == NOW + timedelta(hours=4)


# clear_hold: removes the stored hold and reports whether there was one.


def test_clear_hold_removes_the_stored_hold_and_reports_true(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir)

    cleared = bin_generations.clear_hold(bin_dir)

    assert cleared is True
    assert read_state(bin_dir)["hold"] is None
    assert bin_generations.hold_status(bin_dir, now=NOW) is None


def test_clear_hold_reports_false_for_a_second_clear(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir)
    bin_generations.clear_hold(bin_dir)

    assert bin_generations.clear_hold(bin_dir) is False


def test_clear_hold_reports_false_when_no_hold_is_stored(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    record_promotion(bin_dir, ["gcode"], now=NOW)

    assert bin_generations.clear_hold(bin_dir) is False
    assert read_state(bin_dir).get("hold") is None


def test_clear_hold_on_a_bin_dir_without_a_store_creates_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    assert bin_generations.clear_hold(bin_dir) is False
    assert list(bin_dir.iterdir()) == []


# hold_status: the public view of a hold, without its token.


def test_hold_status_is_none_without_a_hold(tmp_path: Path) -> None:
    assert bin_generations.hold_status(tmp_path / "bin", now=NOW) is None


def test_hold_status_omits_the_token(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir)

    status = bin_generations.hold_status(bin_dir, now=NOW + timedelta(minutes=1))

    assert status is not None
    assert set(status) == STATUS_KEYS


def test_hold_status_reports_the_hold_identity_and_times(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir, now=NOW, hours=1, task_id="task-9", session_id="session-9")

    status = bin_generations.hold_status(bin_dir, now=NOW + timedelta(minutes=1))

    assert status is not None
    assert status["task_id"] == "task-9"
    assert status["session_id"] == "session-9"
    assert datetime.fromisoformat(status["set_at"]) == NOW
    assert datetime.fromisoformat(status["expires_at"]) == NOW + timedelta(hours=1)
    assert status["first_seq"] == 1
    assert status["expired"] is False


def test_hold_status_reports_expired_after_its_expiry(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir, now=NOW, hours=1)

    status = bin_generations.hold_status(bin_dir, now=NOW + timedelta(hours=2))

    assert status is not None
    assert status["expired"] is True


@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_hold_status_with_an_unreadable_expiry_is_not_expired(tmp_path: Path, kind: str) -> None:
    bin_dir = tmp_path / "bin"
    _update_state(bin_dir, hold=_hold_with_unreadable_expiry(kind))

    status = bin_generations.hold_status(bin_dir, now=NOW + timedelta(days=30))

    assert status is not None
    assert status["expired"] is False


# burn_version: a version that may never be promoted again.


def test_burn_version_records_the_time_and_generation(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", generation=FIRST_ID, now=NOW)

    entry = read_state(bin_dir)["burned"]["gdaemon"]["1.0.1"]
    assert datetime.fromisoformat(entry["burned_at"]) == NOW
    assert entry["generation"] == FIRST_ID


def test_burn_version_without_a_generation_records_none(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", now=NOW)

    assert read_state(bin_dir)["burned"]["gdaemon"]["1.0.1"]["generation"] is None


def test_burning_the_same_version_again_keeps_the_first_burn_time(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", now=NOW)

    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", now=NOW + timedelta(hours=1))

    entry = read_state(bin_dir)["burned"]["gdaemon"]["1.0.1"]
    assert datetime.fromisoformat(entry["burned_at"]) == NOW


def test_burning_a_version_keeps_other_burned_versions(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.0", now=NOW)

    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", now=NOW)

    assert set(read_state(bin_dir)["burned"]["gdaemon"]) == {"1.0.0", "1.0.1"}


def test_burn_version_refuses_a_name_outside_the_managed_binaries(tmp_path: Path) -> None:
    with pytest.raises(GenerationStoreError, match="burn|binary|version"):
        bin_generations.burn_version(tmp_path / "bin", "othertool", "1.0.0", now=NOW)


def test_burn_version_refuses_an_empty_version(tmp_path: Path) -> None:
    with pytest.raises(GenerationStoreError, match="burn|version"):
        bin_generations.burn_version(tmp_path / "bin", "gdaemon", "", now=NOW)


# promotion_refusal: what a promotion must be refused for, or None.


def test_promotion_refusal_ignores_names_outside_the_managed_binaries(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _update_state(bin_dir, hold=_hold(first_seq=1))

    refusal = bin_generations.promotion_refusal(
        bin_dir, "othertool", "1.0.0", None, now=NOW + timedelta(minutes=1)
    )

    assert refusal is None


def test_promotion_refusal_is_none_and_creates_nothing_without_a_state_file(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    refusal = bin_generations.promotion_refusal(bin_dir, "gdaemon", "1.0.0", None, now=NOW)

    assert refusal is None
    assert list(bin_dir.iterdir()) == []


@pytest.mark.parametrize("payload", ["{not json", "[]"])
def test_promotion_refusal_refuses_unreadable_state(tmp_path: Path, payload: str) -> None:
    bin_dir = tmp_path / "bin"
    _state_path(bin_dir).parent.mkdir(parents=True)
    _state_path(bin_dir).write_text(payload)

    refusal = bin_generations.promotion_refusal(bin_dir, "gdaemon", "1.0.0", None, now=NOW)

    assert refusal is not None
    assert refusal.startswith(REFUSAL_PREFIX)


def test_promotion_refusal_names_a_burned_version(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", now=NOW)

    refusal = bin_generations.promotion_refusal(bin_dir, "gdaemon", "1.0.1", None, now=NOW)

    assert refusal is not None
    assert refusal.startswith(REFUSAL_PREFIX)
    assert "gdaemon" in refusal
    assert "1.0.1" in refusal
    assert "burned" in refusal


def test_a_matching_hold_token_does_not_lift_a_burn(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    token = _set_hold(bin_dir, now=NOW, hours=1)
    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", now=NOW)

    refusal = bin_generations.promotion_refusal(
        bin_dir, "gdaemon", "1.0.1", token, now=NOW + timedelta(minutes=1)
    )

    assert refusal is not None
    assert refusal.startswith(REFUSAL_PREFIX)
    assert "burned" in refusal


@pytest.mark.parametrize(
    ("name", "version"),
    [("gdaemon", "1.0.2"), ("gterm", "1.0.1")],
    ids=["other-version", "other-binary"],
)
def test_a_burn_refuses_only_its_own_name_and_version(
    tmp_path: Path, name: str, version: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_generations.burn_version(bin_dir, "gdaemon", "1.0.1", now=NOW)

    refusal = bin_generations.promotion_refusal(bin_dir, name, version, None, now=NOW)

    assert refusal is None


def test_an_unexpired_hold_refuses_a_promotion_without_its_token(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir, now=NOW, hours=1, task_id="task-1", session_id="session-1")

    refusal = bin_generations.promotion_refusal(
        bin_dir, "gdaemon", "1.0.2", None, now=NOW + timedelta(minutes=1)
    )

    assert refusal is not None
    assert refusal.startswith(REFUSAL_PREFIX)
    assert "task-1" in refusal
    assert "session-1" in refusal


def test_an_unexpired_hold_refuses_a_promotion_with_a_different_token(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir, now=NOW, hours=1)

    refusal = bin_generations.promotion_refusal(
        bin_dir, "gdaemon", "1.0.2", "some-other-token", now=NOW + timedelta(minutes=1)
    )

    assert refusal is not None
    assert refusal.startswith(REFUSAL_PREFIX)


def test_an_unexpired_hold_admits_a_promotion_with_its_token(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    token = _set_hold(bin_dir, now=NOW, hours=1)

    refusal = bin_generations.promotion_refusal(
        bin_dir, "gdaemon", "1.0.2", token, now=NOW + timedelta(minutes=1)
    )

    assert refusal is None


def test_an_expired_hold_refuses_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_hold(bin_dir, now=NOW, hours=1)

    refusal = bin_generations.promotion_refusal(
        bin_dir, "gdaemon", "1.0.2", None, now=NOW + timedelta(hours=2)
    )

    assert refusal is None


@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_a_hold_with_an_unreadable_expiry_refuses_a_promotion(tmp_path: Path, kind: str) -> None:
    bin_dir = tmp_path / "bin"
    _update_state(bin_dir, hold=_hold_with_unreadable_expiry(kind))

    refusal = bin_generations.promotion_refusal(bin_dir, "gdaemon", "1.0.2", None, now=NOW)

    assert refusal is not None
    assert refusal.startswith(REFUSAL_PREFIX)


@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_a_hold_with_an_unreadable_expiry_admits_its_own_token(tmp_path: Path, kind: str) -> None:
    bin_dir = tmp_path / "bin"
    hold = _hold_with_unreadable_expiry(kind)
    _update_state(bin_dir, hold=hold)

    refusal = bin_generations.promotion_refusal(bin_dir, "gdaemon", "1.0.2", hold["token"], now=NOW)

    assert refusal is None


# record_promotion under a hold: identity is attributed only while the hold is unexpired.


def test_record_promotion_under_an_expired_hold_drops_the_hold_identity(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _update_state(bin_dir, hold=_hold(first_seq=1))

    record_promotion(bin_dir, ["gdaemon"], source_commit="abc123", now=NOW + timedelta(hours=2))

    installed = read_state(bin_dir)["installed"]["gdaemon"]
    assert installed["session_id"] is None
    assert installed["task_id"] is None


# promotion_guard: one lock held across a promotion, reentrant on its own thread.


def test_promotion_guard_captures_exactly_what_retain_outgoing_would(tmp_path: Path) -> None:
    guarded_dir = tmp_path / "guarded"
    retained_dir = tmp_path / "retained"
    for bin_dir in (guarded_dir, retained_dir):
        _seed(bin_dir, "gdaemon", b"gdaemon-0")
        _seed(bin_dir, "gterm", b"gterm-0")
    retained = _capture(retained_dir, ["gdaemon", "gterm"])

    with bin_generations.promotion_guard(guarded_dir, ["gdaemon", "gterm"], now=NOW) as captured:
        pass

    assert captured is not None
    assert captured.id == retained.id
    assert captured.manifest == retained.manifest


def test_promotion_guard_retains_only_the_named_members(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _seed(bin_dir, "gterm", b"gterm-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW) as captured:
        pass

    assert captured is not None
    assert set(captured.manifest["members"]) == {"gdaemon"}


def test_promotion_guard_yields_none_when_nothing_is_installed(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    with bin_generations.promotion_guard(bin_dir, MANAGED_BINARIES, now=NOW) as captured:
        pass

    assert captured is None
    assert list_generations(bin_dir) == []


def test_retain_outgoing_inside_the_guard_succeeds_on_the_same_thread(
    tmp_path: Path, short_lock_wait: None
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
        _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
        inner = retain_outgoing(bin_dir, ["gdaemon"], now=NOW + timedelta(minutes=1))

    assert inner is not None
    assert inner.seq == 2
    assert inner.manifest["members"]["gdaemon"]["sha256"] == _sha(b"gdaemon-1")


def test_record_promotion_inside_the_guard_on_the_same_thread_succeeds(
    tmp_path: Path, short_lock_wait: None
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
        _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
        record_promotion(
            bin_dir, ["gdaemon"], source_commit="abc123", now=NOW + timedelta(minutes=1)
        )

    installed = read_state(bin_dir)["installed"]["gdaemon"]
    assert installed["source_commit"] == "abc123"
    assert installed["sha256"] == _sha(b"gdaemon-1")


def test_a_nested_guard_on_the_same_thread_succeeds(tmp_path: Path, short_lock_wait: None) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
        _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
        with bin_generations.promotion_guard(
            bin_dir, ["gdaemon"], now=NOW + timedelta(minutes=1)
        ) as inner:
            pass

    assert inner is not None
    assert inner.seq == 2


def test_another_thread_capture_is_refused_while_the_guard_body_runs(
    tmp_path: Path, short_lock_wait: None
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
        _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                retain_outgoing, bin_dir, ["gdaemon"], now=NOW + timedelta(minutes=1)
            )
            with pytest.raises(GenerationStoreError, match=LOCK_BUSY):
                pending.result(timeout=10)

    assert [generation.seq for generation in list_generations(bin_dir)] == [1]


def test_another_thread_set_hold_is_refused_while_the_guard_body_runs(
    tmp_path: Path, short_lock_wait: None
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                bin_generations.set_hold,
                bin_dir,
                task_id="task-1",
                session_id="session-1",
                expires_at=NOW + timedelta(hours=1),
                now=NOW,
            )
            with pytest.raises(GenerationStoreError, match=LOCK_BUSY):
                pending.result(timeout=10)

    assert read_state(bin_dir).get("hold") is None


def test_another_thread_capture_succeeds_once_the_guard_exits(
    tmp_path: Path, short_lock_wait: None
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
        _set_binary(bin_dir, "gdaemon", b"gdaemon-1")

    generation = _retain_gdaemon_from_another_thread(bin_dir, minute=1)

    assert generation is not None
    assert generation.seq == 2


def test_the_guard_releases_the_lock_when_its_body_raises(
    tmp_path: Path, short_lock_wait: None
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with pytest.raises(RuntimeError, match="^body failed$"):
        with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
            raise RuntimeError("body failed")

    _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
    generation = _retain_gdaemon_from_another_thread(bin_dir, minute=1)
    assert generation is not None
    assert generation.seq == 2


def test_a_same_thread_record_does_not_release_the_guard_lock(
    tmp_path: Path, short_lock_wait: None
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    with bin_generations.promotion_guard(bin_dir, ["gdaemon"], now=NOW):
        _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
        record_promotion(bin_dir, ["gdaemon"], now=NOW + timedelta(minutes=1))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                retain_outgoing, bin_dir, ["gdaemon"], now=NOW + timedelta(minutes=2)
            )
            with pytest.raises(GenerationStoreError, match=LOCK_BUSY):
                pending.result(timeout=10)

    assert [generation.seq for generation in list_generations(bin_dir)] == [1]


# Retention across repeated promotions: a hold keeps what it was set over.


def test_hold_keeps_the_pre_window_generation_and_every_capture_during_the_hold(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _seed(bin_dir, "gterm", b"gterm-0")
    _set_hold(bin_dir, now=NOW, hours=24)

    _promote_gterm_repeatedly(bin_dir)

    generations = list_generations(bin_dir)
    assert [generation.seq for generation in generations] == [1, 2, 3, 4, 5]
    assert generations[0].manifest["members"]["gdaemon"]["sha256"] == _sha(b"gdaemon-0")


def test_without_a_hold_the_same_promotions_prune_to_the_keep_rule(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _seed(bin_dir, "gterm", b"gterm-0")
    _capture(bin_dir, ["gdaemon", "gterm"], minute=0)

    _promote_gterm_repeatedly(bin_dir)

    assert [generation.seq for generation in list_generations(bin_dir)] == [1, 3, 4, 5]
