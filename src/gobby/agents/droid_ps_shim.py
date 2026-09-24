"""Ad-hoc re-signed ``ps`` for sandboxed Droid agents.

TODO(#22407): Remove this module and its wiring in ``srt_runtime`` once Factory
fixes ``factory-execute-supervisor``. This is a workaround for an upstream defect,
not a Gobby design choice, and it should be deleted the moment Droid ships a fix.
Upstream issue: https://github.com/Factory-AI/factory/issues/33

Droid runs every ``Execute`` command under an inline bash supervisor that probes
its owner with ``ps -o ppid= -p "$$"`` and treats *any* probe failure as owner
death, then runs ``kill -KILL -- -$$`` against its own detached process group --
taking the command it supervises with it. Nothing guards the first iteration, so
the kill lands within milliseconds.

macOS ships ``/bin/ps`` setuid-root (mode 4755) and seatbelt refuses to exec
setuid binaries, so under the SRT sandbox the probe fails with exit 126 and every
Droid shell command dies. Allowing ``/bin/ps`` through the policy is not an option
and would not work anyway: the denial is the setuid exec rule, not the filesystem
allowlist, and granting it would mean permitting setuid-root exec in the sandbox.

Copying ``/bin/ps`` is not sufficient on its own -- AMFI kills an Apple platform
binary executed outside its trusted location (exit 137). Re-signing the copy ad
hoc clears that, and the copy needs no setuid bit for this probe, which only
inspects the caller's own process. The shim is therefore the real ``ps`` reporting
the true parent pid, so Droid's orphan cleanup keeps working instead of being
faked out by a stubbed response.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess  # nosec B404 # re-signs a local system binary copy.
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.paths import get_gobby_home
from gobby.utils import spawn

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

SYSTEM_PS = Path("/bin/ps")
CODESIGN = Path("/usr/bin/codesign")
_SHIM_DIRNAME = "droid-ps-shim"


def _source_digest(path: Path) -> str:
    """Digest the system binary so an OS update re-signs a fresh copy."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def droid_ps_shim_dir() -> Path | None:
    """Return a directory holding an execable ``ps``, or None when unavailable.

    Keyed by the digest of ``/bin/ps`` so a system update lands in a new directory
    rather than serving a stale copy. Failure is non-fatal: the spawn proceeds and
    Droid's ``Execute`` stays broken exactly as it is without the shim.
    """
    if sys.platform != "darwin" or not SYSTEM_PS.exists() or not CODESIGN.exists():
        return None
    try:
        directory = get_gobby_home() / "runtime" / _SHIM_DIRNAME / _source_digest(SYSTEM_PS)
        target = directory / "ps"
        if target.exists():
            return directory
        directory.mkdir(parents=True, exist_ok=True)
        staged = directory / f".ps.{os.getpid()}.tmp"
        try:
            shutil.copyfile(SYSTEM_PS, staged)
            os.chmod(staged, 0o755)  # nosec B103 # mirrors /bin/ps; user-owned home.
            spawn.run(  # nosec B603 # fixed argv, local codesign, no shell.
                [str(CODESIGN), "-f", "-s", "-", str(staged)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Droid ps shim unavailable; sandboxed Droid Execute will fail closed: %s", exc
        )
        return None
    return directory


def ps_shim_path_env(directory: Path | None, env: Mapping[str, str]) -> dict[str, str]:
    """Prepend the shim directory to PATH so Droid's supervisor resolves it first."""
    if directory is None:
        return {}
    existing = env.get("PATH", "")
    return {"PATH": f"{directory}{os.pathsep}{existing}" if existing else str(directory)}
