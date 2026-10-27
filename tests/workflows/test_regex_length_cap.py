"""Definition regex length limits at validation and registry boundaries."""

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.imports import sync_imported_definition
from gobby.workflows.regex_validation import (
    DefinitionRegex,
    iter_definition_regexes,
    validate_definition_regexes,
)
from tests.phase5_contract_helpers import is_bundled_agent_definition

pytestmark = pytest.mark.unit


def _config(cap: int) -> DaemonConfig:
    config = DaemonConfig()
    return config.model_copy(
        update={"workflow": config.workflow.model_copy(update={"definition_regex_max_length": cap})}
    )


@pytest.mark.parametrize(
    "field,build",
    [
        ("effects[0].command_pattern", lambda p: {"effects": [{"command_pattern": p}]}),
        ("effects[0].command_not_pattern", lambda p: {"effects": [{"command_not_pattern": p}]}),
        ("when", lambda p: {"when": f"write_paths_match(event.data, tool_input, {p!r})"}),
        ("prompt", lambda p: {"prompt": "{{ text | regex_search(" + repr(p) + ") }}"}),
        ("prompt", lambda p: {"prompt": "{{ text | regex_replace(" + repr(p) + ", '') }}"}),
        (
            "when",
            lambda p: {"when": f"assistant_response_matches_any([{p!r}], regex=True)"},
        ),
    ],
)
def test_overlong_pattern_rejected(field: str, build: Callable[[str], dict[str, object]]) -> None:
    for length in (7, 8):
        validate_definition_regexes("cap-test", build("x" * length), _config(8))
    with pytest.raises(ValueError) as error:
        validate_definition_regexes("cap-test", build("x" * 9), _config(8))
    message = str(error.value)
    assert "cap-test" in message
    assert field in message
    assert "9" in message
    assert "8" in message


def test_cap_is_configurable() -> None:
    config = DaemonConfig()
    assert config.workflow.model_dump().get("definition_regex_max_length") == 2048
    definition = {"command_pattern": "x" * 16}
    validate_definition_regexes("config-test", definition, _config(16))
    with pytest.raises(ValueError, match="config-test"):
        validate_definition_regexes("config-test", definition, _config(15))


def test_lowercase_boolean_regex_helpers() -> None:
    literal = {"when": "assistant_response_matches_any(['long'], regex=false)"}
    validate_definition_regexes("literal", literal, _config(3))
    regex = {"when": "assistant_response_matches_any(['long'], regex=true)"}
    with pytest.raises(ValueError, match="lowercase.*when.*4.*3"):
        validate_definition_regexes("lowercase", regex, _config(3))


def test_folded_condition_regex_rejected() -> None:
    definition = {"when": "event.data\n  and write_paths_match(event.data, tool_input, 'long')"}
    with pytest.raises(ValueError, match="folded.*when.*4.*3"):
        validate_definition_regexes("folded", definition, _config(3))


def test_bundled_templates_within_cap() -> None:
    root = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
    patterns: list[DefinitionRegex] = []
    files = sorted(p for p in root.rglob("*.yaml") if not is_bundled_agent_definition(p))
    assert files
    for path in files:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        patterns.extend(iter_definition_regexes(data))
        validate_definition_regexes(str(path.relative_to(root)), data, DaemonConfig())
    # Verify that this scan really exercises the bundled regex inputs.
    assert any("command_pattern" in pattern.field for pattern in patterns)
    assert all(len(pattern.pattern) <= 2048 for pattern in patterns)


@pytest.mark.integration
def test_registry_rejects_before_persist(temp_db: HubDatabase) -> None:
    manager = RuleDefinitionManager(temp_db)
    body = {"event": "before_tool", "effects": [{"type": "block", "command_pattern": "x" * 2049}]}
    with pytest.raises(ValueError, match="registry-cap"):
        manager.create("registry-cap", body)
    assert manager.get_by_name("registry-cap") is None


@pytest.mark.integration
def test_registry_uses_current_config_for_create_and_sync_update(temp_db: HubDatabase) -> None:
    mutations = ConfigMutations(temp_db)
    revision = ConfigRepository(temp_db).current_revision()
    result = mutations.patch(
        expected_revision=revision,
        patch=ConfigPatch(values={"workflow.definition_regex_max_length": 16}),
    )
    manager = RuleDefinitionManager(temp_db)
    row = manager.create("configured-registry", {"command_pattern": "x" * 16})
    mutations.patch(
        expected_revision=result.revision,
        patch=ConfigPatch(values={"workflow.definition_regex_max_length": 15}),
    )
    with pytest.raises(ValueError, match="configured-registry.*16.*15"):
        manager.update_from_sync(row.id, definition_json={"command_pattern": "x" * 16})
    with pytest.raises(ValueError, match="lower-cap.*16.*15"):
        manager.create("lower-cap", {"command_pattern": "x" * 16})
    assert manager.get(row.id).definition_json == row.definition_json


@pytest.mark.integration
def test_agent_and_pipeline_writes_validate_nested_regexes(temp_db: HubDatabase) -> None:
    prompt = "{{ text | regex_search(" + repr("x" * 2049) + ") }}"
    agents = AgentDefinitionManager(temp_db)
    with pytest.raises(ValueError, match="agent-cap.*prompt.*2049"):
        agents.upsert_from_sync("agent-cap", {"prompt": prompt}, None)
    assert agents.get_by_name("agent-cap") is None
    pipelines = PipelineDefinitionManager(temp_db)
    with pytest.raises(ValueError, match="pipeline-cap.*prompt.*2049"):
        pipelines.create("pipeline-cap", {"steps": [{"prompt": prompt}]})
    assert pipelines.get_by_name("pipeline-cap") is None


@pytest.mark.integration
def test_import_rejects_before_changing_agent_parent(temp_db: HubDatabase) -> None:
    definition = {
        "type": "agent",
        "name": "import-cap",
        "description": "before",
        "surfaces": ["spawn"],
        "workflows": {"rule_selectors": {}},
        "prompts": {"agent": "Work on the assigned task."},
    }
    row = sync_imported_definition(temp_db, definition, None)
    overlong = "write_paths_match(event.data, tool_input, " + repr("x" * 2049) + ")"
    incoming = {
        **definition,
        "description": "after",
        "step_workflow": {"steps": [{"name": "work"}], "exit_condition": overlong},
    }
    with pytest.raises(ValueError, match="import-cap.*exit_condition.*2049"):
        sync_imported_definition(temp_db, incoming, None)
    assert AgentDefinitionManager(temp_db).get(row.id).description == "before"
