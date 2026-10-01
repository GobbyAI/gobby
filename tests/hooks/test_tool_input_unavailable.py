"""A string tool input that is not a JSON object is marked, never coerced to {} (#23168)."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.hooks.normalization import normalize_tool_fields, tool_input_error

pytestmark = pytest.mark.unit

TRUNCATED = '{"file_path": "/repo/src/app.py", "content": "VALUE = '


@pytest.mark.parametrize(
    ("data", "field", "code"),
    [
        ({"tool_name": "Write", "toolArgs": TRUNCATED}, "toolArgs", "invalid_json"),
        ({"tool_name": "Write", "tool_input": TRUNCATED}, "tool_input", "invalid_json"),
        ({"tool_name": "Write", "parameters": TRUNCATED}, "parameters", "invalid_json"),
        ({"tool_name": "Write", "args": TRUNCATED}, "args", "invalid_json"),
        ({"tool_name": "Read", "toolArgs": "[1]"}, "toolArgs", "non_object_json"),
        ({"tool_name": "Bash", "tool_input": '"ls"'}, "tool_input", "non_object_json"),
    ],
    ids=["toolArgs", "tool_input", "parameters", "args", "array", "json-string"],
)
def test_unparseable_string_tool_input_is_marked_without_content(
    data: dict[str, Any], field: str, code: str
) -> None:
    normalize_tool_fields(data)

    assert "tool_input" not in data
    assert data["tool_input_error"] == {"field": field, "code": code}
    assert tool_input_error(data) == {"field": field, "code": code}


def test_object_string_tool_input_is_parsed() -> None:
    data: dict[str, Any] = {"tool_name": "Read", "tool_input": '{"path": "/repo/a.py"}'}

    normalize_tool_fields(data)

    assert data["tool_input"] == {"path": "/repo/a.py", "file_path": "/repo/a.py"}
    assert tool_input_error(data) is None


def test_marker_survives_a_second_normalization_pass() -> None:
    data: dict[str, Any] = {"tool_name": "Write", "tool_input": TRUNCATED}

    normalize_tool_fields(data)
    normalize_tool_fields(data)

    assert "tool_input" not in data
    assert tool_input_error(data) == {"field": "tool_input", "code": "invalid_json"}


@pytest.mark.parametrize(
    "marker",
    [
        "invalid_json",
        {"field": "tool_input"},
        {"field": "tool_input", "code": "invalid_json", "content": TRUNCATED},
        {"field": "elsewhere", "code": "invalid_json"},
        {"field": "tool_input", "code": "truncated"},
    ],
    ids=["string", "missing-code", "extra-key", "unknown-field", "unknown-code"],
)
def test_malformed_sender_marker_is_dropped(marker: object) -> None:
    data: dict[str, Any] = {"tool_name": "Bash", "tool_input_error": marker}

    normalize_tool_fields(data)

    assert "tool_input_error" not in data
    assert tool_input_error(data) is None


def test_object_tool_input_clears_a_sender_marker() -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_input_error": {"field": "tool_input", "code": "invalid_json"},
    }

    normalize_tool_fields(data)

    assert data["tool_input"] == {"command": "ls"}
    assert "tool_input_error" not in data


def test_freeform_apply_patch_string_still_becomes_write_input() -> None:
    patch = "*** Begin Patch\n*** Add File: /repo/new.py\n+VALUE = 1\n*** End Patch\n"
    data: dict[str, Any] = {"tool_name": "apply_patch", "toolArgs": patch}

    normalize_tool_fields(data)

    assert data["tool_name"] == "Write"
    assert data["tool_input"]["file_path"] == "/repo/new.py"
    assert data["tool_input"]["patch"] == patch
    assert tool_input_error(data) is None
