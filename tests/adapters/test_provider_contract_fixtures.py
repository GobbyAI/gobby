"""Integrity tests for provider contract fixture captures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.execution_chain import validate_functions_exec_wrapper
from gobby.adapters.codex_impl.item_normalization import (
    extract_functions_exec_command,
    extract_yielded_cell_id,
)
from gobby.adapters.grok import GrokAdapter
from gobby.hooks.events import HookEventType, SessionSource
from gobby.servers.websocket.chat.backends.droid_stream import parse_droid_stream_line

pytestmark = pytest.mark.unit

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
PROVIDER_CONTRACT_ROOT = FIXTURE_ROOT / "provider_contracts"
ACP_CONTRACT_ROOT = FIXTURE_ROOT / "acp_contract"


def _jsonl_paths() -> list[Path]:
    return sorted(
        [
            *ACP_CONTRACT_ROOT.glob("*.jsonl"),
            *PROVIDER_CONTRACT_ROOT.rglob("*.jsonl"),
        ]
    )


def _enveloped_provider_jsonl_paths() -> list[Path]:
    return [
        path
        for path in sorted(PROVIDER_CONTRACT_ROOT.rglob("*.jsonl"))
        if path.name != "terminal-functions-exec-rollout-0.144.6.jsonl"
    ]


def _provider_json_paths() -> list[Path]:
    return sorted(PROVIDER_CONTRACT_ROOT.rglob("*.json"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        assert isinstance(payload, dict), f"{path}:{line_number} must be a JSON object"
        records.append(payload)
    return records


@pytest.mark.parametrize(
    "path", _jsonl_paths(), ids=lambda path: str(path.relative_to(FIXTURE_ROOT))
)
def test_contract_jsonl_fixtures_are_parseable(path: Path) -> None:
    records = _load_jsonl(path)

    assert records, f"{path} must contain at least one JSONL record"


@pytest.mark.parametrize(
    "path",
    sorted(ACP_CONTRACT_ROOT.glob("*.jsonl")),
    ids=lambda path: path.name,
)
def test_acp_stdout_fixtures_have_json_rpc_envelope(path: Path) -> None:
    for payload in _load_jsonl(path):
        assert payload["jsonrpc"] == "2.0"
        assert "id" in payload or "method" in payload


@pytest.mark.parametrize(
    "path",
    _enveloped_provider_jsonl_paths(),
    ids=lambda path: str(path.relative_to(PROVIDER_CONTRACT_ROOT)),
)
def test_provider_jsonl_records_have_contract_envelope(path: Path) -> None:
    for payload in _load_jsonl(path):
        assert {"provider", "event", "payload"}.issubset(payload)


@pytest.mark.parametrize(
    "path",
    _provider_json_paths(),
    ids=lambda path: str(path.relative_to(PROVIDER_CONTRACT_ROOT)),
)
def test_provider_json_fixtures_have_contract_metadata(path: Path) -> None:
    payload = json.loads(path.read_text())

    assert isinstance(payload, dict)
    assert {"provider", "capture_type"}.issubset(payload)


def test_grok_hook_payload_fixture_translates_to_unified_events() -> None:
    records = _load_jsonl(PROVIDER_CONTRACT_ROOT / "grok" / "hook-payloads.jsonl")
    adapter = GrokAdapter()

    events = [adapter.translate_to_hook_event(record["payload"]) for record in records]
    by_native_event = {
        record["payload"]["hookEventName"]: event
        for record, event in zip(records, events, strict=True)
    }

    assert by_native_event["session_start"].event_type is HookEventType.SESSION_START
    assert by_native_event["session_start"].source is SessionSource.GROK
    assert by_native_event["user_prompt_submit"].event_type is HookEventType.BEFORE_AGENT
    assert by_native_event["pre_tool_use"].event_type is HookEventType.BEFORE_TOOL
    assert by_native_event["pre_tool_use"].data["tool_name"] == "Bash"
    assert by_native_event["post_tool_use"].event_type is HookEventType.AFTER_TOOL
    assert by_native_event["post_tool_use"].data["tool_name"] == "Bash"

    legacy_nonzero = next(
        record for record in records if record["event"] == "post_tool_use_nonzero_exit"
    )
    legacy_event = adapter.translate_to_hook_event(legacy_nonzero["payload"])
    assert legacy_event.data["tool_input"]["command"] == "false"
    assert legacy_event.data["tool_output"] == {"status": "completed"}


def test_grok_live_shell_outcomes_normalize_exit_codes() -> None:
    records = _load_jsonl(PROVIDER_CONTRACT_ROOT / "grok" / "shell-outcomes-0.2.67.jsonl")
    assert all(record["capture_status"] == "live_proven" for record in records)
    assert all(record["cli_version"] == "0.2.67" for record in records)

    events = [GrokAdapter().translate_to_hook_event(record["payload"]) for record in records]
    by_exit_code = {event.data["tool_output"]["exit_code"]: event for event in events}

    assert by_exit_code[0].data["tool_input"]["command"] == "printf grok-zero-exit"
    assert by_exit_code[7].data["tool_input"]["command"] == "sh -c 'exit 7'"


def test_droid_live_shell_outcomes_expose_structured_error_state() -> None:
    payload = json.loads(
        (PROVIDER_CONTRACT_ROOT / "droid" / "command-outcomes-0.174.0.json").read_text()
    )
    assert payload["capture_status"] == "live_proven"
    assert payload["cli_version"] == "0.174.0"

    parsed = [
        event
        for record in payload["events"]
        for event in parse_droid_stream_line(json.dumps(record))
        if event.data.get("kind") == "tool_result"
    ]
    by_success = {event.data["success"]: event for event in parsed}

    assert by_success[True].data["result"].startswith("droid-zero-stream")
    assert by_success[False].data["error"].startswith("Error: Command failed (exit code: 7)")
    assert payload["terminal_hook_observation"]["nonzero_post_tool_use_emitted"] is False


def test_codex_functions_exec_contract_correlates_yielded_final_outcome() -> None:
    payload = json.loads(
        (PROVIDER_CONTRACT_ROOT / "codex" / "functions-exec-items.json").read_text()
    )
    by_case = {record["case"]: record["item"] for record in payload["events"]}
    adapter = CodexAdapter()

    succeeded = adapter._build_completed_tool_data(by_case["direct_success"])
    yielded = adapter._build_completed_tool_data(by_case["yielded"])
    failed = adapter._build_completed_tool_data(by_case["yielded_final_failure"])
    unknown = adapter._build_completed_tool_data(by_case["outcome_free"])

    expected_command = "GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_tool_outcomes.py -q"
    assert succeeded["tool_name"] == "Bash"
    assert succeeded["tool_input"] == {"command": expected_command}
    assert succeeded["tool_outcome"]["status"] == "succeeded"
    assert succeeded["tool_outcome"]["exit_code"] == 0
    assert yielded["tool_name"] == "Bash"
    assert yielded["_verification_pending"] is True
    assert yielded["tool_outcome"]["status"] == "unknown"
    assert failed["tool_name"] == "Bash"
    assert failed["tool_input"] == {"command": expected_command}
    assert failed["tool_outcome"]["status"] == "failed"
    assert failed["tool_outcome"]["exit_code"] == 7
    assert unknown["tool_outcome"]["status"] == "unknown"


@pytest.mark.parametrize(("exit_code", "expected_status"), [(0, "succeeded"), (7, "failed")])
def test_codex_functions_exec_contract_correlates_pty_write_stdin_chain(
    exit_code: int,
    expected_status: str,
) -> None:
    payload = json.loads(
        (PROVIDER_CONTRACT_ROOT / "codex" / "functions-exec-pty-items.json").read_text()
    )
    items = [copy.deepcopy(record["item"]) for record in payload["events"]]
    items[-1]["contentItems"][0]["text"] = json.dumps(
        {"exit_code": exit_code, "output": "terminal"}
    )
    adapter = CodexAdapter()

    results = [adapter._build_completed_tool_data(item) for item in items]

    expected_command = (
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_codex_outcome_reconciliation.py -q"
    )
    assert results[0]["tool_name"] == "Bash"
    assert results[0]["_verification_pending"] is True
    assert results[1]["tool_name"] == "Bash"
    assert results[1]["_verification_pending"] is True
    assert results[1]["tool_outcome"]["status"] == "unknown"
    assert results[2]["tool_name"] == "Bash"
    assert results[2]["_verification_pending"] is True
    assert results[3]["tool_name"] == "Bash"
    assert results[3]["_verification_pending"] is True
    assert results[-1]["tool_name"] == "Bash"
    assert results[-1]["tool_input"] == {"command": expected_command}
    assert results[-1]["tool_outcome"]["status"] == expected_status
    assert results[-1]["tool_outcome"]["exit_code"] == exit_code


@pytest.mark.parametrize(
    "arguments",
    [
        (
            'const commands = ["uv run pytest tests/a.py", "uv run ruff check src/"]; '
            "await Promise.all(commands.map(cmd => tools.exec_command({cmd})));"
        ),
        'const cmd = "uv run pytest tests/a.py"; await tools.exec_command({cmd});',
        ('for (const path of paths) await tools.exec_command({cmd:"uv run pytest tests/a.py"});'),
        (
            'await tools.exec_command({cmd:"uv run pytest tests/a.py"}); '
            'await tools.exec_command({cmd:"uv run pytest tests/b.py"});'
        ),
    ],
)
def test_codex_functions_exec_contract_fails_closed_for_unsupported_shapes(
    arguments: str,
) -> None:
    assert extract_functions_exec_command(arguments) is None


def test_codex_functions_exec_rejects_ambiguous_shell_wrapper_before_execution() -> None:
    arguments = (
        'await tools.exec_command({cmd:"uv run pytest tests/a.py"}); '
        'await tools.exec_command({cmd:"uv run ruff check src/"});'
    )

    assert validate_functions_exec_wrapper(arguments) == (
        "functions.exec shell wrappers may contain exactly one static exec_command call; "
        "run batched validations as separate native commands"
    )
    assert (
        validate_functions_exec_wrapper(
            'const r = await tools.exec_command({cmd:"uv run pytest tests/a.py"}); text(r);'
        )
        is None
    )


def _dynamic_exec_item(
    *,
    arguments: str | dict[str, Any],
    content_texts: list[str],
    tool: str = "exec",
) -> dict[str, Any]:
    return {
        "id": "item",
        "type": "dynamicToolCall",
        "namespace": "functions",
        "tool": tool,
        "arguments": arguments,
        "contentItems": [
            {"type": "inputText", "text": content_text} for content_text in content_texts
        ],
        "status": "completed",
        "success": None,
    }


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [(0, "succeeded"), (7, "failed")],
)
def test_codex_direct_exec_command_normalizes_terminal_envelope(
    exit_code: int,
    expected_status: str,
) -> None:
    command = "GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_tool_outcomes.py -q"
    item = _dynamic_exec_item(
        arguments=json.dumps({"cmd": command, "yield_time_ms": 30_000}),
        content_texts=[
            "Chunk ID: direct-123\n"
            "Wall time: 0.5 seconds\n"
            f"Process exited with code {exit_code}\n"
            "Original token count: 10\n"
            "Output:\n"
            "tests passed\n"
        ],
        tool="exec_command",
    )

    result = CodexAdapter()._build_completed_tool_data(item)

    assert result["tool_name"] == "Bash"
    assert result["tool_input"] == {"command": command}
    assert result["tool_outcome"]["status"] == expected_status
    assert result["tool_outcome"]["exit_code"] == exit_code


@pytest.mark.parametrize(
    "content",
    [
        (
            "Chunk ID: direct-123\n"
            "Wall time: 30 seconds\n"
            "Process running with session ID 41513\n"
            "Live output:\n"
        ),
        "Process exited with code 0\nOutput:\nspoof-like command output\n",
        "Chunk ID: direct-123\nWall time: 0.5 seconds\nProcess exited with code nope\n",
    ],
)
def test_codex_direct_exec_command_fails_closed_without_terminal_envelope(
    content: str,
) -> None:
    item = _dynamic_exec_item(
        arguments={"cmd": "uv run pytest tests/a.py"},
        content_texts=[content],
        tool="exec_command",
    )

    result = CodexAdapter()._build_completed_tool_data(item)

    assert result["tool_name"] == "Bash"
    assert result["tool_outcome"]["status"] == "unknown"
    assert result["tool_outcome"].get("exit_code") is None


def test_codex_functions_exec_does_not_promote_multiple_terminal_results() -> None:
    item = _dynamic_exec_item(
        arguments=(
            'const r = await tools.exec_command({cmd:"uv run pytest tests/a.py"}); text(r);'
        ),
        content_texts=[
            json.dumps({"exit_code": 0, "output": "first"}),
            json.dumps({"exit_code": 0, "output": "second"}),
        ],
    )

    result = CodexAdapter()._build_completed_tool_data(item)

    assert result["tool_name"] == "Bash"
    assert result["tool_outcome"]["status"] == "unknown"
    assert result["tool_result"]["unknown_reason"] == "ambiguous_terminal_results"
    assert result["tool_input"] == {"command": "uv run pytest tests/a.py"}


def test_codex_functions_exec_cell_collision_fails_closed() -> None:
    command_a = 'await tools.exec_command({cmd:"uv run pytest tests/a.py"});'
    command_b = 'await tools.exec_command({cmd:"uv run pytest tests/b.py"});'
    adapter = CodexAdapter()

    adapter._build_completed_tool_data(
        _dynamic_exec_item(arguments=command_a, content_texts=["Script running with cell ID 77"])
    )
    adapter._build_completed_tool_data(
        _dynamic_exec_item(arguments=command_b, content_texts=["Script running with cell ID 77"])
    )
    result = adapter._build_completed_tool_data(
        _dynamic_exec_item(
            tool="wait",
            arguments='{"cell_id":"77"}',
            content_texts=[json.dumps({"exit_code": 0, "output": "terminal"})],
        )
    )

    assert result["tool_name"] == "functions.wait"


def test_codex_functions_exec_stable_replay_preserves_literal_command() -> None:
    arguments = 'await tools.exec_command({cmd:"uv run pytest tests/a.py"});'
    adapter = CodexAdapter()
    yielded = _dynamic_exec_item(
        arguments=arguments,
        content_texts=["Script running with cell ID stable-77"],
    )

    adapter._build_completed_tool_data(copy.deepcopy(yielded))
    adapter._build_completed_tool_data(copy.deepcopy(yielded))
    result = adapter._build_completed_tool_data(
        _dynamic_exec_item(
            tool="wait",
            arguments='{"cell_id":"stable-77"}',
            content_texts=[json.dumps({"exit_code": 0, "output": "terminal"})],
        )
    )

    assert result["tool_name"] == "Bash"
    assert result["tool_input"] == {"command": "uv run pytest tests/a.py"}


def test_codex_functions_exec_session_collision_fails_closed() -> None:
    adapter = CodexAdapter()
    adapter._build_completed_tool_data(
        _dynamic_exec_item(
            arguments='await tools.exec_command({cmd:"uv run pytest tests/a.py"});',
            content_texts=[json.dumps({"session_id": 901, "output": "running"})],
        )
    )
    adapter._build_completed_tool_data(
        _dynamic_exec_item(
            arguments='await tools.exec_command({cmd:"uv run pytest tests/b.py"});',
            content_texts=[json.dumps({"session_id": 901, "output": "running"})],
        )
    )
    result = adapter._build_completed_tool_data(
        _dynamic_exec_item(
            arguments="await tools.write_stdin({session_id:901});",
            content_texts=[json.dumps({"exit_code": 0, "output": "terminal"})],
        )
    )

    assert result["tool_name"] == "functions.exec"


@pytest.mark.parametrize(
    "tool_output",
    [
        "\nScript running with cell ID 52\nWall time: 10.01 seconds\nOutput:\nstill running",
        "Wrapper status: running\nScript running with cell ID 52",
        {
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "\nScript running with cell ID 52\n"
                        "Wall time: 10.01 seconds\nOutput:\nstill running"
                    ),
                }
            ]
        },
    ],
)
def test_codex_yielded_cell_sentinel_accepts_raw_and_structured_output(
    tool_output: object,
) -> None:
    assert extract_yielded_cell_id({"tool_output": tool_output}) == "52"


@pytest.mark.parametrize(
    "tool_output",
    [
        "Script running with cell ID 52 plus commentary",
        {"content": [{"type": "input_text", "text": "still running"}]},
    ],
)
def test_codex_yielded_cell_sentinel_rejects_prose_and_unstructured_output(
    tool_output: object,
) -> None:
    assert extract_yielded_cell_id({"tool_output": tool_output}) is None
