"""Local code-index preflight warnings carry only sanitized text (plan node-halves 3.5.6)."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor_providers import _prepare_managed_code_index
from gobby.agents.spawn_models import SpawnRequest

pytestmark = pytest.mark.unit

SECRETS = ("ABCDEFGHIJKLMNOPQRSTUV", "abcdefghijklmnopqrstuv", "hunter2secret")
SECTION = "\n\n---\n\n## Code Index\n"


@pytest.mark.asyncio
async def test_local_warning_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    context = PreparedSpawn(
        session_id="child",
        agent_run_id="run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={},
        managed_credential=MagicMock(),
    )
    request = SpawnRequest(
        prompt="Persona\n\nImplement the task",
        cwd="/isolated",
        provider="codex",
        session_id="session",
        run_id="run",
        parent_session_id="parent",
        project_id="project",
        session_manager=MagicMock(),
        initial_variables={"additional_skills": ["gobby:references/code-index/overview.md"]},
        code_index_preflight_mode="best_effort",
        prepared_spawn=context,
    )

    async def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "gcode_index_failed:1:sk-ABCDEFGHIJKLMNOPQRSTUV Bearer abcdefghijklmnopqrstuv "
            "https://deploy:hunter2secret@git.example.com/repo.git"
        )

    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.ensure_isolation_code_index", fail_preflight
    )
    variable_manager = MagicMock()
    with patch(
        "gobby.workflows.state_manager.SessionVariableManager", return_value=variable_manager
    ):
        assert await _prepare_managed_code_index(request, context) is None

    sanitized = (
        "gcode_index_failed:1:sk-<redacted> Bearer <redacted> "
        "https://deploy:<redacted>@git.example.com/repo.git"
    )
    assert request.code_index_preflight_warning == {
        "preflight": "code_index",
        "cwd": "/isolated",
        "message": sanitized,
    }
    head, section = request.prompt.split(SECTION)
    assert head == "Persona\n\nImplement the task"
    assert section.endswith(f"Code-index preflight failed: {sanitized}")
    variable_manager.merge_variables.assert_called_once()
    merged = variable_manager.merge_variables.call_args.args[1]
    assert merged["code_index_preflight_warning"] == request.code_index_preflight_warning
    assert merged["prompt"] == request.prompt
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    kept = json.dumps(merged) + request.prompt + logged
    for secret in SECRETS:
        assert secret not in kept
