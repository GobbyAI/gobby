"""Workspace promotions cannot hide source changes behind an installed version."""

import logging
import subprocess
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from gobby.cli import cutover, install_setup_gclient, install_setup_gterm
from gobby.install import bin_freshness_promotion as promotion
from gobby.install.bin_release_identity import release_identity_path
from gobby.install.bin_set_coherence import promote_workspace_binary_set


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    for name in ("gcode", "gdaemon", "ghook", "gclient", "gterminal", "gcore"):
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


def binary(path: Path, version: str = "1.0.0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = (
        '{"runner_protocol":1,"baseline_version":420,"baseline_checksum":"baseline",'
        '"latest_version":468,"latest_checksum":"latest",'
        '"assets_root_hash":"root"}'
    )
    path.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
        f"printf '%s\\n' '{path.name} {version}'\nelse\n"
        f"printf '%s\\n' '{identity}'\nfi\n"
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("member", ["gterm", "gclient"])
def test_ignored_dependency_outputs_allow_same_source_repromotion(
    workspace: Path, tmp_path: Path, member: str
) -> None:
    (workspace / ".gitignore").write_text(".zig-cache/\nzig-pkg/\nzig-out/\n")
    source = binary(tmp_path / "artifacts" / member)
    destination = tmp_path / "bin" / member
    promotion.stage_and_promote_binary_file(source, destination=destination)
    receipt = (destination.parent / f".{member}-release-identity.json").read_bytes()
    for name in (".zig-cache", "zig-pkg", "zig-out"):
        output = workspace / "crates/gterminal/vendor/libghostty-vt" / name / "output"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"local build output")
    promotion.stage_and_promote_binary_file(source, destination=destination)
    assert destination.read_bytes() == source.read_bytes()
    assert (destination.parent / f".{member}-release-identity.json").read_bytes() == receipt


@pytest.mark.parametrize("phase", ["build", "stop"])
def test_cutover_refuses_sources_changed_after_build_snapshot(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    sources = {
        name: binary(tmp_path / "artifacts" / name) for name in ("gcode", "gdaemon", "ghook")
    }
    calls: list[str] = []

    def change_source() -> None:
        (workspace / "crates/gcore/src/lib.rs").write_text("pub fn changed() {}\n")

    def build(_root: Path) -> dict[str, Path]:
        if phase == "build":
            change_source()
        return sources

    def stop() -> bool:
        calls.append("stop")
        if phase == "stop":
            change_source()
        return True

    monkeypatch.setattr(cutover, "_build_artifacts", build)
    monkeypatch.setattr(cutover, "_prove_catalog_freshness", lambda *_args: None)
    monkeypatch.setattr(
        cutover, "promote_workspace_binary_set", lambda *_args, **_kwargs: calls.append("promote")
    )
    monkeypatch.setattr(cutover, "_verify_restart_target", lambda *_args: None)
    with pytest.raises(cutover.CutoverError, match="source changed"):
        cutover.run_cutover(
            workspace,
            tmp_path / "bin",
            stop_daemon=stop,
            start_daemon=lambda: calls.append("start"),
            start_refusal=lambda _candidate: None,
        )
    assert calls == (["stop"] if phase == "stop" else [])


def test_component_guard_uses_installed_name_for_renamed_candidate(
    workspace: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "bin" / "gclient"
    promotion.stage_and_promote_binary_file(
        binary(tmp_path / "build" / "gclient"), destination=destination
    )
    original = destination.read_bytes()
    (workspace / "crates" / "gcore" / "src" / "lib.rs").write_text("pub fn changed() {}\n")
    candidate = binary(tmp_path / "download" / "candidate")
    with pytest.raises(RuntimeError, match=r"gclient.*bump.*patch version"):
        promotion.stage_and_promote_binary_file(candidate, destination=destination)
    assert destination.read_bytes() == original


def test_legacy_repromotion_cannot_attribute_old_artifact_to_changed_workspace(
    workspace: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "bin" / "gclient"
    source = binary(tmp_path / "build" / "gclient")
    promotion.stage_and_promote_binary_file(source, destination=destination)
    release_identity_path(destination.parent, "gclient").unlink()
    (workspace / "crates" / "gcore" / "src" / "lib.rs").write_text("pub fn changed() {}\n")
    promotion.stage_and_promote_binary_file(source, destination=destination)
    original = destination.read_bytes()
    source.write_bytes(source.read_bytes() + b"# rebuilt from the changed sources\n")
    with pytest.raises(RuntimeError, match=r"gclient.*bump.*patch version"):
        promotion.stage_and_promote_binary_file(source, destination=destination)
    assert destination.read_bytes() == original


def test_current_artifact_shortcut_refuses_changed_workspace(
    workspace: Path, tmp_path: Path
) -> None:
    source = binary(tmp_path / "build" / "gclient")
    destination = tmp_path / "bin" / "gclient"
    promotion.stage_and_promote_binary_file(source, destination=destination)
    promotion.write_source_hash(destination.parent, "gclient", promotion.file_sha256(source))
    (workspace / "crates" / "gcore" / "src" / "lib.rs").write_text("pub fn changed() {}\n")
    with pytest.raises(RuntimeError, match=r"gclient.*bump.*patch version"):
        promotion.workspace_binary_is_current(destination.parent, "gclient", source, destination)


@pytest.mark.parametrize("component", ["gclient", "gterm"])
@pytest.mark.parametrize("method", ["git", "binstall", "install"])
def test_cargo_fallback_refuses_changed_binary_before_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str, method: str
) -> None:
    bin_dir = tmp_path / "bin"
    destination = binary(bin_dir / component)
    original = destination.read_bytes()

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--root" in command:
            target = Path(command[command.index("--root") + 1]) / "bin"
        else:
            target = Path(command[command.index("--install-path") + 1])
        candidate = binary(target / component)
        candidate.write_bytes(candidate.read_bytes() + b"# different release sources\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    module = SimpleNamespace(
        _GCLIENT_BIN_NAME="gclient",
        _GTERM_BIN_NAME="gterm",
        shutil=SimpleNamespace(which=lambda _name: "/fake/cargo"),
        subprocess=SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
        click=SimpleNamespace(echo=lambda _message: None),
        logger=logging.getLogger(__name__),
    )
    monkeypatch.setattr(install_setup_gterm, "_zig_016_available", lambda _module: True)
    installers: dict[tuple[str, str], Callable[[SimpleNamespace, Path], bool]] = {
        ("gclient", "git"): install_setup_gclient.install_gclient_from_cargo_git,
        ("gclient", "binstall"): install_setup_gclient.install_gclient_from_cargo_binstall,
        ("gclient", "install"): install_setup_gclient.install_gclient_from_cargo_install,
        ("gterm", "git"): install_setup_gterm.install_gterm_from_cargo_git,
        ("gterm", "binstall"): install_setup_gterm.install_gterm_from_cargo_binstall,
        ("gterm", "install"): install_setup_gterm.install_gterm_from_cargo_install,
    }
    with pytest.raises(RuntimeError, match=rf"{component}.*bump.*patch version"):
        installers[component, method](module, bin_dir)
    assert destination.read_bytes() == original


@pytest.mark.parametrize("member", ["gclient", "gterm"])
def test_component_refuses_changed_dependency_without_patch_bump(
    workspace: Path, tmp_path: Path, member: str
) -> None:
    source = binary(tmp_path / "artifacts" / member)
    installed = tmp_path / "bin" / member
    promotion.stage_and_promote_binary_file(source, destination=installed)
    original = installed.read_bytes()
    (workspace / "crates/gcore/src/lib.rs").write_text("pub fn changed() {}\n")

    with pytest.raises(RuntimeError, match=rf"{member}.*bump.*patch version"):
        promotion.stage_and_promote_binary_file(source, destination=installed)

    assert installed.read_bytes() == original


def test_coherent_set_refuses_before_replacing_any_member(workspace: Path, tmp_path: Path) -> None:
    sources = {
        name: binary(tmp_path / "artifacts" / name) for name in ("gcode", "gdaemon", "ghook")
    }
    bin_dir = tmp_path / "bin"
    promote_workspace_binary_set(sources, bin_dir=bin_dir)
    originals = {name: (bin_dir / name).read_bytes() for name in sources}
    (workspace / "crates/ghook/src/lib.rs").write_text("pub fn changed() {}\n")
    binary(sources["gcode"], "1.0.1")
    manifest = workspace / "crates/gcode/Cargo.toml"
    manifest.write_text(manifest.read_text().replace('"1.0.0"', '"1.0.1"'))

    with pytest.raises(RuntimeError, match=r"ghook.*bump.*patch version"):
        promote_workspace_binary_set(sources, bin_dir=bin_dir)

    assert {name: (bin_dir / name).read_bytes() for name in sources} == originals


@pytest.mark.parametrize("coherent", [False, True])
def test_same_source_repromotion_and_bumped_source_promote(
    workspace: Path, tmp_path: Path, coherent: bool
) -> None:
    members = ("gcode", "gdaemon", "ghook") if coherent else ("gclient",)
    sources = {name: binary(tmp_path / "artifacts" / name) for name in members}
    bin_dir = tmp_path / "bin"

    def promote() -> None:
        if coherent:
            promote_workspace_binary_set(sources, bin_dir=bin_dir)
        else:
            promotion.stage_and_promote_binary_file(
                sources["gclient"], destination=bin_dir / "gclient"
            )

    promote()
    promote()
    assert all((bin_dir / name).read_bytes() == sources[name].read_bytes() for name in members)
    (workspace / "crates/gcore/src/lib.rs").write_text("pub fn changed() {}\n")
    for name in members:
        binary(sources[name], "1.0.1")
        manifest = workspace / "crates" / name / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace('"1.0.0"', '"1.0.1"'))
    promote()
    assert all((bin_dir / name).read_bytes() == sources[name].read_bytes() for name in members)


def test_cutover_refuses_unbumped_source_before_stop(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = {
        name: binary(tmp_path / "artifacts" / name) for name in ("gcode", "gdaemon", "ghook")
    }
    bin_dir = tmp_path / "bin"
    promote_workspace_binary_set(sources, bin_dir=bin_dir)
    (workspace / "crates/gcore/src/lib.rs").write_text("pub fn changed() {}\n")
    monkeypatch.setattr(cutover, "_build_artifacts", lambda root: sources)
    monkeypatch.setattr(cutover, "_prove_catalog_freshness", lambda root, candidate: None)
    monkeypatch.setattr(cutover, "_verify_restart_target", lambda directory: None)
    stopped: list[bool] = []

    def stop() -> bool:
        stopped.append(True)
        return True

    with pytest.raises(RuntimeError, match=r"gcode.*bump.*patch version"):
        cutover.run_cutover(
            workspace,
            bin_dir,
            stop_daemon=stop,
            start_daemon=lambda: None,
            start_refusal=lambda candidate: None,
        )
    assert stopped == []


def test_component_archive_refuses_changed_bytes_under_installed_version(tmp_path: Path) -> None:
    source = binary(tmp_path / "artifacts" / "gclient")
    bin_dir = tmp_path / "bin"

    def promote() -> None:
        archive = BytesIO()
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("gclient", source.read_bytes())
        promotion.stage_and_promote_release_binary(
            archive.getvalue(),
            archive_ext="zip",
            binary_name="gclient",
            bin_dir=bin_dir,
            asset_name="gclient.zip",
        )

    promote()
    promote()
    original = (bin_dir / "gclient").read_bytes()
    source.write_text(source.read_text() + "# changed release source\n")
    with pytest.raises(RuntimeError, match=r"gclient.*bump.*patch version"):
        promote()
    assert (bin_dir / "gclient").read_bytes() == original
    binary(source, "1.0.1")
    promote()
    assert (bin_dir / "gclient").read_bytes() == source.read_bytes()
