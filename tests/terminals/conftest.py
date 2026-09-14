"""Shared fixtures for the cross-backend runtime contract suite (plan 5.1)."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.guard_set_g import (
    durable_hosts as _durable_hosts,
    finalize_pidfile_host as _finalize_pidfile_host,
    leaked_hosts,
    snapshot_gterm_hosts as _gterm_hosts,
)
from tests.native_binary_selection import (
    NativeBinarySelectionError,
    select_native_binary,
)


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Name the exact gterm used by the runtime contract suite."""
    del config
    try:
        selected = select_native_binary("gterm", required=False)
    except NativeBinarySelectionError as exc:
        raise pytest.UsageError(f"terminal binary selection failed: {exc}") from exc
    return [selected.header()] if selected is not None else []


@pytest.fixture(scope="session", autouse=True)
def _assert_no_leaked_hosts(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Guard set G group 7 for this suite: no host leaks, no durable host ends.

    A durable host (state directory outside every temp root, such as the daemon's
    ``~/.gobby`` host) that exists before the session must still exist after it;
    ending one would take every native terminal on the machine down with it.
    Ownership and cleanup come from ``gobby.guard_set_g`` so the executable
    guard and this fixture cannot drift.
    """
    before = _gterm_hosts()
    yield

    run_tmp = os.environ.get("CLAUDE_CODE_TMPDIR")
    roots: tuple[Path, ...] = (tmp_path_factory.getbasetemp().resolve(),)
    if run_tmp:
        roots += (Path(run_tmp).resolve(),)
    temp_roots: tuple[Path, ...] = (
        *roots,
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
    )
    after = _gterm_hosts()
    leaked = leaked_hosts(before, after, roots)
    for pid, socket_dir in leaked.items():
        _finalize_pidfile_host(pid, socket_dir, roots)

    final = _gterm_hosts()
    remaining = leaked_hosts(before, final, roots)
    assert not remaining, (
        "tests/terminals leaked gterm host processes that use this session's temp roots: "
        f"before={sorted(before)} after={sorted(final)} remaining={sorted(remaining)}"
    )
    ended = {
        pid: socket_dir
        for pid, socket_dir in _durable_hosts(before, temp_roots).items()
        if final.get(pid) != socket_dir
    }
    assert not ended, (
        "tests/terminals ended durable gterm host processes it did not start: "
        f"before={sorted(before)} after={sorted(final)} ended={sorted(ended)}"
    )


def gterm_binary() -> Path | None:
    """Return the explicit or installed gterm, if the default is absent."""
    selected = select_native_binary("gterm", required=False)
    return selected.path if selected is not None else None


def require_backend(backend: str) -> None:
    """Skip a contract cell when its real backend binary is absent."""
    if backend == "tmux" and shutil.which("tmux") is None:
        pytest.skip("tmux binary is not available")
    if backend == "native" and gterm_binary() is None:
        pytest.skip("gterm binary is not available")


@pytest.fixture(params=["tmux", "native"])
def contract_backend(request: pytest.FixtureRequest) -> str:
    backend = str(request.param)
    require_backend(backend)
    return backend
