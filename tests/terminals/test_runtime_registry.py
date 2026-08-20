"""Runtime registry construction and backend resolution (plan 2.2.4, 2.2.6)."""

from __future__ import annotations

import inspect

import pytest

from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.runtime import TerminalRuntime, UnregisteredBackendError
from tests.terminals.fakes import FakeRuntime

pytestmark = pytest.mark.unit


def test_registry_composition_and_tmux_resolution() -> None:
    tmux = FakeRuntime(backend="tmux")
    registry = TerminalRuntimeRegistry()
    registry.register(tmux)

    assert registry.resolve("tmux") is tmux
    with pytest.raises(UnregisteredBackendError, match="native") as exc_info:
        registry.resolve("native")
    assert exc_info.value.backend == "native"


def test_terminal_runtime_has_no_oneshot_spawn_or_stream() -> None:
    names = {
        name for name, _member in inspect.getmembers(TerminalRuntime) if not name.startswith("_")
    }
    assert "prepare_spawn" in names
    assert "commit_spawn" in names
    assert "spawn" not in names
    assert not any("stream" in name.lower() for name in names)
    assert not any("subscribe" in name.lower() for name in names)
