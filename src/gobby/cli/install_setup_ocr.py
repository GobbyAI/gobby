"""Install the pinned open-code-review CLI that the commit gate and code review run.

A matching `ocr` on PATH is left in place. Any other install, or none, is replaced by
the pinned npm package, and an install that cannot be verified is an error.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 # fixed npm invocation resolved with shutil.which
from dataclasses import dataclass
from pathlib import Path

from gobby.utils.dependency_requirements import OCR_PACKAGE, OCR_VERSION, ocr_dependency_status

_INSTALL_TIMEOUT_SECONDS = 300


class OcrInstallError(RuntimeError):
    """The pinned OCR CLI could not be installed or verified."""


@dataclass(frozen=True)
class OcrInstallResult:
    installed: bool
    version: str
    path: Path


def install_ocr_cli() -> OcrInstallResult:
    """Install OCR at the pinned version, or leave a matching install in place."""
    status = ocr_dependency_status()
    if status.state == "healthy" and status.path is not None:
        return OcrInstallResult(installed=False, version=OCR_VERSION, path=Path(status.path))

    npm = shutil.which("npm")
    if npm is None:
        raise OcrInstallError("npm is required to install the pinned OCR CLI")
    try:
        completed = subprocess.run(  # nosec B603 # executable resolved with shutil.which
            [npm, "install", "-g", f"{OCR_PACKAGE}@{OCR_VERSION}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OcrInstallError(f"npm could not install OCR: {exc}") from exc
    if completed.returncode != 0:
        raise OcrInstallError(f"npm failed to install OCR: {completed.stderr.strip()}")

    verified = ocr_dependency_status()
    if verified.state != "healthy" or verified.path is None:
        raise OcrInstallError(verified.error or "npm installed an unexpected OCR version")
    return OcrInstallResult(installed=True, version=OCR_VERSION, path=Path(verified.path))
