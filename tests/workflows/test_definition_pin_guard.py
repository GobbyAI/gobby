"""Definition selections use a synthetic catalog, independent of installed policy."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows import condition_helpers_definition_pins
from gobby.workflows.condition_helpers_definition_pins import (
    definition_pins,
    discover_definition_names,
    projected_definition_pins,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit
NAMES = frozenset({"fixture-agent", "fixture-rule", "fixture-pipeline", "fixture-workflow"})


def test_condition_helper_uses_current_project_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        condition_helpers_definition_pins, "discover_definition_names", lambda: NAMES
    )
    context = {
        "project": {"path": str(tmp_path)},
        "tool_input": {"path": "tests/test_example.py", "content": 'spawn(agent="fixture-agent")'},
        "event": SimpleNamespace(data={"canonical_tool_kind": "write"}),
    }
    helpers = build_condition_helpers(context=context)
    assert "projected_definition_pins" in helpers
    evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=helpers)
    assert evaluator.evaluate("bool(projected_definition_pins(tool_input, event.data))")


def test_catalog_discovers_each_definition_kind_from_fixture_yaml(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "agents/a.yaml").write_text("name: fixture-agent\nprompts: {agent: Act}\n")
    (tmp_path / "agents/default.yaml").write_text("name: default\n")
    (tmp_path / "rules/r.yaml").write_text("rules:\n  fixture-rule: {event: stop}\n")
    (tmp_path / "pipelines/p.yaml").write_text("name: fixture-pipeline\ntype: pipeline\n")
    (tmp_path / "w.yaml").write_text("name: fixture-workflow\ntype: workflow\nsteps: []\n")
    assert discover_definition_names(tmp_path) == NAMES


@pytest.mark.parametrize(
    "source",
    [
        'spawn(agent="fixture-agent")',
        'arguments = {"agent_name": "fixture-agent"}',
        'PipelineExecutor().run("fixture-pipeline")',
        'definitions.get_by_name("fixture-workflow")',
        'agent_registry["fixture-agent"]',
        'registry.get("fixture-rule")',
        'loader.load("fixture-workflow")',
        'load(definition="fixture-workflow")',
        'load_step_workflow("fixture-workflow")',
        'configure(step_workflow="fixture-workflow")',
        'manager = AgentDefinitionManager()\nmanager.create("fixture-agent", {"prompts": {"agent": "Act"}})',
        'definition_registry.register("fixture-rule", {})',
        'spawn({agent: "fixture-agent"})',
        'const request = {"agent_name": "fixture-agent"};',
        'AgentDefinitionBody(prompts={"agent": definitions.get_by_name("fixture-agent").prompt})',
        'AgentDefinitionBody(prompts={"agent": agent_registry["fixture-agent"].prompt})',
        'rules = {"fixture-rule": {"event": "before_tool", "effects": []}}',
        'make_agent_workflows(rules=["fixture-rule"])',
        'workflows = {"rule_selectors": {"include": ["name:fixture-rule"]}}',
        'def test_lookup(manager: AgentDefinitionManager):\n    manager.get_by_name("fixture-agent")',
        'manager = AgentDefinitionManager()\nlookup = manager.get_by_name\nlookup("fixture-agent")',
        'rule_manager.get_by_name(name="fixture-rule")',
        'AgentDefinitionBody(name="fixture-agent", prompts={"agent": "Act"})',
        'name = "fixture-agent"\nspawn(agent=name)',
        'yaml.safe_load("name: fixture-agent\\nprompts: {agent: Act}\\n")',
    ],
)
def test_definition_selection_is_a_pin(source: str) -> None:
    assert definition_pins(source, NAMES)


@pytest.mark.parametrize(
    "source",
    [
        'messages = [{"role": "assistant", "content": "fixture-agent"}]',
        'messages = [{"role": "fixture-agent", "content": "text"}]',
        'messages = [{"type": "message", "name": "fixture-agent"}]',
        'create_task(category="fixture-agent")',
        'InternalToolRegistry(name="fixture-rule")',
        'get_registry("fixture-rule")',
        'registry = InternalToolRegistry(name="fixture-rule")\nregistry.get_tool("fixture-agent")',
        'coordinator.cache_agent_message("session-1", "fixture-agent")',
        'AgentPromptBlocks("fixture-agent")',
        'AgentPromptBlocks(agent="fixture-agent")',
        'body = {"prompts": {"agent": "fixture-agent"}}',
        'messages = AgentMessageManager()\nmessages.create("fixture-agent")',
        'print("name:fixture-rule")',
        'messages = [{"role": "assistant", "content": "name:fixture-rule"}]',
        'pipeline_step = "fixture-workflow"',
        'print("fixture-agent")',
        'spawn(agent="fixture-user-defined")',
    ],
)
def test_ordinary_words_and_unrelated_namespaces_are_allowed(source: str) -> None:
    assert definition_pins(source, NAMES) == []


def test_new_pin_is_blocked_and_existing_pin_can_be_edited(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    target = tests / "test_example.py"
    original = 'spawn(agent="fixture-agent")\n'
    target.write_text(original)
    assert (
        projected_definition_pins(
            {"file_path": str(target), "content": original + "# improve the fixture\n"},
            tmp_path,
            names=NAMES,
        )
        == []
    )
    blocked = projected_definition_pins(
        {"file_path": str(target), "content": original + 'run(pipeline="fixture-pipeline")\n'},
        tmp_path,
        names=NAMES,
    )
    assert len(blocked) == 1
    assert "fixture-pipeline" in blocked[0]
    assert "tests/test_example.py" in blocked[0]


def test_role_data_and_writes_outside_tests_are_unaffected(tmp_path: Path) -> None:
    for path, content in (
        ("tests/test_messages.py", 'messages = [{"role": "assistant"}]'),
        ("src/example.py", 'spawn(agent="fixture-agent")'),
    ):
        assert (
            projected_definition_pins(
                {"file_path": path, "content": content}, tmp_path, names=NAMES
            )
            == []
        )


@pytest.mark.parametrize("operation", ["edit", "multi", "patch", "changes"])
def test_structured_write_variants_project_added_pins(tmp_path: Path, operation: str) -> None:
    target = tmp_path / "tests/test_example.py"
    target.parent.mkdir()
    target.write_text('spawn(agent="fixture-user-defined")\n')
    edit = {"old_string": "fixture-user-defined", "new_string": "fixture-agent"}
    payload: dict[str, object]
    if operation == "edit":
        payload = {"file_path": str(target), **edit}
    elif operation == "multi":
        payload = {"file_path": str(target), "edits": [edit]}
    elif operation == "changes":
        payload = {"changes": [{"path": str(target), **edit}]}
    else:
        payload = {
            "patch": "*** Begin Patch\n*** Update File: tests/test_example.py\n@@\n"
            '-spawn(agent="fixture-user-defined")\n+spawn(agent="fixture-agent")\n*** End Patch'
        }
    assert projected_definition_pins(payload, tmp_path, names=NAMES)


def test_duplicate_pins_and_moves_into_tests_are_detected(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests/test_example.py"
    source = 'spawn(agent="fixture-agent")\n'
    target.write_text(source)
    assert projected_definition_pins(
        {"path": str(target), "content": source * 2}, tmp_path, names=NAMES
    )
    origin = tmp_path / "example.py"
    origin.write_text(source)
    assert projected_definition_pins(
        {
            "patch": "*** Begin Patch\n*** Update File: example.py\n"
            "*** Move to: tests/test_moved.py\n@@\n " + source + "*** End Patch"
        },
        tmp_path,
        names=NAMES,
    )


@pytest.mark.parametrize(
    "source",
    [
        'Path("src/gobby/install/shared/workflows/agents/fixture-agent.yaml").read_text()',
        'root = Path("src") / "gobby" / "install" / "shared" / "workflows"\n'
        '(root / "agents" / "fixture-agent.yaml").read_text()',
        'load_yaml(Path("src/gobby/install/shared/workflows") / "fixture-rule.yaml")',
        'path = Path("src/gobby/install/shared/workflows/agents/fixture-agent.yaml")',
        'Path("src/gobby/install/shared/workflows").glob("agents/fixture-agent*.yaml")',
        'AgentDefinitionBody(prompts={"agent": Path("src/gobby/install/shared/workflows/agents/fixture-agent.yaml").read_text()})',
    ],
)
def test_direct_template_loads_are_pins(source: str) -> None:
    assert any(pin.startswith("template:") for pin in definition_pins(source, NAMES))


def test_name_neutral_catalog_enumeration_is_allowed() -> None:
    source = 'root = Path("src/gobby/install/shared/workflows")\npaths = root.glob("*.yaml")'
    assert definition_pins(source, NAMES) == []


def test_move_source_cannot_read_outside_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (tmp_path / "outside.py").write_text('spawn(agent="fixture-agent")\n')
    result = projected_definition_pins(
        {
            "patch": "*** Begin Patch\n*** Update File: ../outside.py\n"
            "*** Move to: tests/test_moved.py\n*** End Patch"
        },
        root,
        names=NAMES,
    )
    assert len(result) == 1
    assert "outside the project" in result[0]


def test_unknown_write_with_canonical_test_path_fails_closed(tmp_path: Path) -> None:
    result = projected_definition_pins(
        {}, tmp_path, {"canonical_file_paths": ["tests/test_example.py"]}, names=NAMES
    )
    assert len(result) == 1
    assert "projectable write" in result[0]


def test_patch_delete_and_multiple_hunks_do_not_freeze_existing_pins(tmp_path: Path) -> None:
    target = tmp_path / "tests/test_example.py"
    target.parent.mkdir()
    target.write_text('spawn(agent="fixture-agent")\nfirst = 1\nlast = 2')
    patch = "*** Begin Patch\n*** Update File: tests/test_example.py\n@@\n-first = 1\n+first = 3\n"
    patch += "@@\n-last = 2\n+last = 4\n*** End of File\n*** End Patch"
    assert projected_definition_pins({"patch": patch}, tmp_path, names=NAMES) == []
    delete = "*** Begin Patch\n*** Delete File: tests/test_example.py\n*** End Patch"
    assert (
        projected_definition_pins(
            {"patch": delete}, tmp_path, {"canonical_file_paths": [str(target)]}, names=NAMES
        )
        == []
    )


def test_raw_patch_input_and_event_payload_are_projected(tmp_path: Path) -> None:
    patch = '*** Begin Patch\n*** Add File: tests/test_example.py\n+spawn(agent="fixture-agent")\n*** End Patch'
    assert projected_definition_pins(patch, tmp_path, names=NAMES)
    assert projected_definition_pins({}, tmp_path, {"tool_input": {"patch": patch}}, names=NAMES)


@pytest.mark.parametrize("destination", ["tests/test_renamed.py", "examples/example.py"])
def test_canonical_move_paths_are_not_unknown_writes(tmp_path: Path, destination: str) -> None:
    source = tmp_path / "tests/test_example.py"
    source.parent.mkdir()
    source.write_text("value = 1\n")
    payload = {
        "patch": "*** Begin Patch\n*** Update File: tests/test_example.py\n"
        f"*** Move to: {destination}\n@@\n-value = 1\n+value = 2\n*** End Patch"
    }
    assert (
        projected_definition_pins(
            payload,
            tmp_path,
            {"canonical_file_paths": [str(source), str(tmp_path / destination)]},
            names=NAMES,
        )
        == []
    )


@pytest.mark.parametrize("case", ["new-pin", "existing-pin", "role-data", "outside-tests"])
async def test_synthetic_rule_blocks_only_new_test_dependencies(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setattr(
        condition_helpers_definition_pins, "discover_definition_names", lambda: NAMES
    )
    manager = RuleDefinitionManager(temp_db)
    manager.create(
        name="fixture-pin-policy",
        tags=["default"],
        definition_json={
            "event": "before_tool",
            "when": "event.data.get('canonical_tool_kind') == 'write' and "
            "bool(projected_definition_pins(tool_input, event.data))",
            "effects": [
                {
                    "type": "block",
                    "reason": "Use an inline fixture: "
                    "{{ projected_definition_pins(tool_input, event.data) | join('; ') }}",
                }
            ],
        },
    )
    target = tmp_path / ("src/example.py" if case == "outside-tests" else "tests/test_example.py")
    target.parent.mkdir()
    original = 'spawn(agent="fixture-agent")\n' if case == "existing-pin" else ""
    target.write_text(original)
    content = 'messages = [{"role": "assistant", "content": "hello"}]\n'
    if case != "role-data":
        content = original + (
            "# fixture cleanup\n" if original else 'spawn(agent="fixture-agent")\n'
        )
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="11111111-1111-4111-8111-111111111111",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Write",
            "canonical_tool_kind": "write",
            "tool_input": {"file_path": str(target), "content": content},
        },
    )
    engine = RuleEngine(temp_db)
    with patch.object(engine, "_resolve_project_info", return_value={"path": str(tmp_path)}):
        response = await engine.evaluate(event, session_id=event.session_id, variables={})
    assert response.decision == ("block" if case == "new-pin" else "allow")
    if case == "new-pin":
        assert "fixture-agent" in (response.reason or "")
        assert "inline fixture" in (response.reason or "")
