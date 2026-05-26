"""Deterministic E2E coverage for memory-recall-helper delivery."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.e2e.conftest import CLIEventSimulator, DaemonInstance, MCPTestClient

pytestmark = pytest.mark.e2e


ADAPTER_SOURCES = ("claude", "codex", "droid", "gemini")
MEMORY_HELPER_RULE_NAMES = (
    "increment-parent-turn-seq",
    "cancel-stale-memory-recall-helpers",
    "memory-recall-on-prompt",
    "deliver-pending-messages",
    "spawn-memory-recall-helper",
)


def _install_memory_helper_content(postgres_db: Any) -> None:
    """Install the bundled rows this E2E needs before the isolated daemon starts."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.workflows.sync_rules import sync_bundled_rules

    rules_result = sync_bundled_rules(postgres_db)
    assert rules_result["errors"] == []

    placeholders = ", ".join("?" for _ in MEMORY_HELPER_RULE_NAMES)
    postgres_db.execute(
        f"""
        UPDATE workflow_definitions
        SET enabled = FALSE
        WHERE workflow_type = 'rule'
          AND name NOT IN ({placeholders})
        """,
        tuple(MEMORY_HELPER_RULE_NAMES),
    )

    agents_result = sync_bundled_agents(postgres_db)
    assert agents_result["errors"] == []


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: Any) -> None:
    _install_memory_helper_content(postgres_db)


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    assert result.get("success", True) is not False, result
    return result["result"] if "result" in result else result


def _response_context(response: dict[str, Any]) -> str:
    parts: list[str] = []
    hook_output = response.get("hookSpecificOutput")
    if isinstance(hook_output, dict) and isinstance(hook_output.get("additionalContext"), str):
        parts.append(hook_output["additionalContext"])
    if isinstance(response.get("systemMessage"), str):
        parts.append(response["systemMessage"])
    return "\n\n".join(parts)


def _variables(postgres_db: Any, session_id: str) -> dict[str, Any]:
    return _session_variable_rows(postgres_db, session_id).get(session_id, {})


def _send_message(
    mcp_client: MCPTestClient,
    *,
    from_session: str,
    to_session: str,
    content: str,
) -> None:
    result = _unwrap(
        mcp_client.call_tool(
            "gobby-agents",
            "send_message",
            {
                "from_session": from_session,
                "target": "session",
                "target_id": to_session,
                "content": content,
            },
        )
    )
    assert result.get("success", True) is not False, result
    assert result.get("message_ids"), result


def _memory_payload(memory_id: str, content: str, origin_turn_seq: int) -> str:
    return json.dumps(
        {
            "type": "memory_recall",
            "origin_turn_seq": origin_turn_seq,
            "memories": [
                {
                    "id": memory_id,
                    "content": content,
                    "similarity": 0.99,
                    "search_via": "memory-recall-helper",
                }
            ],
        },
        separators=(",", ":"),
    )


def _register_child_session(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    *,
    project_id: str,
    parent_session_id: str,
    label: str,
) -> str:
    external_id = f"{label}-{uuid.uuid4().hex[:8]}"
    result = cli_events.register_session(
        external_id=external_id,
        machine_id="test-machine",
        source="claude",
        project_id=project_id,
        parent_session_id=parent_session_id,
        cwd=str(daemon_instance.project_dir),
    )
    return str(result["id"])


def _register_helper_run(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    *,
    project_id: str,
    parent_session_id: str,
    label: str,
    status: str,
) -> str:
    child_session_id = _register_child_session(
        cli_events,
        daemon_instance,
        project_id=project_id,
        parent_session_id=parent_session_id,
        label=label,
    )
    cli_events.register_test_agent(
        run_id=f"run-{label}-{uuid.uuid4().hex[:8]}",
        session_id=child_session_id,
        parent_session_id=parent_session_id,
        agent_name="memory-recall-helper",
        status=status,
    )
    return child_session_id


def _assert_memory_helper_rule_order(postgres_db: Any) -> None:
    placeholders = ", ".join("?" for _ in MEMORY_HELPER_RULE_NAMES)
    rows = postgres_db.fetchall(
        f"""
        SELECT name, priority, definition_json
        FROM workflow_definitions
        WHERE workflow_type = 'rule'
          AND enabled = TRUE
          AND name IN ({placeholders})
        """,
        tuple(MEMORY_HELPER_RULE_NAMES),
    )
    rules: dict[str, dict[str, Any]] = {}
    for row in rows:
        definition = row["definition_json"]
        if isinstance(definition, str):
            definition = json.loads(definition)
        rules[row["name"]] = {
            "priority": row["priority"],
            "event": definition.get("event") if isinstance(definition, dict) else None,
        }

    assert rules["increment-parent-turn-seq"]["priority"] == 1
    assert rules["cancel-stale-memory-recall-helpers"]["priority"] == 5
    assert rules["memory-recall-on-prompt"]["priority"] == 10
    assert rules["deliver-pending-messages"]["priority"] == 10
    assert rules["spawn-memory-recall-helper"]["priority"] == 12
    assert {rule["event"] for rule in rules.values()} == {"turn_start"}


def _session_variable_rows(postgres_db: Any, *session_ids: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for session_id in session_ids:
        row = postgres_db.fetchone(
            "SELECT variables FROM session_variables WHERE session_id = ?",
            (session_id,),
        )
        if row is None:
            continue
        raw_variables = row["variables"]
        rows[session_id] = (
            json.loads(raw_variables) if isinstance(raw_variables, str) else raw_variables
        )
    return rows


class TestMemoryRecallHelperE2E:
    def test_cross_cli_delivery_filters_and_tracks_canonical_session(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
        postgres_db: Any,
    ) -> None:
        project_id = f"memory-helper-e2e-{uuid.uuid4().hex[:8]}"
        project_result = cli_events.register_test_project(
            project_id=project_id,
            name="Memory Helper E2E",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in {"success", "already_exists"}

        parent_external_id = f"memory-parent-{uuid.uuid4().hex[:8]}"
        parent = cli_events.register_session(
            external_id=parent_external_id,
            machine_id="test-machine",
            source="claude",
            project_id=project_id,
            cwd=str(daemon_instance.project_dir),
        )
        parent_session_id = str(parent["id"])
        mcp_client.session_id = parent_session_id

        cli_events.session_start(
            parent_external_id,
            source="claude",
            project_id=project_id,
            cwd=str(daemon_instance.project_dir),
            terminal_context={"gobby_session_id": parent_session_id},
        )

        seeded_vars = _variables(postgres_db, parent_session_id)
        assert seeded_vars["memory_recall_helper_enabled"] is True
        assert seeded_vars["parent_turn_seq"] == 0
        _assert_memory_helper_rule_order(postgres_db)

        injected_ids: list[str] = []
        for source in ADAPTER_SOURCES:
            current_vars = _variables(postgres_db, parent_session_id)
            origin_turn_seq = int(current_vars["parent_turn_seq"])

            fresh_id = f"fresh-{source}"
            stale_id = f"stale-{source}"
            cancelled_id = f"cancelled-{source}"
            running_id = f"running-{source}"
            fresh_content = f"fresh-memory-{source}-sentinel"
            stale_content = f"stale-memory-{source}-sentinel"
            cancelled_content = f"cancelled-memory-{source}-sentinel"
            running_content = f"running-memory-{source}-sentinel"
            plain_content = f"plain-message-{source}-sentinel"

            fresh_child = _register_helper_run(
                cli_events,
                daemon_instance,
                project_id=project_id,
                parent_session_id=parent_session_id,
                label=f"{source}-fresh",
                status="success",
            )
            stale_child = _register_helper_run(
                cli_events,
                daemon_instance,
                project_id=project_id,
                parent_session_id=parent_session_id,
                label=f"{source}-stale",
                status="success",
            )
            cancelled_child = _register_helper_run(
                cli_events,
                daemon_instance,
                project_id=project_id,
                parent_session_id=parent_session_id,
                label=f"{source}-cancelled",
                status="cancelled",
            )
            running_child = _register_helper_run(
                cli_events,
                daemon_instance,
                project_id=project_id,
                parent_session_id=parent_session_id,
                label=f"{source}-running",
                status="running",
            )

            _send_message(
                mcp_client,
                from_session=fresh_child,
                to_session=parent_session_id,
                content=_memory_payload(fresh_id, fresh_content, origin_turn_seq),
            )
            _send_message(
                mcp_client,
                from_session=stale_child,
                to_session=parent_session_id,
                content=_memory_payload(stale_id, stale_content, origin_turn_seq + 10),
            )
            _send_message(
                mcp_client,
                from_session=cancelled_child,
                to_session=parent_session_id,
                content=_memory_payload(cancelled_id, cancelled_content, origin_turn_seq),
            )
            _send_message(
                mcp_client,
                from_session=running_child,
                to_session=parent_session_id,
                content=_memory_payload(running_id, running_content, origin_turn_seq),
            )
            _send_message(
                mcp_client,
                from_session=cancelled_child,
                to_session=parent_session_id,
                content=plain_content,
            )

            response = cli_events.user_prompt_submit(
                parent_external_id,
                prompt=f"{source} short turn",
                source=source,
                cwd=str(daemon_instance.project_dir),
                project_id=project_id,
            )
            context = _response_context(response)

            assert fresh_content in context
            assert context.count(fresh_content) == 1
            assert plain_content in context
            assert stale_content not in context
            assert cancelled_content not in context
            assert running_content not in context

            injected_ids.append(fresh_id)
            vars_after = _variables(postgres_db, parent_session_id)
            assert vars_after["parent_turn_seq"] == origin_turn_seq + 1
            assert vars_after["injected_memory_ids"].count(fresh_id) == 1
            assert stale_id not in vars_after["injected_memory_ids"]
            assert cancelled_id not in vars_after["injected_memory_ids"]
            assert running_id not in vars_after["injected_memory_ids"]

        final_vars = _variables(postgres_db, parent_session_id)
        assert final_vars["injected_memory_ids"] == injected_ids

        raw_rows = _session_variable_rows(postgres_db, parent_session_id, parent_external_id)
        assert raw_rows[parent_session_id]["injected_memory_ids"] == injected_ids
        assert parent_external_id not in raw_rows
