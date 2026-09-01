from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.install import bin_freshness_promotion
from gobby.install.bin_set_coherence import (
    BinarySetCoherenceError,
    promote_workspace_binary_set,
)

PIN_NAME = ".gdaemon-schema-identity.json"
SET_MEMBERS = ("gcode", "gdaemon", "ghook", "gwiki")


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
    payload = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{payload}'\n", encoding="utf-8")
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
    assert "rebuild and install all four together" in message
    assert installed.read_text(encoding="utf-8") == "old-gcode"


def test_all_four_coherent_members_promote_and_rewrite_pin(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    _write_pin(bin_dir, _identity(1))
    candidates = _candidate_set(source_dir, _identity(2))
    for name in SET_MEMBERS:
        (bin_dir / name).write_text(f"old-{name}", encoding="utf-8")

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
    installed.write_text("old-gcode", encoding="utf-8")

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


def test_all_four_disagreeing_members_are_refused_without_promoting(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    bin_dir.mkdir()
    source_dir.mkdir()
    candidates = _candidate_set(source_dir, _identity(2))
    _write_stub(candidates["gwiki"], _identity(3))
    for name in SET_MEMBERS:
        (bin_dir / name).write_text(f"old-{name}", encoding="utf-8")

    with pytest.raises(BinarySetCoherenceError) as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    message = str(exc_info.value)
    assert "gcode" in message
    assert "gwiki" in message
    assert "rebuild and install all four together" in message
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
        (bin_dir / name).write_text(f"old-{name}", encoding="utf-8")
    real_promote = bin_freshness_promotion.stage_and_promote_binary_file

    def fail_on_ghook(source: Path, *, destination: Path) -> None:
        if destination.name == "ghook":
            raise OSError("synthetic promote failure")
        real_promote(source, destination=destination)

    monkeypatch.setattr(bin_freshness_promotion, "stage_and_promote_binary_file", fail_on_ghook)

    with pytest.raises(BinarySetCoherenceError) as exc_info:
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    message = str(exc_info.value)
    assert "promoted: gcode, gdaemon" in message
    assert "unpromoted: ghook, gwiki" in message
    assert "restored prior install" not in message
    assert (bin_dir / "gcode").read_bytes() == candidates["gcode"].read_bytes()
    assert (bin_dir / "gdaemon").read_bytes() == candidates["gdaemon"].read_bytes()
    assert (bin_dir / "ghook").read_text(encoding="utf-8") == "old-ghook"
    assert (bin_dir / "gwiki").read_text(encoding="utf-8") == "old-gwiki"
    assert json.loads((bin_dir / PIN_NAME).read_text(encoding="utf-8")) == installed_identity
