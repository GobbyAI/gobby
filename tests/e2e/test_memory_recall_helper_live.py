"""Opt-in live smoke for memory-recall-helper quality."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import pytest

from tests.e2e.conftest import CLIEventSimulator, DaemonInstance, MCPTestClient
from tests.e2e.test_memory_recall_helper_e2e import (
    _install_memory_helper_content,
    _response_context,
    _unwrap,
    _variables,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("GOBBY_LIVE_MEMORY_HELPER_E2E") != "1",
        reason="set GOBBY_LIVE_MEMORY_HELPER_E2E=1 to run live helper smoke",
    ),
]


TERMINAL_STATUSES = {"success", "error", "timeout", "cancelled"}


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: Any) -> None:
    _install_memory_helper_content(postgres_db)


def _create_memory(
    mcp_client: MCPTestClient,
    *,
    content: str,
    tags: list[str],
    session_id: str,
) -> str:
    result = _unwrap(
        mcp_client.call_tool(
            "gobby-memory",
            "create_memory",
            {
                "content": content,
                "memory_type": "fact",
                "tags": tags,
                "session_id": session_id,
            },
        )
    )
    assert result.get("success", True) is not False, result
    assert not result.get("skipped"), result
    return str(result["memory"]["id"])


def _search_memory_ids(mcp_client: MCPTestClient, query: str) -> list[str]:
    result = _unwrap(
        mcp_client.call_tool(
            "gobby-memory",
            "search_memories",
            {"query": query, "limit": 4, "min_score": 0.0},
        )
    )
    assert result.get("success", True) is not False, result
    return [str(memory["id"]) for memory in result.get("memories", [])]


def _latest_helper_run(postgres_db: Any, parent_session_id: str) -> dict[str, Any] | None:
    row = postgres_db.fetchone(
        """
        SELECT id, status, child_session_id, result, error, prompt
        FROM agent_runs
        WHERE parent_session_id = ? AND agent_name = 'memory-recall-helper'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (parent_session_id,),
    )
    return dict(row) if row is not None else None


def _wait_for_helper(postgres_db: Any, parent_session_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_run: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_run = _latest_helper_run(postgres_db, parent_session_id)
        if last_run and last_run["status"] in TERMINAL_STATUSES:
            return last_run
        time.sleep(2.0)
    pytest.fail(f"memory-recall-helper did not finish within {timeout}s; last_run={last_run}")


def _pending_memory_recall_payloads(
    mcp_client: MCPTestClient,
    parent_session_id: str,
) -> list[dict[str, Any]]:
    result = _unwrap(
        mcp_client.call_tool(
            "gobby-agents",
            "get_inter_session_messages",
            {
                "target_session_id": parent_session_id,
                "direction": "received",
                "undelivered_only": True,
                "limit": 20,
            },
        )
    )
    assert result.get("success", True) is not False, result
    payloads: list[dict[str, Any]] = []
    for message in result.get("messages", []):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "memory_recall":
            payloads.append(parsed)
    return payloads


def _selected_memory_ids(payloads: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for payload in payloads:
        for memory in payload.get("memories") or []:
            if isinstance(memory, dict) and isinstance(memory.get("id"), str):
                ids.append(memory["id"])
    return ids


def _failure_report(
    *,
    expected_id: str,
    expected_content: str,
    selected_ids: list[str],
    fast_recall_ids: list[str],
    payloads: list[dict[str, Any]],
    origin_turn_seq: int,
    daemon_instance: DaemonInstance,
) -> str:
    duplicate_count = len(selected_ids) - len(set(selected_ids))
    stale_count = sum(
        1 for payload in payloads if payload.get("origin_turn_seq") != origin_turn_seq
    )
    expected_hit_rate = 1.0 if expected_id in selected_ids else 0.0
    return (
        "live memory helper smoke failed\n"
        f"expected_id={expected_id}\n"
        f"expected_hit_rate={expected_hit_rate}\n"
        f"helper_selected_ids={selected_ids}\n"
        f"fast_recall_ids={fast_recall_ids}\n"
        f"duplicate_count={duplicate_count}\n"
        f"stale_count={stale_count}\n"
        f"expected_content={expected_content!r}\n"
        f"daemon_logs_tail={daemon_instance.read_logs()[-4000:]}"
    )


class TestLiveMemoryRecallHelper:
    def test_live_helper_selects_expected_memory_and_delivers_next_turn(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
        postgres_db: Any,
    ) -> None:
        project_id = f"memory-helper-live-{uuid.uuid4().hex[:8]}"
        nonce = uuid.uuid4().hex[:10]
        project_result = cli_events.register_test_project(
            project_id=project_id,
            name="Memory Helper Live",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in {"success", "already_exists"}

        parent_external_id = f"memory-live-parent-{nonce}"
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

        tags = ["memory-helper-live", nonce]
        expected_content = (
            f"Memory helper live corpus {nonce}: the VerdantLedger reconciliation "
            "uses invariant omega-731 with nickel cadence for invoice repair."
        )
        expected_id = _create_memory(
            mcp_client,
            content=expected_content,
            tags=tags,
            session_id=parent_session_id,
        )
        for content in (
            f"Memory helper live corpus {nonce}: VerdantLedger references omega-731 "
            "only for dashboard color labels, not invoice repair.",
            f"Memory helper live corpus {nonce}: nickel cadence belongs to release notes "
            "for a different analytics workflow.",
            f"Memory helper live corpus {nonce}: unrelated deployment preference uses "
            "amber queue naming and weekly review.",
        ):
            _create_memory(mcp_client, content=content, tags=tags, session_id=parent_session_id)

        prompt = (
            f"Investigate the VerdantLedger invoice repair path for corpus {nonce}. "
            "I need the memory about the reconciliation invariant and cadence, while "
            "ignoring dashboard colors, release notes, and unrelated deployment facts."
        )
        fast_recall_ids = _search_memory_ids(mcp_client, prompt)[:2]
        first_response = cli_events.user_prompt_submit(
            parent_external_id,
            prompt=prompt,
            source="claude",
            cwd=str(daemon_instance.project_dir),
            project_id=project_id,
        )
        assert first_response.get("continue") is True
        origin_turn_seq = int(_variables(postgres_db, parent_session_id)["parent_turn_seq"])

        run = _wait_for_helper(postgres_db, parent_session_id, timeout=180.0)
        assert run["status"] == "success", (
            f"helper did not succeed: {run}; logs={daemon_instance.read_logs()[-4000:]}"
        )

        payloads = _pending_memory_recall_payloads(mcp_client, parent_session_id)
        selected_ids = _selected_memory_ids(payloads)
        report = _failure_report(
            expected_id=expected_id,
            expected_content=expected_content,
            selected_ids=selected_ids,
            fast_recall_ids=fast_recall_ids,
            payloads=payloads,
            origin_turn_seq=origin_turn_seq,
            daemon_instance=daemon_instance,
        )
        assert expected_id in selected_ids, report

        delivery_response = cli_events.user_prompt_submit(
            parent_external_id,
            prompt="deliver helper context",
            source="claude",
            cwd=str(daemon_instance.project_dir),
            project_id=project_id,
        )
        delivery_context = _response_context(delivery_response)
        assert expected_content in delivery_context, report
        assert delivery_context.count(expected_content) == 1, report
