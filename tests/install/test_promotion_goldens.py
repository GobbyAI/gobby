"""Goldens for workspace set promotion and release fingerprints.

Recorded before generation retention. They replay unchanged with retention on:
the installed tree is compared with the retained-generation directory left out.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest

from gobby.install import bin_freshness_promotion as promotion
from gobby.install import bin_release_identity as release
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_set_coherence import (
    BinarySetCoherenceError,
    WorkspacePromotionMetadata,
    promote_workspace_binary_set,
)

pytestmark = pytest.mark.unit

SET_MEMBERS = ("gcode", "gdaemon", "ghook")
CRATE_DIRS = ("gcode", "gdaemon", "ghook", "gclient", "gmcp", "gterminal", "gcore")
RETAINED_DIR = "generations"
IDENTITY = {
    "runner_protocol": 1,
    "baseline_version": 420,
    "baseline_checksum": "baseline",
    "latest_version": 468,
    "latest_checksum": "latest",
    "assets_root_hash": "root",
}
# sha256 over each binary's crate closure in the synthetic workspace below.
FINGERPRINTS = {
    "gclient": "2d51d9d6e0ea216605b899821ef29630a62231abd9bab0087a044e12a7aa7c40",
    "gcode": "04f71cb7435ba9c5595b1b1e755a1d86c12a5523920b2e4a882241561eb2e157",
    "gdaemon": "01d60c3d3923bccab099c8c82633c7525734b5b99f585cf74354b28c73b43d36",
    "ghook": "2452997e5fff0daf6832ddbbfc689f1badc8a349d1222307544d9a71c9ebbc16",
    "gmcp": "bbea242649543747cbe0f9ac9fc3ee0d0c3578b512db57103e37dbd778b5e34f",
    "gterm": "9ee556709bceff51af217f93436f8a8ab933d92b29bab6f1930b8d035106d254",
}


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


def _tree(bin_dir: Path) -> dict[str, tuple[int, bytes]]:
    """Installed files by relative name, without lock files or retained generations."""
    files: dict[str, tuple[int, bytes]] = {}
    for path in sorted(bin_dir.rglob("*")):
        relative = path.relative_to(bin_dir)
        if relative.parts[0] in {RETAINED_DIR, ".locks"} or not path.is_file():
            continue
        files[str(relative)] = (stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return files


def _top_level(bin_dir: Path) -> set[str]:
    return {path.name for path in bin_dir.iterdir()} - {RETAINED_DIR}


def _promote(workspace: Path, bin_dir: Path, candidates: dict[str, Path]) -> None:
    promote_workspace_binary_set(
        candidates,
        bin_dir=bin_dir,
        workspace_root=workspace,
        metadata=WorkspacePromotionMetadata(install_method="workspace-cutover", target="test"),
    )


def test_complete_set_promotion_writes_exactly_these_files(workspace: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    first = _candidates(tmp_path, "1.0.0")
    _promote(workspace, bin_dir, first)
    inodes = {name: (bin_dir / name).stat().st_ino for name in SET_MEMBERS}
    for name in SET_MEMBERS:
        manifest = workspace / "crates" / name / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace("1.0.0", "1.0.1"))
    second = _candidates(tmp_path, "1.0.1")

    _promote(workspace, bin_dir, second)

    tree = _tree(bin_dir)
    assert sorted(tree) == sorted(
        [
            ".gdaemon-schema-identity.json",
            *(f".{name}-install.json" for name in SET_MEMBERS),
            *(f".{name}-release-identity.json" for name in SET_MEMBERS),
            *(f".{name}-version" for name in SET_MEMBERS),
            *SET_MEMBERS,
        ]
    )
    for name in SET_MEMBERS:
        assert (bin_dir / name).stat().st_ino != inodes[name]
        assert tree[name] == (0o755, second[name].read_bytes())
        assert tree[f".{name}-version"] == (0o644, b"1.0.1\n")
        mode, raw = tree[f".{name}-install.json"]
        sidecar = json.loads(raw)
        assert mode == 0o644
        assert sorted(sidecar) == [
            "install_method",
            "install_source_url",
            "installed_at",
            "installed_version",
            "target",
        ]
        assert (sidecar["install_method"], sidecar["installed_version"], sidecar["target"]) == (
            "workspace-cutover",
            "1.0.1",
            "test",
        )
        mode, raw = tree[f".{name}-release-identity.json"]
        assert mode == 0o600
        assert json.loads(raw) == {
            "artifact_sha256": hashlib.sha256(second[name].read_bytes()).hexdigest(),
            "source_identity": release.workspace_source_identity(workspace, name),
            "version": "1.0.1",
        }
    mode, raw = tree[".gdaemon-schema-identity.json"]
    assert mode == 0o644
    assert json.loads(raw) == IDENTITY


def test_refused_promotion_changes_no_installed_file(workspace: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.0"))
    names = _top_level(bin_dir)
    tree = _tree(bin_dir)
    (workspace / "crates/gcore/src/lib.rs").write_text("pub fn changed() {}\n")

    with pytest.raises(release.BinaryReleaseVersionError) as refusal:
        _promote(workspace, bin_dir, _candidates(tmp_path / "rebuilt", "1.0.0"))

    assert str(refusal.value) == (
        "gcode: changed or unverified source under installed version 1.0.0; "
        "bump its patch version before promotion"
    )
    assert _tree(bin_dir) == tree
    assert _top_level(bin_dir) == names


def test_unversioned_candidate_refusal_creates_nothing(workspace: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    candidates = _candidates(tmp_path, "1.0.0")
    payload = json.dumps(IDENTITY, separators=(",", ":"), sort_keys=True)
    candidates["ghook"].write_text(
        f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then exit 1; fi\nprintf '%s\\n' '{payload}'\n"
    )

    with pytest.raises(release.BinaryReleaseVersionError) as refusal:
        _promote(workspace, bin_dir, candidates)

    assert str(refusal.value) == "ghook: candidate did not report a version; rebuild it"
    assert not bin_dir.exists()


def test_busy_member_lock_refusal_changes_no_installed_file(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    _promote(workspace, bin_dir, _candidates(tmp_path, "1.0.0"))
    names = _top_level(bin_dir)
    tree = _tree(bin_dir)
    held = try_acquire_native_bin_lock("gdaemon", bin_dir=bin_dir)
    assert held is not None

    with held, pytest.raises(BinarySetCoherenceError) as refusal:
        _promote(workspace, bin_dir, _candidates(tmp_path / "again", "1.0.0"))

    assert str(refusal.value) == (
        "workspace binary set promotion failed; promoted: none; "
        "unpromoted: gcode, gdaemon, ghook; "
        "cause: gdaemon native binary update is already in progress"
    )
    assert _tree(bin_dir) == tree
    assert _top_level(bin_dir) == names


def test_release_fingerprints_of_the_synthetic_workspace(workspace: Path) -> None:
    assert {
        name: release.workspace_source_identity(workspace, name) for name in FINGERPRINTS
    } == FINGERPRINTS
