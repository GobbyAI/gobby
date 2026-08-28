"""Ownership-slice inventory: 2.1 and 2.4 tmux-field/call sweeps."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_MONITOR_PATHS = (
    ROOT / "src/gobby/agents/agent_health.py",
    ROOT / "src/gobby/agents/lifecycle_monitor.py",
    ROOT / "src/gobby/agents/idle_check_handler.py",
    ROOT / "src/gobby/agents/terminal_prompt_monitor.py",
    ROOT / "src/gobby/agents/agent_cleanup.py",
    ROOT / "src/gobby/agents/kill.py",
    ROOT / "src/gobby/agents/capture.py",
    ROOT / "src/gobby/agents/memory_watchdog.py",
    ROOT / "src/gobby/agents/tmux/pane_monitor.py",
)

_OWNED_CONSUMER_PATHS = _MONITOR_PATHS + (
    ROOT / "src/gobby/events/wake.py",
    ROOT / "src/gobby/hooks/session_coordinator.py",
    ROOT / "src/gobby/hooks/factory.py",
    ROOT / "src/gobby/hooks/hook_manager.py",
    ROOT / "src/gobby/hooks/event_handlers/__init__.py",
    ROOT / "src/gobby/mcp_proxy/tools/agent_cancellation.py",
    ROOT / "src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py",
    ROOT / "src/gobby/mcp_proxy/tools/agents_query_tools.py",
    ROOT / "src/gobby/mcp_proxy/tools/agents_termination.py",
    ROOT / "src/gobby/servers/routes/attention.py",
    ROOT / "src/gobby/servers/websocket/handlers/core.py",
    ROOT / "src/gobby/servers/websocket/tmux.py",
    ROOT / "src/gobby/agents/task_recovery.py",
    ROOT / "src/gobby/agents/terminal_cleanup.py",
    ROOT / "src/gobby/agents/watchdog/recovery.py",
    ROOT / "src/gobby/agents/lifecycle_reconciliation.py",
    ROOT / "src/gobby/mcp_proxy/tools/sessions/_terminal.py",
    ROOT / "src/gobby/mcp_proxy/tools/sessions/_terminal_tmux.py",
    ROOT / "src/gobby/mcp_proxy/tools/sessions/_factory.py",
    ROOT / "src/gobby/mcp_proxy/registries.py",
    ROOT / "src/gobby/servers/http.py",
    ROOT / "src/gobby/mcp_proxy/stdio.py",
    ROOT / "src/gobby/mcp_proxy/stdio_server.py",
    ROOT / "src/gobby/adapters/plan_keystrokes.py",
    ROOT / "src/gobby/servers/websocket/handlers/plan_approval.py",
    ROOT / "src/gobby/servers/websocket/handlers/session_config.py",
    ROOT / "src/gobby/servers/websocket/handlers/session_observe.py",
    ROOT / "src/gobby/servers/websocket/handlers/session_observe_proxy.py",
    ROOT / "src/gobby/sessions/compact_continuation.py",
    ROOT / "src/gobby/runner_init/orchestration.py",
    ROOT / "src/gobby/communications/native_plan_actions.py",
    ROOT / "src/gobby/communications/session_notifications.py",
    ROOT / "src/gobby/communications/telegram_actions.py",
    ROOT / "src/gobby/runner_init/servers.py",
    ROOT / "src/gobby/agents/spawn_executor_support.py",
    ROOT / "src/gobby/runner_lifecycle_shutdown.py",
    ROOT / "src/gobby/runner_lifecycle_agents.py",
    ROOT / "src/gobby/runner_lifecycle_reconcile.py",
)

_WRITE_PATH_CONSUMERS = (
    ROOT / "src/gobby/adapters/plan_keystrokes.py",
    ROOT / "src/gobby/servers/websocket/handlers/plan_approval.py",
    ROOT / "src/gobby/servers/websocket/handlers/session_config.py",
    ROOT / "src/gobby/servers/websocket/handlers/session_observe_proxy.py",
    ROOT / "src/gobby/sessions/compact_continuation.py",
)

_TEST_SEAM_PATHS = (
    ROOT / "tests/mcp_proxy/tools/sessions/test_compact_self_readiness.py",
    ROOT / "tests/servers/test_attention_respond.py",
    ROOT / "tests/servers/test_session_control.py",
    ROOT / "tests/servers/websocket/test_voice_warmup.py",
    ROOT / "tests/sessions/test_liveness_monitor.py",
    ROOT / "tests/agents/test_attention_state.py",
)

_ALLOWED_FIELD_REMAINING = {
    "src/gobby/runner_broadcasting.py",
    "src/gobby/runner_lifecycle_processes.py",
}

_DIRECT_CALL_NEEDLES = (
    ".send_keys(",
    ".capture_pane(",
    ".capture_full_pane(",
    ".kill_session(",
    "get_tmux_manager_for_context",
    "get_tmux_session_manager(",
)

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


def _direct_hits(paths: tuple[Path, ...]) -> list[str]:
    hits: list[str] = []
    for root in paths:
        for path in _iter_files(root):
            text = path.read_text(encoding="utf-8")
            rel = str(path.relative_to(ROOT))
            if "tmux_session_name" in text:
                hits.append(f"{rel}:tmux_session_name")
            for needle in _DIRECT_CALL_NEEDLES:
                if needle in text:
                    hits.append(f"{rel}:{needle}")
    return hits


def test_monitors_use_runtime() -> None:
    hits = _direct_hits(_MONITOR_PATHS)
    assert hits == [], f"monitor/cleanup/kill/capture still use tmux directly: {hits}"


def test_owned_consumers_are_backend_neutral() -> None:
    hits = _direct_hits(_OWNED_CONSUMER_PATHS)
    assert hits == [], f"2.4-owned consumers still use tmux directly: {hits}"


def test_write_path_consumers_are_backend_neutral() -> None:
    hits = _direct_hits(_WRITE_PATH_CONSUMERS)
    leftover: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel.startswith("src/gobby/agents/tmux/"):
            continue
        if ".send_keys(" in path.read_text(encoding="utf-8"):
            leftover.append(rel)
    assert hits == [], f"call-sweep consumers still send through tmux: {hits}"
    assert leftover == [], f"send_keys remains outside tmux backend: {leftover}"


def test_remaining_runtime_test_seams_are_owned() -> None:
    hits = _direct_hits(_TEST_SEAM_PATHS)
    assert hits == [], f"named test seams still patch tmux managers: {hits}"


_FIELD_SWEEP_ALLOWED = {
    "src/gobby/runner_lifecycle_processes.py",
    "src/gobby/agents/spawn_models.py",
    "src/gobby/agents/spawn_executor.py",
    "src/gobby/agents/resume_executor.py",
    "src/gobby/agents/spawners/base.py",
    "src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py",
    "src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py",
    "src/gobby/mcp_proxy/tools/spawn_agent/_execution.py",
    "src/gobby/mcp_proxy/tools/spawn_agent/_health.py",
}


def test_repo_wide_field_sweep_is_empty() -> None:
    import re

    field = re.compile(r"\btmux_session_name\b")
    hits: list[str] = []
    for root_name in ("src", "web"):
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            rel = str(path.relative_to(ROOT))
            if rel.startswith("src/gobby/agents/tmux/"):
                continue
            if rel in _FIELD_SWEEP_ALLOWED:
                continue
            if "terminal_context" in rel or rel.endswith("tmux_context.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if field.search(text):
                hits.append(rel)
    assert hits == [], f"tmux_session_name remains outside the 2.5 remainder: {hits}"
