"""Install the pinned open-code-review CLI that the commit gate and code review run.

A verified `ocr` on PATH is left in place. Any other install, or none, is replaced by the
pinned npm package. npm's result is verified before it is accepted, and an install that
fails verification is removed and reported as an error.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess  # nosec B404 # CompletedProcess and TimeoutExpired types only
from dataclasses import dataclass
from pathlib import Path

from gobby.utils import spawn
from gobby.utils.dependency_requirements import OCR_PACKAGE, OCR_VERSION, ocr_dependency_status

_INSTALL_TIMEOUT_SECONDS = 300
# 2100-01-01T00:00:00Z: far past any cooldown the launcher computes from this stamp.
_UPDATE_STAMP_EPOCH = 4_102_444_800


class OcrInstallError(RuntimeError):
    """The pinned OCR CLI could not be installed or verified."""


@dataclass(frozen=True)
class OcrInstallResult:
    installed: bool
    version: str
    path: Path


def _run_npm(npm: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return spawn.run(
            [npm, *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OcrInstallError(f"npm could not run: {exc}") from exc


def _defer_ocr_self_update() -> None:
    """Keep the pinned launcher from replacing itself with npm's latest release.

    Unless OCR_NO_UPDATE is set, the pinned bin/ocr.js spawns `npm i -g <package>@latest`
    whenever ~/.opencodereview/last-update-check is older than its cooldown. Gobby sets that
    variable only in sandboxed seats, so any other caller would move OCR off the pin. A stamp
    dated far ahead keeps every caller inside the cooldown. The launcher bytes are hash-pinned,
    so this behavior is fixed for the installed release.
    """
    stamp = Path.home() / ".opencodereview" / "last-update-check"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (_UPDATE_STAMP_EPOCH, _UPDATE_STAMP_EPOCH))
    except OSError as exc:
        raise OcrInstallError(
            f"could not stop OCR from updating itself off the pin: {exc}"
        ) from exc


def install_ocr_cli() -> OcrInstallResult:
    """Install OCR at the pinned version, or leave a verified install in place."""
    status = ocr_dependency_status()
    if status.state == "healthy" and status.path is not None:
        _defer_ocr_self_update()
        return OcrInstallResult(installed=False, version=OCR_VERSION, path=Path(status.path))

    npm = shutil.which("npm")
    if npm is None:
        raise OcrInstallError("npm is required to install the pinned OCR CLI")
    # --ignore-scripts stops npm from running OCR's postinstall, which fetches a binary from
    # GitHub outside these pins. The pinned platform package is the only native source.
    completed = _run_npm(npm, "install", "-g", "--ignore-scripts", f"{OCR_PACKAGE}@{OCR_VERSION}")
    if completed.returncode != 0:
        raise OcrInstallError(f"npm failed to install OCR: {completed.stderr.strip()}")

    verified = ocr_dependency_status()
    if verified.state != "healthy" or verified.path is None:
        # Remove the unverified install; if removal itself fails, the verification error still stands.
        with contextlib.suppress(OcrInstallError):
            _run_npm(npm, "uninstall", "-g", OCR_PACKAGE)
        raise OcrInstallError(verified.error or "npm installed an unverified OCR CLI")
    _defer_ocr_self_update()
    return OcrInstallResult(installed=True, version=OCR_VERSION, path=Path(verified.path))
