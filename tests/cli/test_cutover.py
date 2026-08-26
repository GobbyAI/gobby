"""Contracts for the transactional native-binary cutover command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.storage.schema_identity_pin import SchemaIdentityError, pin_bytes, stamp_bytes

cutover_module = import_module("gobby.cli.cutover")


IDENTITY: dict[str, int | str] = {
    "assets_root_hash": "assets",
    "baseline_checksum": "baseline",
    "baseline_version": 1,
    "latest_checksum": "latest",
    "latest_version": 2,
    "runner_protocol": 1,
}
VERSION = "9.9.9"
OLD_STAMP = "0.0.1\n"
OLD_SIDECAR = '{"install_method":"github-release"}\n'


@pytest.fixture(autouse=True)
def _stub_binary_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test binaries are text files: stand in for ad-hoc signing and ``--version``."""
    monkeypatch.setattr(cutover_module, "_codesign", lambda _binary: None)
    monkeypatch.setattr(cutover_module, "_probe_version", lambda _binary: VERSION)


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
    # gcode carries prior sidecars (restore path); the others have none (removal path).
    (bin_dir / ".gcode-version").write_text(OLD_STAMP, encoding="utf-8")
    (bin_dir / ".gcode-install.json").write_text(OLD_SIDECAR, encoding="utf-8")
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


def _sidecar_paths(bin_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for name in cutover_module._BINARY_NAMES:
        paths.append(bin_dir / f".{name}-version")
        paths.append(bin_dir / f".{name}-install.json")
    paths.append(bin_dir / ".gdaemon-schema-identity.json")
    return paths


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _staging_dirs(bin_dir: Path) -> list[Path]:
    return [path for path in bin_dir.iterdir() if "-cutover-" in path.name]


def _stage_all(root: Path, bin_dir: Path, artifacts: dict[str, Path]) -> Any:
    replacements = cutover_module._ReplacementSet.stage(
        [(artifacts[name], bin_dir / name, 0o755) for name in cutover_module._BINARY_NAMES]
    )
    replacements.append(
        cutover_module._stage_bytes(
            pin_bytes(IDENTITY),
            root / cutover_module._PIN_PATH,
            mode=0o644,
        )
    )
    return replacements


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
    assert _staging_dirs(bin_dir) == []


def test_run_cutover_signs_staged_binaries_before_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    signed: list[Path] = []
    staged_at_signing: list[str] = []
    live_at_signing: list[str] = []
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_read_schema_identity", lambda *_args, **_kw: IDENTITY)
    monkeypatch.setattr(cutover_module, "_smoke_installed_gcode", lambda *_args, **_kw: None)

    def codesign(binary: Path) -> None:
        signed.append(binary)
        staged_at_signing.append(binary.read_text(encoding="utf-8"))
        live_at_signing.append((bin_dir / binary.name).read_text(encoding="utf-8"))

    monkeypatch.setattr(cutover_module, "_codesign", codesign)

    cutover_module.run_cutover(root, bin_dir, restart_daemon=lambda: None)

    assert [path.name for path in signed] == list(cutover_module._BINARY_NAMES)
    assert staged_at_signing == [f"new-{name}" for name in cutover_module._BINARY_NAMES]
    assert live_at_signing == [f"old-{name}" for name in cutover_module._BINARY_NAMES]
    for path in signed:
        assert path.parent.parent == bin_dir
        assert path.parent.name.startswith(f".{path.name}-cutover-")
    for name in cutover_module._BINARY_NAMES:
        assert (bin_dir / name).read_text(encoding="utf-8") == f"new-{name}"
    assert _staging_dirs(bin_dir) == []


def test_run_cutover_writes_installer_sidecars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_read_schema_identity", lambda *_args, **_kw: IDENTITY)
    monkeypatch.setattr(cutover_module, "_smoke_installed_gcode", lambda *_args, **_kw: None)
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")

    cutover_module.run_cutover(root, bin_dir, restart_daemon=lambda: None)

    for name in cutover_module._BINARY_NAMES:
        assert (bin_dir / f".{name}-version").read_text(encoding="utf-8") == f"{VERSION}\n"
        sidecar = json.loads((bin_dir / f".{name}-install.json").read_text(encoding="utf-8"))
        assert sidecar["install_method"] == "workspace-cutover"
        assert sidecar["installed_version"] == VERSION
        assert sidecar["target"] == "test-target"
        assert sidecar["install_source_url"] is None
        assert isinstance(sidecar["installed_at"], str)
    assert (bin_dir / ".gdaemon-schema-identity.json").read_bytes() == stamp_bytes(IDENTITY)


def test_run_cutover_rolls_back_when_a_binary_reports_no_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    before = _snapshot(_sidecar_paths(bin_dir))
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_read_schema_identity", lambda *_args, **_kw: IDENTITY)
    monkeypatch.setattr(
        cutover_module,
        "_probe_version",
        lambda binary: None if binary.name == "gwiki" else VERSION,
    )

    with pytest.raises(cutover_module.CutoverError, match="gwiki did not report a version"):
        cutover_module.run_cutover(root, bin_dir, restart_daemon=lambda: None)

    assert (bin_dir / "gwiki").read_text(encoding="utf-8") == "old-gwiki"
    assert _snapshot(_sidecar_paths(bin_dir)) == before


def test_build_failure_has_no_install_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    sidecars_before = _snapshot(_sidecar_paths(bin_dir))
    pin_before = (root / cutover_module._PIN_PATH).read_bytes()

    def fail_build(_root: Path) -> dict[str, Path]:
        raise cutover_module.CutoverError("build failed")

    monkeypatch.setattr(cutover_module, "_build_artifacts", fail_build)
    with pytest.raises(cutover_module.CutoverError, match="build failed"):
        cutover_module.run_cutover(root, bin_dir, restart_daemon=lambda: None)

    assert {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES} == before
    assert _snapshot(_sidecar_paths(bin_dir)) == sidecars_before
    assert (root / cutover_module._PIN_PATH).read_bytes() == pin_before


@pytest.mark.parametrize("failure", ["restart", "smoke"])
def test_post_promotion_failure_restores_prior_set_and_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    sidecars_before = _snapshot(_sidecar_paths(bin_dir))
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
    assert _snapshot(_sidecar_paths(bin_dir)) == sidecars_before
    assert (root / cutover_module._PIN_PATH).read_bytes() == pin_before
    assert _staging_dirs(bin_dir) == []


def test_promote_keeps_every_destination_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    old_inodes = {name: (bin_dir / name).stat().st_ino for name in cutover_module._BINARY_NAMES}
    replacements = _stage_all(root, bin_dir, artifacts)
    real_replace = os.replace
    observed_present: list[bool] = []

    def replace(source: str | Path, destination: str | Path) -> None:
        observed_present.append(
            all((bin_dir / name).exists() for name in cutover_module._BINARY_NAMES)
        )
        real_replace(source, destination)

    monkeypatch.setattr(cutover_module.os, "replace", replace)
    replacements.promote()

    assert len(observed_present) == len(cutover_module._BINARY_NAMES) + 1
    assert all(observed_present)
    for replacement in replacements.replacements:
        if replacement.destination.name in cutover_module._BINARY_NAMES:
            assert replacement.backup.stat().st_ino == old_inodes[replacement.destination.name]
            assert replacement.destination.stat().st_ino != old_inodes[replacement.destination.name]


def test_promotion_failure_rolls_back_every_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    replacements = _stage_all(root, bin_dir, artifacts)
    real_replace = os.replace
    failed = False

    def replace(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination) == bin_dir / "ghook" and Path(source).name == "ghook" and not failed:
            failed = True
            raise OSError("promotion failed")
        real_replace(source, destination)

    monkeypatch.setattr(cutover_module.os, "replace", replace)
    with pytest.raises(OSError, match="promotion failed"):
        replacements.promote()
    replacements.rollback()

    assert {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES} == before
    assert (root / cutover_module._PIN_PATH).read_text(encoding="utf-8") == '{"old": true}\n'
    assert _staging_dirs(bin_dir) == []


def test_rollback_restore_failure_keeps_surviving_backups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    replacements = _stage_all(root, bin_dir, artifacts)
    replacements.promote()
    real_replace = os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        if Path(source).name == "gcode.backup":
            raise OSError("restore failed")
        real_replace(source, destination)

    monkeypatch.setattr(cutover_module.os, "replace", replace)
    with pytest.raises(cutover_module.CutoverError, match="backups kept at") as excinfo:
        replacements.rollback()

    gcode_backup = next(
        replacement.backup
        for replacement in replacements.replacements
        if replacement.destination == bin_dir / "gcode"
    )
    assert gcode_backup.exists()
    assert gcode_backup.read_text(encoding="utf-8") == "old-gcode"
    assert str(gcode_backup) in str(excinfo.value)
    assert (bin_dir / "ghook").read_text(encoding="utf-8") == "old-ghook"
    assert (root / cutover_module._PIN_PATH).read_text(encoding="utf-8") == '{"old": true}\n'


def test_pin_promotion_failure_rolls_back_binaries_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _workspace(tmp_path)
    bin_dir = _installed_bins(tmp_path)
    artifacts = _artifacts(root)
    before = {name: (bin_dir / name).read_bytes() for name in cutover_module._BINARY_NAMES}
    sidecars_before = _snapshot(_sidecar_paths(bin_dir))
    pin = root / cutover_module._PIN_PATH
    pin_before = pin.read_bytes()
    restarts = 0
    real_replace = os.replace
    failed = False

    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_read_schema_identity", lambda *_args, **_kw: IDENTITY)

    def replace(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination) == pin and Path(source).name == pin.name and not failed:
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
    assert _snapshot(_sidecar_paths(bin_dir)) == sidecars_before
    assert pin.read_bytes() == pin_before


def test_require_existing_install_names_missing_binaries(tmp_path: Path) -> None:
    bin_dir = _installed_bins(tmp_path)
    (bin_dir / "gwiki").unlink()

    with pytest.raises(cutover_module.CutoverError, match=r"missing: .*gwiki"):
        cutover_module._require_existing_install(bin_dir)


def test_require_existing_install_refuses_symlinked_binaries(tmp_path: Path) -> None:
    bin_dir = _installed_bins(tmp_path)
    linked_target = tmp_path / "target/debug/gcode"
    linked_target.parent.mkdir(parents=True)
    linked_target.write_text("dev-gcode", encoding="utf-8")
    (bin_dir / "gcode").unlink()
    (bin_dir / "gcode").symlink_to(linked_target)

    with pytest.raises(cutover_module.CutoverError, match=r"symlinked .*gcode") as excinfo:
        cutover_module._require_existing_install(bin_dir)

    assert "re-point the link" in str(excinfo.value)


def test_read_schema_identity_translates_probe_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def probe(_gdaemon: Path, *, cwd: Path | None = None) -> dict[str, int | str]:
        raise SchemaIdentityError("gdaemon schema version failed: boom")

    monkeypatch.setattr(cutover_module, "probe_identity", probe)

    with pytest.raises(cutover_module.CutoverError, match="gdaemon schema version failed: boom"):
        cutover_module._read_schema_identity(tmp_path / "gdaemon", cwd=tmp_path)


def test_sign_translates_install_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def codesign(binary: Path) -> None:
        raise cutover_module.GdaemonInstallError(f"{binary.name} ad-hoc signing failed: nope")

    monkeypatch.setattr(cutover_module, "_codesign", codesign)

    with pytest.raises(cutover_module.CutoverError, match="gcode ad-hoc signing failed: nope"):
        cutover_module._sign(tmp_path / "gcode")


def _failing_run(
    behaviour: str,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if behaviour == "timeout":
            raise subprocess.TimeoutExpired(args, 5)
        if behaviour == "oserror":
            raise OSError("no such binary")
        return subprocess.CompletedProcess(args, 2, "", "boom\n")

    return run


@pytest.mark.parametrize(
    ("behaviour", "message"),
    [
        ("nonzero", "release build failed: boom"),
        ("timeout", "release build timed out after 5 seconds"),
        ("oserror", "release build could not start: no such binary"),
    ],
)
def test_run_reports_each_failure_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, behaviour: str, message: str
) -> None:
    monkeypatch.setattr(
        cutover_module,
        "subprocess",
        SimpleNamespace(run=_failing_run(behaviour), TimeoutExpired=subprocess.TimeoutExpired),
    )

    with pytest.raises(cutover_module.CutoverError, match=message):
        cutover_module._run(["cargo", "build"], cwd=tmp_path, label="release build", timeout=5)


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


def _invoke_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_cutover: Callable[..., None],
) -> tuple[Path, str, int]:
    root = _workspace(tmp_path)
    native_dir = tmp_path / "native-bin"
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(native_dir))
    monkeypatch.setattr(cutover_module, "run_cutover", run_cutover)
    result = CliRunner().invoke(cli, ["cutover", "--path", str(root)])
    return native_dir, result.output, result.exit_code


def test_cli_targets_the_native_bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[Path] = []

    def run_cutover(_root: Path, bin_dir: Path, **_kwargs: object) -> None:
        seen.append(bin_dir)

    native_dir, output, exit_code = _invoke_cli(monkeypatch, tmp_path, run_cutover)

    assert exit_code == 0, output
    assert seen == [native_dir]
    assert "Cutover complete" in output


def test_cli_reports_daemon_restart_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing_restart(verbose: bool, docker_flag: bool) -> None:
        sys.exit(3)

    monkeypatch.setattr(cutover_module, "restart", failing_restart)

    def run_cutover(_root: Path, _bin_dir: Path, *, restart_daemon: Callable[[], None]) -> None:
        restart_daemon()

    _native_dir, output, exit_code = _invoke_cli(monkeypatch, tmp_path, run_cutover)

    assert exit_code != 0
    assert "daemon restart failed (exit 3)" in output


def test_cutover_is_registered() -> None:
    result = CliRunner().invoke(cli, ["cutover", "--help"])

    assert result.exit_code == 0
    assert "Build and atomically activate all schema-aware native binaries" in result.output
