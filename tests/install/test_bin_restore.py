"""Restoring a retained generation as the installed managed binaries.

Restores run through restore_refusal and run_restore against real files under pytest temp
directories. Each managed binary is a shell script that answers --version and the schema
identity probe. The daemon stop, start and restart check are recording fakes. Holds are set
with a far-future expiry, so the clock the promotion reads and the injected clock agree.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from gobby.install import bin_freshness_promotion as promotion
from gobby.install.bin_generations import (
    GENERATIONS_DIR,
    MANAGED_BINARIES,
    Generation,
    burn_version,
    hold_status,
    promotion_refusal,
    read_state,
    retain_outgoing,
    set_hold,
)
from gobby.install.version_probe import probe_native_bin_version

if TYPE_CHECKING:
    from gobby.install.bin_restore import RestoreOutcome

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
HOLD_TASK = "restore-owner-task"
HOLD_SESSION = "restore-owner-session"
MIB = 1024 * 1024
STAMPED_SET = ("gcode", "gdaemon", "ghook")
CRATE_DIRS = ("gcode", "gdaemon", "ghook", "gclient", "gmcp", "gterminal", "gcore")
IDENTITY: dict[str, int | str] = {
    "runner_protocol": 1,
    "baseline_version": 420,
    "baseline_checksum": "baseline",
    "latest_version": 468,
    "latest_checksum": "latest",
    "assets_root_hash": "root",
}
OTHER_IDENTITY: dict[str, int | str] = {**IDENTITY, "baseline_version": 421}


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


class _Daemon:
    """Recording stand-ins for the daemon stop, start and restart check.

    Every call appends its name to events, so the order of the restore is observable. The
    restart check also records the probed gdaemon version at the moment it runs.
    """

    def __init__(
        self,
        *,
        stops: bool = True,
        refusal: str | None = None,
        before_stop: Callable[[], None] | None = None,
    ) -> None:
        self._stops = stops
        self._refusal = refusal
        self._before_stop = before_stop
        self.events: list[str] = []
        self.checked: list[tuple[Path | None, bytes | None]] = []
        self.gdaemon_at_verify: list[str | None] = []

    def stop(self) -> bool:
        self.events.append("stop")
        if self._before_stop is not None:
            self._before_stop()
        return self._stops

    def start(self) -> None:
        self.events.append("start")

    def start_refusal(self, candidate: Path | None) -> str | None:
        self.events.append("start_refusal")
        payload = candidate.read_bytes() if candidate is not None else None
        self.checked.append((candidate, payload))
        return self._refusal

    def verify(self, bin_dir: Path) -> None:
        self.events.append("verify")
        self.gdaemon_at_verify.append(probe_native_bin_version(bin_dir / "gdaemon"))


@pytest.fixture(autouse=True)
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Gobby-shaped checkout, so release preflight never reads the real one."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    for name in CRATE_DIRS:
        crate = root / "crates" / name
        (crate / "src").mkdir(parents=True)
        dependency = '\n[dependencies]\ngobby-core = { path = "../gcore" }\n'
        if name == "gclient":
            dependency += 'gobby-terminal = { path = "../gterminal" }\n'
        (crate / "Cargo.toml").write_text(
            f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
            + (dependency if name != "gcore" else "")
        )
        (crate / "src" / "lib.rs").write_text("pub fn initial() {}\n")
    monkeypatch.setattr(promotion, "_workspace_crates_root", lambda: root / "crates")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _script(name: str, version: str | None, marker: str) -> bytes:
    """A managed binary that prints its version for --version and its schema identity otherwise.

    A version of None makes --version fail, so the binary reports no version at all. The marker
    is a comment, so two builds at one version still have different bytes.
    """
    identity = json.dumps(IDENTITY, separators=(",", ":"), sort_keys=True)
    report = f"printf '%s\\n' '{name} {version}'" if version is not None else "exit 1"
    script = (
        "#!/bin/sh\n"
        f"# build {marker}\n"
        'if [ "$1" = "--version" ]; then\n'
        f"{report}\n"
        "else\n"
        f"printf '%s\\n' '{identity}'\n"
        "fi\n"
    )
    return script.encode()


def _place(
    bin_dir: Path,
    name: str,
    payload: bytes,
    receipt_version: str | None,
    *,
    sidecars: bool = True,
) -> None:
    """Install a binary file and the receipts a real promotion leaves beside it."""
    _write(bin_dir / name, payload, 0o755)
    receipt = {
        "artifact_sha256": _sha(payload),
        "source_identity": f"source-{name}",
        "version": receipt_version,
    }
    _write(bin_dir / f".{name}-release-identity.json", _json_bytes(receipt), 0o600)
    if sidecars:
        _write(bin_dir / f".{name}-version", f"{receipt_version or ''}\n".encode(), 0o644)
        _write(
            bin_dir / f".{name}-install.json",
            _json_bytes({"installed_version": receipt_version}),
            0o644,
        )
        _write(bin_dir / f".{name}-source-sha256", f"{_sha(payload)}\n".encode(), 0o644)


def _install(bin_dir: Path, name: str, version: str, *, marker: str, sidecars: bool = True) -> None:
    """Install a managed binary that reports version, with the receipts a promotion leaves."""
    _place(bin_dir, name, _script(name, version, marker), version, sidecars=sidecars)


def _write_stamp(bin_dir: Path, identity: dict[str, int | str] = IDENTITY) -> None:
    _write(bin_dir / ".gdaemon-schema-identity.json", _json_bytes(identity), 0o644)


def _retain(bin_dir: Path, members: list[str], minute: int) -> Generation:
    generation = retain_outgoing(bin_dir, members, now=_at(minute))
    assert generation is not None
    return generation


def _outgoing_store(tmp_path: Path) -> tuple[Path, Generation]:
    """A bin dir whose retained gdaemon is 1.0.0 and whose installed gdaemon is 1.0.1."""
    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _install(bin_dir, "gdaemon", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gdaemon"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="new")
    return bin_dir, generation


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under root with its bytes, or None for a directory."""
    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


def _restore(
    bin_dir: Path,
    generation: Generation,
    members: list[str],
    daemon: _Daemon,
    *,
    hold_token: str | None = None,
) -> RestoreOutcome:
    from gobby.install.bin_restore import run_restore

    return run_restore(
        bin_dir,
        generation,
        members,
        hold_token=hold_token,
        stop_daemon=daemon.stop,
        start_daemon=daemon.start,
        start_refusal=daemon.start_refusal,
        verify_restart_target=daemon.verify,
        now=_at(10),
    )


# Member selection: what a restore touches by default, by name, or in full.


def test_restore_members_defaults_to_gdaemon(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_members

    _, generation = _outgoing_store(tmp_path)

    assert restore_members(generation) == ["gdaemon"]


def test_restore_members_with_all_members_lists_every_recorded_member_in_managed_order(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import restore_members

    bin_dir = tmp_path / "bin"
    for name in ("gclient", "gdaemon", "gcode"):
        _install(bin_dir, name, "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gclient", "gdaemon", "gcode"], 0)

    assert restore_members(generation, all_members=True) == ["gcode", "gdaemon", "gclient"]


def test_restore_members_with_names_returns_exactly_those_members(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_members

    bin_dir = tmp_path / "bin"
    for name in ("gcode", "gclient"):
        _install(bin_dir, name, "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gcode", "gclient"], 0)

    members = restore_members(generation, names=["gclient", "gcode"])

    assert sorted(members) == sorted(["gclient", "gcode"])


def test_restore_members_refuses_a_requested_member_the_generation_does_not_record(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import RestoreError, restore_members

    _, generation = _outgoing_store(tmp_path)

    with pytest.raises(RestoreError, match="gmcp"):
        restore_members(generation, names=["gmcp"])


def test_restore_members_refuses_names_combined_with_all_members(tmp_path: Path) -> None:
    from gobby.install.bin_restore import RestoreError, restore_members

    _, generation = _outgoing_store(tmp_path)

    with pytest.raises(RestoreError, match=r"names.*all|all.*names"):
        restore_members(generation, names=["gdaemon"], all_members=True)


def test_restore_members_refuses_the_default_when_gdaemon_is_not_recorded(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import RestoreError, restore_members

    bin_dir = tmp_path / "bin"
    _install(bin_dir, "gmcp", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gmcp"], 0)

    with pytest.raises(RestoreError, match="gdaemon"):
        restore_members(generation)


# Restore refusal: a read-only proof taken before anything stops.


def test_restore_refusal_is_none_for_a_restorable_generation(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)

    assert restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10)) is None


def test_restore_refusal_writes_nothing_under_bin_dir(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)
    before = _snapshot(bin_dir)

    restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10))

    assert _snapshot(bin_dir) == before


def test_restore_refusal_when_a_retained_binary_is_missing(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)
    (generation.path / "gdaemon").unlink()

    assert restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10)) is not None


def test_restore_refusal_when_a_retained_binary_differs_from_its_manifest_sha256(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)
    (generation.path / "gdaemon").write_bytes(b"tampered")

    assert restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10)) is not None


def test_restore_refusal_when_a_retained_binary_reports_another_version(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    # The bytes are version 1.0.1, but the receipt that a capture records says 1.0.0.
    _place(bin_dir, "gdaemon", _script("gdaemon", "1.0.1", "mislabeled"), "1.0.0")
    generation = _retain(bin_dir, ["gdaemon"], 0)

    assert restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10)) is not None


def test_restore_refusal_when_the_manifest_records_no_version(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _place(bin_dir, "gdaemon", _script("gdaemon", "1.0.0", "unversioned"), None)
    generation = _retain(bin_dir, ["gdaemon"], 0)

    assert restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10)) is not None


def test_restore_refusal_when_an_unexpired_hold_lacks_its_token(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)
    set_hold(
        bin_dir,
        task_id=HOLD_TASK,
        session_id=HOLD_SESSION,
        expires_at=FAR_FUTURE,
        now=_at(5),
    )

    reason = restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10))

    assert reason is not None
    assert HOLD_TASK in reason


def test_restore_refusal_is_none_with_the_hold_token(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)
    token = set_hold(
        bin_dir,
        task_id=HOLD_TASK,
        session_id=HOLD_SESSION,
        expires_at=FAR_FUTURE,
        now=_at(5),
    )

    reason = restore_refusal(bin_dir, generation, ["gdaemon"], hold_token=token, now=_at(10))

    assert reason is None


def test_restore_refusal_when_the_version_to_restore_is_burned(tmp_path: Path) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)
    burn_version(bin_dir, "gdaemon", "1.0.0", now=_at(5))

    reason = restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10))

    assert reason is not None
    assert "gdaemon 1.0.0 is burned" in reason


def test_restore_refusal_for_a_subset_with_another_schema_identity_asks_for_all_members(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir, OTHER_IDENTITY)
    _install(bin_dir, "gdaemon", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gdaemon"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="new")
    _write_stamp(bin_dir)

    reason = restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10))

    assert reason is not None
    assert re.search(r"all .*members", reason)


def test_restore_refusal_for_the_full_set_ignores_a_schema_identity_difference(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir, OTHER_IDENTITY)
    for name in STAMPED_SET:
        _install(bin_dir, name, "1.0.0", marker="old")
    generation = _retain(bin_dir, list(STAMPED_SET), 0)
    _write_stamp(bin_dir)

    assert restore_refusal(bin_dir, generation, list(STAMPED_SET), now=_at(10)) is None


def test_restore_refusal_when_the_store_has_no_room_for_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.install.bin_restore import restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)

    def fake_disk_usage(_path: object) -> _Usage:
        return _Usage(total=1 << 40, used=1 << 39, free=16 * MIB)

    monkeypatch.setattr("gobby.install.bin_generations.shutil.disk_usage", fake_disk_usage)

    assert restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10)) is not None


# Running a restore: refusals, the copy out of the store, the daemon, and what is replaced.


def test_run_restore_refuses_a_tampered_generation_before_anything_stops(tmp_path: Path) -> None:
    from gobby.install.bin_restore import RestoreError, restore_refusal

    bin_dir, generation = _outgoing_store(tmp_path)
    (generation.path / "gdaemon").write_bytes(b"tampered")
    reason = restore_refusal(bin_dir, generation, ["gdaemon"], now=_at(10))
    assert reason is not None
    expected = re.escape(f"refusing to restore: {reason}")
    before = _snapshot(bin_dir)
    daemon = _Daemon()

    with pytest.raises(RestoreError, match=f"^{expected}$"):
        _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert "stop" not in daemon.events
    assert "start" not in daemon.events
    assert _snapshot(bin_dir) == before


@pytest.mark.parametrize("token", [None, "not-the-hold-token"], ids=["missing", "wrong"])
def test_run_restore_refuses_a_held_store_without_its_token(
    tmp_path: Path, token: str | None
) -> None:
    from gobby.install.bin_restore import RestoreError

    bin_dir, generation = _outgoing_store(tmp_path)
    set_hold(
        bin_dir,
        task_id=HOLD_TASK,
        session_id=HOLD_SESSION,
        expires_at=FAR_FUTURE,
        now=_at(5),
    )
    before = _snapshot(bin_dir)
    daemon = _Daemon()

    with pytest.raises(RestoreError, match=rf"^refusing to restore: .*{HOLD_TASK}"):
        _restore(bin_dir, generation, ["gdaemon"], daemon, hold_token=token)

    assert "stop" not in daemon.events
    assert _snapshot(bin_dir) == before


def test_run_restore_of_the_oldest_kept_generation_survives_its_own_capture_pruning_it(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _install(bin_dir, "gdaemon", "1.0.0", marker="v0")
    oldest = _retain(bin_dir, ["gdaemon"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="v1")
    _retain(bin_dir, ["gdaemon"], 1)
    _install(bin_dir, "gdaemon", "1.0.2", marker="v2")
    _retain(bin_dir, ["gdaemon"], 2)
    _install(bin_dir, "gdaemon", "1.0.3", marker="v3")
    # Premise: on a copy, the capture a restore takes first prunes the oldest generation.
    copy = tmp_path / "copy"
    shutil.copytree(bin_dir, copy)
    retain_outgoing(copy, MANAGED_BINARIES, now=_at(3))
    assert not (copy / GENERATIONS_DIR / oldest.id).exists()
    daemon = _Daemon()

    _restore(bin_dir, oldest, ["gdaemon"], daemon)

    assert b"# build v0" in (bin_dir / "gdaemon").read_bytes()
    assert probe_native_bin_version(bin_dir / "gdaemon") == "1.0.0"


def test_run_restore_checks_the_start_against_a_copy_of_the_restored_gdaemon(
    tmp_path: Path,
) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    [(candidate, payload)] = daemon.checked
    assert candidate is not None
    assert payload == (generation.path / "gdaemon").read_bytes()
    assert generation.path not in candidate.parents
    assert bin_dir / GENERATIONS_DIR not in candidate.parents


def test_run_restore_without_gdaemon_checks_the_start_with_no_candidate(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "gmcp", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gmcp"], 0)
    _install(bin_dir, "gmcp", "1.0.1", marker="new")
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gmcp"], daemon)

    assert daemon.checked == [(None, None)]


def test_run_restore_refuses_when_the_start_check_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import RestoreError

    bin_dir, generation = _outgoing_store(tmp_path)
    before = _snapshot(bin_dir)
    reason = "gdaemon 1.0.0 cannot start with this schema"
    daemon = _Daemon(refusal=reason)
    expected = re.escape(f"refusing to restore: {reason}")

    with pytest.raises(RestoreError, match=f"^{expected}$"):
        _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert "stop" not in daemon.events
    assert _snapshot(bin_dir) == before


def test_run_restore_refuses_when_the_daemon_does_not_stop(tmp_path: Path) -> None:
    from gobby.install.bin_restore import RestoreError

    bin_dir, generation = _outgoing_store(tmp_path)
    before = _snapshot(bin_dir)
    daemon = _Daemon(stops=False)

    with pytest.raises(RestoreError, match="did not stop"):
        _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert "start" not in daemon.events
    assert _snapshot(bin_dir) == before


def test_run_restore_installs_the_generations_gdaemon_version(tmp_path: Path) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert probe_native_bin_version(bin_dir / "gdaemon") == "1.0.0"


def test_run_restore_outcome_names_the_generation_and_the_restored_version(
    tmp_path: Path,
) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    daemon = _Daemon()

    outcome = _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert outcome.generation == generation.id
    assert outcome.restored == {"gdaemon": "1.0.0"}


def test_run_restore_records_the_version_and_source_identity_in_the_receipt(
    tmp_path: Path,
) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    receipt: dict[str, Any] = json.loads(
        (bin_dir / ".gdaemon-release-identity.json").read_text(encoding="utf-8")
    )
    assert receipt["version"] == "1.0.0"
    assert receipt["source_identity"] == "source-gdaemon"


def test_run_restore_records_the_version_when_the_manifest_has_no_source_identity(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _install(bin_dir, "gdaemon", "1.0.0", marker="old")
    _write(bin_dir / ".gdaemon-release-identity.json", _json_bytes({"version": "1.0.0"}), 0o600)
    generation = _retain(bin_dir, ["gdaemon"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="new")
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    receipt: dict[str, Any] = json.loads(
        (bin_dir / ".gdaemon-release-identity.json").read_text(encoding="utf-8")
    )
    assert receipt["version"] == "1.0.0"


def test_run_restore_copies_the_members_sidecars_from_the_generation(tmp_path: Path) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    member_files: list[str] = generation.manifest["members"]["gdaemon"]["files"]
    sidecars = [
        name for name in member_files if name not in ("gdaemon", ".gdaemon-release-identity.json")
    ]
    assert sidecars
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    for name in sidecars:
        assert (bin_dir / name).read_bytes() == (generation.path / name).read_bytes()


def test_run_restore_removes_installed_sidecars_the_generation_does_not_record(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _install(bin_dir, "gdaemon", "1.0.0", marker="old", sidecars=False)
    generation = _retain(bin_dir, ["gdaemon"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="new")
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert not (bin_dir / ".gdaemon-install.json").exists()
    assert not (bin_dir / ".gdaemon-version").exists()


def test_run_restore_puts_back_the_retained_gclient_alias(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "gclient", "1.0.0", marker="old")
    _write(bin_dir / "gobby", b"alias-old", 0o755)
    generation = _retain(bin_dir, ["gclient"], 0)
    _install(bin_dir, "gclient", "1.0.1", marker="new")
    _write(bin_dir / "gobby", b"alias-new", 0o755)
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gclient"], daemon)

    assert (bin_dir / "gobby").read_bytes() == b"alias-old"


def test_run_restore_removes_the_installed_gclient_alias_when_the_generation_has_none(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "gclient", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gclient"], 0)
    _install(bin_dir, "gclient", "1.0.1", marker="new")
    _write(bin_dir / "gobby", b"alias-current", 0o755)
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gclient"], daemon)

    assert not (bin_dir / "gobby").exists()


def test_run_restore_moves_aside_an_installed_binary_that_reports_no_version(
    tmp_path: Path,
) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    unverified = _script("gdaemon", None, "unverified-build")
    _write(bin_dir / "gdaemon", unverified, 0o755)
    daemon = _Daemon()

    outcome = _restore(bin_dir, generation, ["gdaemon"], daemon)

    [moved] = outcome.moved_aside
    assert moved.startswith(".")
    assert "unverified" in moved
    assert (bin_dir / moved).read_bytes() == unverified
    assert probe_native_bin_version(bin_dir / "gdaemon") == "1.0.0"


def test_run_restore_burns_the_outgoing_version_it_replaces(tmp_path: Path) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    daemon = _Daemon()

    outcome = _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert outcome.burned == {"gdaemon": "1.0.1"}
    burned: dict[str, dict[str, Any]] = read_state(bin_dir)["burned"]
    assert "1.0.1" in burned["gdaemon"]


def test_run_restore_never_burns_the_version_it_restores(tmp_path: Path) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert promotion_refusal(bin_dir, "gdaemon", "1.0.0", None) is None


def test_run_restore_does_not_burn_a_member_whose_outgoing_version_equals_the_restored(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _install(bin_dir, "gdaemon", "1.0.0", marker="old")
    _install(bin_dir, "gmcp", "1.0.0", marker="same")
    generation = _retain(bin_dir, ["gdaemon", "gmcp"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="new")
    daemon = _Daemon()

    # gmcp keeps the generation's bytes, so its outgoing version equals the one restored.
    outcome = _restore(bin_dir, generation, ["gdaemon", "gmcp"], daemon)

    assert outcome.burned == {"gdaemon": "1.0.1"}


def test_run_restore_orders_the_start_check_stop_promotion_verify_and_start(
    tmp_path: Path,
) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert daemon.events == ["start_refusal", "stop", "verify", "start"]
    assert daemon.gdaemon_at_verify == ["1.0.0"]


def test_run_restore_without_gdaemon_skips_the_restart_check(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "gmcp", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gmcp"], 0)
    _install(bin_dir, "gmcp", "1.0.1", marker="new")
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gmcp"], daemon)

    assert daemon.events == ["start_refusal", "stop", "start"]


def test_run_restore_with_the_hold_token_restores_and_leaves_the_hold_stored(
    tmp_path: Path,
) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    token = set_hold(
        bin_dir,
        task_id=HOLD_TASK,
        session_id=HOLD_SESSION,
        expires_at=FAR_FUTURE,
        now=_at(5),
    )
    daemon = _Daemon()

    _restore(bin_dir, generation, ["gdaemon"], daemon, hold_token=token)

    assert probe_native_bin_version(bin_dir / "gdaemon") == "1.0.0"
    assert hold_status(bin_dir, now=_at(10)) is not None


def test_run_restore_names_the_stopped_daemon_when_promotion_fails_after_the_stop(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import RestoreError

    bin_dir = tmp_path / "bin"
    _install(bin_dir, "gmcp", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gmcp"], 0)
    _install(bin_dir, "gmcp", "1.0.1", marker="new")
    installed = (bin_dir / "gmcp").read_bytes()
    # The restored version is burned after the restore's own preflight and before its promotion.
    daemon = _Daemon(before_stop=lambda: burn_version(bin_dir, "gmcp", "1.0.0", now=_at(7)))

    with pytest.raises(RestoreError, match="daemon is stopped") as error:
        _restore(bin_dir, generation, ["gmcp"], daemon)

    assert "gmcp" in str(error.value)
    assert "start" not in daemon.events
    assert (bin_dir / "gmcp").read_bytes() == installed


def test_run_restore_of_the_default_member_leaves_gcode_and_ghook_untouched(
    tmp_path: Path,
) -> None:
    from gobby.install.bin_restore import restore_members

    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _install(bin_dir, "gcode", "1.0.0", marker="code")
    _install(bin_dir, "ghook", "1.0.0", marker="hook")
    _install(bin_dir, "gdaemon", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gdaemon"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="new")
    untouched = {name: (bin_dir / name).read_bytes() for name in ("gcode", "ghook")}
    members = restore_members(generation)
    daemon = _Daemon()

    _restore(bin_dir, generation, members, daemon)

    assert members == ["gdaemon"]
    assert probe_native_bin_version(bin_dir / "gdaemon") == "1.0.0"
    for name, payload in untouched.items():
        assert (bin_dir / name).read_bytes() == payload
