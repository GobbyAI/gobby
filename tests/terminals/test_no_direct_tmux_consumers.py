"""Ownership-slice inventory: 2.1 must not mention tmux_session_name."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SLICE_PATHS = (
    ROOT / "src/gobby/storage/agents",
    ROOT / "src/gobby/runner_init/orchestration.py",
    ROOT / "tests/agents/terminal_fixtures.py",
    ROOT / "tests/agents/test_lifecycle_monitor.py",
    ROOT / "tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py",
    ROOT / "tests/agents/test_lifecycle_monitor_extra.py",
    ROOT / "tests/agents/test_lifecycle_monitor_watchdog_diagnostics.py",
    ROOT / "tests/agents/tmux/test_pane_monitor.py",
    ROOT / "tests/agents/test_memory_watchdog.py",
    ROOT / "tests/agents/test_spawn_executor.py",
    ROOT / "tests/agents/test_resume_executor.py",
    ROOT / "tests/agents/test_capture.py",
    ROOT / "tests/agents/test_kill.py",
    ROOT / "tests/agents/test_agent_cleanup.py",
    ROOT / "tests/agents/test_attention_state.py",
    ROOT / "tests/agents/test_attention_metadata.py",
    ROOT / "tests/mcp_proxy/tools/spawn_agent/test_execution.py",
    ROOT / "tests/mcp_proxy/tools/spawn_agent/test_error_handling.py",
    ROOT / "tests/mcp_proxy/tools/test_agents.py",
    ROOT / "tests/hooks/test_session_coordinator.py",
    ROOT / "tests/servers/routes/test_config_startup_stragglers.py",
    ROOT / "tests/tasks/test_plan_gate.py",
    ROOT / "tests/test_runner_shutdown.py",
    ROOT / "tests/mcp_proxy/tools/test_spawn_agent_speed.py",
    ROOT / "tests/config/test_live_policy_consumers.py",
    ROOT / "tests/agents/test_tmux.py",
    ROOT / "tests/agents/test_terminal_prompt_monitor.py",
    ROOT / "tests/agents/test_lifecycle_task_completion.py",
    ROOT / "tests/test_runner_lifecycle_restart_replay.py",
    ROOT / "tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py",
    ROOT / "tests/mcp_proxy/test_wait_for_output.py",
    ROOT / "tests/mcp_proxy/tools/test_agent_cancellation.py",
    ROOT / "tests/agents/test_spawn_executor_droid.py",
    ROOT / "tests/mcp_proxy/tools/spawn_agent/test_health.py",
    ROOT / "tests/storage/test_agent_capture.py",
    ROOT / "tests/storage/test_storage_agents.py",
    ROOT / "tests/agents/test_lifecycle_monitor_completion.py",
    ROOT / "tests/agents/test_srt_spawn.py",
    ROOT / "tests/agents/test_task_recovery.py",
    ROOT / "tests/e2e/test_build_dispatcher_autonomy.py",
    ROOT / "tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py",
    ROOT / "tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py",
    ROOT / "tests/mcp_proxy/tools/test_cancel_stale_helpers.py",
    ROOT / "tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py",
    ROOT / "tests/storage/test_local_model_flags.py",
)


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(child for child in path.rglob("*") if child.is_file() and child.suffix == ".py")


def test_storage_slice_inventory_is_fully_assigned() -> None:
    hits: list[str] = []
    for root in _SLICE_PATHS:
        for path in _iter_files(root):
            text = path.read_text(encoding="utf-8")
            if "tmux_session_name" in text:
                hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"tmux_session_name remains in 2.1 slice: {hits}"
