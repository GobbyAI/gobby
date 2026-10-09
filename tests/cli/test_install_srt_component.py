"""`gobby install srt` restages only the managed SRT runner, under an isolated GOBBY_HOME."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.agents import srt_runtime
from gobby.cli import install_setup_srt
from gobby.cli.install_components import run_install_components
from gobby.cli.install_setup_srt import SrtInstallResult
from gobby.utils.dependency_requirements import SRT_RELEASE, DependencyStatus

pytestmark = pytest.mark.unit

_BUNDLED_RUNNER = Path(srt_runtime.__file__).with_name("srt_runner.mjs")
_BUNDLED_LOCK = Path(srt_runtime.__file__).parents[1] / "install" / "srt-package-lock.json"
_STALE_RUNNER = b"// runner from an earlier Gobby build\n"
_SWAPPED = ("runner.mjs", "receipt.json", "content-manifest.json")
_INSTALL_LOCK = f".{SRT_RELEASE.version}.lock"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_installed_tree(root: Path, runner: bytes) -> None:
    from tests.srt_fixture_helpers import write_srt_proxy_fixture

    write_srt_proxy_fixture(root)
    package_dir = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "package.json").write_text(
        json.dumps({"name": SRT_RELEASE.package, "version": SRT_RELEASE.version}),
        encoding="utf-8",
    )
    for architecture in ("arm64", "x64"):
        helper = package_dir / "vendor" / "seccomp" / architecture / "apply-seccomp"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"executable helper")
    (root / "runner.mjs").write_bytes(runner)
    shutil.copyfile(_BUNDLED_LOCK, root / "package-lock.json")
    receipt = SRT_RELEASE.receipt_fields() | {
        "node": "/usr/bin/node",
        "runner_sha256": _sha(runner),
    }
    (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    srt_runtime.write_srt_content_manifest(root)
    srt_runtime.make_srt_installation_immutable(root)


def _snapshot(home: Path, skip: Callable[[Path], bool]) -> dict[str, tuple[str, int, int]]:
    snapshot: dict[str, tuple[str, int, int]] = {}
    for path in sorted(home.rglob("*")):
        if path.is_file() and not path.is_symlink() and not skip(path):
            info = path.stat()
            snapshot[path.relative_to(home).as_posix()] = (
                _sha(path.read_bytes()),
                info.st_ino,
                info.st_mode,
            )
    return snapshot


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "gobby-home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "gdaemon").write_bytes(b"gdaemon")
    (home / "bin" / "gterm").write_bytes(b"gterm")
    (home / "bootstrap.yaml").write_text("database_url: isolated\n", encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(
        srt_runtime,
        "node_dependency_status",
        lambda: DependencyStatus(
            state="healthy",
            installed_version="22.12.0",
            minimum_version="22.12.0",
            expected_version=None,
            path="/usr/bin/node",
            error=None,
        ),
    )
    return home


@pytest.fixture
def runtime() -> MagicMock:
    untouchable = MagicMock()
    untouchable.require_database.side_effect = AssertionError("srt touched the database")
    untouchable.require_config.side_effect = AssertionError("srt loaded daemon config")
    return untouchable


def _run_srt_component(project: Path, runtime: MagicMock) -> dict[str, object]:
    results = run_install_components(
        ("srt",), project_path=project, no_interactive=True, embedding=None, runtime=runtime
    )
    return results["srt"]


def test_srt_component_restages_only_the_runner(
    gobby_home: Path, runtime: MagicMock, tmp_path: Path
) -> None:
    root = srt_runtime.srt_install_root()
    _write_installed_tree(root, _STALE_RUNNER)
    stale_runner_inode = (root / "runner.mjs").stat().st_ino
    srt_tree = gobby_home / "tools" / "srt"
    outside_before = _snapshot(gobby_home, lambda path: srt_tree in path.parents)
    package_before = _snapshot(root, lambda path: path.name in _SWAPPED)

    result = _run_srt_component(tmp_path, runtime)

    assert result["success"] is True
    assert result["installed"] is True
    runner = (root / "runner.mjs").read_bytes()
    assert runner == _BUNDLED_RUNNER.read_bytes()
    assert _sha(runner) == SRT_RELEASE.runner_sha256
    assert (root / "runner.mjs").stat().st_ino != stale_runner_inode
    assert srt_runtime.verify_srt_installation().root == root.resolve()
    assert outside_before == _snapshot(gobby_home, lambda path: srt_tree in path.parents)
    assert package_before == _snapshot(root, lambda path: path.name in _SWAPPED)
    assert root.stat().st_mode & 0o777 == 0o555
    assert [path.name for path in root.iterdir() if path.name.startswith(".")] == []
    runtime.require_database.assert_not_called()
    runtime.require_config.assert_not_called()


def test_srt_component_leaves_a_current_install_untouched(
    gobby_home: Path, runtime: MagicMock, tmp_path: Path
) -> None:
    root = srt_runtime.srt_install_root()
    _write_installed_tree(root, _BUNDLED_RUNNER.read_bytes())
    before = _snapshot(gobby_home, lambda path: path.name == _INSTALL_LOCK)

    result = _run_srt_component(tmp_path, runtime)

    assert result["success"] is True
    assert result["installed"] is False
    assert before == _snapshot(gobby_home, lambda path: path.name == _INSTALL_LOCK)


def test_srt_component_swaps_each_file_by_rename_of_a_complete_sibling(
    gobby_home: Path, runtime: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = srt_runtime.srt_install_root()
    _write_installed_tree(root, _STALE_RUNNER)
    real_replace = os.replace
    replaced: list[str] = []

    def observe_replace(source: str | Path, destination: str | Path) -> None:
        source_path, destination_path = Path(source), Path(destination)
        assert source_path.parent == destination_path.parent == root
        assert source_path.name.startswith(".")
        if destination_path.name == "runner.mjs":
            assert destination_path.read_bytes() == _STALE_RUNNER
            assert source_path.read_bytes() == _BUNDLED_RUNNER.read_bytes()
        replaced.append(destination_path.name)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", observe_replace)

    assert _run_srt_component(tmp_path, runtime)["installed"] is True
    assert sorted(replaced) == sorted(_SWAPPED)


_PACKAGE_DIR = Path("node_modules/@anthropic-ai/sandbox-runtime")


def _append_to_package_json(root: Path) -> None:
    package_json = root / _PACKAGE_DIR / "package.json"
    package_json.chmod(0o644)
    package_json.write_text(package_json.read_text(encoding="utf-8") + " ", encoding="utf-8")
    package_json.chmod(0o444)


def _in_writable_root(root: Path, change: Callable[[Path], None]) -> None:
    root.chmod(0o755)
    change(root)
    root.chmod(0o555)


def _replace_text(name: str, text: str) -> Callable[[Path], None]:
    def rewrite(directory: Path) -> None:
        (directory / name).unlink()
        (directory / name).write_text(text, encoding="utf-8")
        (directory / name).chmod(0o444)

    return lambda root: _in_writable_root(root, rewrite)


def _remove(name: str) -> Callable[[Path], None]:
    return lambda root: _in_writable_root(root, lambda directory: (directory / name).unlink())


def _unpatch_proxy_and_rewrite_manifest(root: Path) -> None:
    from gobby.agents.srt_package_patch import HTTP_PROXY_PATH
    from tests.srt_fixture_helpers import upstream_proxy_bytes

    proxy = root / HTTP_PROXY_PATH
    proxy.chmod(0o644)
    proxy.write_bytes(upstream_proxy_bytes())
    (root / "content-manifest.json").chmod(0o644)
    srt_runtime.write_srt_content_manifest(root)
    srt_runtime.make_srt_installation_immutable(root)


_INVALID_TREES: dict[str, Callable[[Path], None]] = {
    "unpatched-proxy-rewritten-manifest": _unpatch_proxy_and_rewrite_manifest,
    "changed-package-content": _append_to_package_json,
    "missing-manifest": _remove("content-manifest.json"),
    "malformed-manifest": _replace_text("content-manifest.json", "{not json"),
    "missing-receipt": _remove("receipt.json"),
    "malformed-receipt": _replace_text("receipt.json", "{not json"),
    "writable-root": lambda root: root.chmod(0o755),
    "writable-package-file": lambda root: (root / _PACKAGE_DIR / "package.json").chmod(0o644),
    "nonexecutable-seccomp-helper": lambda root: (
        root / _PACKAGE_DIR / "vendor" / "seccomp" / "arm64" / "apply-seccomp"
    ).chmod(0o444),
}


@pytest.mark.parametrize("corrupt", list(_INVALID_TREES.values()), ids=list(_INVALID_TREES))
def test_srt_component_reinstalls_an_invalid_tree_without_touching_it(
    gobby_home: Path,
    runtime: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt: Callable[[Path], None],
) -> None:
    root = srt_runtime.srt_install_root()
    _write_installed_tree(root, _STALE_RUNNER)
    corrupt(root)
    before = _snapshot(gobby_home, lambda path: path.name == _INSTALL_LOCK)
    full_installs: list[None] = []

    def full_install() -> SrtInstallResult:
        full_installs.append(None)
        return SrtInstallResult(root, SRT_RELEASE.version, installed=True)

    monkeypatch.setattr(install_setup_srt, "install_srt_runtime", full_install)

    assert _run_srt_component(tmp_path, runtime)["installed"] is True
    assert full_installs == [None]
    assert before == _snapshot(gobby_home, lambda path: path.name == _INSTALL_LOCK)
