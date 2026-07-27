from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.execution_chain import extract_direct_exec_terminal_result
from gobby.sessions.transcripts.base import raw_lines_from_texts
from gobby.sessions.transcripts.codex import CodexNestedExecOutcome, CodexTranscriptParser

pytestmark = pytest.mark.unit

_PTY_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures/provider_contracts/codex/terminal-functions-exec-pty-rollout.jsonl"
)


def _response_item(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "timestamp": "2026-07-18T21:00:00Z",
            "type": "response_item",
            "payload": payload,
        }
    )


def _call(call_id: str, name: str, tool_input: str) -> str:
    return _response_item(
        {
            "type": "custom_tool_call",
            "call_id": call_id,
            "name": name,
            "input": tool_input,
        }
    )


def _output(call_id: str, *texts: str) -> str:
    return _response_item(
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": text} for text in texts],
        }
    )


def _function_call(call_id: str, name: str, arguments: str) -> str:
    return _response_item(
        {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        }
    )


def _raw_output(call_id: str, output: Any, *, payload_type: str) -> str:
    return _response_item(
        {
            "type": payload_type,
            "call_id": call_id,
            "output": output,
        }
    )


def _outcomes(parser: CodexTranscriptParser, lines: Iterable[str]) -> list[CodexNestedExecOutcome]:
    outcomes: list[CodexNestedExecOutcome] = []
    for event in parser.iter_parse_events(raw_lines_from_texts(lines)):
        outcomes.extend(event.codex_exec_outcomes)
    return outcomes


def test_derives_single_literal_exec_result() -> None:
    parser = CodexTranscriptParser()
    lines = [
        _call(
            "exec-1",
            "exec",
            'const r = await tools.exec_command({cmd:"uv run ruff check src/gobby"}); text(r);',
        ),
        _output("exec-1", json.dumps({"exit_code": 0, "output": "All checks passed"})),
    ]

    outcomes = _outcomes(parser, lines)

    assert [(item.identity, item.command, item.result["exit_code"]) for item in outcomes] == [
        ("exec-1:0", "uv run ruff check src/gobby", 0)
    ]


def test_derives_batched_results_in_content_block_order() -> None:
    parser = CodexTranscriptParser()
    lines = [
        _call(
            "exec-batch",
            "exec",
            "const rs = await Promise.all(commands.map(cmd => tools.exec_command({cmd})));",
        ),
        _output(
            "exec-batch",
            "Script completed successfully",
            json.dumps({"cmd": "pytest focused", "exit_code": 1, "output": "failed"}),
            json.dumps({"command": "ruff check", "exitCode": 0, "output": "passed"}),
        ),
    ]

    outcomes = _outcomes(parser, lines)

    assert [(item.identity, item.command, item.result) for item in outcomes] == [
        (
            "exec-batch:0",
            "pytest focused",
            {"cmd": "pytest focused", "exit_code": 1, "output": "failed"},
        ),
        (
            "exec-batch:1",
            "ruff check",
            {"command": "ruff check", "exitCode": 0, "output": "passed"},
        ),
    ]


def test_correlates_yielded_exec_with_final_wait_result() -> None:
    parser = CodexTranscriptParser()
    lines = [
        _call(
            "exec-yield",
            "functions.exec",
            'const r = await tools.exec_command({cmd:"pytest slow",yield_time_ms:1000}); text(r);',
        ),
        _output("exec-yield", "Script running with cell ID cell-7"),
        _call("wait-1", "wait", json.dumps({"cell_id": "cell-7"})),
        _output("wait-1", "Script running with cell ID cell-7"),
        _call("wait-2", "functions.wait", json.dumps({"cell_id": "cell-7"})),
        _output("wait-2", json.dumps({"exit_code": 7, "output": "one failed"})),
    ]

    outcomes = _outcomes(parser, lines)

    assert [(item.identity, item.command, item.result["exit_code"]) for item in outcomes] == [
        ("exec-yield:0", "pytest slow", 7)
    ]


def test_hydrates_pending_yielded_exec_state() -> None:
    first_parser = CodexTranscriptParser()
    _outcomes(
        first_parser,
        [
            _call(
                "exec-restart",
                "exec",
                'const r = await tools.exec_command({cmd:"mypy src"}); text(r);',
            ),
            _output("exec-restart", "Script running with cell ID 53"),
        ],
    )
    resumed_parser = CodexTranscriptParser()
    resumed_parser.hydrate_state(first_parser.snapshot_state())

    outcomes = _outcomes(
        resumed_parser,
        [
            _call("wait-restart", "wait", json.dumps({"cell_id": 53})),
            _output("wait-restart", json.dumps({"exit_code": 0, "output": "clean"})),
        ],
    )

    assert [(item.identity, item.command, item.result["exit_code"]) for item in outcomes] == [
        ("exec-restart:0", "mypy src", 0)
    ]


def test_hydrates_pending_exec_call_before_output() -> None:
    first_parser = CodexTranscriptParser()
    _outcomes(
        first_parser,
        [
            _call(
                "exec-before-output",
                "exec",
                'const r = await tools.exec_command({cmd:"ruff check src"}); text(r);',
            )
        ],
    )
    resumed_parser = CodexTranscriptParser()
    resumed_parser.hydrate_state(first_parser.snapshot_state())

    outcomes = _outcomes(
        resumed_parser,
        [_output("exec-before-output", json.dumps({"exit_code": 0, "output": "clean"}))],
    )

    assert [(item.identity, item.command, item.result["exit_code"]) for item in outcomes] == [
        ("exec-before-output:0", "ruff check src", 0)
    ]


def test_function_call_wait_survives_repeated_yields_and_hydration() -> None:
    first_parser = CodexTranscriptParser()
    initial_outcomes = _outcomes(
        first_parser,
        [
            _call(
                "exec-batched-yield",
                "exec",
                "const rs = await Promise.all(commands.map(cmd => tools.exec_command({cmd})));",
            ),
            _raw_output(
                "exec-batched-yield",
                "\nScript running with cell ID 52\nWall time: 10.01 seconds\nOutput:\nstill running",
                payload_type="custom_tool_call_output",
            ),
        ],
    )
    resumed_parser = CodexTranscriptParser()
    resumed_parser.hydrate_state(first_parser.snapshot_state())

    repeated_wait_outcomes = _outcomes(
        resumed_parser,
        [
            _function_call("wait-52-1", "wait", json.dumps({"cell_id": "52"})),
            _raw_output(
                "wait-52-1",
                "Script running with cell ID 52\nWall time: 10.00 seconds\nOutput:\nstill running",
                payload_type="function_call_output",
            ),
        ],
    )
    final_parser = CodexTranscriptParser()
    final_parser.hydrate_state(resumed_parser.snapshot_state())

    final_outcomes = _outcomes(
        final_parser,
        [
            _function_call("wait-52-2", "wait", json.dumps({"cell_id": 52})),
            _raw_output(
                "wait-52-2",
                [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {"cmd": "pytest focused", "exit_code": 7, "output": "failed"}
                        ),
                    },
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {"command": "ruff check", "exitCode": 0, "output": "passed"}
                        ),
                    },
                ],
                payload_type="function_call_output",
            ),
        ],
    )

    assert initial_outcomes == []
    assert repeated_wait_outcomes == []
    assert [(item.identity, item.command, item.result) for item in final_outcomes] == [
        (
            "exec-batched-yield:0",
            "pytest focused",
            {"cmd": "pytest focused", "exit_code": 7, "output": "failed"},
        ),
        (
            "exec-batched-yield:1",
            "ruff check",
            {"command": "ruff check", "exitCode": 0, "output": "passed"},
        ),
    ]


def test_pty_write_stdin_chain_survives_repeated_yields_and_parser_hydration() -> None:
    records = [json.loads(line) for line in _PTY_FIXTURE.read_text().splitlines()]
    lines = [json.dumps(record["payload"]) for record in records]
    parser = CodexTranscriptParser()
    outcomes: list[CodexNestedExecOutcome] = []

    for offset in range(0, len(lines), 4):
        outcomes.extend(_outcomes(parser, lines[offset : offset + 4]))
        resumed = CodexTranscriptParser()
        resumed.hydrate_state(parser.snapshot_state())
        parser = resumed

    assert [(item.identity, item.command, item.result["exit_code"]) for item in outcomes] == [
        ("exec-pty-success:0", "pytest pty-success", 0),
        ("exec-pty-failure:0", "pytest pty-failure", 7),
    ]


def test_direct_write_stdin_poll_preserves_original_execution_identity() -> None:
    parser = CodexTranscriptParser()
    outcomes = _outcomes(
        parser,
        [
            _call(
                "exec-direct-stdin",
                "functions.exec",
                'const r = await tools.exec_command({cmd:"pytest direct",tty:true}); text(r);',
            ),
            _output(
                "exec-direct-stdin",
                json.dumps({"session_id": 91, "output": "still running"}),
            ),
            _function_call(
                "stdin-poll-1",
                "write_stdin",
                json.dumps({"session_id": 91, "chars": ""}),
            ),
            _raw_output(
                "stdin-poll-1",
                json.dumps({"session_id": 91, "output": "still running"}),
                payload_type="function_call_output",
            ),
            _function_call(
                "stdin-poll-2",
                "functions.write_stdin",
                json.dumps({"session_id": 91, "chars": ""}),
            ),
            _raw_output(
                "stdin-poll-2",
                json.dumps({"exit_code": 7, "output": "one failed"}),
                payload_type="function_call_output",
            ),
        ],
    )

    assert [(item.identity, item.command, item.result["exit_code"]) for item in outcomes] == [
        ("exec-direct-stdin:0", "pytest direct", 7)
    ]


def test_nested_exec_write_stdin_accepts_native_terminal_envelope() -> None:
    parser = CodexTranscriptParser()
    outcomes = _outcomes(
        parser,
        [
            _call(
                "exec-nested-stdin",
                "functions.exec",
                'const r = await tools.exec_command({cmd:"pytest nested"}); text(r);',
            ),
            _output(
                "exec-nested-stdin",
                json.dumps({"session_id": 92, "output": "still running"}),
            ),
            _function_call(
                "stdin-poll-running",
                "write_stdin",
                json.dumps({"session_id": 92, "chars": ""}),
            ),
            _raw_output(
                "stdin-poll-running",
                (
                    "Chunk ID: still-running\n"
                    "Wall time: 30.001 seconds\n"
                    "Process running with session ID 92\n"
                    "Original token count: 5\n"
                    "Output:\n"
                    "still running\n"
                ),
                payload_type="function_call_output",
            ),
            _function_call(
                "stdin-poll-native",
                "write_stdin",
                json.dumps({"session_id": 92, "chars": ""}),
            ),
            _raw_output(
                "stdin-poll-native",
                (
                    "Chunk ID: sanitized\n"
                    "Wall time: 30.001 seconds\n"
                    "Process exited with code 0\n"
                    "Original token count: 3\n"
                    "Final output:\n"
                    "one passed\n"
                ),
                payload_type="function_call_output",
            ),
        ],
    )

    assert [(item.identity, item.command, item.result["exit_code"]) for item in outcomes] == [
        ("exec-nested-stdin:0", "pytest nested", 0)
    ]


@pytest.mark.parametrize(
    "envelope",
    [
        (
            "Chunk ID: direct-123\n"
            "Wall time: 0.5 seconds\n"
            "Process exited with code 0\n"
            "Original token count: nope\n"
            "Output:\n"
        ),
        (
            "Chunk ID: direct-123\n"
            "Wall time: 0.5 seconds\n"
            "Process exited with code 0\n"
            "Original token count: -1\n"
            "Output:\n"
        ),
        (
            "Chunk ID: direct-123\n"
            "Wall time: 0.5 seconds\n"
            "Process exited with code 0\n"
            "Original token count: 1\n"
            "Original token count: 2\n"
            "Output:\n"
        ),
        (
            "ordinary command output\n"
            "Chunk ID: direct-123\n"
            "Wall time: 0.5 seconds\n"
            "Process exited with code 0\n"
            "Original token count: 1\n"
            "Output:\n"
        ),
    ],
)
def test_direct_exec_terminal_envelope_rejects_non_authoritative_shapes(
    envelope: str,
) -> None:
    assert extract_direct_exec_terminal_result(envelope) is None


@pytest.mark.parametrize(
    ("tool_input", "result_texts"),
    [
        (
            'const r = await tools.exec_command({cmd:"pytest"}); text(r.output);',
            ("tests failed",),
        ),
        (
            "const rs = await Promise.all(commands.map(cmd => tools.exec_command({cmd})));",
            (
                json.dumps({"exit_code": 0, "output": "first"}),
                json.dumps({"exit_code": 0, "output": "second"}),
            ),
        ),
        (
            'const r = await tools.exec_command({cmd:"pytest"}); text(r);',
            (json.dumps({"exit_code": 0, "exitCode": 1, "output": "conflict"}),),
        ),
        (
            "const rs = await Promise.all(commands.map(cmd => tools.exec_command({cmd})));",
            (
                json.dumps(
                    {
                        "cmd": "pytest one",
                        "command": "pytest two",
                        "exit_code": 0,
                    }
                ),
            ),
        ),
    ],
)
def test_ambiguous_or_unstructured_outputs_remain_unknown(
    tool_input: str, result_texts: tuple[str, ...]
) -> None:
    parser = CodexTranscriptParser()

    outcomes = _outcomes(
        parser,
        [_call("exec-unknown", "exec", tool_input), _output("exec-unknown", *result_texts)],
    )

    if tool_input in {
        'const r = await tools.exec_command({cmd:"pytest"}); text(r.output);',
        'const r = await tools.exec_command({cmd:"pytest"}); text(r);',
    }:
        assert len(outcomes) == 1
        assert outcomes[0].identity == "exec-unknown:0"
        assert outcomes[0].command == "pytest"
        assert outcomes[0].result["success"] is None
        assert outcomes[0].result["unknown_reason"] == "terminal_result_missing_structured_outcome"
    else:
        assert outcomes == []
