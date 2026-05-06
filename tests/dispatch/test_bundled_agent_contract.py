from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

pytestmark = pytest.mark.unit

AGENTS_DIR = Path("src/gobby/install/shared/workflows/agents")
# Keep this local until the runtime exposes a shared external MCP schema provider.
EXTERNAL_TOOL_INVENTORY = {
    "github": {
        "create_pull_request",
        "create_pull_request_review",
        "get_pull_request",
        "get_pull_request_reviews",
        "get_pull_request_status",
        "merge_pull_request",
        "update_pull_request_branch",
    }
}
REMOVED_LIFECYCLE_TOOLS = (
    "mark_task_pr_opened",
    "mark_task_merged",
    "mark_task_merge_failed",
    "advance_lifecycle",
)
SELF_TERMINATION_KILL_AGENT_PHRASES = (
    "call kill_agent to terminate",
    "call `kill_agent` to terminate",
    "call kill_agent to shut down",
    "without calling kill_agent",
    "requires `kill_agent`",
    "requires kill_agent",
    "self-terminate by calling kill_agent",
)


def _tool_inventory(
    temp_db, temp_dir: Path, sample_project: dict[str, object]
) -> dict[str, set[str]]:
    from gobby.config.app import DaemonConfig
    from gobby.mcp_proxy.registries import setup_internal_registries
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.sync.tasks import TaskSyncManager
    from gobby.worktrees.merge.resolver import MergeResolver

    task_manager = LocalTaskManager(temp_db)
    manager = setup_internal_registries(
        DaemonConfig(),
        task_manager=task_manager,
        sync_manager=TaskSyncManager(task_manager, str(temp_dir / "tasks.jsonl")),
        session_manager=SessionManager(temp_db),
        db=temp_db,
        worktree_storage=LocalWorktreeManager(temp_db),
        clone_storage=LocalCloneManager(temp_db),
        merge_storage=MergeResolutionManager(temp_db),
        merge_resolver=MergeResolver(),
        agent_runner=MagicMock(),
        project_id=str(sample_project["id"]),
    )
    inventory = {
        registry.name: {str(tool["name"]) for tool in registry.list_tools()}
        for registry in manager.get_all_registries()
    }
    inventory.update(EXTERNAL_TOOL_INVENTORY)
    return inventory


def _agent_yaml_files() -> list[Path]:
    return sorted(path for path in AGENTS_DIR.glob("*.yaml") if path.is_file())


def _all_agent_yaml_files() -> list[Path]:
    return sorted(path for path in AGENTS_DIR.rglob("*.yaml") if path.is_file())


def test_bundled_agent_mcp_references_match_registered_tool_inventory(
    temp_db,
    temp_dir: Path,
    sample_project,
) -> None:
    inventory = _tool_inventory(temp_db, temp_dir, sample_project)
    missing: list[str] = []
    handler_not_allowed: list[str] = []

    for path in _agent_yaml_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for step in data.get("steps") or []:
            step_name = step.get("name", "<unnamed>")
            allowed = step.get("allowed_mcp_tools")
            allowed_set = set(allowed) if isinstance(allowed, list) else None
            for field_name in ("allowed_mcp_tools", "blocked_mcp_tools"):
                for ref in _mcp_tool_refs(step.get(field_name)):
                    if not _tool_ref_exists(ref, inventory):
                        missing.append(f"{path.name}:{step_name}:{field_name}:{ref}")
            for field_name in ("on_mcp_success", "on_mcp_error"):
                for ref in _handler_refs(step.get(field_name)):
                    if not _tool_ref_exists(ref, inventory):
                        missing.append(f"{path.name}:{step_name}:{field_name}:{ref}")
                    if allowed_set is not None and ref not in allowed_set:
                        handler_not_allowed.append(f"{path.name}:{step_name}:{field_name}:{ref}")

    assert missing == []
    assert handler_not_allowed == []


def test_bundled_agent_and_skill_assets_do_not_reference_removed_lifecycle_tools() -> None:
    paths = [
        *AGENTS_DIR.glob("*.yaml"),
        *Path("src/gobby/install/shared/skills").rglob("SKILL.md"),
    ]
    offenders: list[str] = []
    for path in sorted(paths):
        text = path.read_text(encoding="utf-8")
        for tool_name in REMOVED_LIFECYCLE_TOOLS:
            if tool_name in text:
                offenders.append(f"{path}:{tool_name}")

    assert offenders == []


def test_bundled_agents_use_end_agent_run_for_self_termination() -> None:
    offenders: list[str] = []

    for path in _all_agent_yaml_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        agent_blocked_tools = set(_mcp_tool_refs(data.get("blocked_mcp_tools")))
        if path.name == "default.yaml":
            if "gobby-agents:kill_agent" in agent_blocked_tools:
                offenders.append(f"{path.name}:agent:blocks kill_agent")
        elif "gobby-agents:kill_agent" not in agent_blocked_tools:
            offenders.append(f"{path.name}:agent:missing kill_agent block")

        _collect_self_termination_phrase_offenders(
            offenders,
            path=path,
            field_name="instructions",
            text=str(data.get("instructions") or ""),
        )

        for step in data.get("steps") or []:
            if not isinstance(step, dict) or step.get("name") != "terminate":
                continue

            allowed_tools = set(_mcp_tool_refs(step.get("allowed_mcp_tools")))
            if "gobby-agents:kill_agent" in allowed_tools:
                offenders.append(f"{path.name}:terminate:allows kill_agent")
            if "gobby-agents:end_agent_run" not in allowed_tools:
                offenders.append(f"{path.name}:terminate:missing end_agent_run")

            terminate_text = "\n".join(
                str(step.get(field_name) or "")
                for field_name in ("description", "status_message")
            )
            _collect_self_termination_phrase_offenders(
                offenders,
                path=path,
                field_name="terminate",
                text=terminate_text,
            )

    assert offenders == []


def _mcp_tool_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and ":" in item]


def _handler_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        server = item.get("server")
        tool = item.get("tool")
        if isinstance(server, str) and isinstance(tool, str):
            refs.append(f"{server}:{tool}")
    return refs


def _tool_ref_exists(ref: str, inventory: dict[str, set[str]]) -> bool:
    server, tool = ref.split(":", 1)
    return server in inventory and (tool == "*" or tool in inventory[server])


def _collect_self_termination_phrase_offenders(
    offenders: list[str],
    *,
    path: Path,
    field_name: str,
    text: str,
) -> None:
    normalized = " ".join(text.lower().split())
    for phrase in SELF_TERMINATION_KILL_AGENT_PHRASES:
        if phrase in normalized:
            offenders.append(f"{path.name}:{field_name}:{phrase}")
