"""PID bookkeeping for remote-mode Python backends on Windows."""

from __future__ import annotations

import os
from pathlib import Path

from gobby.runner_pid_file import (
    PidOwnershipResolution,
    adopt_inherited_claim,
    claim_pid_file,
    convert_or_acquire_service_claim,
)


def claim_windows_pid(pid_file: Path) -> PidOwnershipResolution | None:
    try:
        if os.environ.get("GOBBY_SERVICE_LAUNCH") == "1":
            return convert_or_acquire_service_claim(pid_file)
        return adopt_inherited_claim(pid_file) or claim_pid_file(pid_file)
    finally:
        os.environ.pop("GOBBY_SERVICE_LAUNCH", None)
        os.environ.pop("GOBBY_SERVICE_NONCE", None)
