"""Tests for tdd-enforcement rules.

Verifies the enforce-tdd-block and enforce-tdd-track-tests rules
sync correctly, have valid structure, and evaluate conditions properly.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import sync_bundled_rules
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


def _normalized_bash_event(command: str, event_type: HookEventType) -> HookEvent:
    data: dict[str, object] = {"tool_name": "Bash", "tool_input": {"command": command}}
    normalize_tool_fields(data)
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


TDD_ENFORCEMENT_RULES = {
    "enforce-tdd-block",
    "enforce-tdd-track-tests",
}


def test_block_holds_until_named_acceptance_test_is_written(
    db: HubDatabase,
    manager: RuleDefinitionManager,
) -> None:
    _sync_bundled(db)
    block_row = manager.get_by_name("enforce-tdd-block")
    track_row = manager.get_by_name("enforce-tdd-track-tests")
    assert block_row is not None
    assert track_row is not None
    block = RuleDefinitionBody.model_validate(block_row.definition_json)
    track = RuleDefinitionBody.model_validate(track_row.definition_json)
    acceptance_path = "crates/gclient/tests/parity/sidebar.rs"
    source_path = "crates/gclient/src/ui/sidebar/agents.rs"

    def evaluate(
        body: RuleDefinitionBody,
        variables: dict[str, object],
        path: str,
    ) -> tuple[bool, SafeExpressionEvaluator, dict[str, object]]:
        event_data: dict[str, object] = {
            "canonical_tool_kind": "write",
            "canonical_file_path": path,
            "canonical_file_paths": [path],
        }
        context: dict[str, object] = {
            "variables": variables,
            "event": type("E", (), {"data": event_data})(),
            "tool_input": {"file_path": f"/repo/{path}"},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        assert body.when is not None
        return evaluator.evaluate(body.when), evaluator, {**context, **allowed_funcs}

    variables: dict[str, object] = {
        "enforce_tdd": False,
        "claimed_task_requires_tdd": True,
        "claimed_task_acceptance_test_paths": [acceptance_path],
        "tdd_tests_written": [],
    }
    blocked, _, render_context = evaluate(block, variables, source_path)
    assert blocked is True
    assert evaluate(block, variables, source_path)[0] is True
    assert [effect.type for effect in block.resolved_effects] == ["block"]
    block_reason = block.resolved_effects[0].reason
    assert block_reason is not None
    rendered_reason = TemplateEngine().render(block_reason, render_context)
    assert acceptance_path in rendered_reason
    assert "`test-driven-development` skill's `Rust Stub-First RED` sequence" in rendered_reason
    for duplicated_policy in ("Compiler failures", "assertion or panic", "reconstructed RED"):
        assert duplicated_policy not in rendered_reason
    legacy_variable = "tdd_" + "nudged_files"
    assert legacy_variable not in json.dumps(block_row.definition_json)
    assert all(effect.type != "mcp_call" for effect in block.resolved_effects)

    tracked, track_evaluator, _ = evaluate(track, variables, acceptance_path)
    assert tracked is True
    track_effect = track.resolved_effects[0]
    assert track_effect.value is not None
    variables["tdd_tests_written"] = track_evaluator.evaluate_value(track_effect.value)
    assert variables["tdd_tests_written"] == [acceptance_path]
    # Tracking is observability; only a resolved task's retained named RED opens the gate.
    assert evaluate(block, variables, source_path)[0] is True

    fallback_variables: dict[str, object] = {
        "enforce_tdd": True,
        "claimed_task_requires_tdd": False,
        "claimed_task_acceptance_test_paths": [],
        "tdd_tests_written": [],
    }
    fallback_test_path = "web/src/app.spec.ts"
    assert evaluate(block, fallback_variables, "web/src/app.ts")[0] is True
    tracked, fallback_evaluator, _ = evaluate(track, fallback_variables, fallback_test_path)
    assert tracked is True
    fallback_variables["tdd_tests_written"] = fallback_evaluator.evaluate_value(track_effect.value)
    assert evaluate(block, fallback_variables, "web/src/app.ts")[0] is True


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
@pytest.mark.parametrize("tool_name", ["Write", "Edit", "apply_patch"])
@pytest.mark.asyncio
async def test_named_rust_acceptance_file_is_tracked_before_source_writes(
    db: HubDatabase,
    *,
    absolute: bool,
    tool_name: str,
) -> None:
    _sync_bundled(db)
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name IN (%s, %s)",
            ("enforce-tdd-block", "enforce-tdd-track-tests"),
        )
    engine = RuleEngine(db)
    acceptance_path = "crates/gcode/src/communities/remap_tests.rs"
    source_path = "crates/gcode/src/communities/remap.rs"
    tool_path = f"/repo/{acceptance_path}" if absolute else acceptance_path

    def acceptance_event(event_type: HookEventType) -> HookEvent:
        if tool_name == "apply_patch":
            tool_input: object = (
                "*** Begin Patch\n"
                f"*** Update File: {tool_path}\n"
                "@@\n"
                "-fn old_test() {}\n"
                "+fn new_test() {}\n"
                "*** End Patch\n"
            )
        elif tool_name == "Edit":
            tool_input = {
                "file_path": tool_path,
                "old_string": "fn old_test() {}",
                "new_string": "fn new_test() {}",
            }
        else:
            tool_input = {"file_path": tool_path, "content": "fn new_test() {}\n"}
        return HookEvent(
            event_type=event_type,
            session_id="test-session",
            source=SessionSource.CODEX if tool_name == "apply_patch" else SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            cwd="/repo",
            data={"tool_name": tool_name, "tool_input": tool_input},
            metadata={"project_path": "/repo"},
        )

    def source_event() -> HookEvent:
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            cwd="/repo",
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": source_path, "content": "pub fn remap() {}\n"},
            },
            metadata={"project_path": "/repo"},
        )

    def tdd_variables() -> dict[str, object]:
        return {
            "enforce_tdd": False,
            "claimed_task_requires_tdd": True,
            "claimed_task_acceptance_test_paths": [acceptance_path],
            "tdd_tests_written": [],
            "project": {"path": "/repo"},
        }

    unopened_variables = tdd_variables()
    blocked = await engine.evaluate(
        source_event(), session_id="test-session", variables=unopened_variables
    )
    assert blocked.decision == "block"

    variables = tdd_variables()
    before_test = await engine.evaluate(
        acceptance_event(HookEventType.BEFORE_TOOL),
        session_id="test-session",
        variables=variables,
    )
    assert before_test.decision == "allow"
    await engine.evaluate(
        acceptance_event(HookEventType.AFTER_TOOL),
        session_id="test-session",
        variables=variables,
    )
    assert variables["tdd_tests_written"] == [acceptance_path]
    after_test = await engine.evaluate(
        source_event(), session_id="test-session", variables=variables
    )
    assert after_test.decision == "block"


@pytest.mark.asyncio
async def test_multi_file_patch_with_acceptance_and_source_is_blocked(
    db: HubDatabase,
) -> None:
    _sync_bundled(db)
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
            ("enforce-tdd-block",),
        )
    engine = RuleEngine(db)
    acceptance_path = "crates/gcode/src/communities/remap_acceptance.rs"
    source_path = "crates/gcode/src/communities/remap.rs"
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {acceptance_path}\n"
        "@@\n"
        "-fn old_test() {}\n"
        "+fn new_test() {}\n"
        f"*** Update File: {source_path}\n"
        "@@\n"
        "-pub fn old_remap() {}\n"
        "+pub fn new_remap() {}\n"
        "*** End Patch\n"
    )
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd="/repo",
        data={"tool_name": "apply_patch", "tool_input": patch},
        metadata={"project_path": "/repo"},
    )
    variables: dict[str, object] = {
        "enforce_tdd": False,
        "claimed_task_requires_tdd": True,
        "claimed_task_acceptance_test_paths": [acceptance_path],
        "tdd_tests_written": [],
        "project": {"path": "/repo"},
    }

    result = await engine.evaluate(event, session_id="test-session", variables=variables)

    assert result.decision == "block"


def test_named_acceptance_path_overrides_test_convention_classifier() -> None:
    acceptance_path = "crates/gcode/src/communities/remap_acceptance.rs"
    event_data = {
        "canonical_file_path": f"/repo/{acceptance_path}",
        "canonical_file_paths": [f"/repo/{acceptance_path}"],
    }
    context: dict[str, object] = {
        "variables": {"claimed_task_acceptance_test_paths": [acceptance_path]},
        "project": {"path": "/repo"},
    }
    helpers = build_condition_helpers(context=context)

    assert helpers["first_tdd_code_path"](event_data, {}) == ""
    assert helpers["first_tdd_test_path"](event_data, {}) == acceptance_path


# --- Named RED write barrier ---

_GATE_START = datetime(2026, 10, 19, tzinfo=UTC)
_GATE_SESSION = "0b6f2d51-4a4e-4c55-9d1e-246746000001"
_GATE_TASK = "0b6f2d51-4a4e-4c55-9d1e-246746000002"
_FEATURE_TEST = "tests/test_feature.py"
_OTHER_TEST = "tests/test_other.py"
_FEATURE_REF = f"{_FEATURE_TEST}::test_feature"
_OTHER_REF = f"{_OTHER_TEST}::test_other"
_FEATURE_CRITERIA = f"1. The feature returns two; test: {_FEATURE_REF}"
_BOTH_CRITERIA = f"{_FEATURE_CRITERIA}\n2. The other path agrees; test: {_OTHER_REF}"
_FEATURE_BODY = "def test_feature():\n    assert feature() == 2\n"
_OTHER_BODY = "def test_other():\n    assert other() == 2\n"
_IMPLEMENTATION = "def feature() -> int:\n    return 2\n"
_LEGACY = "def legacy(value: int) -> int:\n    return value\n"
_RUST_LEGACY = "pub fn legacy(value: u32) -> u32 {\n    value\n}\n"


@pytest.fixture
def gate_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src/legacy.py").write_text(_LEGACY, encoding="utf-8")
    rust = repo / "crates/demo/src/lib.rs"
    rust.parent.mkdir(parents=True)
    rust.write_text(_RUST_LEGACY, encoding="utf-8")
    return repo


def _transcript_pair(
    repo: Path, step: int, name: str, arguments: dict[str, Any], result: object
) -> list[dict[str, Any]]:
    call = f"call-{step}"
    at = _GATE_START + timedelta(seconds=step)
    return [
        {
            "type": "assistant",
            "timestamp": at.isoformat(),
            "cwd": str(repo),
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call, "name": name, "input": arguments}],
            },
        },
        {
            "type": "user",
            "timestamp": (at + timedelta(milliseconds=500)).isoformat(),
            "cwd": str(repo),
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call, "content": result}],
            },
        },
    ]


def _claim_and_tests(repo: Path, tests: dict[str, str] | None = None) -> list[dict[str, Any]]:
    claim = json.dumps({"success": True, "result": {"success": True, "task_id": _GATE_TASK}})
    records = _transcript_pair(
        repo,
        0,
        "mcp__gobby__call_tool",
        {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
            "arguments": {"task_id": "#24746"},
        },
        [{"type": "text", "text": claim}],
    )
    for step, (path, body) in enumerate((tests or {_FEATURE_TEST: _FEATURE_BODY}).items(), 1):
        (repo / path).write_text(body, encoding="utf-8")
        records += _transcript_pair(
            repo, step, "Write", {"file_path": str(repo / path), "content": body}, ""
        )
    return records


def _pytest_records(repo: Path, step: int, output: str, path: str) -> list[dict[str, Any]]:
    return _transcript_pair(repo, step, "Bash", {"command": f"uv run pytest {path} -q"}, output)


def _red_output(path: str, *symbols: str) -> str:
    sections = "".join(
        f"________________ {symbol} ________________\nE   assert 1 == 2\n{path}:2: AssertionError\n"
        for symbol in symbols
    )
    summary = "".join(f"FAILED {path}::{symbol} - assert 1 == 2\n" for symbol in symbols)
    return f"{sections}{summary}{len(symbols)} failed in 0.02s"


def _write(repo: Path, path: str, content: str) -> dict[str, Any]:
    return {"file_path": str(repo / path), "content": content}


def _tdd_block(db: HubDatabase, manager: RuleDefinitionManager) -> RuleDefinitionBody:
    _sync_bundled(db)
    row = manager.get_by_name("enforce-tdd-block")
    assert row is not None
    return RuleDefinitionBody.model_validate(row.definition_json)


def _gate_blocks(
    block: RuleDefinitionBody,
    repo: Path,
    records: list[dict[str, Any]],
    *,
    tool_input: dict[str, Any] | str,
    tool_name: str = "Write",
    criteria: str = _FEATURE_CRITERIA,
    tests_written: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Evaluate the bundled block rule as the engine would for one write."""
    transcript = repo.parent / "gate.jsonl"
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    session = Session(
        id=_GATE_SESSION,
        external_id="gate",
        machine_id="isolated",
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=_GATE_START,
        updated_at=_GATE_START,
    )
    task = SimpleNamespace(
        id=_GATE_TASK,
        seq_num=24746,
        labels=["tdd:required"],
        additional_skills=[],
        validation_criteria=criteria,
        claimed_by_session_id=_GATE_SESSION,
        state=None,
    )
    data: dict[str, object] = {"tool_name": tool_name, "tool_input": tool_input}
    normalize_tool_fields(data)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="gate",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(repo),
        data=data,
        metadata={"project_path": str(repo), "_platform_session_id": _GATE_SESSION},
    )
    context: dict[str, object] = {
        "variables": {
            "enforce_tdd": False,
            "claimed_task_requires_tdd": True,
            "claimed_tasks": {_GATE_TASK: "#24746"},
            "claimed_task_acceptance_test_paths": re.findall(r"test: ([^:\s]+)::", criteria),
            "tdd_tests_written": list(tests_written),
        },
        "event": event,
        "tool_input": tool_input,
        "project": {"path": str(repo)},
    }
    helpers = build_condition_helpers(
        task_manager=SimpleNamespace(get_task=lambda ref: task if ref == _GATE_TASK else None),
        session_manager=SimpleNamespace(get=lambda ref: session if ref == _GATE_SESSION else None),
        context=context,
    )
    assert block.when is not None
    if not SafeExpressionEvaluator(context=context, allowed_funcs=helpers).evaluate(block.when):
        return False, ""
    reason = block.resolved_effects[0].reason
    assert reason is not None
    return True, TemplateEngine().render(reason, {**context, **helpers})


def test_acceptance_file_write_without_named_red_stays_blocked(
    db: HubDatabase, manager: RuleDefinitionManager, gate_repo: Path
) -> None:
    block = _tdd_block(db, manager)

    blocked, reason = _gate_blocks(
        block,
        gate_repo,
        _claim_and_tests(gate_repo),
        tool_input=_write(gate_repo, "src/feature.py", _IMPLEMENTATION),
        tests_written=(_FEATURE_TEST,),
    )

    assert blocked is True
    assert _FEATURE_REF in reason


def test_same_file_other_test_red_does_not_open_gate(
    db: HubDatabase, manager: RuleDefinitionManager, gate_repo: Path
) -> None:
    block = _tdd_block(db, manager)
    records = _claim_and_tests(gate_repo, {_FEATURE_TEST: f"{_FEATURE_BODY}\n\n{_OTHER_BODY}"})
    records += _pytest_records(
        gate_repo, 3, _red_output(_FEATURE_TEST, "test_other"), _FEATURE_TEST
    )

    blocked, reason = _gate_blocks(
        block,
        gate_repo,
        records,
        tool_input=_write(gate_repo, "src/feature.py", _IMPLEMENTATION),
        tests_written=(_FEATURE_TEST,),
    )

    assert blocked is True
    assert _FEATURE_REF in reason


def test_first_green_detector_record_run_stays_blocked(
    db: HubDatabase, manager: RuleDefinitionManager, gate_repo: Path
) -> None:
    block = _tdd_block(db, manager)
    # A record-mode detector's first run writes its corpus and passes: no RED ever ran.
    records = _claim_and_tests(gate_repo)
    records += _pytest_records(gate_repo, 3, "1 passed in 0.01s", _FEATURE_TEST)

    blocked, reason = _gate_blocks(
        block,
        gate_repo,
        records,
        tool_input=_write(gate_repo, "src/feature.py", _IMPLEMENTATION),
        tests_written=(_FEATURE_TEST,),
    )

    assert blocked is True
    assert _FEATURE_REF in reason


def test_all_behavior_reds_open_production_gate(
    db: HubDatabase, manager: RuleDefinitionManager, gate_repo: Path
) -> None:
    block = _tdd_block(db, manager)
    implementation = _write(gate_repo, "src/feature.py", _IMPLEMENTATION)
    records = _claim_and_tests(gate_repo, {_FEATURE_TEST: _FEATURE_BODY, _OTHER_TEST: _OTHER_BODY})
    records += _pytest_records(
        gate_repo, 3, _red_output(_FEATURE_TEST, "test_feature"), _FEATURE_TEST
    )

    blocked, reason = _gate_blocks(
        block, gate_repo, records, tool_input=implementation, criteria=_BOTH_CRITERIA
    )
    assert blocked is True
    assert _OTHER_REF in reason
    assert _FEATURE_REF not in reason

    records += _pytest_records(gate_repo, 4, _red_output(_OTHER_TEST, "test_other"), _OTHER_TEST)
    opened, _ = _gate_blocks(
        block, gate_repo, records, tool_input=implementation, criteria=_BOTH_CRITERIA
    )
    assert opened is False


def test_verified_stub_only_write_can_prepare_red(
    db: HubDatabase, manager: RuleDefinitionManager, gate_repo: Path
) -> None:
    from gobby.workflows.condition_helpers import tdd_stub_only_write

    block = _tdd_block(db, manager)
    records = _claim_and_tests(gate_repo)
    stub = "def feature() -> int:\n    raise NotImplementedError\n"
    patch = (
        f"*** Begin Patch\n*** Add File: {gate_repo / 'src/feature.py'}\n"
        "+def feature() -> int:\n+    raise NotImplementedError\n*** End Patch\n"
    )
    stubs: tuple[tuple[str, dict[str, Any] | str], ...] = (
        ("apply_patch", patch),
        ("Write", _write(gate_repo, "src/feature.py", stub)),
        ("Write", _write(gate_repo, "src/legacy.py", f"{_LEGACY}\n\n{stub}")),
        (
            "Edit",
            {
                "file_path": str(gate_repo / "src/legacy.py"),
                "old_string": _LEGACY,
                "new_string": (
                    "def legacy(value: int, *, scale: int = 1) -> int:\n"
                    "    del scale\n"
                    "    return value\n"
                ),
            },
        ),
        (
            "Edit",
            {
                "file_path": str(gate_repo / "crates/demo/src/lib.rs"),
                "old_string": _RUST_LEGACY,
                "new_string": f"{_RUST_LEGACY}\npub fn remap(input: u32) -> u32 {{\n    todo!()\n}}\n",
            },
        ),
    )

    for tool_name, tool_input in stubs:
        blocked, _ = _gate_blocks(
            block, gate_repo, records, tool_name=tool_name, tool_input=tool_input
        )
        assert blocked is False, tool_input
    implementation, _ = _gate_blocks(
        block, gate_repo, records, tool_input=_write(gate_repo, "src/feature.py", _IMPLEMENTATION)
    )
    assert implementation is True
    assert tdd_stub_only_write(
        {}, {"raw": f"text(await tools.apply_patch({json.dumps(patch)}));"}, str(gate_repo)
    )
    mixed = patch.replace(
        "*** End Patch",
        f"*** Add File: {gate_repo / 'src/behavior.py'}\n"
        "+def behavior():\n+    return 42\n*** End Patch",
    )
    unsafe: tuple[tuple[str, dict[str, Any] | str], ...] = (
        ("apply_patch", mixed),
        (
            "functions.exec",
            {"raw": f"if (false) {{ text(await tools.apply_patch({json.dumps(patch)})); }}"},
        ),
        ("Bash", {"raw": patch, "command": "printf 'VALUE = 42' > src/behavior.py"}),
    )
    for tool_name, tool_input in unsafe:
        assert tdd_stub_only_write({}, tool_input, str(gate_repo)) is False, tool_input
        if tool_name != "functions.exec":
            blocked, _ = _gate_blocks(
                block, gate_repo, records, tool_name=tool_name, tool_input=tool_input
            )
            assert blocked is True, tool_input


def test_stub_plus_implementation_patch_is_blocked(
    db: HubDatabase, manager: RuleDefinitionManager, gate_repo: Path
) -> None:
    block = _tdd_block(db, manager)
    records = _claim_and_tests(gate_repo)
    stub = "def feature() -> int:\n    raise NotImplementedError\n"
    mixed: tuple[tuple[str, dict[str, Any]], ...] = (
        ("Write", _write(gate_repo, "src/feature.py", f"{stub}\n\n{_IMPLEMENTATION}")),
        ("Write", _write(gate_repo, "src/legacy.py", f"{_LEGACY}\n\n{stub}\n\n{_IMPLEMENTATION}")),
        (
            "Write",
            _write(
                gate_repo, "src/legacy.py", f"{stub}\n\n{_LEGACY.replace('value\n', 'value + 1\n')}"
            ),
        ),
        (
            "Edit",
            {
                "file_path": str(gate_repo / "src/legacy.py"),
                "old_string": _LEGACY,
                "new_string": (
                    "def legacy(value: int, *, scale: int = 1) -> int:\n    return value * scale\n"
                ),
            },
        ),
        (
            "Edit",
            {
                "file_path": str(gate_repo / "crates/demo/src/lib.rs"),
                "old_string": _RUST_LEGACY,
                "new_string": (
                    "pub fn legacy(value: u32) -> u32 {\n    value + 1\n}\n"
                    "\npub fn remap(input: u32) -> u32 {\n    todo!()\n}\n"
                ),
            },
        ),
    )

    for tool_name, tool_input in mixed:
        blocked, _ = _gate_blocks(
            block,
            gate_repo,
            records,
            tool_name=tool_name,
            tool_input=tool_input,
            tests_written=(_FEATURE_TEST,),
        )
        assert blocked is True, tool_input


def test_shell_and_structured_write_share_named_red_barrier(
    db: HubDatabase, manager: RuleDefinitionManager, gate_repo: Path
) -> None:
    block = _tdd_block(db, manager)
    records = _claim_and_tests(gate_repo)
    shell = ("Bash", {"command": f"cat > src/feature.py <<'EOF'\n{_IMPLEMENTATION}EOF"})
    shell_stub = (
        "Bash",
        {
            "command": "printf 'def feature() -> int:\\n    raise NotImplementedError\\n' > src/feature.py"
        },
    )
    structured = ("Write", _write(gate_repo, "src/feature.py", _IMPLEMENTATION))

    for tool_name, tool_input in (shell, shell_stub, structured):
        blocked, reason = _gate_blocks(
            block,
            gate_repo,
            records,
            tool_name=tool_name,
            tool_input=tool_input,
            tests_written=(_FEATURE_TEST,),
        )
        assert blocked is True, tool_input
        assert _FEATURE_REF in reason

    records += _pytest_records(
        gate_repo, 3, _red_output(_FEATURE_TEST, "test_feature"), _FEATURE_TEST
    )
    for tool_name, tool_input in (shell, structured):
        blocked, _ = _gate_blocks(
            block, gate_repo, records, tool_name=tool_name, tool_input=tool_input
        )
        assert blocked is False, tool_input


# --- Sync tests ---


class TestTddEnforcementSync:
    """Test that tdd-enforcement rules sync correctly."""

    def test_bundled_file_syncs_all_rules(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Both TDD enforcement rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        assert TDD_ENFORCEMENT_RULES.issubset(rule_names), (
            f"Missing: {TDD_ENFORCEMENT_RULES - rule_names}"
        )

    def test_all_rules_have_group(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        """All rules should have group='tdd-enforcement'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in TDD_ENFORCEMENT_RULES:
                body = row.definition_json
                assert body.get("group") == "tdd-enforcement", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in TDD_ENFORCEMENT_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                assert body.event is not None


# --- enforce-tdd-block structure ---


class TestEnforceTddBlockStructure:
    """Verify enforce-tdd-block rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"

    def test_has_only_block_effect(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        effects = body.resolved_effects
        assert [effect.type for effect in effects] == ["block"]

    def test_block_targets_write_and_bash(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        block_effect = body.resolved_effects[0]
        assert block_effect.tools == ["Write", "Bash"]

    def test_when_checks_tdd_trigger_and_gate(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "enforce_tdd" in body.when
        assert "claimed_task_requires_tdd" in body.when
        assert "canonical_tool_kind" in body.when
        assert "first_tdd_code_path" in body.when
        assert "tdd_gate_open" in body.when


# --- enforce-tdd-block condition evaluation ---


_RUST_ENCODE_BODY = (
    'pub fn encode(frame: &Frame) -> String {\n    format!("frame {}", frame.id)\n}\n'
)

_RUST_PRODUCTION_SOURCE = f"""\
use std::fmt;

pub struct Frame {{
    pub id: u64,
}}

{_RUST_ENCODE_BODY}"""

_RUST_TEST_MODULE = """
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_reports_id() {
        assert_eq!(encode(&Frame { id: 7 }), "frame 7");
    }
}
"""

_RUST_MODULE_SOURCE = _RUST_PRODUCTION_SOURCE + _RUST_TEST_MODULE


def _write_rust_module_file(tmp_path: Path, text: str = _RUST_MODULE_SOURCE) -> str:
    path = tmp_path / "src" / "store.rs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestEnforceTddBlockCondition:
    """Test the when condition evaluates correctly for various file paths."""

    CONDITION = (
        "(variables.get('enforce_tdd') or variables.get('claimed_task_requires_tdd')) "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and first_tdd_code_path(event.data, tool_input) "
        "and not tdd_gate_open(variables)"
    )

    def _eval(
        self,
        file_path: str,
        *,
        enforce_tdd: bool = True,
        claimed_task_requires_tdd: bool = False,
        canonical_tool_kind: str = "write",
        tests_written: list[str] | None = None,
        acceptance_paths: list[str] | None = None,
        tool_input: dict[str, Any] | None = None,
    ) -> bool:
        event_data = {
            "canonical_tool_kind": canonical_tool_kind,
            "canonical_file_path": file_path,
            "canonical_file_paths": [file_path],
        }
        context = {
            "variables": {
                "enforce_tdd": enforce_tdd,
                "claimed_task_requires_tdd": claimed_task_requires_tdd,
                "claimed_task_acceptance_test_paths": acceptance_paths or [],
                "tdd_tests_written": tests_written or [],
            },
            "event": type("E", (), {"data": event_data})(),
            "tool_input": tool_input if tool_input is not None else {"file_path": file_path},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_blocks_new_source_file(self) -> None:
        assert self._eval("/project/src/gobby/utils/helper.py") is True

    def test_blocks_nested_source_file(self) -> None:
        assert self._eval("/project/src/gobby/deep/nested/module.py") is True

    def test_skips_when_enforce_tdd_false(self) -> None:
        assert self._eval("/project/src/main.py", enforce_tdd=False) is False

    def test_guards_init_file(self) -> None:
        assert self._eval("/project/src/gobby/__init__.py") is True

    def test_skips_conftest(self) -> None:
        assert self._eval("/project/tests/conftest.py") is False

    def test_skips_test_file_by_prefix(self) -> None:
        assert self._eval("/project/test_something.py") is False

    def test_skips_test_file_in_tests_dir(self) -> None:
        assert self._eval("/project/tests/test_main.py") is False

    def test_skips_test_file_by_suffix(self) -> None:
        assert self._eval("/project/src/main_test.py") is False

    def test_test_write_without_named_red_stays_blocked(self) -> None:
        assert (
            self._eval("/project/src/gobby/new_module.py", tests_written=["tests/test.py"]) is True
        )

    def test_skips_non_source_files(self) -> None:
        assert self._eval("/project/config.yaml") is False
        assert self._eval("/project/README.md") is False
        assert self._eval("/project/data.json") is False

    def test_blocks_canonical_write_from_bash(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="write") is True

    def test_skips_non_write_kind(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="read") is False

    def test_skips_rust_module_tests_rs_write(self) -> None:
        assert self._eval("/project/crates/gterminal/src/host/backpressure/tests.rs") is False

    def test_skips_rust_inline_cfg_test_module_addition(self, tmp_path: Path) -> None:
        path = _write_rust_module_file(tmp_path, text=_RUST_PRODUCTION_SOURCE)
        assert (
            self._eval(
                path,
                tool_input={
                    "file_path": path,
                    "old_str": _RUST_ENCODE_BODY,
                    "new_str": _RUST_ENCODE_BODY + _RUST_TEST_MODULE,
                },
            )
            is False
        )

    def test_skips_rust_edit_inside_existing_test_module(self, tmp_path: Path) -> None:
        path = _write_rust_module_file(tmp_path)
        inside = '        assert_eq!(encode(&Frame { id: 7 }), "frame 7");'
        assert (
            self._eval(
                path,
                tool_input={
                    "file_path": path,
                    "old_str": inside,
                    "new_str": inside.replace("7", "9"),
                },
            )
            is False
        )

    def test_blocks_rust_production_edit_outside_test_module(self, tmp_path: Path) -> None:
        path = _write_rust_module_file(tmp_path)
        assert (
            self._eval(
                path,
                tool_input={
                    "file_path": path,
                    "old_str": "pub fn encode",
                    "new_str": "pub fn encode_quoted",
                },
            )
            is True
        )

    def test_blocks_rust_write_changing_production_beside_test_module(self, tmp_path: Path) -> None:
        path = _write_rust_module_file(tmp_path)
        rewritten = _RUST_MODULE_SOURCE.replace("pub fn encode(", "pub fn encode_quoted(")
        assert self._eval(path, tool_input={"file_path": path, "content": rewritten}) is True


# --- enforce-tdd-track-tests structure ---


class TestEnforceTddTrackTestsStructure:
    """Verify enforce-tdd-track-tests rule structure."""

    def test_is_after_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "after_tool"

    def test_has_set_variable_effect(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects is not None
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "tdd_tests_written"
        assert "tdd_tests_written" in body.effects[0].value
        assert "first_tdd_test_path" in body.effects[0].value

    def test_when_checks_enforce_tdd_and_tool(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "enforce_tdd" in body.when
        assert "claimed_task_requires_tdd" in body.when
        assert "canonical_tool_kind" in body.when
        assert "first_tdd_test_path" in body.when


# --- enforce-tdd-track-tests condition evaluation ---


class TestEnforceTddTrackTestsCondition:
    """Test the tracking condition evaluates correctly."""

    CONDITION = (
        "(variables.get('enforce_tdd') or variables.get('claimed_task_requires_tdd')) "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and not event.data.get('error') "
        "and first_tdd_test_path(event.data, tool_input)"
    )

    def _eval(
        self,
        file_path: str,
        *,
        enforce_tdd: bool = True,
        claimed_task_requires_tdd: bool = False,
        canonical_tool_kind: str = "write",
        error: bool = False,
        tool_input: dict[str, Any] | None = None,
    ) -> bool:
        event_data = {
            "canonical_tool_kind": canonical_tool_kind,
            "canonical_file_path": file_path,
            "canonical_file_paths": [file_path],
            "error": error,
        }
        context = {
            "variables": {
                "enforce_tdd": enforce_tdd,
                "claimed_task_requires_tdd": claimed_task_requires_tdd,
            },
            "event": type("E", (), {"data": event_data})(),
            "tool_input": tool_input if tool_input is not None else {"file_path": file_path},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_tracks_test_file_by_prefix(self) -> None:
        assert self._eval("/project/test_main.py") is True

    def test_tracks_test_file_in_tests_dir(self) -> None:
        assert self._eval("/project/tests/test_utils.py") is True

    def test_tracks_test_file_by_suffix(self) -> None:
        assert self._eval("/project/src/utils_test.py") is True

    def test_tracks_non_test_file_in_tests_dir(self) -> None:
        """Even non-test-prefixed files in tests/ directory are tracked."""
        assert self._eval("/project/tests/conftest.py") is True

    def test_skips_source_file(self) -> None:
        assert self._eval("/project/src/gobby/main.py") is False

    def test_skips_when_enforce_tdd_false(self) -> None:
        assert self._eval("/project/test_main.py", enforce_tdd=False) is False

    def test_skips_on_error(self) -> None:
        assert self._eval("/project/test_main.py", error=True) is False

    def test_tracks_bash_canonical_write(self) -> None:
        assert self._eval("/project/tests/test_main.py", canonical_tool_kind="write") is True

    def test_skips_non_write_kind(self) -> None:
        assert self._eval("/project/tests/test_main.py", canonical_tool_kind="read") is False

    def test_tracks_rust_module_tests_rs_write(self) -> None:
        assert self._eval("/project/crates/gterminal/src/host/backpressure/tests.rs") is True

    def test_tracks_rust_inline_cfg_test_addition(self, tmp_path: Path) -> None:
        path = _write_rust_module_file(tmp_path, text=_RUST_PRODUCTION_SOURCE)
        assert (
            self._eval(
                path,
                tool_input={
                    "file_path": path,
                    "old_str": _RUST_ENCODE_BODY,
                    "new_str": _RUST_ENCODE_BODY + _RUST_TEST_MODULE,
                },
            )
            is True
        )

    def test_tracks_rust_edit_inside_existing_test_module(self, tmp_path: Path) -> None:
        path = _write_rust_module_file(tmp_path)
        inside = '        assert_eq!(encode(&Frame { id: 7 }), "frame 7");'
        assert (
            self._eval(
                path,
                tool_input={
                    "file_path": path,
                    "old_str": inside,
                    "new_str": inside.replace("7", "9"),
                },
            )
            is True
        )

    def test_skips_rust_production_edit_outside_test_module(self, tmp_path: Path) -> None:
        path = _write_rust_module_file(tmp_path)
        assert (
            self._eval(
                path,
                tool_input={
                    "file_path": path,
                    "old_str": "pub fn encode",
                    "new_str": "pub fn encode_quoted",
                },
            )
            is False
        )


# --- Variable definitions ---


class TestTddVariableDefinitions:
    """Verify TDD variables are defined in gobby-default-variables.yaml."""

    def test_variables_file_contains_tdd_variables(self) -> None:
        import yaml

        from gobby.workflows.sync_rules import get_bundled_rules_path

        vars_path = get_bundled_rules_path().parent / "variables" / "gobby-default-variables.yaml"
        with open(vars_path) as f:
            data = yaml.safe_load(f)

        variables = data["variables"]
        assert "enforce_tdd" in variables
        assert variables["enforce_tdd"]["value"] is False

        assert variables["claimed_task_requires_tdd"]["value"] is False
        assert variables["claimed_task_acceptance_test_paths"]["value"] == []

        assert "tdd_tests_written" in variables
        assert variables["tdd_tests_written"]["value"] == []
