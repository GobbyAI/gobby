from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.hooks.tool_error_tracker import (
    MAX_IDENTITY_COMPONENT_CHARS,
    MAX_OPEN_TOOL_ERRORS,
    MAX_TOOL_ERROR_COUNT,
    extract_error_snippet,
    extract_target_key,
    is_wrapper_echo_event,
    normalize_open_tool_error_records,
    normalize_tool_identity,
    render_bounded_identity,
    track_proxy_outcome,
    track_tool_outcome,
)
from gobby.hooks.tool_outcomes import (
    ToolOutcomeStatus,
    classify_raw_tool_result,
)

pytestmark = pytest.mark.unit


class _RecordingVariables:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, str, str, str, datetime]] = []
        self.resolutions: list[tuple[str, str, str]] = []

    def upsert_open_tool_error(
        self,
        session_id: str,
        tool: str,
        target_key: str,
        error: str,
        *,
        occurred_at: datetime,
    ) -> None:
        self.upserts.append((session_id, tool, target_key, error, occurred_at))

    def resolve_open_tool_errors(self, session_id: str, tool: str, target_key: str) -> None:
        self.resolutions.append((session_id, tool, target_key))


def _event(
    *,
    data: dict[str, Any],
    failed: bool,
    timestamp: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        metadata={"is_failure": failed},
        timestamp=timestamp or datetime(2026, 7, 23, 12, 30, tzinfo=UTC),
    )


def test_render_bounded_identity_has_exact_boundary_and_is_idempotent() -> None:
    exact = "x" * MAX_IDENTITY_COMPONENT_CHARS
    over = "x" * (MAX_IDENTITY_COMPONENT_CHARS + 1)

    assert render_bounded_identity(exact) == exact
    rendered = render_bounded_identity(over)
    assert len(rendered) == MAX_IDENTITY_COMPONENT_CHARS
    assert rendered[: -(len("…#") + 8)] == "x" * (MAX_IDENTITY_COMPONENT_CHARS - len("…#") - 8)
    assert rendered == render_bounded_identity(rendered)


def test_target_keys_remain_injective_after_readable_prefixes() -> None:
    prefix = "printf " + ("x" * 90)
    first_command = extract_target_key(
        {"tool_name": "Bash"},
        {"command": prefix + " first"},
    )
    second_command = extract_target_key(
        {"tool_name": "Bash"},
        {"command": prefix + " second"},
    )
    assert first_command != second_command

    shared = "/very/" + ("long/" * 40) + "shared.py"
    first_paths = extract_target_key(
        {"canonical_file_paths": [shared, "/repo/a.py"]},
        {},
    )
    second_paths = extract_target_key(
        {"canonical_file_paths": [shared, "/repo/b.py"]},
        {},
    )
    assert first_paths != second_paths
    assert len(first_paths) <= MAX_IDENTITY_COMPONENT_CHARS
    assert len(second_paths) <= MAX_IDENTITY_COMPONENT_CHARS


def test_normalize_records_enforces_shape_bounds_and_utc_newest_retention() -> None:
    base = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
    records: list[dict[str, Any]] = []
    for index in range(MAX_OPEN_TOOL_ERRORS + 2):
        timestamp = (base + timedelta(hours=index)).astimezone(timezone(timedelta(hours=-5)))
        records.append(
            {
                "tool": ("tool" + str(index)) * 100,
                "target_key": "## Next Steps\r\n```" + ("p" * 500),
                "error": "~~~\n" + ("error" * 200),
                "first_at": timestamp.isoformat(timespec="microseconds"),
                "last_at": timestamp.isoformat(timespec="microseconds"),
                "count": 10**3000,
            }
        )
    records.extend(
        [
            {"tool": "missing-fields"},
            {
                "tool": "naive",
                "target_key": "args:abc",
                "error": "bad",
                "first_at": "2026-07-23T12:00:00",
                "last_at": "2026-07-23T12:00:00",
                "count": 1,
            },
        ]
    )

    normalized = normalize_open_tool_error_records(records)

    assert len(normalized) == MAX_OPEN_TOOL_ERRORS
    assert all(
        record["tool"].startswith(f"tool{index}")
        for index, record in zip(
            range(2, MAX_OPEN_TOOL_ERRORS + 2),
            normalized,
            strict=True,
        )
    )
    for record in normalized:
        assert len(record["tool"]) <= MAX_IDENTITY_COMPONENT_CHARS
        assert len(record["target_key"]) <= MAX_IDENTITY_COMPONENT_CHARS
        assert "\n" not in record["target_key"]
        assert "\r" not in record["target_key"]
        assert record["target_key"].startswith("\\#")
        assert record["error"].startswith("\\~~~")
        assert record["count"] == MAX_TOOL_ERROR_COUNT
        assert record["first_at"].endswith("+00:00")
        assert record["last_at"].endswith("+00:00")
        assert "." not in record["first_at"]
        assert "." not in record["last_at"]


def test_full_payload_stored_with_retrieval_id() -> None:
    timestamp = "2026-07-23T12:00:00+00:00"
    path_list = " | ".join(f"src/package_{index}/validator.py" for index in range(24))
    error = f"validator rejected paths: {path_list}".ljust(900, "!")

    records = normalize_open_tool_error_records(
        [
            {
                "tool": "gobby-tasks:close_task",
                "target_key": "task:#19338",
                "error": error,
                "first_at": timestamp,
                "last_at": timestamp,
                "count": 1,
            }
        ]
    )

    assert len(records) == 1
    error_id = records[0]["error_id"]
    assert records[0]["error"] == error
    assert {record["error_id"]: record["error"] for record in records}[error_id] == error
    assert normalize_open_tool_error_records(records)[0]["error_id"] == error_id


def test_native_failure_tracks_and_matching_success_resolves() -> None:
    variables = _RecordingVariables()
    failed = _event(
        data={
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/a.py", "new_string": "changed"},
            "tool_output": "## Next Steps\nfailure",
        },
        failed=True,
    )
    succeeded = _event(
        data={
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/a.py", "new_string": "fixed"},
            "tool_output": "ok",
        },
        failed=False,
    )

    track_tool_outcome(variables, "session-1", failed)
    track_tool_outcome(variables, "session-1", succeeded)

    assert len(variables.upserts) == 1
    _, tool, target_key, error, _ = variables.upserts[0]
    assert tool == "Edit"
    assert target_key.startswith("/repo/a.py#")
    assert error == "\\#\\# Next Steps failure"
    assert variables.resolutions == [("session-1", tool, target_key)]


def test_proxy_identity_uses_nested_route_and_wrapper_echo_is_skipped() -> None:
    event = _event(
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#18819"},
            },
            "mcp_server": "gobby-tasks",
            "mcp_tool": "close_task",
        },
        failed=True,
    )

    assert normalize_tool_identity(event) == (
        "gobby-tasks/close_task",
        {"task_id": "#18819"},
    )
    assert is_wrapper_echo_event(event)


def test_content_only_call_tool_error_produces_nonempty_snippet() -> None:
    result = CallToolResult(
        is_error=True,
        content=[TextContent(type="text", text="content-only failure")],
    )

    assert extract_error_snippet(result) == "content-only failure"
    assert extract_error_snippet(object())


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ({"error": "top-level"}, "top-level"),
        ({"tool_response": {"error": {"message": "nested"}}}, "nested"),
        ({"structuredContent": {"error": "structured"}}, "structured"),
        ("native string failure", "native string failure"),
    ],
)
def test_error_snippet_covers_supported_failure_shapes(
    source: object,
    expected: str,
) -> None:
    assert extract_error_snippet(source) == expected


def test_raw_result_classifier_covers_proxy_result_shapes() -> None:
    content_error = CallToolResult(
        is_error=True,
        content=[TextContent(type="text", text="failed")],
    )

    assert (
        classify_raw_tool_result({"result": {"success": False, "error": "failed"}}).status
        is ToolOutcomeStatus.FAILED
    )
    assert classify_raw_tool_result(content_error).status is ToolOutcomeStatus.FAILED
    assert classify_raw_tool_result({"value": 42}).status is ToolOutcomeStatus.SUCCEEDED
    assert classify_raw_tool_result("plain success").status is ToolOutcomeStatus.SUCCEEDED
    assert classify_raw_tool_result(None).status is ToolOutcomeStatus.FAILED


def test_argument_fingerprint_is_canonical_across_mapping_order() -> None:
    first = extract_target_key(
        {"tool_name": "close_task"},
        {"task_id": "#1", "nested": {"b": 2, "a": 1}},
    )
    second = extract_target_key(
        {"tool_name": "close_task"},
        {"nested": {"a": 1, "b": 2}, "task_id": "#1"},
    )

    assert first == second
    assert first.startswith("args:")


def test_recursive_and_deep_payloads_have_bounded_stable_identity() -> None:
    recursive: dict[str, Any] = {}
    recursive["self"] = recursive
    deep: dict[str, Any] = {"leaf": "failure"}
    for _ in range(32):
        deep = {"nested": deep}

    first = extract_target_key({"tool_name": "exec"}, recursive)
    second = extract_target_key({"tool_name": "exec"}, recursive)
    snippet = extract_error_snippet({"error": deep})

    assert first == second
    assert first
    assert snippet


def test_proxy_outcome_classes_apply_caller_and_final_resolution_policy() -> None:
    variables = _RecordingVariables()
    caller = ("server-a", "run", {"command": "echo original"})
    final = ("server-b", "fixed", {"command": "echo fixed"})

    track_proxy_outcome(
        variables,
        "session-1",
        caller,
        final,
        {"success": False, "error": "denied"},
        "policy_denied",
    )
    track_proxy_outcome(
        variables,
        "session-1",
        caller,
        final,
        {"success": False, "error": "invalid"},
        "invalid_call",
    )
    assert variables.upserts == []
    assert variables.resolutions == []

    track_proxy_outcome(
        variables,
        "session-1",
        caller,
        final,
        {"success": False, "error": "workflow failed"},
        "failed_pre_dispatch",
    )
    assert variables.upserts[-1][1:4] == (
        "server-a/run",
        extract_target_key({"tool_name": "run"}, caller[2]),
        "workflow failed",
    )

    variables.upserts.clear()
    variables.resolutions.clear()
    track_proxy_outcome(
        variables,
        "session-1",
        caller,
        final,
        {"success": False, "error": "dispatch failed"},
        "executed",
    )
    assert variables.resolutions == [
        (
            "session-1",
            "server-a/run",
            extract_target_key({"tool_name": "run"}, caller[2]),
        )
    ]
    assert variables.upserts[-1][1:4] == (
        "server-b/fixed",
        extract_target_key({"tool_name": "fixed"}, final[2]),
        "dispatch failed",
    )

    variables.upserts.clear()
    variables.resolutions.clear()
    track_proxy_outcome(
        variables,
        "session-1",
        caller,
        final,
        {"success": True},
        "executed",
    )
    assert variables.upserts == []
    assert variables.resolutions == [
        (
            "session-1",
            "server-a/run",
            extract_target_key({"tool_name": "run"}, caller[2]),
        ),
        (
            "session-1",
            "server-b/fixed",
            extract_target_key({"tool_name": "fixed"}, final[2]),
        ),
    ]


def test_unknown_proxy_outcome_class_logs_and_fails_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    variables = _RecordingVariables()

    with caplog.at_level(logging.WARNING, logger="gobby.hooks.tool_error_tracker"):
        track_proxy_outcome(
            variables,
            "session-1",
            ("server-a", "run", {}),
            ("server-b", "fixed", {}),
            {"success": False, "error": "future outcome"},
            "future_outcome",
        )

    assert variables.upserts == []
    assert variables.resolutions == []
    assert "Ignoring unknown proxy outcome class" in caplog.text


def test_same_identity_executed_failure_updates_without_prior_resolution() -> None:
    variables = _RecordingVariables()
    identity = ("server-a", "run", {"command": "false"})

    track_proxy_outcome(
        variables,
        "session-1",
        identity,
        identity,
        {"success": False, "error": "failed"},
        "executed",
    )

    assert len(variables.upserts) == 1
    assert variables.resolutions == []


def test_tracker_record_normalization_is_byte_identical_and_lossless() -> None:
    timestamp = "2026-07-23T12:00:00+00:00"
    error = "e" * 900
    records = [
        {
            "tool": "t" * MAX_IDENTITY_COMPONENT_CHARS,
            "target_key": "p" * MAX_IDENTITY_COMPONENT_CHARS,
            "error": error,
            "first_at": timestamp,
            "last_at": timestamp,
            "count": MAX_TOOL_ERROR_COUNT,
        }
        for _ in range(MAX_OPEN_TOOL_ERRORS)
    ]

    normalized = normalize_open_tool_error_records(records)

    assert all(record["error"] == error for record in normalized)
    assert all(record["error_id"].startswith("error-") for record in normalized)
    assert normalize_open_tool_error_records(normalized) == normalized


def test_long_parseable_fractional_timestamp_is_reemitted_canonically() -> None:
    long_timestamp = "2026-07-23T12:00:00." + ("1" * 5_000) + "+00:00"
    record = {
        "tool": "Edit",
        "target_key": "/repo/a.py#12345678",
        "error": "failed",
        "first_at": long_timestamp,
        "last_at": long_timestamp,
        "count": 1,
    }

    normalized = normalize_open_tool_error_records([record])

    assert normalized[0]["first_at"] == "2026-07-23T12:00:00+00:00"
    assert normalized[0]["last_at"] == "2026-07-23T12:00:00+00:00"


def test_no_resolvable_session_never_mutates_error_state() -> None:
    variables = _RecordingVariables()
    identity: tuple[str, str, dict[str, object]] = ("server-a", "run", {})

    track_proxy_outcome(
        variables,
        None,
        identity,
        identity,
        {"success": False, "error": "failed"},
        "executed",
    )

    assert variables.upserts == []
    assert variables.resolutions == []
