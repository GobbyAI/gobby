from __future__ import annotations

import ast
from pathlib import Path

_POLICY_CALLS = {
    "capture_then_kill_async",
    "capture_then_kill_sync",
    "terminate_managed_tmux_async",
    "terminate_managed_runtime_async",
}
_RAW_KILL_ALLOWLIST = {
    ("src/gobby/agents/lifecycle_monitor.py", "AgentLifecycleMonitor.__init__"),
    ("src/gobby/agents/tmux/session_manager.py", "TmuxSessionManager.kill_session"),
    ("src/gobby/agents/tmux/session_manager.py", "TmuxSessionManager.destroy_session"),
    ("src/gobby/agents/tmux/spawner.py", "TmuxSpawner._async_spawn"),
    ("src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py", "_terminate_spawn_process"),
    ("src/gobby/servers/websocket/tmux.py", "TmuxMixin._handle_tmux_kill_session"),
    ("src/gobby/terminals/tmux_runtime.py", "TmuxTerminalRuntime.terminate"),
}


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _contains_raw_kill(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Attribute) and call.func.attr == "kill_session":
        return True
    return any(
        isinstance(node, ast.Constant) and node.value == "kill-session" for node in ast.walk(call)
    )


def _qualified_functions(tree: ast.AST) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    functions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            qualifier = ".".join([*self.classes, node.name])
            functions.append((qualifier, node))

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            qualifier = ".".join([*self.classes, node.name])
            functions.append((qualifier, node))

    Visitor().visit(tree)
    return functions


def test_managed_tmux_kills_route_through_capture_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []

    for path in sorted((root / "src" / "gobby").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for qualifier, function in _qualified_functions(tree):
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            raw_calls = [call for call in calls if _contains_raw_kill(call)]
            if not raw_calls:
                continue
            policy_routed = any(_call_name(call) in _POLICY_CALLS for call in calls)
            if policy_routed or (relative, qualifier) in _RAW_KILL_ALLOWLIST:
                continue
            raw_lines = ",".join(str(call.lineno) for call in raw_calls)
            violations.append(f"{relative}:{qualifier}:{raw_lines}")

    assert violations == [], "raw tmux kill bypasses capture policy: " + "; ".join(violations)
