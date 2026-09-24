"""JSON Schema argument checks for the MCP proxy."""

from __future__ import annotations

from gobby.mcp_proxy.services.argument_validation import check_arguments


def test_enum_value_outside_declared_values_is_rejected() -> None:
    schema = {
        "type": "object",
        "properties": {
            "response_detail": {"type": "string", "enum": ["concise", "diagnostic"]},
        },
    }

    rejected = check_arguments({"response_detail": "summary"}, schema)
    accepted = check_arguments({"response_detail": "diagnostic"}, schema)

    assert rejected == [
        "Invalid value for parameter 'response_detail': expected one of 'concise', 'diagnostic'"
    ]
    assert accepted == []


def test_enum_inside_anyof_branch_is_enforced() -> None:
    schema = {
        "type": "object",
        "properties": {
            "mode": {
                "anyOf": [
                    {"type": "string", "enum": ["concise", "diagnostic"]},
                    {"type": "null"},
                ]
            }
        },
    }

    assert check_arguments({"mode": None}, schema) == []
    assert check_arguments({"mode": "diagnostic"}, schema) == []
    assert check_arguments({"mode": "summary"}, schema) == [
        "Invalid value for parameter 'mode': expected one of 'concise', 'diagnostic'"
    ]
