"""Agent spawn resolves the native terminal runtime."""

from __future__ import annotations

from typing import cast

import pytest

from gobby.agents.spawn_executor import resolve_terminal_services
from gobby.agents.spawn_models import SpawnRequest
from gobby.storage.terminals import TerminalManager
from gobby.terminals.runtime import TerminalRuntime
from tests.agents.prepared_spawn import prepared_spawn
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit


def test_resume_shaped_spawn_resolves_the_native_runtime() -> None:
    native = cast(TerminalRuntime, FakeRuntime(backend="native"))
    # Resume builds its SpawnRequest without naming a backend.
    request = SpawnRequest(
        prompt="continue",
        cwd="/tmp/work",
        provider="claude",
        session_id="child-session",
        run_id="run-1",
        parent_session_id="parent-session",
        project_id="project-1",
        prepared_spawn=prepared_spawn(),
        terminal_manager=cast(TerminalManager, object()),
        terminal_runtime_registry=runtime_registry(native),
    )

    _, _, runtime, backend = resolve_terminal_services(request)

    assert backend == "native"
    assert runtime is native
