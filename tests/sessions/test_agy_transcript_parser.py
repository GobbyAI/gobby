"""Fixture-backed tests for the AGY transcript parser (plan 4.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.sessions.transcripts import PARSER_REGISTRY, get_parser
from gobby.sessions.transcripts.agy import AgyTranscriptParser
from gobby.sessions.transcripts.base import (
    UNMODELED_RECORD_CONTENT_TYPE,
    ParsedMessage,
    ParsedToolEvent,
    RawLine,
)

pytestmark = pytest.mark.unit

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "provider_contracts"
    / "agy"
    / "transcript-manifest.json"
)
_CONVERSATION_ID = "agy-conv-1"
_CREATED = "2026-08-22T08:21:24Z"

_LEGACY_RESULT_TYPES = (
    "RUN_COMMAND",
    "VIEW_FILE",
    "MCP_TOOL",
    "LIST_DIRECTORY",
    "GREP_SEARCH",
    "SEARCH_WEB",
    "CODE_ACTION",
)


def _agy_parser(
    *,
    session_id: str = _CONVERSATION_ID,
    transcript_path: str | Path | None = None,
) -> AgyTranscriptParser:
    assert "agy" in PARSER_REGISTRY
    parser = get_parser("agy", session_id=session_id, transcript_path=transcript_path)
    assert isinstance(parser, AgyTranscriptParser)
    return parser


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record)


def _parse(
    parser: AgyTranscriptParser, records: list[dict[str, Any]], *, start_index: int = 0
) -> list[ParsedMessage | ParsedToolEvent]:
    return parser.parse_lines([_line(record) for record in records], start_index=start_index)


def _record(
    step_index: int,
    source: str,
    type_: str,
    *,
    status: str = "DONE",
    created_at: str = _CREATED,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step_index": step_index,
        "source": source,
        "type": type_,
        "status": status,
        "created_at": created_at,
    }
    payload.update(extra)
    return payload


def _user(step_index: int, content: str) -> dict[str, Any]:
    return _record(step_index, "USER_EXPLICIT", "USER_INPUT", content=content)


def _planner(
    step_index: int,
    *,
    content: str | None = None,
    thinking: str | None = None,
    tool_calls: Any = None,
    status: str = "DONE",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(extra)
    if content is not None:
        payload["content"] = content
    if thinking is not None:
        payload["thinking"] = thinking
    if tool_calls is not None:
        payload["tool_calls"] = tool_calls
    return _record(step_index, "MODEL", "PLANNER_RESPONSE", status=status, **payload)


def _generic(
    step_index: int, content: str, *, status: str = "DONE", **extra: Any
) -> dict[str, Any]:
    return _record(step_index, "MODEL", "GENERIC", status=status, content=content, **extra)


def _run_command_call(command: str) -> dict[str, Any]:
    return {
        "name": "run_command",
        "args": {
            "CommandLine": command,
            "Cwd": "/workspace",
            "WaitMsBeforeAsync": 2000,
        },
    }


def _manifest() -> dict[str, Any]:
    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _tool_events(records: list[Any]) -> list[ParsedToolEvent]:
    return [record for record in records if isinstance(record, ParsedToolEvent)]


def _messages(records: list[Any]) -> list[ParsedMessage]:
    return [record for record in records if isinstance(record, ParsedMessage)]


def test_agy_parser_supports_incremental_state() -> None:
    parser = _agy_parser()
    assert getattr(parser, "supports_incremental_state", False) is True
    assert getattr(parser, "cli_name", None) == "agy"


def test_user_checkpoint_and_system_records() -> None:
    parser = _agy_parser()
    records = _parse(
        parser,
        [
            _record(0, "SYSTEM", "CHECKPOINT", content="checkpoint"),
            _user(1, "list the repo"),
            _record(2, "SYSTEM", "SYSTEM_MESSAGE", content="bookkeeping"),
            _record(3, "SYSTEM", "EPHEMERAL_MESSAGE", content="inject"),
            _record(4, "SYSTEM", "ERROR_MESSAGE", content="blocked"),
            _record(5, "SYSTEM", "CONVERSATION_HISTORY", content="old"),
            _planner(6, content="looking now"),
        ],
    )
    messages = _messages(records)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "list the repo"
    assert messages[1].content == "looking now"
    assert messages[1].usage is None
    assert all(event.usage is None for event in messages)


def test_thinking_is_distinct_from_content() -> None:
    parser = _agy_parser()
    records = _parse(
        parser,
        [_planner(2, content="visible answer", thinking="internal plan")],
    )
    messages = _messages(records)
    thinking = [message for message in messages if message.content_type == "thinking"]
    text = [message for message in messages if message.content_type == "text"]
    assert [message.content for message in thinking] == ["internal plan"]
    assert [message.content for message in text] == ["visible answer"]


def test_generic_run_command_pairing_parses_exit_sentence() -> None:
    parser = _agy_parser()
    zero = _manifest()["zero_exit_run_command"]["transcript_full"]
    records = _parse(parser, zero)
    events = _tool_events(records)
    assert [event.phase for event in events] == ["begin", "end"]
    assert events[0].tool == "Bash"
    assert events[0].arguments["command"] == "ls -la"
    assert events[0].call_id == events[1].call_id
    assert events[1].result is not None
    assert events[1].result["exit_code"] == 0
    assert events[1].raw_json["type"] == "GENERIC"


def test_nonzero_generic_exit_and_unstructured_outcome() -> None:
    parser = _agy_parser()
    nonzero = _manifest()["nonzero_exit_run_command"]["transcript_full"]
    failure = _tool_events(_parse(parser, nonzero))
    assert failure[-1].result is not None
    assert failure[-1].result["exit_code"] == 7

    unstructured = _tool_events(
        _parse(
            _agy_parser(),
            [
                _planner(2, tool_calls=[_run_command_call("uv run pytest tests/x.py")]),
                _generic(3, "Created At: now\nOutput:\nno exit sentence"),
            ],
        )
    )
    assert unstructured[-1].result is not None
    assert "exit_code" not in unstructured[-1].result
    assert unstructured[-1].result.get("unknown_reason") == "unstructured"


@pytest.mark.parametrize("result_type", _LEGACY_RESULT_TYPES)
def test_legacy_and_unknown_model_types_are_tool_results(result_type: str) -> None:
    parser = _agy_parser()
    records = _parse(
        parser,
        [
            _planner(2, tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/tmp/a"}}]),
            _record(3, "MODEL", result_type, content="file body"),
        ],
    )
    events = _tool_events(records)
    assert events[0].tool == "Read"
    assert events[1].phase == "end"
    assert events[1].raw_json["type"] == result_type
    assert events[0].call_id == events[1].call_id


def test_unknown_model_type_is_a_tool_result() -> None:
    parser = _agy_parser()
    events = _tool_events(
        _parse(
            parser,
            [
                _planner(4, tool_calls=[{"name": "list_dir", "args": {"DirectoryPath": "."}}]),
                _record(5, "MODEL", "FUTURE_TOOL", content="ok"),
            ],
        )
    )
    assert events[0].tool == "Ls"
    assert events[1].raw_json["type"] == "FUTURE_TOOL"
    assert events[0].call_id == events[1].call_id


def test_truncated_fields_do_not_raise() -> None:
    parser = _agy_parser()
    records = _parse(
        parser,
        [
            _planner(
                2,
                tool_calls="not-a-list",
                truncated_fields=["tool_calls"],
            ),
            _generic(3, "The command exited with code 0.\n", truncated_fields=["content"]),
        ],
    )
    assert records is not None


def test_malformed_and_unknown_records_preserve_order() -> None:
    parser = _agy_parser()
    lines = [
        "{",
        "not json",
        _line(_user(1, "hello")),
        _line({"source": "ALIEN", "type": "WEIRD", "step_index": 2}),
        _line(_planner(3, content="hi back")),
    ]
    records = parser.parse_lines(lines)
    messages = _messages(records)
    conversation = [m for m in messages if m.content_type != UNMODELED_RECORD_CONTENT_TYPE]
    unmodeled = [m for m in messages if m.content_type == UNMODELED_RECORD_CONTENT_TYPE]
    assert [message.content for message in conversation] == ["hello", "hi back"]
    assert [message.content for message in unmodeled] == ["ALIEN/WEIRD"]
    assert unmodeled[0].role == "system"
    assert conversation[0].index < unmodeled[0].index < conversation[1].index


def test_malformed_line_between_valid_lines_yields_positioned_event() -> None:
    parser = _agy_parser()
    texts = [_line(_user(1, "hello")), "{not json", _line(_planner(2, content="hi back"))]
    raws: list[RawLine] = []
    offset = 0
    for line_no, text in enumerate(texts):
        raws.append(RawLine(byte_offset=offset, raw_line_no=line_no, text=text))
        offset += len(text.encode("utf-8")) + 1

    events = list(parser.iter_parse_events(raws, start_index=0))

    assert [event.raw_line_no for event in events] == [0, 1, 2]
    assert [event.byte_offset for event in events] == [raw.byte_offset for raw in raws]
    assert [event.parsed_index for event in events] == [0, 1, 2]
    assert events[1].records == []
    assert all(event.parser_safe for event in events)
    assert [message.content for message in _messages(events[0].records)] == ["hello"]
    assert [message.content for message in _messages(events[2].records)] == ["hi back"]
    reply = events[2].records[0]
    assert isinstance(reply, ParsedMessage)
    assert reply.index == events[2].parsed_index

    resumed = list(_agy_parser().iter_parse_events(raws[2:], start_index=events[2].parsed_index))
    assert [message.index for message in _messages(resumed[0].records)] == [2]


def test_structured_exit_code_field_is_never_read() -> None:
    parser = _agy_parser()
    unstructured = _tool_events(
        _parse(
            parser,
            [
                _planner(2, tool_calls=[_run_command_call("uv run pytest tests/x.py")]),
                _generic(3, "Output:\nno exit sentence", exit_code=0),
            ],
        )
    )
    assert unstructured[-1].result is not None
    assert "exit_code" not in unstructured[-1].result
    assert unstructured[-1].result.get("unknown_reason") == "unstructured"

    sentence = _tool_events(
        _parse(
            _agy_parser(),
            [
                _planner(4, tool_calls=[_run_command_call("uv run pytest tests/y.py")]),
                _generic(5, "The command exited with code 7.\nOutput:\nboom", exit_code=0),
            ],
        )
    )
    assert sentence[-1].result is not None
    assert sentence[-1].result["exit_code"] == 7


def test_two_calls_in_one_planner_response_get_distinct_ids() -> None:
    parser = _agy_parser()
    records = _parse(
        parser,
        [
            _planner(
                8,
                tool_calls=[
                    {"name": "list_dir", "args": {"DirectoryPath": "."}},
                    {"name": "find_by_name", "args": {"Pattern": "*.py"}},
                ],
            ),
            _generic(9, "dir listing"),
            _generic(10, "matches"),
        ],
    )
    begins = [event for event in _tool_events(records) if event.phase == "begin"]
    ends = [event for event in _tool_events(records) if event.phase == "end"]
    assert begins[0].call_id != begins[1].call_id
    assert begins[0].call_id == f"{_CONVERSATION_ID}:8:0"
    assert begins[1].call_id == f"{_CONVERSATION_ID}:8:1"
    assert [event.call_id for event in ends] == [begins[0].call_id, begins[1].call_id]
    assert begins[0].tool == "Ls"
    assert begins[1].tool == "Glob"


def test_pairing_is_positional_and_never_compares_names() -> None:
    parser = _agy_parser()
    events = _tool_events(
        _parse(
            parser,
            [
                _planner(2, tool_calls=[{"name": "run_command", "args": {"CommandLine": "pwd"}}]),
                _record(3, "MODEL", "RUN_COMMAND", content="The command exited with code 0.\n"),
            ],
        )
    )
    assert events[0].tool == "Bash"
    assert events[1].raw_json["type"] == "RUN_COMMAND"
    assert events[0].call_id == events[1].call_id


def test_running_record_is_interrupted() -> None:
    parser = _agy_parser()
    events = _tool_events(
        _parse(
            parser,
            [
                _planner(2, tool_calls=[_run_command_call("sleep 10")]),
                _generic(3, "still going", status="RUNNING"),
            ],
        )
    )
    assert events[-1].phase == "end"
    assert events[-1].result is not None
    assert events[-1].result.get("status") == "RUNNING"
    assert events[-1].result.get("unknown_reason") == "nonterminal"


def test_incremental_append_only_resume() -> None:
    parser = _agy_parser()
    prefix = [
        _user(1, "run tests"),
        _planner(2, tool_calls=[_run_command_call("uv run pytest tests/x.py")]),
    ]
    first = _parse(parser, prefix)
    assert [event.phase for event in _tool_events(first)] == ["begin"]
    second = _parse(
        parser,
        [_generic(3, "The command exited with code 0.\nOutput:\npassed\n")],
        start_index=len(prefix),
    )
    ends = [event for event in _tool_events(second) if event.phase == "end"]
    assert len(ends) == 1
    assert ends[0].call_id == _tool_events(first)[0].call_id
    assert ends[0].result is not None
    assert ends[0].result["exit_code"] == 0


def test_snapshot_rehydrates_pending_calls_across_restart() -> None:
    live = _agy_parser()
    prefix = [
        _planner(
            4,
            tool_calls=[
                _run_command_call("uv run pytest tests/a.py"),
                {"name": "list_dir", "args": {"DirectoryPath": "."}},
            ],
        )
    ]
    begins = [event for event in _tool_events(_parse(live, prefix)) if event.phase == "begin"]
    state = live.snapshot_state()
    assert state

    restored = _agy_parser()
    restored.hydrate_state(state)
    results = _parse(
        restored,
        [
            _generic(5, "The command exited with code 7.\nOutput:\nboom\n"),
            _generic(6, "entries"),
        ],
        start_index=1,
    )
    ends = [event for event in _tool_events(results) if event.phase == "end"]
    assert [event.call_id for event in ends] == [begins[0].call_id, begins[1].call_id]
    assert ends[0].result is not None
    assert ends[0].result["exit_code"] == 7
    assert ends[1].tool == "Ls"


def test_transcript_full_does_not_decode_native_args(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    native_args = {"CommandLine": '{"k": 1}', "Note": "[1, 2]"}
    path.write_text(
        _line(_planner(2, tool_calls=[{"name": "run_command", "args": native_args}])) + "\n",
        encoding="utf-8",
    )
    events = _tool_events(_parse(_agy_parser(transcript_path=path), [json.loads(path.read_text())]))
    assert events[0].arguments["CommandLine"] == '{"k": 1}'
    assert events[0].arguments["Note"] == "[1, 2]"


def test_token_efficient_twin_decodes_to_the_same_records(tmp_path: Path) -> None:
    manifest = _manifest()["zero_exit_run_command"]
    full_path = tmp_path / "transcript_full.jsonl"
    twin_path = tmp_path / "transcript.jsonl"
    full_path.write_text(
        "\n".join(_line(record) for record in manifest["transcript_full"]) + "\n",
        encoding="utf-8",
    )
    twin_path.write_text(
        "\n".join(_line(record) for record in manifest["transcript_token_efficient"]) + "\n",
        encoding="utf-8",
    )
    full_events = _tool_events(
        _agy_parser(transcript_path=full_path).parse_lines(
            full_path.read_text(encoding="utf-8").splitlines()
        )
    )
    twin_events = _tool_events(
        _agy_parser(transcript_path=twin_path).parse_lines(
            twin_path.read_text(encoding="utf-8").splitlines()
        )
    )
    assert [event.tool for event in full_events] == [event.tool for event in twin_events]
    assert full_events[0].arguments["CommandLine"] == twin_events[0].arguments["CommandLine"]
    assert (
        full_events[0].arguments["WaitMsBeforeAsync"]
        == twin_events[0].arguments["WaitMsBeforeAsync"]
    )
    assert full_events[0].arguments["command"] == twin_events[0].arguments["command"]


def test_parser_does_not_open_chunk_mirrors(tmp_path: Path) -> None:
    transcript = tmp_path / "logs" / "transcript_full.jsonl"
    chunks = tmp_path / "logs" / "chunks" / "transcript_full" / "00000000.jsonl"
    chunks.parent.mkdir(parents=True)
    payload = _line(_user(1, "hello")) + "\n"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(payload, encoding="utf-8")
    chunks.write_text("should-not-be-read\n", encoding="utf-8")
    records = _agy_parser(transcript_path=transcript).parse_lines(
        transcript.read_text(encoding="utf-8").splitlines()
    )
    assert _messages(records)[0].content == "hello"


def test_agy_messages_emit_no_usage() -> None:
    parser = _agy_parser()
    records = _parse(
        parser,
        [
            _user(1, "hi"),
            _planner(2, content="hello", thinking="thought"),
        ],
    )
    assert all(isinstance(record, ParsedMessage) and record.usage is None for record in records)
