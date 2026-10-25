"""Pins the Droid isolation hooks the Python oracle writes to a committed golden.

The Rust node repair (crates/gdaemon/tests/node_agents.rs) compares against the
same golden bytes, so both sides must produce identical output for the same
existing user file and the same fixed ghook and hooks paths.
"""

import os
from pathlib import Path

import pytest

from gobby.agents import isolation_repair
from gobby.cli.installers import hook_commands

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "crates/gdaemon/tests/fixtures/node_isolation"
GOLDEN = FIXTURE_DIR / "droid_hooks.json"
EXISTING = FIXTURE_DIR / "droid_hooks_existing.json"
FIXTURE_HOME = "/gobby-fixture/home"
FIXTURE_GHOOK = f"{FIXTURE_HOME}/bin/ghook"
FIXTURE_HOOKS_DIR = f"{FIXTURE_HOME}/hooks"


@pytest.fixture
def pinned_hook_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hook_commands,
        "resolve_native_bin_or_default",
        lambda _name: FIXTURE_GHOOK,
        raising=True,
    )
    monkeypatch.setenv("GOBBY_HOOKS_DIR", FIXTURE_HOOKS_DIR)


def _write_isolated_hooks(workspace: Path) -> bytes:
    hooks_path = workspace / ".factory" / "hooks" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_bytes(EXISTING.read_bytes())
    isolation_repair._write_droid_isolation_hooks(hooks_path)
    return hooks_path.read_bytes()


def test_droid_hooks_golden_matches_python(tmp_path: Path, pinned_hook_paths: None) -> None:
    produced = _write_isolated_hooks(tmp_path)
    if os.environ.get("GOBBY_RECORD_NODE_ISOLATION_GOLDEN") == "1":
        GOLDEN.write_bytes(produced)
    assert produced == GOLDEN.read_bytes()
