from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from gobby.cli import install_setup
from gobby.cli.install_setup_gdaemon import GdaemonInstallError, ensure_gdaemon
from gobby.install import bin_freshness_promotion, bin_set_coherence
from gobby.install.bin_release_identity import BinaryReleaseIdentity
from gobby.install.bin_set_coherence import (
    BinarySetCoherenceError,
    WorkspacePromotionMetadata,
    promote_workspace_binary_set,
)
from gobby.storage.schema_divergence import binary_set_apply_refusal
from gobby.utils import spawn

PIN_NAME = ".gdaemon-schema-identity.json"
SET_MEMBERS = ("gcode", "gdaemon", "ghook")


def _identity(version: int) -> dict[str, int | str]:
    return {
        "runner_protocol": 1,
        "baseline_version": version,
        "baseline_checksum": f"baseline-{version}",
        "latest_version": version,
        "latest_checksum": f"latest-{version}",
        "assets_root_hash": f"root-{version}",
    }


def _write_stub(path: Path, identity: dict[str, int | str]) -> None:
    version = f"{identity['latest_version']}.0.0"
    if path.name == "ghook":
        _write_ghook_stub(path, identity, version=version, stamp=_runtime_stamp(version))
        return
    payload = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    path.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
        f"printf '%s\\n' '{path.name} {version}'\nelse\n"
        f"printf '%s\\n' '{payload}'\nfi\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _runtime_stamp(version: str) -> str:
    """The document `ghook --runtime-stamp` prints, without its trailing newline."""
    stamp = {
        "ghook_version": version,
        "response_capability": "hook-response.v1",
        "schema_version": 1,
    }
    return json.dumps(stamp, separators=(",", ":"), sort_keys=True)


def _write_ghook_stub(
    path: Path, identity: dict[str, int | str], *, version: str, stamp: str | None
) -> None:
    """A ghook whose --runtime-stamp prints stamp; None leaves the flag unknown, so it exits 2."""
    payload = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    runtime = "exit 2" if stamp is None else f"printf '%s\\n' '{stamp}'"
    path.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
        f"printf '%s\\n' 'ghook {version}'\n"
        'elif [ "$1" = "--runtime-stamp" ]; then\n'
        f"{runtime}\n"
        f"else\nprintf '%s\\n' '{payload}'\nfi\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_pin(bin_dir: Path, identity: dict[str, int | str]) -> None:
    (bin_dir / PIN_NAME).write_text(json.dumps(identity) + "\n", encoding="utf-8")


def _candidate_set(source_dir: Path, identity: dict[str, int | str]) -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    for name in SET_MEMBERS:
        source = source_dir / name
        _write_stub(source, identity)
        candidates[name] = source
    return candidates


def test_partial_mixed_identity_is_refused_without_promoting(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    installed_identity = _identity(1)
    candidate_identity = _identity(2)
    _write_pin(bin_dir, installed_identity)
    installed = bin_dir / "gcode"
    installed.write_text("old-gcode", encoding="utf-8")
    candidate = source_dir / "gcode"
    _write_stub(candidate, candidate_identity)

    with pytest.raises(BinarySetCoherenceError) as exc_info:
        promote_workspace_binary_set({"gcode": candidate}, bin_dir=bin_dir)

    message = str(exc_info.value)
    assert "gcode" in message
    assert json.dumps(candidate_identity, separators=(",", ":"), sort_keys=True) in message
    assert json.dumps(installed_identity, separators=(",", ":"), sort_keys=True) in message
    assert "rebuild and install all three together" in message
    assert installed.read_text(encoding="utf-8") == "old-gcode"


def test_complete_set_coherent_members_promote_and_rewrite_pin(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    _write_pin(bin_dir, _identity(1))
    candidates = _candidate_set(source_dir, _identity(2))
    for name in SET_MEMBERS:
        _write_stub(bin_dir / name, _identity(1))

    promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    for name, source in candidates.items():
        assert (bin_dir / name).read_bytes() == source.read_bytes()
    assert json.loads((bin_dir / PIN_NAME).read_text(encoding="utf-8")) == _identity(2)


def test_partial_agreeing_member_promotes(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    identity = _identity(1)
    _write_pin(bin_dir, identity)
    candidate = source_dir / "gcode"
    _write_stub(candidate, identity)
    installed = bin_dir / "gcode"
    _write_stub(installed, identity)

    promote_workspace_binary_set({"gcode": candidate}, bin_dir=bin_dir)

    assert installed.read_bytes() == candidate.read_bytes()
    assert json.loads((bin_dir / PIN_NAME).read_text(encoding="utf-8")) == identity


def test_partial_promotion_falls_back_to_installed_gdaemon_identity(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    identity = _identity(1)
    _write_stub(bin_dir / "gdaemon", identity)
    candidate = source_dir / "gcode"
    _write_stub(candidate, identity)

    promote_workspace_binary_set({"gcode": candidate}, bin_dir=bin_dir)

    assert (bin_dir / "gcode").read_bytes() == candidate.read_bytes()
    assert not (bin_dir / PIN_NAME).exists()


def test_first_workspace_member_can_bootstrap_an_empty_bin_dir(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    candidate = source_dir / "gdaemon"
    _write_stub(candidate, _identity(1))

    promote_workspace_binary_set({"gdaemon": candidate}, bin_dir=bin_dir)

    assert (bin_dir / "gdaemon").read_bytes() == candidate.read_bytes()


def test_complete_set_disagreeing_members_are_refused_without_promoting(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    candidates = _candidate_set(source_dir, _identity(2))
    _write_stub(candidates["ghook"], _identity(3))
    for name in SET_MEMBERS:
        (bin_dir / name).write_text(f"old-{name}", encoding="utf-8")

    with pytest.raises(BinarySetCoherenceError) as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    message = str(exc_info.value)
    assert "gcode" in message
    assert "ghook" in message
    assert "rebuild and install all three together" in message
    for name in SET_MEMBERS:
        assert (bin_dir / name).read_text(encoding="utf-8") == f"old-{name}"


def test_mid_set_failure_reports_promoted_and_unpromoted_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    installed_identity = _identity(1)
    _write_pin(bin_dir, installed_identity)
    candidates = _candidate_set(source_dir, _identity(2))
    for name in SET_MEMBERS:
        _write_stub(bin_dir / name, installed_identity)
    old_ghook = (bin_dir / "ghook").read_bytes()
    real_swap = bin_freshness_promotion.swap_staged_binary

    def fail_on_ghook(
        staged_binary: Path,
        *,
        bin_dir: Path,
        binary_name: str,
        identity: BinaryReleaseIdentity | None,
        after_swap: Callable[[], None] | None = None,
    ) -> None:
        if binary_name == "ghook":
            raise OSError("synthetic promote failure")
        real_swap(
            staged_binary,
            bin_dir=bin_dir,
            binary_name=binary_name,
            identity=identity,
            after_swap=after_swap,
        )

    monkeypatch.setattr(bin_freshness_promotion, "swap_staged_binary", fail_on_ghook)

    with pytest.raises(BinarySetCoherenceError) as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    message = str(exc_info.value)
    assert "promoted: gcode, gdaemon" in message
    assert "unpromoted: ghook" in message
    assert "restored prior install" not in message
    assert (bin_dir / "gcode").read_bytes() == candidates["gcode"].read_bytes()
    assert (bin_dir / "gdaemon").read_bytes() == candidates["gdaemon"].read_bytes()
    assert (bin_dir / "ghook").read_bytes() == old_ghook
    assert json.loads((bin_dir / PIN_NAME).read_text(encoding="utf-8")) == installed_identity


def _outgoing_stamped_set(bin_dir: Path, installed_identity: dict[str, int | str]) -> None:
    """Installed 1.0.0 members whose version stamps describe them, over the outgoing pin."""
    _write_pin(bin_dir, installed_identity)
    for name in SET_MEMBERS:
        _write_stub(bin_dir / name, installed_identity)
        (bin_dir / f".{name}-version").write_text("1.0.0\n", encoding="utf-8")


def _fail_swap_of_ghook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let gcode and gdaemon swap in, then fail the third member's swap."""
    real_swap = bin_freshness_promotion.swap_staged_binary

    def fail_on_ghook(
        staged_binary: Path,
        *,
        bin_dir: Path,
        binary_name: str,
        identity: BinaryReleaseIdentity | None,
        after_swap: Callable[[], None] | None = None,
    ) -> None:
        if binary_name == "ghook":
            raise OSError("synthetic promote failure")
        real_swap(
            staged_binary,
            bin_dir=bin_dir,
            binary_name=binary_name,
            identity=identity,
            after_swap=after_swap,
        )

    monkeypatch.setattr(bin_freshness_promotion, "swap_staged_binary", fail_on_ghook)


_TARGET_METADATA = WorkspacePromotionMetadata(
    install_method="workspace-cutover", target="test-target"
)


def test_complete_set_failing_part_way_settles_the_stamps_of_members_swapped_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    installed_identity = _identity(1)
    _outgoing_stamped_set(bin_dir, installed_identity)
    candidates = _candidate_set(source_dir, _identity(2))
    _fail_swap_of_ghook(monkeypatch)

    with pytest.raises(BinarySetCoherenceError) as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir, metadata=_TARGET_METADATA)

    message = str(exc_info.value)
    assert "promoted: gcode, gdaemon" in message
    assert "unpromoted: ghook" in message
    # gcode and gdaemon hold the new 2.0.0 bytes, so their stamps and sidecars must say so.
    for name in ("gcode", "gdaemon"):
        assert (bin_dir / f".{name}-version").read_text(encoding="utf-8") == "2.0.0\n"
        sidecar = json.loads((bin_dir / f".{name}-install.json").read_text(encoding="utf-8"))
        assert sidecar["installed_version"] == "2.0.0"
        assert sidecar["target"] == "test-target"
    # ghook never swapped, so its stamp still describes the bytes it has.
    assert (bin_dir / ".ghook-version").read_text(encoding="utf-8") == "1.0.0\n"
    assert json.loads((bin_dir / PIN_NAME).read_text(encoding="utf-8")) == installed_identity


def test_complete_set_failing_part_way_names_members_whose_stamps_stay_unsettled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    _outgoing_stamped_set(bin_dir, _identity(1))
    candidates = _candidate_set(source_dir, _identity(2))
    _fail_swap_of_ghook(monkeypatch)
    real_write = bin_set_coherence.write_atomic_bytes

    def fail_gdaemon_stamp(path: Path, content: bytes, *, mode: int) -> None:
        if path.name == ".gdaemon-version":
            raise OSError("synthetic stamp write failure")
        real_write(path, content, mode=mode)

    monkeypatch.setattr(bin_set_coherence, "write_atomic_bytes", fail_gdaemon_stamp)

    with pytest.raises(BinarySetCoherenceError) as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir, metadata=_TARGET_METADATA)

    message = str(exc_info.value)
    assert "promoted: gcode, gdaemon" in message
    assert "unpromoted: ghook" in message
    assert "stamps unsettled for: gdaemon" in message
    assert "synthetic promote failure" in message
    assert "synthetic stamp write failure" in message
    assert exc_info.value.promoted == ("gcode", "gdaemon")


def test_set_codesign_timeout_is_refused_before_any_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    installed_identity = _identity(1)
    _write_pin(bin_dir, installed_identity)
    candidates = _candidate_set(source_dir, _identity(2))
    for name in SET_MEMBERS:
        _write_stub(bin_dir / name, installed_identity)
    installed = {name: (bin_dir / name).read_bytes() for name in SET_MEMBERS}
    real_run = spawn.run

    def run_with_codesign_timeout(command: list[str], **kwargs: Any) -> Any:
        if command[0] == "codesign":
            raise subprocess.TimeoutExpired("codesign", 30)
        return real_run(command, **kwargs)

    monkeypatch.setattr(bin_set_coherence, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        bin_set_coherence, "shutil", SimpleNamespace(which=lambda _name: "codesign")
    )
    monkeypatch.setattr(spawn, "run", run_with_codesign_timeout)

    with pytest.raises(BinarySetCoherenceError, match="ad-hoc signing timed out after 30 seconds"):
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    assert {name: (bin_dir / name).read_bytes() for name in SET_MEMBERS} == installed
    assert json.loads((bin_dir / PIN_NAME).read_text(encoding="utf-8")) == installed_identity


def test_set_member_installs_without_workspace_build_keep_stamped_set_coherent(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / ".gobby" / "bin"
    bin_dir.mkdir(parents=True)
    identity = _identity(420)
    for member in SET_MEMBERS:
        _write_stub(bin_dir / member, identity)
    _write_pin(bin_dir, identity)
    before = {path.name: path.read_bytes() for path in bin_dir.iterdir()}

    with (
        patch("gobby.cli.install_setup.sys.platform", "darwin"),
        patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
        patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        patch("gobby.cli.install_setup.shutil.which", return_value=None),
        patch(
            "gobby.cli.install_release.urlopen",
            side_effect=AssertionError("set members never download release artifacts"),
        ),
    ):
        gcode = install_setup._install_gcode(force=True)
        ghook = install_setup._install_ghook(force=True)
        with pytest.raises(GdaemonInstallError, match="gdaemon installs only from"):
            ensure_gdaemon(bin_dir=bin_dir, force=True)

    for member, result in (("gcode", gcode), ("ghook", ghook)):
        assert result["installed"] is False
        assert f"{member} installs only from the Gobby workspace build" in result["reason"]
    assert {path.name: path.read_bytes() for path in bin_dir.iterdir()} == before
    assert binary_set_apply_refusal(bin_dir) is None


RUNTIME_STAMP = ".ghook-runtime.json"
PRIOR_STAMP = b'{"prior": true}\n'


def _install_identity_one(bin_dir: Path) -> None:
    """A complete installed set at identity 1, so a candidate at identity 2 is a real swap."""
    _write_pin(bin_dir, _identity(1))
    for member in SET_MEMBERS:
        _write_stub(bin_dir / member, _identity(1))


def test_ghook_stamp_is_held_until_the_swap_then_holds_the_candidate_stamp_bytes(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    (bin_dir / RUNTIME_STAMP).write_bytes(PRIOR_STAMP)
    candidates = _candidate_set(source_dir, _identity(2))
    seen_before_swap: list[bytes] = []

    def record_live_stamp(_staged: Path) -> None:
        seen_before_swap.append((bin_dir / RUNTIME_STAMP).read_bytes())

    promote_workspace_binary_set(
        candidates, bin_dir=bin_dir, prepare_staged={"ghook": record_live_stamp}
    )

    assert seen_before_swap == [PRIOR_STAMP]
    assert (bin_dir / RUNTIME_STAMP).read_bytes() == (_runtime_stamp("2.0.0") + "\n").encode()


def test_ghook_candidate_without_runtime_stamp_is_refused_before_any_swap(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    _install_identity_one(bin_dir)
    (bin_dir / RUNTIME_STAMP).write_bytes(PRIOR_STAMP)
    candidates = _candidate_set(source_dir, _identity(2))
    _write_ghook_stub(candidates["ghook"], _identity(2), version="2.0.0", stamp=None)
    installed = {name: (bin_dir / name).read_bytes() for name in SET_MEMBERS}

    with pytest.raises(BinarySetCoherenceError, match="--runtime-stamp") as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    assert exc_info.value.promoted == ()
    assert {name: (bin_dir / name).read_bytes() for name in SET_MEMBERS} == installed
    assert (bin_dir / RUNTIME_STAMP).read_bytes() == PRIOR_STAMP


def test_ghook_runtime_stamp_naming_another_version_is_refused_before_any_swap(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    _install_identity_one(bin_dir)
    (bin_dir / RUNTIME_STAMP).write_bytes(PRIOR_STAMP)
    candidates = _candidate_set(source_dir, _identity(2))
    _write_ghook_stub(
        candidates["ghook"], _identity(2), version="2.0.0", stamp=_runtime_stamp("9.9.9")
    )
    installed = {name: (bin_dir / name).read_bytes() for name in SET_MEMBERS}

    with pytest.raises(BinarySetCoherenceError, match="9.9.9") as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    assert exc_info.value.promoted == ()
    assert {name: (bin_dir / name).read_bytes() for name in SET_MEMBERS} == installed
    assert (bin_dir / RUNTIME_STAMP).read_bytes() == PRIOR_STAMP


def test_failed_ghook_stamp_write_after_the_swap_is_stamps_not_settled(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    _install_identity_one(bin_dir)
    (bin_dir / RUNTIME_STAMP).mkdir()  # a directory where the stamp file must go
    candidates = _candidate_set(source_dir, _identity(2))

    with pytest.raises(
        BinarySetCoherenceError, match="was installed but its stamps were not settled"
    ) as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    assert exc_info.value.promoted == SET_MEMBERS
    assert (bin_dir / "ghook").read_bytes() == candidates["ghook"].read_bytes()
