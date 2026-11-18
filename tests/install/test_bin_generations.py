"""Generation retention for managed binaries: captures, dedupe, pruning and promotion hooks.

The store is driven through its public entry points against real files in pytest temp
directories. Stub binaries and the workspace fixture follow test_promotion_goldens.py.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest
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
    retention_refusal,
)

from gobby.install import bin_freshness_promotion as promotion
from gobby.install import bin_generations
from gobby.install import bin_release_identity as release
from gobby.install.bin_set_coherence import (
    WorkspacePromotionMetadata,
    promote_workspace_binary_set,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
FIRST_ID = "000001-20261117T120000Z"
MIB = 1024 * 1024
REFUSAL_PREFIX = "generation retention unavailable: "
SET_MEMBERS = ("gcode", "gdaemon", "ghook")
CRATE_DIRS = ("gcode", "gdaemon", "ghook", "gclient", "gmcp", "gterminal", "gcore")
IDENTITY = {
    "runner_protocol": 1,
    "baseline_version": 420,
    "baseline_checksum": "baseline",
    "latest_version": 468,
    "latest_checksum": "latest",
    "assets_root_hash": "root",
}


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


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


def _seed(bin_dir: Path, name: str, payload: bytes, *, version: str = "1.0.0") -> None:
    """Install a managed binary with the receipt files a real promotion leaves beside it."""
    _set_binary(bin_dir, name, payload)
    receipt = {
        "artifact_sha256": _sha(payload),
        "source_identity": f"src-{name}",
        "version": version,
    }
    _write(bin_dir / f".{name}-release-identity.json", _json_bytes(receipt), 0o600)
    _write(bin_dir / f".{name}-version", f"{version}\n".encode(), 0o644)
    _write(bin_dir / f".{name}-install.json", _json_bytes({"installed_version": version}), 0o644)
    _write(bin_dir / f".{name}-source-sha256", f"{_sha(payload)}\n".encode(), 0o644)


def _standard_files(name: str) -> list[str]:
    """File names `_seed` installs for a member, in the sorted order a manifest records them."""
    return sorted(
        [
            name,
            f".{name}-release-identity.json",
            f".{name}-version",
            f".{name}-install.json",
            f".{name}-source-sha256",
        ]
    )


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


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
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


def _binary(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(IDENTITY, separators=(",", ":"), sort_keys=True)
    path.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
        f"printf '%s\\n' '{path.name} {version}'\nelse\n"
        f"printf '%s\\n' '{payload}'\nfi\n"
    )
    path.chmod(0o755)
    return path


def _candidates(root: Path, version: str) -> dict[str, Path]:
    return {name: _binary(root / version / name, version) for name in SET_MEMBERS}


def _promote(workspace: Path, bin_dir: Path, candidates: dict[str, Path]) -> None:
    promote_workspace_binary_set(
        candidates,
        bin_dir=bin_dir,
        workspace_root=workspace,
        metadata=WorkspacePromotionMetadata(install_method="workspace-cutover", target="test"),
    )


def _bump_set_members(workspace: Path) -> None:
    for name in SET_MEMBERS:
        manifest = workspace / "crates" / name / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace("1.0.0", "1.0.1"))


# Captures, manifests and the empty or missing directory cases.


def test_missing_bin_dir_captures_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "missing"

    assert retain_outgoing(bin_dir, MANAGED_BINARIES, now=NOW) is None

    assert not bin_dir.exists()
    assert list_generations(bin_dir) == []


def test_empty_bin_dir_captures_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    assert retain_outgoing(bin_dir, MANAGED_BINARIES, now=NOW) is None

    assert list(bin_dir.iterdir()) == []


def test_unmanaged_binaries_are_never_captured(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_binary(bin_dir, "othertool", b"othertool-0")

    assert retain_outgoing(bin_dir, ["othertool"], now=NOW) is None

    assert not (bin_dir / GENERATIONS_DIR).exists()


def test_first_capture_writes_named_generation_with_manifest(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    generation = _capture(bin_dir, ["gdaemon"])

    assert generation.id == FIRST_ID
    assert generation.seq == 1
    assert generation.path == bin_dir / GENERATIONS_DIR / FIRST_ID
    assert generation.path.is_dir()
    assert set(generation.manifest) == {
        "format",
        "id",
        "seq",
        "created_at",
        "schema_identity",
        "members",
    }
    assert generation.manifest["format"] == 1
    assert generation.manifest["id"] == FIRST_ID
    assert generation.manifest["seq"] == 1
    assert datetime.fromisoformat(generation.manifest["created_at"]) == NOW
    assert generation.manifest["schema_identity"] is None
    assert set(generation.manifest["members"]) == {"gdaemon"}
    assert generation.manifest["members"]["gdaemon"] == {
        "sha256": _sha(b"gdaemon-0"),
        "version": "1.0.0",
        "source_identity": "src-gdaemon",
        "promoted_at": None,
        "source_commit": None,
        "session_id": None,
        "task_id": None,
        "files": _standard_files("gdaemon"),
    }
    assert read_state(bin_dir)["next_seq"] == 2


def test_capture_copies_are_identical_separate_inodes_with_modes(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    generation = _capture(bin_dir, ["gdaemon"])

    for name in _standard_files("gdaemon"):
        installed = bin_dir / name
        copy = generation.path / name
        assert copy.read_bytes() == installed.read_bytes()
        assert stat.S_IMODE(copy.stat().st_mode) == stat.S_IMODE(installed.stat().st_mode)
        assert copy.stat().st_ino != installed.stat().st_ino
        assert copy.stat().st_nlink == 1
    assert stat.S_IMODE((generation.path / "gdaemon").stat().st_mode) == 0o755
    receipt = generation.path / ".gdaemon-release-identity.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_gdaemon_schema_identity_and_ghook_extras_are_copied(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _write(bin_dir / ".gdaemon-schema-identity.json", _json_bytes(IDENTITY), 0o644)
    _seed(bin_dir, "ghook", b"ghook-0")
    _write(bin_dir / ".ghook-compatibility", b"compatibility\n", 0o644)
    _write(bin_dir / ".ghook-runtime.json", b'{"runtime": 1}\n', 0o644)

    generation = _capture(bin_dir, ["gdaemon", "ghook"])

    assert generation.manifest["schema_identity"] == IDENTITY
    gdaemon_files = sorted([*_standard_files("gdaemon"), ".gdaemon-schema-identity.json"])
    assert generation.manifest["members"]["gdaemon"]["files"] == gdaemon_files
    ghook_files = sorted([*_standard_files("ghook"), ".ghook-compatibility", ".ghook-runtime.json"])
    assert generation.manifest["members"]["ghook"]["files"] == ghook_files
    for name in (".gdaemon-schema-identity.json", ".ghook-compatibility", ".ghook-runtime.json"):
        assert (generation.path / name).read_bytes() == (bin_dir / name).read_bytes()


def test_gclient_alias_is_recorded_and_copied_when_present(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gclient", b"gclient-0")
    _write(bin_dir / "gobby", b"alias-bytes", 0o755)

    generation = _capture(bin_dir, ["gclient"])

    member = generation.manifest["members"]["gclient"]
    assert member["alias"] is True
    assert "gobby" in member["files"]
    assert (generation.path / "gobby").read_bytes() == b"alias-bytes"


def test_gclient_alias_absent_is_recorded_false_and_not_copied(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gclient", b"gclient-0")

    generation = _capture(bin_dir, ["gclient"])

    member = generation.manifest["members"]["gclient"]
    assert member["alias"] is False
    assert "gobby" not in member["files"]
    assert not (generation.path / "gobby").exists()


# Dedupe: nothing changed means no new directory.


def test_unchanged_store_dedupes_and_a_changed_binary_records_every_considered_member(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    for name in SET_MEMBERS:
        _seed(bin_dir, name, f"{name}-0".encode())
    _capture(bin_dir, MANAGED_BINARIES)

    assert retain_outgoing(bin_dir, MANAGED_BINARIES, now=NOW + timedelta(minutes=1)) is None
    assert [g.seq for g in list_generations(bin_dir)] == [1]

    _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
    second = _capture(bin_dir, MANAGED_BINARIES, minute=2)

    assert second.seq == 2
    assert set(second.manifest["members"]) == set(SET_MEMBERS)
    assert second.manifest["members"]["gcode"]["sha256"] == _sha(b"gcode-0")
    assert second.manifest["members"]["gdaemon"]["sha256"] == _sha(b"gdaemon-1")
    assert second.manifest["members"]["ghook"]["sha256"] == _sha(b"ghook-0")
    assert [g.seq for g in list_generations(bin_dir)] == [1, 2]


# Pruning: count, pins, holds, installed bytes and per-member retention.


def test_prune_keeps_only_the_newest_kept_generations(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    captures = KEEP_GENERATIONS + 2
    for index in range(captures):
        _set_binary(bin_dir, "gdaemon", f"gdaemon-{index}".encode())
        _capture(bin_dir, ["gdaemon"], minute=index)

    newest = list(range(captures - KEEP_GENERATIONS + 1, captures + 1))
    assert [g.seq for g in list_generations(bin_dir)] == newest


def test_pinned_generation_survives_only_until_its_pin_expires(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_binary(bin_dir, "gdaemon", b"gdaemon-0")
    pinned = _capture(bin_dir, ["gdaemon"], minute=0)
    _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
    expired = _capture(bin_dir, ["gdaemon"], minute=1)
    _set_binary(bin_dir, "gdaemon", b"gdaemon-2")
    _capture(bin_dir, ["gdaemon"], minute=2)
    _update_state(
        bin_dir,
        pins={
            pinned.id: _pin(NOW + timedelta(days=30)),
            expired.id: _pin(NOW - timedelta(days=1)),
        },
    )

    _set_binary(bin_dir, "gdaemon", b"gdaemon-3")
    _capture(bin_dir, ["gdaemon"], minute=3)
    assert [g.seq for g in list_generations(bin_dir)] == [1, 2, 3, 4]

    _set_binary(bin_dir, "gdaemon", b"gdaemon-4")
    _capture(bin_dir, ["gdaemon"], minute=4)
    assert [g.seq for g in list_generations(bin_dir)] == [1, 3, 4, 5]


def test_hold_keeps_generations_from_first_seq_onward(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_binary(bin_dir, "gdaemon", b"gdaemon-0")
    _capture(bin_dir, ["gdaemon"], minute=0)
    _update_state(bin_dir, hold=_hold(first_seq=2))

    for index in range(1, 5):
        _set_binary(bin_dir, "gdaemon", f"gdaemon-{index}".encode())
        _capture(bin_dir, ["gdaemon"], minute=index)

    assert [g.seq for g in list_generations(bin_dir)] == [2, 3, 4, 5]


def test_generation_matching_the_installed_gdaemon_survives_pruning(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _set_binary(bin_dir, "gdaemon", b"gdaemon-0")
    _capture(bin_dir, ["gdaemon"], minute=0)
    _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
    _capture(bin_dir, ["gdaemon"], minute=1)
    _set_binary(bin_dir, "gdaemon", b"gdaemon-2")
    _capture(bin_dir, ["gdaemon"], minute=2)

    # Seq 1 is about to fall out of the newest three. Writing its bytes back as the
    # installed gdaemon keeps it, because its recorded sha256 matches the installed binary.
    _set_binary(bin_dir, "gdaemon", b"gdaemon-0")
    _capture(bin_dir, ["gdaemon"], minute=3)

    assert [g.seq for g in list_generations(bin_dir)] == [1, 2, 3, 4]


def test_per_member_retention_keeps_a_generation_still_newest_for_another_member(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _seed(bin_dir, "gclient", b"gclient-0")
    _capture(bin_dir, MANAGED_BINARIES, minute=0)
    for index in range(1, 4):
        _set_binary(bin_dir, "gclient", f"gclient-{index}".encode())
        _capture(bin_dir, ["gclient"], minute=index)

    # Seq 1 is no longer among the newest three gclient generations, but it is the only
    # generation recording gdaemon, so it stays.
    assert [g.seq for g in list_generations(bin_dir)] == [1, 2, 3, 4]


# Promotion records and the hold identity they carry.


def test_record_promotion_creates_generations_directory_and_state(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    record_promotion(bin_dir, ["ghook"], now=NOW)

    assert (bin_dir / GENERATIONS_DIR).is_dir()
    assert _state_path(bin_dir).is_file()
    assert read_state(bin_dir)["installed"]["ghook"]["source_commit"] is None


def test_record_promotion_writes_installed_entries_and_keeps_other_state(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    pins = {"generation-x": _pin(NOW + timedelta(days=1))}
    burned = {"gdaemon": {"1.0.0": {"burned_at": NOW.isoformat(), "generation": None}}}
    _update_state(bin_dir, next_seq=7, pins=pins, burned=burned)

    record_promotion(bin_dir, ["gcode", "gdaemon"], source_commit="abc123", now=NOW)

    state = read_state(bin_dir)
    assert state["next_seq"] == 7
    assert state["pins"] == pins
    assert state["burned"] == burned
    assert set(state["installed"]) == {"gcode", "gdaemon"}
    entry = state["installed"]["gcode"]
    assert datetime.fromisoformat(entry["promoted_at"]) == NOW
    assert entry["source_commit"] == "abc123"
    assert entry["session_id"] is None
    assert entry["task_id"] is None


def test_promotion_hold_identity_reaches_the_next_manifest(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _update_state(bin_dir, hold=_hold(first_seq=99))
    record_promotion(bin_dir, ["gdaemon"], source_commit="abc123", now=NOW)
    installed = read_state(bin_dir)["installed"]["gdaemon"]
    assert installed["session_id"] == "session-1"
    assert installed["task_id"] == "task-1"

    _set_binary(bin_dir, "gdaemon", b"gdaemon-1")
    generation = _capture(bin_dir, ["gdaemon"], minute=1)

    member = generation.manifest["members"]["gdaemon"]
    assert datetime.fromisoformat(member["promoted_at"]) == NOW
    assert member["source_commit"] == "abc123"
    assert member["session_id"] == "session-1"
    assert member["task_id"] == "task-1"


# Failure modes: unreadable state fails closed, broken generations are skipped.


@pytest.mark.parametrize("payload", ["{not json", "[]"])
def test_unreadable_state_fails_closed(tmp_path: Path, payload: str) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _state_path(bin_dir).parent.mkdir(parents=True)
    _state_path(bin_dir).write_text(payload)

    with pytest.raises(GenerationStoreError):
        retain_outgoing(bin_dir, MANAGED_BINARIES, now=NOW)
    with pytest.raises(GenerationStoreError):
        record_promotion(bin_dir, ["gdaemon"], now=NOW)
    with pytest.raises(GenerationStoreError):
        read_state(bin_dir)


def test_list_generations_skips_directories_without_a_readable_format_1_manifest(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    kept = _capture(bin_dir, ["gdaemon"])
    store = bin_dir / GENERATIONS_DIR
    (store / "000002-broken").mkdir()
    (store / "000002-broken" / "manifest.json").write_text("{not json")
    (store / "000003-bare").mkdir()
    _write(
        store / "000004-format-2" / "manifest.json",
        _json_bytes({"format": 2, "id": "000004-format-2", "seq": 4, "members": {}}),
        0o644,
    )

    assert [g.id for g in list_generations(bin_dir)] == [kept.id]


def test_state_file_is_private_to_the_owner(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _capture(bin_dir, ["gdaemon"])

    assert stat.S_IMODE(_state_path(bin_dir).stat().st_mode) == 0o600


# Retention refusal: a snapshot is either possible or it names the reason.


def test_retention_refusal_is_none_for_a_writable_store_and_leaves_no_probe(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    assert retention_refusal(bin_dir) is None

    store = bin_dir / GENERATIONS_DIR
    assert not store.exists() or list(store.iterdir()) == []


def test_retention_refusal_when_generations_is_a_regular_file(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")
    _write(bin_dir / GENERATIONS_DIR, b"not a directory", 0o644)

    reason = retention_refusal(bin_dir)

    assert reason is not None
    assert reason.startswith(REFUSAL_PREFIX)


def test_retention_refusal_when_free_space_is_below_binaries_plus_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    _seed(bin_dir, "gdaemon", b"gdaemon-0")

    def fake_disk_usage(_path: object) -> _Usage:
        return _Usage(total=1 << 40, used=1 << 39, free=16 * MIB)

    monkeypatch.setattr(bin_generations.shutil, "disk_usage", fake_disk_usage)

    reason = retention_refusal(bin_dir)

    assert reason is not None
    assert reason.startswith(REFUSAL_PREFIX)


def test_retention_refusal_is_none_and_creates_nothing_without_bin_dir(tmp_path: Path) -> None:
    bin_dir = tmp_path / "missing"

    assert retention_refusal(bin_dir) is None

    assert not bin_dir.exists()


# Integration through promote_workspace_binary_set.


def test_first_promotion_into_an_empty_bin_dir_creates_no_generation(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"

    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.0"))

    assert list_generations(bin_dir) == []


def test_second_promotion_retains_the_outgoing_set_as_one_generation(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.0"))
    outgoing = {name: (bin_dir / name).read_bytes() for name in SET_MEMBERS}
    _bump_set_members(workspace)

    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.1"))

    generations = list_generations(bin_dir)
    assert [g.seq for g in generations] == [1]
    generation = generations[0]
    assert sorted(generation.manifest["members"]) == sorted(SET_MEMBERS)
    for name in SET_MEMBERS:
        member = generation.manifest["members"][name]
        assert member["version"] == "1.0.0"
        assert member["sha256"] == _sha(outgoing[name])
        assert (generation.path / name).read_bytes() == outgoing[name]


def test_installed_gclient_is_retained_in_the_same_generation(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.0"))
    gclient_bytes = _binary(bin_dir / "gclient", "1.0.0").read_bytes()
    _bump_set_members(workspace)

    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.1"))

    [generation] = list_generations(bin_dir)
    assert sorted(generation.manifest["members"]) == sorted([*SET_MEMBERS, "gclient"])
    member = generation.manifest["members"]["gclient"]
    assert member["sha256"] == _sha(gclient_bytes)
    assert member["alias"] is False
    assert (generation.path / "gclient").read_bytes() == gclient_bytes


def test_refused_promotion_retains_no_new_generation(workspace: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.0"))
    _bump_set_members(workspace)
    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.1"))
    before = list_generations(bin_dir)
    (workspace / "crates/gcore/src/lib.rs").write_text("pub fn changed() {}\n")

    with pytest.raises(release.BinaryReleaseVersionError):
        _promote(workspace, bin_dir, _candidates(tmp_path / "rebuilt", "1.0.1"))

    assert list_generations(bin_dir) == before


def test_promotion_records_installed_state_for_each_member(workspace: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.0"))
    _bump_set_members(workspace)

    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.1"))

    installed = read_state(bin_dir)["installed"]
    for name in SET_MEMBERS:
        assert installed[name]["promoted_at"] is not None
        assert installed[name]["source_commit"] is None


# Integration through stage_and_promote_binary_file.


def test_stage_and_promote_managed_name_retains_one_generation_of_that_member(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    outgoing = _binary(bin_dir / "gmcp", "1.0.0").read_bytes()
    source = _binary(tmp_path / "source" / "gmcp", "2.0.0")

    promotion.stage_and_promote_binary_file(
        source,
        destination=bin_dir / "gmcp",
        identity=release.BinaryReleaseIdentity("2.0.0", "artifact:x", "x"),
    )

    [generation] = list_generations(bin_dir)
    assert sorted(generation.manifest["members"]) == ["gmcp"]
    assert generation.manifest["members"]["gmcp"]["sha256"] == _sha(outgoing)
    assert (bin_dir / "gmcp").read_bytes() == source.read_bytes()
    assert read_state(bin_dir)["installed"]["gmcp"]["promoted_at"] is not None


def test_stage_and_promote_unmanaged_name_retains_no_generation(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _binary(bin_dir / "othertool", "1.0.0")
    source = _binary(tmp_path / "source" / "othertool", "2.0.0")

    promotion.stage_and_promote_binary_file(source, destination=bin_dir / "othertool")

    assert list_generations(bin_dir) == []
    assert (bin_dir / "othertool").read_bytes() == source.read_bytes()
