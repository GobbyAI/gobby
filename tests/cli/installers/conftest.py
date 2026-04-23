"""Shared fixtures for CLI installer tests.

The launchctl guard prevents any test from accidentally executing real
launchctl commands (bootout/bootstrap/kickstart) against the production
daemon, even if subprocess.run mocking is incomplete or forgotten (#10682).
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

# Commands that touch launchd service state — must never run in tests.
_GUARDED_LAUNCHCTL_SUBCOMMANDS = {"bootout", "bootstrap", "kickstart"}


@pytest.fixture(autouse=True)
def _guard_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real launchctl bootout/bootstrap/kickstart during tests.

    Wraps subprocess.run so that any call with a launchctl command
    targeting service state raises instead of executing.  Tests that
    already mock subprocess.run are unaffected — this only catches
    calls that fall through to the real implementation.
    """
    real_run = subprocess.run

    def _guarded_run(args: Any, *posargs: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if isinstance(args, (list, tuple)) and len(args) >= 2:
            if str(args[0]) == "launchctl" and str(args[1]) in _GUARDED_LAUNCHCTL_SUBCOMMANDS:
                raise RuntimeError(
                    f"Test attempted real launchctl {args[1]} — "
                    f"mock subprocess.run in your test to prevent "
                    f"production service disruption."
                )
        return real_run(args, *posargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded_run)


@pytest.fixture(autouse=True)
def _disable_live_claude_statusline_ghook_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep installer unit tests from probing a real ghook binary."""
    from gobby.cli.installers import claude

    monkeypatch.setattr(claude, "resolve_native_bin", lambda name: None)
