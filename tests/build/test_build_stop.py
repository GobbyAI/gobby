"""Red tests for project-wide build stop/resume."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_stop_disables_dispatcher_cron() -> None:
    from gobby.build.service import build_stop

    result = build_stop(project_id="project-1")
    assert result.enabled is False


def test_resume_enables_dispatcher_cron() -> None:
    from gobby.build.service import build_resume

    result = build_resume(project_id="project-1")
    assert result.enabled is True


def test_lifecycle_event_appended() -> None:
    from gobby.build.service import build_stop

    result = build_stop(project_id="project-1")
    assert result.lifecycle_event.reason == "gobby build stop"


def test_in_flight_agents_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[str] = []
    monkeypatch.setattr("gobby.build.service.kill_agent", lambda run_id: killed.append(run_id), raising=False)

    from gobby.build.service import build_stop

    build_stop(project_id="project-1")

    assert killed == []


def test_kick_no_op_when_dispatcher_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.build.service import _kick_dispatcher_tick

    assert _kick_dispatcher_tick(dispatcher_enabled=False) == 0


def test_kick_fires_when_dispatcher_enabled() -> None:
    from gobby.build.service import _kick_dispatcher_tick

    assert _kick_dispatcher_tick(dispatcher_enabled=True) == 1


def test_no_task_flag_exposed() -> None:
    from gobby.cli.build import build_stop_command

    param_names = {param.name for param in build_stop_command.params}
    assert "task" not in param_names
