"""2.3.1: spawn paths must not construct TmuxSpawner or TmuxSessionManager."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SPAWN_PATHS = (
    ROOT / "src/gobby/agents/spawn_executor.py",
    ROOT / "src/gobby/agents/spawn_executor_providers.py",
    ROOT / "src/gobby/agents/spawn.py",
    ROOT / "src/gobby/agents/spawn_executor_support.py",
    ROOT / "src/gobby/agents/resume_executor.py",
    ROOT / "src/gobby/storage/daemon_resume_keys.py",
    ROOT / "src/gobby/runner_lifecycle_agents.py",
    ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py",
    ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py",
    ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_health.py",
    ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_execution.py",
    ROOT / "src/gobby/dispatch/spawn_actions.py",
    ROOT / "src/gobby/servers/routes/agent_spawn.py",
    ROOT / "src/gobby/agents/dry_run.py",
)

_CONSTRUCTORS = ("TmuxSpawner(", "TmuxSessionManager(")


def test_spawn_paths_use_runtime() -> None:
    hits: list[str] = []
    for path in _SPAWN_PATHS:
        text = path.read_text(encoding="utf-8")
        for ctor in _CONSTRUCTORS:
            if ctor in text:
                hits.append(f"{path.relative_to(ROOT)}:{ctor}")
    assert hits == [], f"spawn paths still construct tmux types: {hits}"


def test_srt_wrap_is_single_chokepoint() -> None:
    wrap_hits: list[str] = []
    for path in (
        ROOT / "src/gobby/agents/spawn_executor.py",
        ROOT / "src/gobby/agents/spawn_executor_providers.py",
        ROOT / "src/gobby/agents/resume_executor.py",
        ROOT / "src/gobby/agents/srt_runtime.py",
    ):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "launch.wrap(" in line or ".wrap(cmd)" in line or ".wrap(command)" in line:
                wrap_hits.append(f"{path.relative_to(ROOT)}:{lineno}:{line.strip()}")
    allowed = {
        "src/gobby/agents/spawn_executor.py",
        "src/gobby/agents/srt_runtime.py",
    }
    unexpected = [hit for hit in wrap_hits if hit.split(":")[0] not in allowed]
    assert unexpected == [], f"wrap sites outside the chokepoint: {unexpected}"
    assert any(hit.startswith("src/gobby/agents/spawn_executor.py") for hit in wrap_hits)
    assert any("_preflight_srt" in hit or "preflight" in hit for hit in wrap_hits)


def test_identity_generation_absent_from_runtimes() -> None:
    runtime_root = ROOT / "src/gobby/terminals"
    hits: list[str] = []
    for path in runtime_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "uuid.uuid4" in text or "uuid4()" in text:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"TerminalRuntime modules mint identities: {hits}"
