"""Build the gdaemon binary that schema-contract calls use during tests.

The test schema authority is this checkout's own gdaemon, built by cargo from the tree
under test. The installed ``~/.gobby/bin`` binary is never used: it can lack migrations
this checkout carries, so a schema applied from it would not describe the code under
test. Cargo reuses the shared target cache, so an up-to-date checkout costs one cargo
check. A failed build or a missing artifact raises CheckoutBinaryError; nothing falls
back to an installed binary.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

CARGO_PACKAGE = "gobby-daemon"
CARGO_BUILD_TIMEOUT_SECONDS = 1800
_BUILT: dict[tuple[str, str, str], Path] = {}


class CheckoutBinaryError(RuntimeError):
    """This checkout's gdaemon could not be built or located."""


def checkout_debug_dir(repo_root: Path, env: Mapping[str, str]) -> Path:
    """Resolve debug artifacts from the configured Cargo target or the checkout."""
    target = Path(env.get("CARGO_TARGET_DIR") or "target")
    if not target.is_absolute():
        target = repo_root / target
    return target / "debug"


def build_checkout_gdaemon(repo_root: Path, env: Mapping[str, str], binary_name: str) -> Path:
    """Build gdaemon from this checkout and return the artifact cargo produced.

    The build runs once per process for each checkout, target directory and binary name.
    """
    key = (str(repo_root), str(checkout_debug_dir(repo_root, env)), binary_name)
    if key not in _BUILT:
        _BUILT[key] = _cargo_build_gdaemon(repo_root, env, binary_name)
    return _BUILT[key]


def _cargo_build_gdaemon(repo_root: Path, env: Mapping[str, str], binary_name: str) -> Path:
    """Run ``cargo build --locked`` for gdaemon and return the executable it reports."""
    command = [
        "cargo",
        "build",
        "-p",
        CARGO_PACKAGE,
        "--bin",
        "gdaemon",
        "--locked",
        "--message-format=json-render-diagnostics",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=CARGO_BUILD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckoutBinaryError(f"could not run `{shlex.join(command)}`: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CheckoutBinaryError(f"`cargo build -p {CARGO_PACKAGE}` failed: {detail}")
    artifact = _artifact_from_cargo_json(result.stdout, binary_name)
    if artifact is None:
        raise CheckoutBinaryError(
            f"`cargo build -p {CARGO_PACKAGE}` produced no gdaemon artifact in {repo_root}"
        )
    return artifact


def _artifact_from_cargo_json(stdout: str, binary_name: str) -> Path | None:
    """Return the executable cargo reports for ``binary_name``, fresh or rebuilt."""
    found: Path | None = None
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        executable = message.get("executable")
        if message.get("reason") == "compiler-artifact" and executable:
            if Path(executable).name == binary_name:
                found = Path(executable)
    return found
