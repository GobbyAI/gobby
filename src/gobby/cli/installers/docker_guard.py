"""Fail-closed guard for real Docker execution under GOBBY_TEST_PROTECT."""

from __future__ import annotations

import os
import subprocess  # nosec B404 # identity reference for the real runner only

from gobby.utils.env import is_test_protect_enabled

_TRUTHY = ("1", "true", "yes")
_REAL_SUBPROCESS_RUN = subprocess.run


class DockerTestProtectError(RuntimeError):
    """Raised when a test-protected process would execute a real Docker command."""


def resolves_to_real_run(runner: object) -> bool:
    """Follow the ``__wrapped__`` chain to see whether ``runner`` really executes.

    Delegating wrappers (e.g. the launchctl guard in test conftests) apply
    ``functools.wraps`` and therefore expose the real runner; test stubs and
    mocks do not resolve to it.
    """
    seen: set[int] = set()
    current: object | None = runner
    while current is not None and id(current) not in seen:
        if current is _REAL_SUBPROCESS_RUN:
            return True
        seen.add(id(current))
        current = getattr(current, "__wrapped__", None)
    return False


def ensure_docker_allowed(context: str, *, runner: object) -> None:
    """Fail closed before docker/docker compose execution under GOBBY_TEST_PROTECT.

    The managed-services Compose template pins ``container_name`` and volume
    names globally, and the Compose project label derives from the services
    directory basename, so an invocation from a temporary home still manages
    the production containers. Pass the call site's module-local
    ``subprocess.run`` as ``runner``: a test double passes the guard, the real
    runner fails closed. Fully isolated environments (dedicated Docker
    context, no production daemon) may opt in with ``GOBBY_TEST_ALLOW_DOCKER=1``.
    """
    if not is_test_protect_enabled():
        return
    if not resolves_to_real_run(runner):
        return
    if os.environ.get("GOBBY_TEST_ALLOW_DOCKER", "").lower() in _TRUTHY:
        return
    raise DockerTestProtectError(
        f"GOBBY_TEST_PROTECT blocks real Docker execution ({context}). "
        "Stub subprocess.run in unit tests, or set GOBBY_TEST_ALLOW_DOCKER=1 "
        "in a fully isolated environment."
    )
