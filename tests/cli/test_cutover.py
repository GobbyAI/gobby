from __future__ import annotations

import importlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner, Result

from gobby.cli import cli
from gobby.install import bin_set_coherence

cutover_module = importlib.import_module("gobby.cli.cutover")

pytestmark = pytest.mark.unit

SET_MEMBERS = ("gcode", "gdaemon", "ghook")
VERSION = "0.5.0"


def _identity(version: int) -> dict[str, int | str]:
    return {
        "baseline_version": 1,
        "latest_version": version,
        "baseline_checksum": "a" * 64,
        "latest_checksum": f"{version:064x}",
        "assets_root_hash": "b" * 64,
        "runner_protocol": 3,
    }


def _write_stub(path: Path, identity: dict[str, int | str]) -> None:
    payload = json.dumps(identity, sort_keys=True)
    path.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"--version\" ]; then printf '%s\\n' '{path.name} {VERSION}'; "
        f"else printf '%s\\n' '{payload}'; fi\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _artifacts(root: Path, identity: dict[str, int | str]) -> dict[str, Path]:
    release = root / "target" / "release"
    release.mkdir(parents=True)
    artifacts = {member: release / member for member in SET_MEMBERS}
    for artifact in artifacts.values():
        _write_stub(artifact, identity)
    return artifacts


def test_run_cutover_builds_and_promotes_through_shared_set_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    identity = _identity(420)
    artifacts = _artifacts(root, identity)
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    monkeypatch.setattr(
        cutover_module,
        "resolve_native_bin",
        lambda name: str(bin_dir / name) if name == "gdaemon" else None,
    )
    signed: list[str] = []
    monkeypatch.setattr(
        bin_set_coherence,
        "_codesign_workspace_binary",
        lambda binary: signed.append(binary.name),
    )
    promotion = Mock(wraps=bin_set_coherence.promote_workspace_binary_set)
    monkeypatch.setattr(cutover_module, "promote_workspace_binary_set", promotion)
    restarted: list[bytes] = []

    cutover_module.run_cutover(
        root,
        bin_dir,
        restart_daemon=lambda: restarted.append((bin_dir / "gdaemon").read_bytes()),
        start_refusal=lambda _candidate: None,
    )

    assert promotion.call_count == 1
    assert tuple(promotion.call_args.args[0]) == SET_MEMBERS
    assert signed == list(SET_MEMBERS)
    for member, artifact in artifacts.items():
        assert (bin_dir / member).read_bytes() == artifact.read_bytes()
        sidecar = json.loads((bin_dir / f".{member}-install.json").read_text(encoding="utf-8"))
        assert sidecar["install_method"] == "workspace-cutover"
        assert sidecar["target"] == "test-target"
    assert (
        json.loads((bin_dir / ".gdaemon-schema-identity.json").read_text(encoding="utf-8"))
        == identity
    )
    assert restarted == [(bin_dir / "gdaemon").read_bytes()]


def test_run_cutover_fails_closed_when_resolved_gdaemon_differs_from_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    identity = _identity(420)
    artifacts = _artifacts(root, identity)
    stale_gdaemon = tmp_path / "stale-gdaemon"
    _write_stub(stale_gdaemon, _identity(419))
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    monkeypatch.setattr(cutover_module, "resolve_native_bin", lambda _name: str(stale_gdaemon))
    monkeypatch.setattr(bin_set_coherence, "_codesign_workspace_binary", lambda _path: None)

    with pytest.raises(cutover_module.CutoverError) as exc_info:
        cutover_module.run_cutover(
            root,
            bin_dir,
            restart_daemon=pytest.fail,
            start_refusal=lambda _candidate: None,
        )

    message = str(exc_info.value)
    assert str(stale_gdaemon) in message
    assert "v419" in message
    assert "v420" in message
    assert "rebuild and install all three together" in message
    assert (bin_dir / "gdaemon").read_bytes() == artifacts["gdaemon"].read_bytes()
    assert "restored prior install" not in message


def test_build_uses_one_locked_release_command_for_all_set_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path, _identity(420))
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cutover_module.subprocess, "run", run)

    assert cutover_module._build_artifacts(tmp_path) == artifacts
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
        ]
    ]


def _invoke_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_cutover: Callable[..., None],
    *,
    dirty: list[str] | None = None,
    extra_args: tuple[str, ...] = (),
) -> Result:
    workspace = tmp_path / "workspace"
    (workspace / "crates").mkdir(parents=True)
    (workspace / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    pin = workspace / "src" / "gobby" / "storage" / "schema_expected_identity.json"
    pin.parent.mkdir(parents=True)
    pin.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cutover_module, "run_cutover", run_cutover)
    monkeypatch.setattr(cutover_module, "_dirty_schema_inputs", lambda _root: list(dirty or []))
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(tmp_path / "managed-bin"))
    return CliRunner().invoke(cli, ["cutover", "--path", str(workspace), *extra_args])


def test_cli_targets_the_native_bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: list[Path] = []

    def run_cutover(_root: Path, bin_dir: Path, **_kwargs: object) -> None:
        observed.append(bin_dir)

    result = _invoke_cli(tmp_path, monkeypatch, run_cutover)

    assert result.exit_code == 0, result.output
    assert observed == [tmp_path / "managed-bin"]


def test_cli_reports_daemon_restart_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failing_restart(**_kwargs: object) -> None:
        raise SystemExit(7)

    monkeypatch.setattr(cutover_module, "restart", failing_restart)

    def run_cutover(
        _root: Path, _bin_dir: Path, *, restart_daemon: Callable[[], None], **_kwargs: object
    ) -> None:
        restart_daemon()

    result = _invoke_cli(tmp_path, monkeypatch, run_cutover)

    assert result.exit_code == 1
    assert "daemon restart failed (exit 7)" in result.output


def test_cutover_has_no_private_replacement_or_rollback_machinery() -> None:
    assert not hasattr(cutover_module, "_ReplacementSet")
    assert not hasattr(cutover_module, "_stage_sidecars")
    assert "cutover" in cli.commands


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Gobby Tests", "-c", "user.email=tests@example.com", *args],
        cwd=repo,
        check=True,
        timeout=30,
        capture_output=True,
    )


def _schema_repo(tmp_path: Path) -> Path:
    """A real git repository carrying both schema inputs and unrelated crate sources."""
    repo = tmp_path / "repo"
    (repo / "crates" / "gcore" / "assets" / "schema").mkdir(parents=True)
    (repo / "crates" / "gcore" / "src" / "schema").mkdir(parents=True)
    (repo / "crates" / "gcode").mkdir(parents=True)
    pin = repo / cutover_module._PIN_PATH
    pin.parent.mkdir(parents=True)
    pin.write_text("{}\n", encoding="utf-8")
    (repo / "crates" / "gcore" / "assets" / "schema" / "baseline.sql").write_text("-- baseline\n")
    (repo / "crates" / "gcore" / "src" / "schema" / "runner.rs").write_text("// runner\n")
    (repo / "crates" / "gcode" / "lib.rs").write_text("// gcode\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--no-gpg-sign", "-q", "-m", "initial")
    return repo


def test_run_cutover_refuses_before_promotion_when_candidate_plan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal after promotion would already have replaced the installed set."""
    root = tmp_path / "workspace"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    artifacts = _artifacts(root, _identity(420))
    monkeypatch.setattr(cutover_module, "_build_artifacts", lambda _root: artifacts)
    monkeypatch.setattr(cutover_module, "_platform_target", lambda: "test-target")
    promotion = Mock(side_effect=AssertionError("promotion must not run"))
    monkeypatch.setattr(cutover_module, "promote_workspace_binary_set", promotion)

    with pytest.raises(cutover_module.CutoverError) as exc_info:
        cutover_module.run_cutover(
            root,
            bin_dir,
            restart_daemon=pytest.fail,
            start_refusal=lambda _candidate: "unrecognized schema lineage",
        )

    message = str(exc_info.value)
    assert "refusing to promote" in message
    assert "unrecognized schema lineage" in message
    assert not bin_dir.exists(), "nothing may be promoted on refusal"
    promotion.assert_not_called()


def test_cli_refuses_dirty_schema_inputs_and_names_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run_cutover(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the build must not run from dirty schema inputs")

    result = _invoke_cli(
        tmp_path, monkeypatch, run_cutover, dirty=["crates/gcore/src/schema/assets.rs"]
    )

    assert result.exit_code == 1, result.output
    assert "uncommitted schema inputs" in result.output
    assert "crates/gcore/src/schema/assets.rs" in result.output
    assert "--allow-dirty" in result.output


def test_cli_allow_dirty_skips_the_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: list[Path] = []

    def run_cutover(_root: Path, bin_dir: Path, **_kwargs: object) -> None:
        observed.append(bin_dir)

    result = _invoke_cli(
        tmp_path,
        monkeypatch,
        run_cutover,
        dirty=["crates/gcore/src/schema/assets.rs"],
        extra_args=("--allow-dirty",),
    )

    assert result.exit_code == 0, result.output
    assert observed == [tmp_path / "managed-bin"]


def test_dirty_schema_inputs_is_scoped_to_schema_paths(tmp_path: Path) -> None:
    repo = _schema_repo(tmp_path)
    assert cutover_module._dirty_schema_inputs(repo) == []

    (repo / "crates" / "gcode" / "lib.rs").write_text("// edited\n")
    assert cutover_module._dirty_schema_inputs(repo) == [], "non-schema dirt must not block"

    pin = repo / cutover_module._PIN_PATH
    pin.write_text('{"latest_version": 1}\n', encoding="utf-8")
    assert cutover_module._dirty_schema_inputs(repo) == [str(cutover_module._PIN_PATH)]

    pin.write_text("{}\n", encoding="utf-8")
    migration = repo / "crates" / "gcore" / "assets" / "schema" / "migrations" / "440_x.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    assert cutover_module._dirty_schema_inputs(repo) == [
        "crates/gcore/assets/schema/migrations/440_x.sql"
    ]


def test_dirty_schema_inputs_fails_closed_without_git(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    with pytest.raises(cutover_module.CutoverError):
        cutover_module._dirty_schema_inputs(outside)
