"""Tests for the optional same-session monolith enforcement rules."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.monolith_guard import (
    MONOLITH_SOURCE_EXTENSIONS,
    is_monolith_guard_path,
    outstanding_monolith_paths,
    projected_monolith_paths,
)
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

RULE_NAMES = (
    "require-decompose-monolith-before-threshold-write",
    "require-monolith-resolution-before-commit",
    "require-monolith-resolution-before-task-transition",
    "require-monolith-resolution-before-turn-end",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _lines(count: int, *, first: str = "line 0") -> str:
    return "\n".join([first, *(f"line {index}" for index in range(1, count))])


def _rust_with_inline_tests(count: int) -> str:
    prefix = ["pub fn value() -> usize { 1 }", "", "#[cfg(test)]", "mod tests {"]
    suffix = ["}"]
    body_count = count - len(prefix) - len(suffix)
    return "\n".join(
        [*prefix, *(f"    // inline test line {index}" for index in range(body_count)), *suffix]
    )


def _write_lines(root: Path, relative_path: str, count: int) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_lines(count), encoding="utf-8")
    return path


def _condition_result(
    root: Path,
    tool_input: dict[str, Any],
    *,
    loaded_skills: list[str] | None = None,
) -> bool:
    event_data = {
        "canonical_tool_kind": "write",
        "canonical_file_paths": [tool_input["file_path"]],
    }
    context = {
        "variables": {"loaded_skills": loaded_skills or []},
        "event": SimpleNamespace(data=event_data),
        "tool_input": tool_input,
        "project": {"path": str(root)},
    }
    evaluator = SafeExpressionEvaluator(
        context=context,
        allowed_funcs=build_condition_helpers(context=context),
    )
    return evaluator.evaluate(
        "event.data.get('canonical_tool_kind') == 'write' and "
        "bool(projected_monolith_paths(tool_input, event.data)) and "
        "not skill_loaded('decompose-monolith')"
    )


@pytest.mark.parametrize("extension", sorted(MONOLITH_SOURCE_EXTENSIONS))
def test_new_thousand_line_file_triggers_for_every_supported_extension(
    tmp_path: Path,
    extension: str,
) -> None:
    path = f"src/new_file{extension}"

    result = projected_monolith_paths(
        {"file_path": path, "content": _lines(1_000)},
        tmp_path,
    )

    assert result == [path]


@pytest.mark.parametrize(
    ("line_count", "expected"),
    [(999, []), (1_000, ["crates/example/src/lib.rs"])],
)
def test_rust_inline_tests_count_toward_file_ceiling(
    tmp_path: Path,
    line_count: int,
    expected: list[str],
) -> None:
    path = "crates/example/src/lib.rs"

    result = projected_monolith_paths(
        {"file_path": path, "content": _rust_with_inline_tests(line_count)},
        tmp_path,
    )

    assert result == expected


def test_targeted_edit_allows_999_lines_without_growth(tmp_path: Path) -> None:
    _write_lines(tmp_path, "src/app.py", 999)

    result = projected_monolith_paths(
        {"file_path": "src/app.py", "old_string": "line 0", "new_string": "changed"},
        tmp_path,
    )

    assert result == []


def test_targeted_edit_detects_999_to_1000_crossing(tmp_path: Path) -> None:
    _write_lines(tmp_path, "src/app.py", 999)

    result = projected_monolith_paths(
        {
            "file_path": "src/app.py",
            "old_string": "line 0",
            "new_string": "changed\ninserted",
        },
        tmp_path,
    )

    assert result == ["src/app.py"]


def test_existing_thousand_line_file_triggers_even_when_write_shrinks_it(
    tmp_path: Path,
) -> None:
    _write_lines(tmp_path, "src/app.py", 1_000)

    result = projected_monolith_paths(
        {"file_path": "src/app.py", "content": _lines(20)},
        tmp_path,
    )

    assert result == ["src/app.py"]


def test_multi_file_apply_patch_reports_only_threshold_crossing_path(
    tmp_path: Path,
) -> None:
    _write_lines(tmp_path, "src/first.py", 999)
    _write_lines(tmp_path, "src/second.ts", 998)
    patch = """*** Begin Patch
*** Update File: src/first.py
@@
+one line
*** Update File: src/second.ts
@@
+one line
*** End Patch
"""

    result = projected_monolith_paths(
        {
            "patch": patch,
            "file_paths": ["src/first.py", "src/second.ts"],
        },
        tmp_path,
    )

    assert result == ["src/first.py"]


def test_apply_patch_creation_and_deletion_are_projected(tmp_path: Path) -> None:
    _write_lines(tmp_path, "src/old.py", 1_000)
    additions = "\n".join(f"+line {index}" for index in range(1_000))
    patch = (
        "*** Begin Patch\n"
        "*** Delete File: src/old.py\n"
        "*** Add File: src/new.rs\n"
        f"{additions}\n"
        "*** End Patch\n"
    )

    result = projected_monolith_paths(
        {"patch": patch, "file_paths": ["src/old.py", "src/new.rs"]},
        tmp_path,
    )

    assert result == ["src/old.py", "src/new.rs"]


@pytest.mark.parametrize(
    "relative_path",
    [
        "tests/test_app.py",
        "src/app_test.ts",
        "src/widget.spec.tsx",
        "docs/example.js",
        "generated/client.py",
        "vendor/library.rs",
        "crates/example/src/module/tests.rs",
        "baselines/output.css",
        "fixtures/sample.sh",
        "src/schema.generated.ts",
        "README.md",
    ],
)
def test_excluded_artifacts_never_trigger(tmp_path: Path, relative_path: str) -> None:
    result = projected_monolith_paths(
        {"file_path": relative_path, "content": _lines(1_000)},
        tmp_path,
    )

    assert result == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/gobby/build/coordinator.py",
        "src/gobby/build/service.py",
        "src/gobby/dist/manifest.ts",
        "src/gobby/target/config.rs",
    ],
)
def test_production_packages_named_like_output_directories_are_guarded(
    relative_path: str,
) -> None:
    assert is_monolith_guard_path(relative_path) is True


@pytest.mark.parametrize(
    "relative_path",
    [
        "build/generated.py",
        "dist/bundle.js",
        "target/generated.rs",
        "web/dist/bundle.js",
        "crates/gcode/target/debug/generated.rs",
        "build/src/generated.py",
    ],
)
def test_output_directories_outside_source_roots_are_excluded(relative_path: str) -> None:
    assert is_monolith_guard_path(relative_path) is False


def test_completion_guard_uses_only_session_task_attribution(tmp_path: Path) -> None:
    _write_lines(tmp_path, "src/owned.py", 1_000)
    _write_lines(tmp_path, "src/foreign.py", 1_000)
    variables = {"task_edited_files": {"task-1": ["src/owned.py"]}}

    result = outstanding_monolith_paths(variables, tmp_path)

    assert result == ["src/owned.py"]


def test_completion_guard_clears_after_decomposition_or_deletion(tmp_path: Path) -> None:
    owned = _write_lines(tmp_path, "src/owned.py", 999)
    variables = {
        "task_edited_files": {
            "task-1": ["src/owned.py", "src/deleted.py"],
        }
    }

    assert outstanding_monolith_paths(variables, tmp_path) == []

    owned.write_text(_lines(1_000), encoding="utf-8")
    assert outstanding_monolith_paths(variables, tmp_path) == ["src/owned.py"]


def test_loading_skill_unlocks_write_but_never_completion_ceiling(tmp_path: Path) -> None:
    _write_lines(tmp_path, "src/app.py", 999)
    tool_input = {
        "file_path": "src/app.py",
        "old_string": "line 0",
        "new_string": "changed\ninserted",
    }

    assert _condition_result(tmp_path, tool_input) is True
    assert (
        _condition_result(
            tmp_path,
            tool_input,
            loaded_skills=["decompose-monolith"],
        )
        is False
    )

    _write_lines(tmp_path, "src/app.py", 1_000)
    context = {
        "variables": {
            "loaded_skills": ["decompose-monolith"],
            "task_edited_files": {"task-1": ["src/app.py"]},
        },
        "project": {"path": str(tmp_path)},
    }
    helpers = build_condition_helpers(context=context)
    assert helpers["outstanding_monolith_paths"]() == ["src/app.py"]


def test_bundled_sync_installs_enabled_rules_and_preserves_the_user_toggle(
    temp_db: HubDatabase,
) -> None:
    """66dbca284 (#19408) flipped these templates to enabled, matching the DB.

    A resync must still preserve whatever the user set, so the toggle is
    flipped the other way here than it was when the templates shipped disabled.
    """
    manager = RuleDefinitionManager(temp_db)
    sync_bundled_rules(temp_db, get_bundled_rules_path())

    rows = [manager.get_by_name(name) for name in RULE_NAMES]
    assert all(row is not None and row.enabled is True for row in rows)

    write_row = rows[0]
    assert write_row is not None
    manager.update(write_row.id, enabled=False)
    sync_bundled_rules(temp_db, get_bundled_rules_path())

    refreshed = manager.get_by_name(RULE_NAMES[0])
    assert refreshed is not None
    assert refreshed.enabled is False


def test_bundled_rules_cover_commit_transitions_turn_end_and_required_guidance(
    temp_db: HubDatabase,
) -> None:
    manager = RuleDefinitionManager(temp_db)
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    bodies: dict[str, RuleDefinitionBody] = {}
    for name in RULE_NAMES:
        row = manager.get_by_name(name)
        assert row is not None
        bodies[name] = RuleDefinitionBody.model_validate(row.definition_json)

    commit = bodies[RULE_NAMES[1]]
    commit_effect = commit.resolved_effects[0]
    assert commit_effect.tools == ["Bash"]
    assert commit_effect.command_pattern == r"\bgit\s+commit\b"

    transition_effect = bodies[RULE_NAMES[2]].resolved_effects[0]
    assert set(transition_effect.mcp_tools or []) == {
        "gobby-tasks:close_task",
        "gobby-tasks:de_escalate_task",
        "gobby-tasks-ops:submit_for_review",
        "gobby-tasks-ops:approve_review",
        "gobby-tasks-ops:reject_review",
    }
    assert bodies[RULE_NAMES[3]].event == "turn_end"

    reasons = "\n".join(body.resolved_effects[0].reason or "" for body in bodies.values()).lower()
    assert "decompose-monolith" in reasons
    assert "code-index" in reasons
    assert "current claimed task and session" in reasons
    assert "deferred refactor tasks are prohibited" in reasons


def test_task_skill_removes_follow_up_refactor_task_direction() -> None:
    body = (PROJECT_ROOT / "src/gobby/install/shared/skills/tasks/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "finish the decomposition inside the current" in body
    assert "claimed task and session" in body
    assert "Deferred refactor tasks are prohibited" in body
    assert "newly created one left unclaimed" not in body
