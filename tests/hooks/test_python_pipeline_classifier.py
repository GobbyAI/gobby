"""Tests for read-only inline Python pipeline classification."""

from typing import Any

import pytest

from gobby.hooks._python_pipeline_classifier import _is_read_only_python_pipeline
from gobby.hooks.normalization import normalize_tool_fields


def _classify_python_pipeline(script: str) -> bool:
    return _is_read_only_python_pipeline(["python3", "-c", script])


@pytest.mark.parametrize("callback", ["__import__", "eval", "exec", "compile", "getattr"])
def test_python_pipeline_normalization_rejects_forbidden_callback_names(
    callback: str,
) -> None:
    assert not _classify_python_pipeline(f"list(map({callback}, []))")


@pytest.mark.parametrize(
    "script",
    [
        'list(map(__import__, ["os"]))',
        'list(filter(exec, ["pass"]))',
        'import sys; sorted(["x"], key=lambda value: sys.modules["builtins"].eval)',
        'import sys; min(["x"], key=lambda value: sys.modules["builtins"].eval)',
        'import sys; max(["x"], key=lambda value: sys.modules["builtins"].eval)',
        'import sys; iter(sys.modules["builtins"].eval, "")',
        'import sys; next(sys.modules["builtins"].iter([]))',
        'sorted(["x"], key=lambda value: value.__class__)',
        "import sys; sys.stdin = []",
        "values = [1]; values[0] = 2",
        (
            'import sys; list(map(sys.modules["builtins"].eval, '
            '["__import__(\\"os\\").system(\\"touch src/pwn\\")"]))'
        ),
    ],
)
def test_python_pipeline_normalization_rejects_higher_order_callback_escapes(
    script: str,
) -> None:
    assert not _classify_python_pipeline(script)


@pytest.mark.parametrize(
    "script",
    [
        "import json, sys; print(json.load(sys.stdin))",
        "import sys; lines = sys.stdin.readlines(); print(sorted(lines))",
        "import sys; x = sys.stdin.readlines(); print(len(list(filter(None, x))))",
        "import sys; print(list(map(str, sys.stdin.readlines())))",
        ("import sys; print(sorted(sys.stdin.readlines(), key=lambda line: line.lower()))"),
    ],
)
def test_python_pipeline_normalization_keeps_legitimate_read_only_scripts_safe(
    script: str,
) -> None:
    assert _classify_python_pipeline(script)


@pytest.mark.parametrize(
    "script",
    [
        'list(map(__import__, ["os"]))',
        'list(filter(exec, ["pass"]))',
        'import sys; sorted(["x"], key=lambda value: sys.modules["builtins"].eval)',
    ],
)
def test_python_pipeline_normalization_drops_callback_escape_read_only_classification(
    script: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "exec_command",
        "tool_input": {"command": f"gcode outline src/app.py | python3 -c '{script}'"},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert "canonical_code_index_navigation" not in data


@pytest.mark.parametrize(
    "script",
    [
        "import sys; lines = sys.stdin.readlines(); print(sorted(lines))",
        "import sys; x = sys.stdin.readlines(); print(len(list(filter(None, x))))",
    ],
)
def test_python_pipeline_normalization_preserves_safe_read_only_classification(
    script: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "exec_command",
        "tool_input": {"command": f"gcode outline src/app.py | python3 -c '{script}'"},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "read"
    assert data["canonical_code_index_navigation"] is True
    assert "canonical_repo_mutation" not in data
