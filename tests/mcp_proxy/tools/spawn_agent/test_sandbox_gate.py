"""spawn_agent never launches outside managed SRT (placed-agent-launch 1.8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents import srt_runtime
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_gate import SandboxRequiredError, require_managed_srt
from gobby.agents.srt_runtime import SrtRuntimeError
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from tests.servers import test_mcp_programmatic_boundary as programmatic

pytestmark = pytest.mark.unit

_IMPL = "gobby.mcp_proxy.tools.spawn_agent._implementation"
_VERIFIER = "gobby.agents.sandbox_gate.verify_srt_installation"

# The agent-token MCP boundary, reused so 1.8.5 drives the same daemon routes.
boundary = programmatic.boundary


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage.has_active_run_for_task.return_value = False
    return runner


async def _spawn(runner: MagicMock, daemon_config: DaemonConfig) -> dict[str, Any]:
    return await spawn_agent_impl(
        terminal_backend="tmux",
        prompt="Do the thing",
        runner=runner,
        provider="claude",
        parent_session_id="parent-session-xyz",
        daemon_config=daemon_config,
    )


@pytest.mark.parametrize(
    ("agent_sandbox", "granted"),
    [
        pytest.param({"enabled": False}, False, id="disabled"),
        pytest.param({"enabled": True, "backend": "provider-native"}, False, id="provider-native"),
        pytest.param({"enabled": False}, True, id="disabled-with-write-grant"),
    ],
)
async def test_unsandboxed_config_refused_before_side_effects(
    agent_sandbox: dict[str, Any], granted: bool, tmp_path: Path
) -> None:
    runner = _runner()
    grant = {"canonical_roots": [str(tmp_path)]} if granted else None
    with (
        patch(_VERIFIER) as verifier,
        patch(f"{_IMPL}.authorize_write_grant", return_value=grant),
        patch(f"{_IMPL}.get_project_context", return_value={"id": "p", "project_path": "/repo"}),
        patch(f"{_IMPL}.get_machine_id", return_value="21000000-0000-4000-8000-000000000001"),
        patch(f"{_IMPL}.get_isolation_handler") as isolation,
        patch(f"{_IMPL}.prepare_terminal_spawn") as prepare,
        patch(f"{_IMPL}.execute_spawn") as execute,
    ):
        result = await _spawn(runner, DaemonConfig(agent_sandbox=agent_sandbox))

    assert result["success"] is False
    assert result["error_code"] == "sandbox_required"
    verifier.assert_not_called()
    runner.can_spawn.assert_not_called()
    isolation.assert_not_called()
    prepare.assert_not_called()
    execute.assert_not_called()


def test_gate_passes_only_verified_managed_srt() -> None:
    config = SandboxConfig(enabled=True, backend="srt")
    with patch(_VERIFIER) as verifier:
        assert require_managed_srt(config) == config
    verifier.assert_called_once_with()

    with patch(_VERIFIER, side_effect=SrtRuntimeError("manifest mismatch")):
        with pytest.raises(SandboxRequiredError, match="manifest mismatch"):
            require_managed_srt(config)

    with patch(_VERIFIER) as verifier:
        with pytest.raises(SandboxRequiredError, match="managed SRT"):
            require_managed_srt(SandboxConfig(enabled=False, backend="srt"))
    verifier.assert_not_called()


async def test_gate_refuses_when_isolated_srt_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(srt_runtime, "_verified_srt_cache", None)
    runner = _runner()
    with (
        patch(f"{_IMPL}.get_project_context", return_value={"id": "p", "project_path": "/repo"}),
        patch(f"{_IMPL}.get_machine_id", return_value="21000000-0000-4000-8000-000000000001"),
        patch(f"{_IMPL}.get_isolation_handler") as isolation,
        patch(f"{_IMPL}.execute_spawn") as execute,
    ):
        result = await _spawn(runner, DaemonConfig())

    assert result["success"] is False
    assert result["error_code"] == "sandbox_required"
    # The default config is enabled srt, so the refusal is the real verifier's.
    assert result["error"].startswith("managed SRT is unavailable:")
    runner.can_spawn.assert_not_called()
    isolation.assert_not_called()
    execute.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["agent"], indirect=True)
@pytest.mark.parametrize(
    "route", ["/api/mcp/tools/call", f"/api/mcp/{programmatic.SERVER}/tools/echo"]
)
def test_loopback_mcp_calls_are_rule_enforced(boundary: programmatic.Boundary, route: str) -> None:
    """Regression pin: #22961 already enforces agent rules on the REST and CLI routes."""
    blocked = boundary.call(route, {"value": "hello"})

    assert blocked["success"] is False
    assert blocked["error_code"] == "TOOL_BLOCKED"
    assert boundary.target.calls == []
