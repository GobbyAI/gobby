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

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

SYSTEM_PS = Path("/bin/ps")
CODESIGN = Path("/usr/bin/codesign")
_SHIM_DIRNAME = "droid-ps-shim"
_SHELL_WRAPPER_NAME = "gobby-droid-shell"

# Prepending the shim to PATH is not enough on its own. Droid builds the
# environment it runs every ``Execute`` in by harvesting ``$SHELL -ilc`` (its
# ``loadShellEnvironment``), and on macOS a login shell runs
# ``/usr/libexec/path_helper``, which rebuilds PATH from ``/etc/paths`` and
# appends the inherited entries *after* ``/bin``. That demotes the shim, ``ps``
# resolves to setuid ``/bin/ps`` again, and the supervisor's probe fails exactly
# as it did before the shim existed. This wrapper stands in as ``SHELL`` and
# re-asserts the shim inside the command itself, which runs after the real
# shell's startup files.
_SHELL_WRAPPER_SCRIPT = """#!/bin/sh
# TODO(#22407): Gobby-owned shell wrapper for an upstream Droid defect.
# ON REMOVAL: delete this file with the rest of the shim -- nothing replaces it.
real_shell=${GOBBY_DROID_REAL_SHELL:-/bin/sh}
shim_dir=${GOBBY_DROID_PS_SHIM_DIR:-}

if [ -z "${shim_dir}" ] || [ "$#" -lt 2 ]; then
  exec "${real_shell}" "$@"
fi

case "${real_shell##*/}" in
  fish) shim_prefix="set -x PATH ${shim_dir} \\$PATH" ;;
  *) shim_prefix="PATH=${shim_dir}:\\$PATH" ;;
esac

# Rotate argv so the trailing command operand lands in $1 while the leading
# flags keep their order in the remainder.
argc=$#
rotated=1
while [ "${rotated}" -lt "${argc}" ]; do
  set -- "$@" "$1"
  shift
  rotated=$((rotated + 1))
done
command_operand=$1
shift

# Only -c style invocations carry a command operand worth rewriting. A newline
# separator keeps a command that opens with a comment intact.
for flag do
  case "${flag}" in
    -*c*)
      exec "${real_shell}" "$@" "${shim_prefix}
${command_operand}"
      ;;
  esac
done

exec "${real_shell}" "$@" "${command_operand}"
"""


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
        if not target.exists():
            directory.mkdir(parents=True, exist_ok=True)
            staged = directory / f".ps.{os.getpid()}.tmp"
            try:
                shutil.copyfile(SYSTEM_PS, staged)
                os.chmod(staged, 0o755)  # nosec B103 # mirrors /bin/ps; user-owned home.
                subprocess.run(  # nosec B603 # fixed argv, local codesign, no shell.
                    [str(CODESIGN), "-f", "-s", "-", str(staged)],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
        _write_shell_wrapper(directory)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Droid ps shim unavailable; sandboxed Droid Execute will fail closed: %s", exc
        )
        return None
    return directory


def _write_shell_wrapper(directory: Path) -> None:
    """Materialize the wrapper that re-asserts the shim after shell startup files."""
    target = directory / _SHELL_WRAPPER_NAME
    if target.exists() and target.read_text(encoding="utf-8") == _SHELL_WRAPPER_SCRIPT:
        return
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f".{_SHELL_WRAPPER_NAME}.{os.getpid()}.tmp"
    try:
        staged.write_text(_SHELL_WRAPPER_SCRIPT, encoding="utf-8")
        os.chmod(staged, 0o755)  # nosec B103 # user-owned home; exec'd as $SHELL.
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def ps_shim_env(directory: Path | None, env: Mapping[str, str]) -> dict[str, str]:
    """Return the environment that makes ``ps`` resolve to the shim inside Droid.

    PATH carries the shim into the Droid process itself. ``SHELL`` carries it
    through Droid's own environment harvest, which would otherwise hand every
    ``Execute`` a PATH that ``path_helper`` has already demoted the shim out of.

    A resume re-prepares the launch from an environment this function already
    rewrote, so the incoming ``SHELL`` can be a wrapper from the previous launch.
    Recording that as the real shell would make the wrapper exec itself forever,
    so the previously recorded real shell wins and a wrapper is never adopted.
    """
    if directory is None:
        return {}
    wrapper = directory / _SHELL_WRAPPER_NAME
    existing = env.get("PATH", "")
    overrides = {
        "PATH": f"{directory}{os.pathsep}{existing}" if existing else str(directory),
        "GOBBY_DROID_PS_SHIM_DIR": str(directory),
        "SHELL": str(wrapper),
    }
    real_shell = env.get("GOBBY_DROID_REAL_SHELL") or env.get("SHELL")
    if real_shell and Path(real_shell).name != _SHELL_WRAPPER_NAME:
        overrides["GOBBY_DROID_REAL_SHELL"] = real_shell
    return overrides
