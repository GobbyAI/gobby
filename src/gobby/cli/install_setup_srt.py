"""Checksum-verified installation of Gobby's pinned Sandbox Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from gobby.agents.srt_runtime import (
    _CONTENT_MANIFEST_NAME,
    SrtRuntimeError,
    _verify_srt_content,
    make_srt_installation_immutable,
    srt_install_lock,
    srt_install_root,
    verify_srt_installation_locked,
    write_srt_content_manifest,
)
from gobby.utils.dependency_requirements import SRT_RELEASE, node_dependency_status

_MAX_TARBALL_BYTES = 16 * 1024 * 1024
_MAX_TARBALL_REQUESTS = 8
_PACKAGE_JSON_PATH = Path("node_modules/@anthropic-ai/sandbox-runtime/package.json")
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


def restage_srt_runner() -> SrtInstallResult:
    """Bring the staged runner up to this build, swapping only runner-owned files.

    A runner change bumps ``SRT_RELEASE.runner_sha256`` and leaves every other
    pinned file alone, so the installed package tree is kept when it still
    matches its manifest. Anything else falls back to the full staged install.
    """
    try:
        restaged = _restage_srt_runner()
    except SrtRuntimeError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise SrtRuntimeError(f"failed to restage the managed SRT runner: {exc}") from exc
    return restaged if restaged is not None else install_srt_runtime()


def _restage_srt_runner() -> SrtInstallResult | None:
    target = srt_install_root()
    runner = (Path(__file__).parents[1] / "agents" / "srt_runner.mjs").read_bytes()
    if hashlib.sha256(runner).hexdigest() != SRT_RELEASE.runner_sha256:
        raise SrtRuntimeError("bundled SRT runner checksum mismatch")
    with srt_install_lock():
        if target.is_symlink() or not target.is_dir():
            return None
        try:
            verify_srt_installation_locked()
        except SrtRuntimeError:
            pass
        else:
            return SrtInstallResult(target.resolve(), SRT_RELEASE.version, installed=False)

        eligible = _runner_only_drift(target)
        if eligible is None:
            return None
        manifest, receipt = eligible
        _require_node()
        manifest["runner.mjs"] = SRT_RELEASE.runner_sha256
        receipt |= SRT_RELEASE.receipt_fields()
        root_mode = stat.S_IMODE(target.stat().st_mode)
        target.chmod(root_mode | stat.S_IWUSR)
        try:
            _replace_file(target / "runner.mjs", runner, 0o555)
            _replace_file(
                target / "receipt.json",
                (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                0o444,
            )
            _replace_file(
                target / _CONTENT_MANIFEST_NAME,
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                0o444,
            )
        finally:
            target.chmod(root_mode)
        verify_srt_installation_locked()
        return SrtInstallResult(target.resolve(), SRT_RELEASE.version, installed=True)


def _runner_only_drift(target: Path) -> tuple[dict[str, str], dict[str, object]] | None:
    """Return the installed manifest and receipt when only runner-owned state is stale.

    Every other invariant the verifier enforces must already hold: package
    content, lockfile pin, package identity, immutable modes, and executable
    seccomp helpers. Never re-hash or re-permission an invalid tree.
    """
    try:
        manifest = json.loads((target / _CONTENT_MANIFEST_NAME).read_text(encoding="utf-8"))
        receipt = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
        package = json.loads((target / _PACKAGE_JSON_PATH).read_text(encoding="utf-8"))
        runner_sha256 = hashlib.sha256((target / "runner.mjs").read_bytes()).hexdigest()
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in manifest.items()
        )
        or not isinstance(receipt, dict)
        or not isinstance(package, dict)
        or package.get("name") != SRT_RELEASE.package
        or package.get("version") != SRT_RELEASE.version
        or manifest.get("package-lock.json") != SRT_RELEASE.lockfile_sha256
        or _without(receipt, "runner_sha256", "node")
        != _without(SRT_RELEASE.receipt_fields(), "runner_sha256")
    ):
        return None
    try:
        _verify_srt_content(target, manifest | {"runner.mjs": runner_sha256})
    except (OSError, SrtRuntimeError):
        return None
    return manifest, receipt


def _without(mapping: Mapping[str, object], *keys: str) -> dict[str, object]:
    return {key: value for key, value in mapping.items() if key not in keys}


def _replace_file(path: Path, data: bytes, mode: int) -> None:
    """Write a complete sibling, then rename it over ``path`` as a new inode."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _require_node() -> Path:
    status = node_dependency_status()
    if status.state != "healthy" or status.path is None:
        raise SrtRuntimeError(status.error or "Node.js version could not be verified")
    return Path(status.path)


def _download_verified_tarball(destination: Path) -> None:
    if urlsplit(SRT_RELEASE.tarball_url).scheme != "https":
        raise SrtRuntimeError("managed SRT tarball URL must use HTTPS")

    headers = {"User-Agent": f"gobby-srt/{SRT_RELEASE.version}"}
    mismatch_details = "no download attempts"
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
        mismatch_details = (
            f"artifact={destination.name}, source={SRT_RELEASE.tarball_url}, "
            f"expected_sha256={SRT_RELEASE.tarball_sha256}, actual_sha256={digest.hexdigest()}, "
            f"downloaded_bytes={total}, content_length={expected_total}"
        )
        destination.unlink(missing_ok=True)

    raise SrtRuntimeError(
        f"SRT tarball checksum mismatch after {_MAX_TARBALL_REQUESTS} attempts: {mismatch_details}"
    )


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
