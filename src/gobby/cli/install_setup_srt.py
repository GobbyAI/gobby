"""Checksum-verified installation of Gobby's pinned Sandbox Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
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
        try:
            existing = verify_srt_installation_locked()
        except SrtRuntimeError:
            existing = None
        if existing is not None:
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
            if staging.exists():
                shutil.rmtree(staging)

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
    request = Request(
        SRT_RELEASE.tarball_url,
        headers={"User-Agent": f"gobby-srt/{SRT_RELEASE.version}"},
    )
    digest = hashlib.sha256()
    total = 0
    with (
        urlopen(request, timeout=30) as response,  # HTTPS enforced above  # nosec B310
        destination.open("wb") as output,
    ):
        while chunk := response.read(64 * 1024):
            total += len(chunk)
            if total > _MAX_TARBALL_BYTES:
                raise SrtRuntimeError("SRT tarball exceeded the expected size limit")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != SRT_RELEASE.tarball_sha256:
        raise SrtRuntimeError("SRT tarball checksum mismatch")


def _promote_install(staging: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        staging.rename(target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
