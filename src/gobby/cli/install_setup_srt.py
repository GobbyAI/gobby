"""Checksum-verified installation of Gobby's pinned Sandbox Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from gobby.agents.srt_runtime import (
    SrtRuntimeError,
    make_srt_installation_immutable,
    srt_install_lock,
    srt_install_root,
    verify_srt_installation_locked,
    write_srt_content_manifest,
)
from gobby.utils.dependency_requirements import SRT_RELEASE, node_dependency_status

_MAX_TARBALL_BYTES = 16 * 1024 * 1024
_MAX_TARBALL_REQUESTS = 8
_PACKAGE_JSON = {
    "name": "gobby-managed-srt",
    "version": "0.0.0",
    "private": True,
    "dependencies": {SRT_RELEASE.package: "file:sandbox-runtime.tgz"},
}


@dataclass(frozen=True)
class SrtInstallResult:
    path: Path
    version: str
    installed: bool


def install_srt_runtime() -> SrtInstallResult:
    """Install SRT and normalize operational failures to the fail-closed contract."""
    try:
        return _install_srt_runtime()
    except SrtRuntimeError:
        raise
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, URLError) as exc:
        raise SrtRuntimeError(
            f"failed to install managed SRT {SRT_RELEASE.version}: {exc}"
        ) from exc


def _install_srt_runtime() -> SrtInstallResult:
    """Install the immutable SRT dependency graph, or reuse a verified copy."""
    target = srt_install_root()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with srt_install_lock():
        existing = None
        if target.exists() or target.is_symlink():
            try:
                existing = verify_srt_installation_locked()
            except SrtRuntimeError:
                pass
        if existing is not None:
            _cleanup_install_tree(target.with_name(f".{target.name}.previous"))
            return SrtInstallResult(existing.root, SRT_RELEASE.version, installed=False)

        node = _require_node()
        npm_raw = shutil.which("npm")
        if not npm_raw:
            raise SrtRuntimeError("npm is required to install Gobby's managed SRT runtime")
        npm = Path(npm_raw).resolve(strict=True)

        staging = Path(tempfile.mkdtemp(prefix=f".{SRT_RELEASE.version}-", dir=target.parent))
        try:
            tarball = staging / "sandbox-runtime.tgz"
            _download_verified_tarball(tarball)
            _write_json(staging / "package.json", _PACKAGE_JSON)
            lock_source = Path(__file__).parents[1] / "install" / "srt-package-lock.json"
            lock_target = staging / "package-lock.json"
            shutil.copyfile(lock_source, lock_target)
            if _sha256(lock_target) != SRT_RELEASE.lockfile_sha256:
                raise SrtRuntimeError("bundled SRT lockfile checksum mismatch")

            result = subprocess.run(
                [
                    str(npm),
                    "ci",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--omit=dev",
                ],
                cwd=staging,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            if result.returncode != 0:
                raise SrtRuntimeError(f"npm failed to install managed SRT: {result.stderr.strip()}")

            installed_package = (
                staging / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "package.json"
            )
            package = json.loads(installed_package.read_text(encoding="utf-8"))
            if (
                package.get("name") != SRT_RELEASE.package
                or package.get("version") != SRT_RELEASE.version
            ):
                raise SrtRuntimeError("npm installed an unexpected Sandbox Runtime package")

            runner_source = Path(__file__).parents[1] / "agents" / "srt_runner.mjs"
            runner_target = staging / "runner.mjs"
            shutil.copyfile(runner_source, runner_target)
            os.chmod(runner_target, 0o700)
            if _sha256(runner_target) != SRT_RELEASE.runner_sha256:
                raise SrtRuntimeError("bundled SRT runner checksum mismatch")
            _write_json(
                staging / "receipt.json",
                SRT_RELEASE.receipt_fields() | {"node": str(node)},
            )
            write_srt_content_manifest(staging)
            make_srt_installation_immutable(staging)
            _promote_install(staging, target)
        finally:
            _cleanup_install_tree(staging)

        verify_srt_installation_locked()
        return SrtInstallResult(target.resolve(), SRT_RELEASE.version, installed=True)


def _require_node() -> Path:
    status = node_dependency_status()
    if status.state != "healthy" or status.path is None:
        raise SrtRuntimeError(status.error or "Node.js version could not be verified")
    return Path(status.path)


def _download_verified_tarball(destination: Path) -> None:
    if urlsplit(SRT_RELEASE.tarball_url).scheme != "https":
        raise SrtRuntimeError("managed SRT tarball URL must use HTTPS")

    headers = {"User-Agent": f"gobby-srt/{SRT_RELEASE.version}"}
    for _attempt in range(_MAX_TARBALL_REQUESTS):
        # Every attempt starts from scratch: partial data from a truncated
        # transfer is discarded rather than resumed.
        digest = hashlib.sha256()
        total = 0
        expected_total: int | None = None
        request = Request(SRT_RELEASE.tarball_url, headers=headers)
        with urlopen(request, timeout=30) as response:  # HTTPS enforced above  # nosec B310
            response_headers = getattr(response, "headers", {})
            if content_length := response_headers.get("Content-Length"):
                expected_total = int(content_length)
            if expected_total is not None and expected_total > _MAX_TARBALL_BYTES:
                raise SrtRuntimeError("SRT tarball exceeded the expected size limit")
            with destination.open("wb") as output:
                while True:
                    try:
                        chunk = response.read(64 * 1024)
                    except IncompleteRead as exc:
                        chunk = exc.partial
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_TARBALL_BYTES:
                        raise SrtRuntimeError("SRT tarball exceeded the expected size limit")
                    digest.update(chunk)
                    output.write(chunk)

        complete = expected_total is None or total >= expected_total
        if complete and digest.hexdigest() == SRT_RELEASE.tarball_sha256:
            return
        destination.unlink(missing_ok=True)

    raise SrtRuntimeError("SRT tarball checksum mismatch")


def _cleanup_install_tree(path: Path) -> None:
    """Remove an owned staging or backup tree without following its symlinks."""
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    # Unlinking immutable files requires write access to their directories only.
    for _root, _directories, _files, directory_fd in os.fwalk(path, follow_symlinks=False):
        mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
        os.fchmod(directory_fd, mode | stat.S_IRWXU)
    shutil.rmtree(path)


def _promote_install(staging: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.previous")
    _cleanup_install_tree(backup)
    if os.path.lexists(target):
        target.rename(backup)
    try:
        staging.rename(target)
    except Exception:
        if os.path.lexists(backup) and not os.path.lexists(target):
            backup.rename(target)
        raise
    _cleanup_install_tree(backup)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
