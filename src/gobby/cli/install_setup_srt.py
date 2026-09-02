"""Checksum-verified installation of Gobby's pinned Sandbox Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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

    digest = hashlib.sha256()
    total = 0
    expected_total: int | None = None
    for _attempt in range(_MAX_TARBALL_REQUESTS):
        request_offset = total
        headers = {"User-Agent": f"gobby-srt/{SRT_RELEASE.version}"}
        if request_offset:
            headers["Range"] = f"bytes={request_offset}-"
        request = Request(SRT_RELEASE.tarball_url, headers=headers)
        with urlopen(request, timeout=30) as response:  # HTTPS enforced above  # nosec B310
            status = getattr(response, "status", 200)
            response_headers = getattr(response, "headers", {})
            if request_offset and status != 206:
                destination.unlink(missing_ok=True)
                digest = hashlib.sha256()
                total = 0
                expected_total = None
            elif request_offset:
                content_range = response_headers.get("Content-Range", "")
                range_prefix = f"bytes {request_offset}-"
                if not content_range.startswith(range_prefix):
                    raise SrtRuntimeError("SRT tarball returned an invalid byte range")
                try:
                    expected_total = int(content_range.partition("/")[2])
                except ValueError as exc:
                    raise SrtRuntimeError("SRT tarball returned an invalid byte range") from exc
            elif content_length := response_headers.get("Content-Length"):
                expected_total = int(content_length)

            if expected_total is not None and expected_total > _MAX_TARBALL_BYTES:
                raise SrtRuntimeError("SRT tarball exceeded the expected size limit")
            mode = "ab" if total else "wb"
            with destination.open(mode) as output:
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

        if expected_total is not None and total < expected_total:
            continue
        if digest.hexdigest() == SRT_RELEASE.tarball_sha256:
            return
        destination.unlink(missing_ok=True)
        digest = hashlib.sha256()
        total = 0
        expected_total = None

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
