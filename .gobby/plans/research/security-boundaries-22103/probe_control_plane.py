"""#22103 probes 4 and 5, approval provenance and policy tampering.

Local stub plus static template reads. Matches synthetic requests against the
agent API-token capability matrix, builds an in-memory PipelineExecution,
inspects signatures, and reads bundled agent and rule templates. Sends no
request, approves nothing, toggles nothing, calls no daemon.
Run: uv run python <this file>
"""

import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from starlette.requests import HTTPConnection

from gobby.mcp_proxy.tools.workflows._pipeline_execution import approve_pipeline
from gobby.mcp_proxy.tools.workflows._rules import toggle_rule
from gobby.servers.auth_service import _agent_capability_allows
from gobby.workflows.enforcement.blocking import TASK_MUTATION_TOOLS_BY_SERVER
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

print("== agent API-token route matrix")
for method, path in (
    ("POST", "/api/mcp/tools/call"),
    ("PUT", "/api/rules/block-direct-provider-launch/toggle"),
    ("PUT", "/api/rules/bulk-toggle"),
    ("POST", "/api/pipelines/approve/tok-stub"),
):
    scope = {"type": "http", "method": method, "path": path, "headers": []}
    verdict = "ALLOW" if _agent_capability_allows(HTTPConnection(scope)) else "deny"
    print(f"{verdict:5}  {method} {path}")

print("== approval provenance")
print(f"approve_pipeline{inspect.signature(approve_pipeline)}")
now = datetime.now(UTC)
execution = PipelineExecution(
    id="pe-000000000000",
    pipeline_name="stub",
    project_id="stub",
    status=ExecutionStatus.WAITING_APPROVAL,
    created_at=now,
    updated_at=now,
    resume_token="tok-stub",
)
print(f"PipelineExecution.to_dict() carries resume_token: {'resume_token' in execution.to_dict()}")

print("== policy tampering")
print(f"toggle_rule{inspect.signature(toggle_rule)}")
print(f"TASK_MUTATION_TOOLS_BY_SERVER servers={sorted(TASK_MUTATION_TOOLS_BY_SERVER)}")


def blocked_lists(node: Any, trail: str) -> list[tuple[str, Any, list[str]]]:
    found: list[tuple[str, Any, list[str]]] = []
    if isinstance(node, dict):
        if "blocked_mcp_tools" in node:
            found.append((trail, node.get("allowed_tools"), node["blocked_mcp_tools"]))
        for key, value in node.items():
            found.extend(blocked_lists(value, f"{trail}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            name = value.get("name", index) if isinstance(value, dict) else index
            found.extend(blocked_lists(value, f"{trail}[{name}]"))
    return found


control_tools = {
    f"gobby-workflows:{tool}"
    for tool in ("toggle_rule", "update_rule", "delete_rule", "approve_pipeline")
}
agents_root = Path("src/gobby/install/shared/workflows/agents")
blocked_anywhere: set[str] = set()
for agent_file in sorted(agents_root.glob("*.yaml")):
    for trail, allowed, blocked in blocked_lists(yaml.safe_load(agent_file.read_text()), ""):
        blocked_anywhere.update(blocked)
        if agent_file.stem == "backend-developer" and allowed == "all":
            print(f"backend-developer{trail}: allowed_tools=all blocked_mcp_tools={blocked}")
print(f"control tools blocked by any bundled agent: {sorted(control_tools & blocked_anywhere)}")

rule_file = Path(
    "src/gobby/install/shared/workflows/rules/worker-safety/block-docker-policy-edits.yaml"
)
for line in rule_file.read_text().splitlines():
    if "toggle_rule" in line:
        print(f"block-docker-policy-edits: {line.strip()}")
