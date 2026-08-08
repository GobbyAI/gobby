from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from gobby.install.manifest import build_bundled_content_manifest


def test_bundled_content_manifest_matches_tree() -> None:
    install_dir = Path(__file__).resolve().parents[2] / "src" / "gobby" / "install"
    committed = json.loads(
        (install_dir / "bundled_content_manifest.json").read_text(encoding="utf-8")
    )

    assert committed == build_bundled_content_manifest(install_dir / "shared")


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
