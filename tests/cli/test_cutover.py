"""Contracts for the transactional native-binary cutover command."""

from __future__ import annotations

import json
import os
import subprocess
from importlib import import_module
from pathlib import Path

import pytest
from click.testing import CliRunner

from gobby.cli import cli

cutover_module = import_module("gobby.cli.cutover")


IDENTITY: dict[str, int | str] = {
    "assets_root_hash": "assets",
    "baseline_checksum": "baseline",
    "baseline_version": 1,
    "latest_checksum": "latest",
    "latest_version": 2,
    "runner_protocol": 1,
}


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "crates").mkdir(parents=True)
    (root / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    pin = root / "src/gobby/storage/schema_expected_identity.json"
    pin.parent.mkdir(parents=True)
    pin.write_text('{"old": true}\n', encoding="utf-8")
    return root


def _installed_bins(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "home/bin"
    bin_dir.mkdir(parents=True)
    for name in cutover_module._BINARY_NAMES:
        path = bin_dir / name
        path.write_text(f"old-{name}", encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def _artifacts(root: Path) -> dict[str, Path]:
    target = root / "target/release"
    target.mkdir(parents=True)
    artifacts: dict[str, Path] = {}
    for name in cutover_module._BINARY_NAMES:
        path = target / name
        path.write_text(f"new-{name}", encoding="utf-8")
        path.chmod(0o755)
        artifacts[name] = path
    return artifacts


def test_run_cutover_promotes_complete_set_with_new_inodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    old_inodes = {name: (bin_dir / name).stat().st_ino for name in cutover_module._BINARY_NAMES}
    events: list[str] = []
    identity_probes: list[Path] = []
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)

    def read_identity(binary: Path, **_kwargs: object) -> dict[str, int | str]:
        identity_probes.append(binary)
        return IDENTITY

    monkeypatch.setattr(cutover_module, "_read_schema_identity", read_identity)
    monkeypatch.setattr(
        cutover_module,
        "_smoke_installed_gcode",
        lambda *_args, **_kw: events.append("smoke"),
    )

    cutover_module.run_cutover(
        root,
        bin_dir,
        restart_daemon=lambda: events.append("restart"),
    )

    assert events == ["restart", "smoke"]
    assert identity_probes == [artifacts["gdaemon"], bin_dir / "gdaemon"]
    for name in cutover_module._BINARY_NAMES:
        installed = bin_dir / name
        assert installed.read_text(encoding="utf-8") == f"new-{name}"
        assert installed.stat().st_ino != old_inodes[name]
    assert json.loads((root / cutover_module._PIN_PATH).read_text()) == IDENTITY


def test_build_failure_has_no_install_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    pin_before = (root / cutover_module._PIN_PATH).read_bytes()

    def fail_build(_root: Path) -> dict[str, Path]:
        raise cutover_module.CutoverError("build failed")

    monkeypatch.setattr(cutover_module, "_build_artifacts", fail_build)
    with pytest.raises(cutover_module.CutoverError, match="build failed"):
        cutover_module.run_cutover(root, bin_dir, restart_daemon=lambda: None)

    assert {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES} == before
    assert (root / cutover_module._PIN_PATH).read_bytes() == pin_before


@pytest.mark.parametrize("failure", ["restart", "smoke"])
def test_post_promotion_failure_restores_prior_set_and_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    pin_before = (root / cutover_module._PIN_PATH).read_bytes()
    restarts = 0

    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_read_schema_identity", lambda *_args, **_kw: IDENTITY)

    def restart_daemon() -> None:
        nonlocal restarts
        restarts += 1
        if failure == "restart" and restarts == 1:
            raise RuntimeError("restart failed")

    def smoke(*_args: object, **_kwargs: object) -> None:
        if failure == "smoke":
            raise cutover_module.CutoverError("smoke failed")

    monkeypatch.setattr(cutover_module, "_smoke_installed_gcode", smoke)
    with pytest.raises(cutover_module.CutoverError, match="restored prior install"):
        cutover_module.run_cutover(root, bin_dir, restart_daemon=restart_daemon)

    assert restarts == 2
    assert {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES} == before
    assert (root / cutover_module._PIN_PATH).read_bytes() == pin_before


def test_promotion_failure_rolls_back_every_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    replacements = cutover_module._ReplacementSet.stage(
        [(artifacts[name], bin_dir / name, 0o755) for name in cutover_module._BINARY_NAMES]
    )
    replacements.append(
        cutover_module._stage_bytes(
            cutover_module._pin_content(IDENTITY),
            root / cutover_module._PIN_PATH,
            mode=0o644,
        )
    )
    real_replace = os.replace
    failed = False

    def replace(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination).name == "ghook" and Path(source).name == "staged" and not failed:
            failed = True
            raise OSError("promotion failed")
        real_replace(source, destination)

    monkeypatch.setattr(cutover_module.os, "replace", replace)
    with pytest.raises(OSError, match="promotion failed"):
        replacements.promote()
    replacements.rollback()

    assert {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES} == before
    assert (root / cutover_module._PIN_PATH).read_text(encoding="utf-8") == '{"old": true}\n'


def test_pin_promotion_failure_rolls_back_binaries_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    pin = root / cutover_module._PIN_PATH
    pin_before = pin.read_bytes()
    restarts = 0
    real_replace = os.replace
    failed = False

    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_read_schema_identity", lambda *_args, **_kw: IDENTITY)

    def replace(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination) == pin and Path(source).name == "staged" and not failed:
            failed = True
            raise OSError("pin promotion failed")
        real_replace(source, destination)

    def restart_daemon() -> None:
        nonlocal restarts
        restarts += 1

    monkeypatch.setattr(cutover_module.os, "replace", replace)
    with pytest.raises(cutover_module.CutoverError, match="restored prior install"):
        cutover_module.run_cutover(root, bin_dir, restart_daemon=restart_daemon)

    assert restarts == 1
    assert {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES} == before
    assert pin.read_bytes() == pin_before


def test_build_uses_one_release_command_for_all_four_packages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    artifacts = _artifacts(root)
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cutover_module, "_run", run)
    assert cutover_module._build_artifacts(root) == artifacts
    assert calls == [
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "-p",
            "gobby-code",
            "-p",
            "gobby-daemon",
            "-p",
            "gobby-hooks",
            "-p",
            "gobby-wiki",
        ]
    ]


def test_cutover_is_registered() -> None:
    result = CliRunner().invoke(cli, ["cutover", "--help"])

    assert result.exit_code == 0
    assert "Build and atomically activate all schema-aware native binaries" in result.output
