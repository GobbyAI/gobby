"""Isolated-daemon coverage for the transcript-backed close checklist."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
)

pytestmark = pytest.mark.e2e

PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
VALIDATION_COMMAND = "uv run pytest tests/e2e/test_task_close_checklist_e2e.py -q"


class _ValidationLLMServer(ThreadingHTTPServer):
    validation_calls: int
    requests: list[dict[str, Any]]


class _ValidationLLMHandler(BaseHTTPRequestHandler):
    server: _ValidationLLMServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        self.server.validation_calls += 1
        self.server.requests.append(cast(dict[str, Any], request))
        verdict = {
            "status": "valid",
            "criteria": [{"index": 1, "satisfied": True, "gap": None}],
            "feedback": "The linked diff coherently satisfies the criterion.",
        }
        response = {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "e2e-validation",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(verdict),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


@pytest.fixture
def validation_llm_server() -> Generator[_ValidationLLMServer]:
    server = _ValidationLLMServer(("127.0.0.1", 0), _ValidationLLMHandler)
    server.validation_calls = 0
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def e2e_pre_daemon_setup(
    e2e_config: tuple[Path, int, int],
    validation_llm_server: _ValidationLLMServer,
    postgres_db: Any,
) -> None:
    """Enable close validation against the local deterministic LLM stub."""
    from gobby.prompts.sync import sync_bundled_prompts

    sync_bundled_prompts(postgres_db)
    config_path, _http_port, _ws_port = e2e_config
    config = cast(dict[str, Any], yaml.safe_load(config_path.read_text()))
    validation = config["gobby_tasks"]["validation"]
    validation["enabled"] = True
    validation["candidates"] = ["endpoint:e2e/e2e-validation"]
    config["ai"] = {
        "generation": {
            "timeout_seconds": 15,
            "candidate_timeout_seconds": 5,
            "cli_candidate_timeout_seconds": 5,
            "endpoints": {
                "e2e": {
                    "protocol": "openai-compatible",
                    "wire_api": "chat-completions",
                    "api_base": (f"http://127.0.0.1:{validation_llm_server.server_port}/v1"),
                    "model": "e2e-validation",
                }
            },
        }
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return cast(dict[str, Any], nested) if isinstance(nested, dict) else result


def _register_claimed_task(
    daemon: DaemonInstance,
    client: MCPTestClient,
    events: CLIEventSimulator,
    *,
    suffix: str,
    task_type: str = "task",
) -> tuple[str, str, str]:
    project_result = events.register_test_project(
        project_id=PROJECT_ID,
        name="E2E Test Project",
        repo_path=str(daemon.project_dir),
    )
    assert project_result["status"] in {"success", "already_exists"}
    external_id = f"close-checklist-{suffix}-{uuid.uuid4().hex[:8]}"
    session = events.register_session(
        external_id=external_id,
        source="codex",
        project_id=PROJECT_ID,
        cwd=str(daemon.project_dir),
    )
    session_id = cast(str, session["id"])
    client.session_id = session_id
    task = _unwrap(
        client.call_tool(
            "gobby-tasks",
            "create_task",
            {
                "title": f"Close checklist E2E {suffix}",
                "task_type": task_type,
                "category": "code",
                "implementation_domain": "backend",
                "validation_criteria": "The committed Python module contains VALUE = 1.",
                "claim": True,
            },
        )
    )
    assert task.get("id"), task
    return cast(str, task["id"]), session_id, external_id


def _record_edit(
    *,
    postgres_db: Any,
    session_id: str,
    relative_path: str,
    old: str,
    new: str,
) -> dict[str, Any]:
    from gobby.workflows.state_manager import SessionVariableManager

    patch = (
        f"*** Begin Patch\n*** Update File: {relative_path}\n@@\n-{old}\n+{new}\n*** End Patch\n"
    )
    assert SessionVariableManager(postgres_db).record_edited_file(session_id, relative_path)
    return {
        "type": "custom_tool_call",
        "call_id": f"patch-{uuid.uuid4().hex}",
        "name": "apply_patch",
        "input": patch,
    }


def _validation_events(
    *,
    outcome: int,
    timestamp: datetime,
) -> list[dict[str, Any]]:
    call_id = f"exec-{uuid.uuid4().hex}"
    return [
        {
            "type": "response_item",
            "timestamp": timestamp.isoformat(),
            "payload": {
                "type": "function_call",
                "call_id": call_id,
                "name": "exec_command",
                "arguments": json.dumps({"cmd": VALIDATION_COMMAND}),
            },
        },
        {
            "type": "response_item",
            "timestamp": (timestamp + timedelta(milliseconds=100)).isoformat(),
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": {
                    "exit_code": outcome,
                    "output": "passed" if outcome == 0 else "failed",
                },
            },
        },
    ]


def _response_item(payload: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    return {"type": "response_item", "timestamp": timestamp.isoformat(), "payload": payload}


def _write_transcript(
    daemon: DaemonInstance,
    external_id: str,
    records: list[dict[str, Any]],
) -> None:
    timestamp = datetime.now(UTC)
    directory = (
        daemon.gobby_home
        / ".codex"
        / "sessions"
        / str(timestamp.year)
        / f"{timestamp.month:02d}"
        / f"{timestamp.day:02d}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    transcript = directory / f"rollout-{external_id}.jsonl"
    transcript.write_text("".join(f"{json.dumps(record)}\n" for record in records))


def _commit(project: Path, message: str) -> str:
    subprocess.run(["git", "add", "src/close_checklist.py"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=project, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _close(client: MCPTestClient, task_id: str, commit_sha: str) -> dict[str, Any]:
    return _unwrap(
        client.call_tool(
            "gobby-tasks",
            "close_task",
            {
                "task_id": task_id,
                "commit_sha": commit_sha,
                "changes_summary": "Added and validated the requested Python module.",
                "preview": True,
                "response_detail": "diagnostic",
            },
        )
    )


def test_ready_codex_task_closes_with_one_llm_call(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    validation_llm_server: _ValidationLLMServer,
    postgres_db: Any,
) -> None:
    task_id, session_id, external_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="ready"
    )
    source = daemon_instance.project_dir / "src" / "close_checklist.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    timestamp = datetime.now(UTC) + timedelta(seconds=1)
    edit = _record_edit(
        postgres_db=postgres_db,
        session_id=session_id,
        relative_path="src/close_checklist.py",
        old="",
        new="VALUE = 1",
    )
    _write_transcript(
        daemon_instance,
        external_id,
        [
            _response_item(edit, timestamp),
            *_validation_events(outcome=0, timestamp=timestamp + timedelta(seconds=1)),
        ],
    )
    commit_sha = _commit(daemon_instance.project_dir, "Add close checklist module")

    started = time.monotonic()
    result = _close(mcp_client, task_id, commit_sha)

    assert result["closed"] is True, result
    assert result["can_close"] is True
    assert time.monotonic() - started < 120
    assert validation_llm_server.validation_calls == 1
    assert len(validation_llm_server.requests) == 1


def test_dirty_task_file_blocks_before_llm(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    validation_llm_server: _ValidationLLMServer,
    postgres_db: Any,
) -> None:
    task_id, session_id, external_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="dirty"
    )
    source = daemon_instance.project_dir / "src" / "close_checklist.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    timestamp = datetime.now(UTC) + timedelta(seconds=1)
    first_edit = _record_edit(
        postgres_db=postgres_db,
        session_id=session_id,
        relative_path="src/close_checklist.py",
        old="",
        new="VALUE = 1",
    )
    commit_sha = _commit(daemon_instance.project_dir, "Add close checklist module")
    source.write_text("VALUE = 2\n")
    second_edit = _record_edit(
        postgres_db=postgres_db,
        session_id=session_id,
        relative_path="src/close_checklist.py",
        old="VALUE = 1",
        new="VALUE = 2",
    )
    _write_transcript(
        daemon_instance,
        external_id,
        [
            _response_item(first_edit, timestamp),
            *_validation_events(outcome=0, timestamp=timestamp + timedelta(seconds=1)),
            _response_item(second_edit, timestamp + timedelta(seconds=2)),
        ],
    )
    result = _close(mcp_client, task_id, commit_sha)

    assert result.get("error") == "uncommitted_task_edits", result
    assert result["closed"] is False
    assert validation_llm_server.validation_calls == 0


def test_failed_validation_run_blocks_before_llm(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    validation_llm_server: _ValidationLLMServer,
    postgres_db: Any,
) -> None:
    task_id, session_id, external_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="failed"
    )
    source = daemon_instance.project_dir / "src" / "close_checklist.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    timestamp = datetime.now(UTC) + timedelta(seconds=1)
    edit = _record_edit(
        postgres_db=postgres_db,
        session_id=session_id,
        relative_path="src/close_checklist.py",
        old="",
        new="VALUE = 1",
    )
    _write_transcript(
        daemon_instance,
        external_id,
        [
            _response_item(edit, timestamp),
            *_validation_events(outcome=1, timestamp=timestamp + timedelta(seconds=1)),
        ],
    )
    commit_sha = _commit(daemon_instance.project_dir, "Add close checklist module")

    result = _close(mcp_client, task_id, commit_sha)

    assert result.get("error") == "validation_command_required", result
    assert result["closed"] is False
    assert validation_llm_server.validation_calls == 0


def test_epic_closes_without_llm_or_invalid_skipped_status(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    validation_llm_server: _ValidationLLMServer,
) -> None:
    task_id, _session_id, _external_id = _register_claimed_task(
        daemon_instance,
        mcp_client,
        cli_events,
        suffix="epic",
        task_type="epic",
    )

    result = _unwrap(
        mcp_client.call_tool(
            "gobby-tasks",
            "close_task",
            {
                "task_id": task_id,
                "preview": True,
                "response_detail": "diagnostic",
            },
        )
    )

    assert result["closed"] is True, result
    assert all(gate["status"] == "skipped" for gate in result["checklist"][4:])
    assert validation_llm_server.validation_calls == 0


def test_edit_after_clean_run_makes_transcript_evidence_stale(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
    cli_events: CLIEventSimulator,
    validation_llm_server: _ValidationLLMServer,
    postgres_db: Any,
) -> None:
    task_id, session_id, external_id = _register_claimed_task(
        daemon_instance, mcp_client, cli_events, suffix="stale"
    )
    source = daemon_instance.project_dir / "src" / "close_checklist.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    timestamp = datetime.now(UTC) + timedelta(seconds=1)
    first_edit = _record_edit(
        postgres_db=postgres_db,
        session_id=session_id,
        relative_path="src/close_checklist.py",
        old="",
        new="VALUE = 1",
    )
    source.write_text("VALUE = 1\nDETAIL = 'after validation'\n")
    second_edit = _record_edit(
        postgres_db=postgres_db,
        session_id=session_id,
        relative_path="src/close_checklist.py",
        old="VALUE = 1",
        new="VALUE = 1\\nDETAIL = 'after validation'",
    )
    _write_transcript(
        daemon_instance,
        external_id,
        [
            _response_item(first_edit, timestamp),
            *_validation_events(outcome=0, timestamp=timestamp + timedelta(seconds=1)),
            _response_item(second_edit, timestamp + timedelta(seconds=2)),
        ],
    )
    commit_sha = _commit(daemon_instance.project_dir, "Add post-validation edit")

    result = _close(mcp_client, task_id, commit_sha)

    assert result.get("error") == "validation_command_required", result
    assert result["closed"] is False
    assert validation_llm_server.validation_calls == 0
