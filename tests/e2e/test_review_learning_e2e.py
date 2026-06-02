"""E2E coverage for the review-learning MCP registry."""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
)
from gobby.storage.config_store import ConfigStore
from tests.e2e.conftest import DaemonInstance, MCPTestClient, find_free_port

pytestmark = pytest.mark.e2e

REVIEW_LEARNING_SERVER = "gobby-review-learning"
PATTERN_ID = "review-learning-e2e-durable-write"
FINDING_FINGERPRINT = "review-learning-e2e-fingerprint"
PATTERN_TAG = f"pattern:{PATTERN_ID}"
EMBEDDING_MODEL = "gobby-e2e-embed"
EMBEDDING_DIM = 4


class _FakeEmbeddingHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible embeddings endpoint for the test daemon."""

    def do_POST(self) -> None:
        if self.path != "/v1/embeddings":
            self._send_json({"error": "not found"}, status=404)
            return

        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(body.decode("utf-8")) if body else {}
        inputs = payload.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]

        self._send_json(
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": _embedding_for_text(str(text)),
                    }
                    for index, text in enumerate(inputs)
                ],
                "model": payload.get("model", EMBEDDING_MODEL),
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        response = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def _embedding_for_text(text: str) -> list[float]:
    seed = sum(text.encode("utf-8")) or 1
    return [float((seed + offset) % 17 + 1) / 17.0 for offset in range(EMBEDDING_DIM)]


@pytest.fixture
def fake_embedding_api() -> Generator[str]:
    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeEmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def e2e_pre_daemon_setup(fake_embedding_api: str, postgres_db: Any) -> None:
    config_store = ConfigStore(postgres_db)
    config_store.set_many(
        {
            AI_EMBEDDING_API_BASE_KEY: fake_embedding_api,
            AI_EMBEDDING_MODEL_KEY: EMBEDDING_MODEL,
            AI_EMBEDDING_DIM_KEY: EMBEDDING_DIM,
        },
        source="test",
    )


def _tool_result(response: dict[str, Any]) -> dict[str, Any]:
    """Extract the internal MCP tool payload from the daemon wrapper."""
    if "result" not in response:
        return response

    result = response["result"]
    if response.get("success") is True and isinstance(result, dict) and "success" not in result:
        return {"success": True, **result}
    if isinstance(result, dict):
        return result
    return {"result": result}


def _assert_not_failed(result: dict[str, Any]) -> None:
    assert result.get("success") is not False, result
    assert "error" not in result, result


def _finding(
    *,
    pattern_id: str = PATTERN_ID,
    fingerprint: str = FINDING_FINGERPRINT,
    path: str = "src/gobby/tasks/state.py",
    symbol: str = "TaskStateStore.save",
) -> dict[str, Any]:
    return {
        "title": "Durable task state write missing",
        "message": "State changed in memory without a durable task-store write.",
        "pattern_id": pattern_id,
        "finding_fingerprint": fingerprint,
        "lesson_type": "durable-writes",
        "principle": "Persist state after changing task state.",
        "root_cause": "The transition mutated state before storage persisted it.",
        "prevention": "Add regression coverage around durable task-state writes.",
        "path": path,
        "symbol": symbol,
        "rule_id": "review-learning-e2e/durable-write",
        "diagnostic_format": "raw",
        "query_hints": [pattern_id, fingerprint, path, symbol],
    }


def _record_review_lesson(
    mcp_client: MCPTestClient,
    *,
    source_review: str,
    finding: dict[str, Any] | None = None,
    source_kind: str = "agent_review",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = mcp_client.call_tool(
        server_name=REVIEW_LEARNING_SERVER,
        tool_name="record_review_lesson",
        arguments={
            "source_kind": source_kind,
            "source": "e2e-reviewer",
            "source_review": source_review,
            "decision": "confirmed",
            "finding": finding or _finding(),
            "evidence": evidence
            if evidence is not None
            else {"commit_sha": f"{source_review}-commit"},
            "repo": "gobby",
            "language": "python",
            "risk": "medium",
        },
    )
    result = _tool_result(response)
    _assert_not_failed(result)
    return result


def _list_lesson_memories(mcp_client: MCPTestClient, pattern_id: str) -> list[dict[str, Any]]:
    response = mcp_client.call_tool(
        server_name="gobby-memory",
        tool_name="list_memories",
        arguments={
            "memory_type": "pattern",
            "limit": 20,
            "tags_all": ["review-lesson", f"pattern:{pattern_id}"],
        },
    )
    result = _tool_result(response)
    _assert_not_failed(result)
    return result["memories"]


class TestReviewLearningMCP:
    def test_review_learning_discovery_and_schema(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        servers = mcp_client.list_servers()
        server_names = {server.get("name") for server in servers}
        assert REVIEW_LEARNING_SERVER in server_names

        tools = mcp_client.list_tools(REVIEW_LEARNING_SERVER)
        tool_names = {tool["name"] for tool in tools}
        expected_tools = {"recall_review_context", "record_review_lesson"}
        assert expected_tools.issubset(tool_names)

        for tool_name in expected_tools:
            schema = mcp_client.get_tool_schema(REVIEW_LEARNING_SERVER, tool_name)
            assert schema.get("name") == tool_name
            assert schema.get("inputSchema", {}).get("type") == "object"

    def test_review_learning_records_promotes_and_recalls(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        first = _record_review_lesson(
            mcp_client,
            source_review="review-learning-e2e-review-1",
        )

        assert first["pattern_id"] == PATTERN_ID
        assert first["finding_fingerprint"] == FINDING_FINGERPRINT
        assert first["occurrence_count"] == 1
        assert first["guardrail_target"] is None

        memories = _list_lesson_memories(mcp_client, PATTERN_ID)
        assert len(memories) == 1
        lesson = memories[0]
        assert lesson["id"] == first["lesson_id"]
        assert {"review-lesson", "confirmed", PATTERN_TAG}.issubset(set(lesson["tags"]))
        assert FINDING_FINGERPRINT in lesson["content"]

        second = _record_review_lesson(
            mcp_client,
            source_review="review-learning-e2e-review-2",
        )

        assert second["occurrence_count"] == 2
        assert second["guardrail_target"] == "test"
        assert second["task_id"]

        memories = _list_lesson_memories(mcp_client, PATTERN_ID)
        assert len(memories) == 2

        task_response = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="get_task",
            arguments={"task_id": second["task_id"], "brief": False},
        )
        task = _tool_result(task_response)
        _assert_not_failed(task)

        labels = set(task["labels"])
        assert task["category"] == "test"
        assert {"guardrail", "review-learning", PATTERN_TAG, "target:test"}.issubset(labels)
        assert PATTERN_ID in task["title"]
        assert "guardrail_target: test" in task["description"]

        recall_response = mcp_client.call_tool(
            server_name=REVIEW_LEARNING_SERVER,
            tool_name="recall_review_context",
            arguments={
                "findings": [_finding()],
                "source": "e2e-reviewer",
                "source_kind": "agent_review",
                "repo": "gobby",
                "language": "python",
            },
        )
        recall = _tool_result(recall_response)
        _assert_not_failed(recall)

        lesson_matches = [
            match
            for match in recall["matches"]
            if {"review-lesson", PATTERN_TAG}.issubset(set(match["tags"]))
        ]
        assert lesson_matches, recall

    def test_review_learning_skips_ci_lessons_without_verified_fix(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        pattern_id = "review-learning-e2e-ci-unverified"
        result = _record_review_lesson(
            mcp_client,
            source_kind="test_failure",
            source_review="review-learning-e2e-ci-review",
            finding=_finding(pattern_id=pattern_id, fingerprint="review-learning-e2e-ci"),
            evidence={},
        )

        assert result["skipped_reason"] == "missing_verified_fix"
        assert _list_lesson_memories(mcp_client, pattern_id) == []
