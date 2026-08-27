from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from gobby.install.manifest import (
    build_bundled_content_manifest,
    check_committed_bundled_content_manifest,
    check_linked_committed_bundled_manifest,
    write_bundled_content_manifest,
)


def test_bundled_content_manifest_matches_tree() -> None:
    install_dir = Path(__file__).resolve().parents[2] / "src" / "gobby" / "install"
    committed = json.loads(
        (install_dir / "bundled_content_manifest.json").read_text(encoding="utf-8")
    )

    assert committed == build_bundled_content_manifest(install_dir / "shared")


def test_current_committed_bundled_content_manifest_matches_git_tree() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    result = check_committed_bundled_content_manifest(repo_root)

    assert result.ok is True
    assert result.errors == ()
    assert result.expected_file_count > 0


def test_committed_checker_ignores_worktree_and_scopes_linked_commits(tmp_path: Path) -> None:
    install_dir = tmp_path / "src" / "gobby" / "install"
    shared_dir = install_dir / "shared"
    shared_dir.mkdir(parents=True)
    rule = shared_dir / "rule.yaml"
    rule.write_text("enabled: true\n", encoding="utf-8")
    write_bundled_content_manifest(install_dir)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@gobby.local")
    _git(tmp_path, "config", "user.name", "Gobby Tests")
    _git(tmp_path, "add", "src/gobby/install")
    _git(tmp_path, "commit", "-qm", "initial manifest")
    assert check_committed_bundled_content_manifest(tmp_path).ok is True

    rule.write_text("enabled: false\n", encoding="utf-8")
    _git(tmp_path, "add", "src/gobby/install/shared/rule.yaml")
    _git(tmp_path, "commit", "-qm", "change shared rule")
    shared_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    # A fixed working-tree manifest and foreign untracked content cannot make
    # the stale committed tree pass.
    write_bundled_content_manifest(install_dir)
    (shared_dir / "foreign.yaml").write_text("foreign: true\n", encoding="utf-8")
    stale = check_committed_bundled_content_manifest(tmp_path)
    linked = check_linked_committed_bundled_manifest(tmp_path, [shared_sha])
    assert stale.ok is False
    assert stale.errors[0] == "Committed bundled content manifest is stale."
    assert linked is not None and linked.ok is False
    stale_cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "gobby.install.manifest",
            "--repo-root",
            str(tmp_path),
            "--treeish",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stale_cli.returncode == 1
    assert "Committed bundled content manifest is stale." in stale_cli.stderr

    readme = tmp_path / "README.md"
    readme.write_text("unrelated\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "unrelated")
    unrelated_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert check_linked_committed_bundled_manifest(tmp_path, [unrelated_sha]) is None

    _git(tmp_path, "add", "src/gobby/install/bundled_content_manifest.json")
    _git(tmp_path, "commit", "-qm", "refresh manifest")
    rule.write_text("foreign working-tree edit\n", encoding="utf-8")
    clean = check_committed_bundled_content_manifest(tmp_path)
    assert clean.ok is True

    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "gobby.install.manifest",
            "--repo-root",
            str(tmp_path),
            "--treeish",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0
    assert "Committed bundled content manifest matches HEAD" in cli.stdout


def test_manifest_membership_matches_wheel(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source_root = tmp_path / "source"
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for raw_relative in tracked:
        if not raw_relative:
            continue
        relative = Path(os.fsdecode(raw_relative))
        if relative.parts[0] == ".gobby":
            continue
        source = repo_root / relative
        destination = source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, destination)
    build_environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "SETUPTOOLS_SCM_PRETEND_VERSION": "0.5.0",
    }
    sdist_dir = tmp_path / "sdist"
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(sdist_dir)],
        cwd=source_root,
        env=build_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    sdist_path = next(sdist_dir.glob("*.tar.gz"))
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), str(sdist_path)],
        cwd=source_root,
        env=build_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    wheel_path = next(wheel_dir.glob("*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(installed)

    package_install = installed / "gobby" / "install"
    committed = json.loads(
        (package_install / "bundled_content_manifest.json").read_text(encoding="utf-8")
    )
    shared_dir = package_install / "shared"

    assert committed == build_bundled_content_manifest(shared_dir)
    assert set(committed["files"]) == {
        path.relative_to(shared_dir).as_posix() for path in shared_dir.rglob("*") if path.is_file()
    }
    assert all(
        not any(part.startswith(".") for part in Path(relative_path).parts)
        for relative_path in committed["files"]
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
