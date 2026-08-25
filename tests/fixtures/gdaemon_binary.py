"""Select the gdaemon binary that schema-contract calls use during tests.

The installed ``~/.gobby/bin`` binary is the default: it is what the daemon and every
CLI path use, and gdaemon checks its embedded schema identity against this checkout's
pin, so a mismatch fails loudly instead of testing the wrong migrations. A branch that
carries unreleased migrations cannot use the installed binary without cutting the whole
machine over, so ``GOBBY_TEST_GDAEMON=checkout`` opts into the checkout's own debug
build — and that build must be newer than the crate sources it was built from.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

CHECKOUT_BINARY_ENV = "GOBBY_TEST_GDAEMON"
CHECKOUT_BINARY_VALUE = "checkout"
INSTALLED_BINARY_VALUE = "installed"
SOURCE_DIRS = ("crates/gcore", "crates/gdaemon")


class CheckoutBinaryError(RuntimeError):
    """The checkout's debug gdaemon was requested but cannot be trusted."""


def newest_source(repo_root: Path) -> tuple[Path, int] | None:
    """Return the newest file under the gdaemon crate sources and its mtime in ns."""
    newest: tuple[Path, int] | None = None
    for relative in SOURCE_DIRS:
        for path in (repo_root / relative).rglob("*"):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime_ns
            if newest is None or mtime > newest[1]:
                newest = (path, mtime)
    return newest


def select_test_gdaemon(
    repo_root: Path,
    env: Mapping[str, str],
    binary_name: str,
) -> Path | None:
    """Return the checkout debug binary when opted in, else None for the installed one.

    Raises CheckoutBinaryError when the opt-in names a missing or stale build, and
    ValueError for an unrecognized selector value, so a typo never silently falls back.
    """
    selector = env.get(CHECKOUT_BINARY_ENV, "")
    if selector in ("", INSTALLED_BINARY_VALUE):
        return None
    if selector != CHECKOUT_BINARY_VALUE:
        raise ValueError(
            f"{CHECKOUT_BINARY_ENV}={selector!r} is not recognized; use "
            f"{CHECKOUT_BINARY_VALUE!r} or {INSTALLED_BINARY_VALUE!r}"
        )
    binary = repo_root / "target" / "debug" / binary_name
    if not binary.is_file():
        raise CheckoutBinaryError(
            f"{CHECKOUT_BINARY_ENV}={CHECKOUT_BINARY_VALUE} but {binary} does not exist; "
            "run `cargo build -p gobby-daemon`"
        )
    newest = newest_source(repo_root)
    if newest is not None and newest[1] > binary.stat().st_mtime_ns:
        raise CheckoutBinaryError(
            f"{binary} is older than {newest[0]}; run `cargo build -p gobby-daemon` "
            "before testing against the checkout binary"
        )
    return binary
