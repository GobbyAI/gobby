"""Provider children must not inherit managed capabilities from the daemon."""

import sys
from pathlib import Path

import pytest

from gobby.ai._text_generation_adapters import (
    _droid_isolated_env,
    _run_cli_text_generation_command,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [{}, {"GOBBY_AGENT_API_TOKEN": "dummy-overlay-token"}])
async def test_cli_child_has_no_parent_or_overlay_agent_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlay: dict[str, str]
) -> None:
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "dummy-parent-token")
    output = await _run_cli_text_generation_command(
        "dummy-provider",
        [sys.executable, "-c", "import os; print('GOBBY_AGENT_API_TOKEN' in os.environ)"],
        neutral_cwd=tmp_path,
        timeout_seconds=5,
        env_overrides={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/dummy/own/grant.json", **overlay},
    )

    assert output.strip() == "False"


def test_droid_child_keeps_envelope_path_without_parent_token(tmp_path: Path) -> None:
    env = _droid_isolated_env(
        {
            "GOBBY_AGENT_API_TOKEN": "dummy-parent-token",
            "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/dummy/own/grant.json",
        },
        tmp_path / "home",
    )

    assert "GOBBY_AGENT_API_TOKEN" not in env
    assert "dummy-parent-token" not in env.values()
    assert env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] == "/dummy/own/grant.json"
