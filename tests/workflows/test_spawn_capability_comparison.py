"""Capability comparison uses stored synthetic definitions and fallback chains."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.condition_helpers_sessions import spawn_target_allowed
from gobby.workflows.condition_helpers_spawn import spawn_capabilities_allowed
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


def _define(
    manager: AgentDefinitionManager,
    name: str,
    body: dict[str, Any],
    *,
    project_id: str,
    enabled: bool = True,
) -> None:
    manager.create(
        name,
        {
            "prompts": {"agent": "Synthetic capability fixture."},
            "workflows": {"rule_selectors": {"include": []}},
            **body,
        },
        project_id=project_id,
        enabled=enabled,
    )


def _caller(
    db: HubDatabase,
    project_id: str,
    capabilities: list[str],
    *,
    spawnable_agents: list[str] | None = None,
    spawned: bool = False,
) -> tuple[SessionManager, str]:
    _define(
        AgentDefinitionManager(db),
        "fixture-caller",
        {"capabilities": capabilities, "spawnable_agents": spawnable_agents or []},
        project_id=project_id,
    )
    sessions = SessionManager(db)
    parent = sessions.register("fixture-parent", None, "claude", project_id=project_id)
    caller = sessions.register_session(
        external_id="fixture-spawn-caller",
        machine_id=None,
        source="claude",
        project_id=project_id,
        parent_session_id=parent.id if spawned else None,
        agent_depth=1 if spawned else 0,
    )
    if spawned:
        run = LocalAgentRunManager(db).create(
            parent_session_id=parent.id,
            provider="claude",
            prompt="Synthetic launch fixture.",
            agent_name="fixture-caller",
            child_session_id=caller,
        )
        sessions.update_terminal_pickup_metadata(caller, agent_run_id=run.id)
    SessionVariableManager(db).set_variable(caller, "_agent_type", "fixture-caller")
    return sessions, caller


@pytest.mark.parametrize("spawned", [False, True])
@pytest.mark.parametrize("scope", [[], ["*"], ["fixture-target"], ["fixture-other"]])
def test_enforced_spawn_scope_controls_capability_comparison(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    spawned: bool,
    scope: list[str],
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, [], spawnable_agents=scope, spawned=spawned)
    definitions = AgentDefinitionManager(temp_db)
    for name in ("fixture-target", "default"):
        _define(
            definitions, name, {"capabilities": ["fixture_child_permission"]}, project_id=project_id
        )
    target = "default" if not scope else "fixture-target"
    scope_allowed = spawn_target_allowed(
        sessions, caller, "spawn_agent", target, target_project_id=project_id
    )
    assert scope_allowed is (scope in (["*"], ["fixture-target"]))
    allowed = scope_allowed and spawn_capabilities_allowed(
        sessions, caller, target, parent_session_id=caller
    )
    assert allowed is (scope == ["fixture-target"])


def test_operator_root_spawn_scope_remains_unrestricted(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    caller = sessions.register(
        "fixture-operator", None, "claude", project_id=str(sample_project["id"])
    )
    assert spawn_target_allowed(sessions, caller.id, "spawn_agent", "fixture-target")


@pytest.mark.parametrize("spawned", [False, True])
@pytest.mark.parametrize("include_fallback", [False, True])
def test_bounded_scope_still_checks_every_fallback(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    spawned: bool,
    include_fallback: bool,
) -> None:
    project_id = str(sample_project["id"])
    scope = ["fixture-target", "fixture-backup"] if include_fallback else ["fixture-target"]
    sessions, caller = _caller(temp_db, project_id, [], spawnable_agents=scope, spawned=spawned)
    definitions = AgentDefinitionManager(temp_db)
    _define(
        definitions, "fixture-target", {"fallback_agent": "fixture-backup"}, project_id=project_id
    )
    _define(
        definitions,
        "fixture-backup",
        {"capabilities": ["fixture_child_permission"]},
        project_id=project_id,
    )
    assert (
        spawn_target_allowed(
            sessions, caller, "spawn_agent", "fixture-target", target_project_id=project_id
        )
        is include_fallback
    )
    assert spawn_capabilities_allowed(sessions, caller, "fixture-target", parent_session_id=caller)


@pytest.mark.parametrize("has_authority", [False, True])
def test_yaml_selected_authority_allows_stronger_named_target(
    temp_db: HubDatabase, sample_project: dict[str, Any], has_authority: bool
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(
        temp_db,
        project_id,
        ["fixture_launch_any"] if has_authority else [],
        spawnable_agents=["*"],
        spawned=True,
    )
    _define(
        AgentDefinitionManager(temp_db),
        "fixture-target",
        {"capabilities": ["fixture_child_permission"]},
        project_id=project_id,
    )
    assert (
        spawn_capabilities_allowed(
            sessions, caller, "fixture-target", None, caller, "fixture_launch_any"
        )
        is has_authority
    )


@pytest.mark.parametrize("target", [None, "default", "fixture-fallback"])
def test_launch_authority_never_covers_default(
    temp_db: HubDatabase, sample_project: dict[str, Any], target: str | None
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, ["fixture_launch_any"], spawnable_agents=["*"])
    definitions = AgentDefinitionManager(temp_db)
    _define(definitions, "default", {"capabilities": []}, project_id=project_id)
    _define(definitions, "fixture-fallback", {"fallback_agent": "default"}, project_id=project_id)
    assert not spawn_capabilities_allowed(
        sessions, caller, target, None, caller, "fixture_launch_any"
    )


@pytest.mark.parametrize(
    ("caller_caps", "target_caps", "fallback_caps", "expected"),
    [
        (["fixture_read"], ["fixture_read"], None, True),
        (["fixture_read", "fixture_write"], ["fixture_read"], None, True),
        (["fixture_read"], [], None, True),
        (["fixture_read"], ["fixture_write"], None, False),
        (["fixture_read"], ["fixture_read", "fixture_write"], None, False),
        ([], ["fixture_read"], None, False),
        (["fixture_read"], [], ["fixture_write"], False),
        (["fixture_read"], [], ["fixture_read"], True),
    ],
)
def test_capabilities_cover_target_and_fallback(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caller_caps: list[str],
    target_caps: list[str],
    fallback_caps: list[str] | None,
    expected: bool,
) -> None:
    project_id = str(sample_project["id"])
    definitions = AgentDefinitionManager(temp_db)
    sessions, caller = _caller(temp_db, project_id, caller_caps)
    body: dict[str, Any] = {"capabilities": target_caps}
    if fallback_caps is not None:
        _define(
            definitions, "fixture-fallback", {"capabilities": fallback_caps}, project_id=project_id
        )
        body["fallback_agent"] = "fixture-fallback"
    _define(definitions, "fixture-target", body, project_id=project_id)
    assert (
        spawn_capabilities_allowed(sessions, caller, "fixture-target", parent_session_id=caller)
        is expected
    )


@pytest.mark.parametrize("state", ["missing", "disabled", "transitive", "cycle"])
def test_fallbacks_cannot_hide_additional_capabilities(
    temp_db: HubDatabase, sample_project: dict[str, Any], state: str
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, ["fixture_read"])
    definitions = AgentDefinitionManager(temp_db)
    _define(
        definitions, "fixture-target", {"fallback_agent": "fixture-middle"}, project_id=project_id
    )
    if state != "missing":
        _define(
            definitions,
            "fixture-middle",
            {"fallback_agent": "fixture-end"},
            project_id=project_id,
            enabled=state != "disabled",
        )
        _define(
            definitions,
            "fixture-end",
            {"capabilities": ["fixture_write"], "fallback_agent": "fixture-target"}
            if state == "cycle"
            else {"capabilities": ["fixture_write"]},
            project_id=project_id,
        )
    assert not spawn_capabilities_allowed(
        sessions, caller, "fixture-target", parent_session_id=caller
    )


@pytest.mark.parametrize("path_kind", ["explicit", "unmarked", "idless"])
def test_explicit_project_identity_controls_definition_lookup(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path, path_kind: str
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, ["fixture_read"])
    other = LocalProjectManager(temp_db).create("fixture-other-project")
    definitions = AgentDefinitionManager(temp_db)
    _define(definitions, "fixture-target", {"capabilities": []}, project_id=project_id)
    _define(definitions, "fixture-target", {"capabilities": ["fixture_write"]}, project_id=other.id)
    if path_kind != "unmarked":
        marker = tmp_path / ".gobby"
        marker.mkdir()
        (marker / "project.json").write_text(
            json.dumps({"id": other.id} if path_kind == "explicit" else {})
        )
    assert spawn_capabilities_allowed(
        sessions, caller, "fixture-target", f" {tmp_path} ", parent_session_id=caller
    ) is (path_kind == "unmarked")


@pytest.mark.parametrize("project_path", [None, "", "  "])
@pytest.mark.parametrize("parent_ref", [None, ""])
def test_blank_optional_inputs_use_caller_project(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    project_path: str | None,
    parent_ref: str | None,
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, ["fixture_read"])
    _define(
        AgentDefinitionManager(temp_db),
        "fixture-target",
        {"capabilities": ["fixture_read"]},
        project_id=project_id,
    )
    assert spawn_capabilities_allowed(sessions, caller, "fixture-target", project_path, parent_ref)


def test_parent_reference_changes_target_scope_without_granting_authority(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions, caller = _caller(temp_db, str(sample_project["id"]), ["fixture_read"])
    other = LocalProjectManager(temp_db).create("fixture-parent-project")
    parent = sessions.register_session(
        external_id="fixture-other-parent", machine_id=None, source="claude", project_id=other.id
    )
    definitions = AgentDefinitionManager(temp_db)
    _define(
        definitions,
        "fixture-powerful-parent",
        {"capabilities": ["fixture_write"]},
        project_id=other.id,
    )
    _define(definitions, "fixture-target", {"capabilities": ["fixture_write"]}, project_id=other.id)
    SessionVariableManager(temp_db).set_variable(parent, "_agent_type", "fixture-powerful-parent")
    assert not spawn_capabilities_allowed(
        sessions, caller, "fixture-target", parent_session_id=parent
    )


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("has_authority", [False, True])
@pytest.mark.parametrize("capabilities", [["fixture_read"], ["fixture_write"]])
async def test_comparison_is_available_to_synthetic_rule_conditions(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    capabilities: list[str],
    has_authority: bool,
    operator: bool,
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(
        temp_db,
        project_id,
        ["fixture_read", "fixture_launch_any"] if has_authority else ["fixture_read"],
        spawnable_agents=["*"],
    )
    if operator:
        caller = sessions.register(
            "fixture-operator-rule", None, "claude", project_id=project_id
        ).id
    _define(
        AgentDefinitionManager(temp_db),
        "fixture-target",
        {"capabilities": capabilities},
        project_id=project_id,
    )
    RuleDefinitionManager(temp_db).create(
        "fixture-delegation-check",
        {
            "event": "before_tool",
            "mandatory": True,
            "when": "not operator_session(event.metadata.get('_platform_session_id')) and "
            "not spawn_capabilities_allowed(event.metadata.get('_platform_session_id'), "
            "'fixture-target', None, event.metadata.get('_platform_session_id'), 'fixture_launch_any')",
            "effects": [{"type": "block", "reason": "fixture capability boundary"}],
        },
    )
    response = await RuleEngine(temp_db).evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            source=SessionSource.CLAUDE,
            session_id=caller,
            timestamp=datetime.now(UTC),
            metadata={"_platform_session_id": caller},
            data={"tool_name": "fixture_call", "tool_input": {}},
        ),
        session_id=caller,
        variables={},
    )
    allowed = operator or has_authority or capabilities == ["fixture_read"]
    assert response.decision == ("allow" if allowed else "block")
    if not allowed:
        assert "fixture capability boundary" in (response.reason or "")


@pytest.mark.parametrize("agent", [None, "default"])
def test_default_caller_can_spawn_its_own_capabilities(
    temp_db: HubDatabase, sample_project: dict[str, Any], agent: str | None
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, ["fixture_read"])
    _define(
        AgentDefinitionManager(temp_db),
        "default",
        {"capabilities": ["fixture_read"]},
        project_id=project_id,
    )
    SessionVariableManager(temp_db).set_variable(caller, "_agent_type", "default")
    assert spawn_capabilities_allowed(sessions, caller, agent, parent_session_id=caller)


@pytest.mark.parametrize("state", ["missing", "disabled"])
def test_unavailable_caller_definition_grants_no_capabilities(
    temp_db: HubDatabase, sample_project: dict[str, Any], state: str
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, ["fixture_read"])
    definitions = AgentDefinitionManager(temp_db)
    _define(definitions, "fixture-target", {"capabilities": []}, project_id=project_id)
    if state == "disabled":
        _define(
            definitions,
            "fixture-unavailable",
            {"capabilities": ["fixture_read"]},
            project_id=project_id,
            enabled=False,
        )
    SessionVariableManager(temp_db).set_variable(caller, "_agent_type", "fixture-unavailable")
    assert not spawn_capabilities_allowed(
        sessions, caller, "fixture-target", parent_session_id=caller
    )


@pytest.mark.parametrize(("granted", "allowed"), [(["fixture_read"], True), ([], False)])
def test_hook_path_without_ambient_project_uses_the_caller_project(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    granted: list[str],
    allowed: bool,
) -> None:
    """Rule evaluation has no per-call project context; the verified caller supplies it."""
    monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
    project_id = str(sample_project["id"])
    sessions, caller = _caller(temp_db, project_id, granted, spawned=True)
    _define(
        AgentDefinitionManager(temp_db),
        "fixture-target",
        {"capabilities": ["fixture_read"]},
        project_id=project_id,
    )
    assert spawn_capabilities_allowed(sessions, caller, "fixture-target") is allowed


@pytest.mark.parametrize(
    ("caller_capabilities", "spawnable", "expected"),
    [
        # Containment passes in every row; only the spawner requirement decides.
        (["fixture_child_permission"], ["*"], False),
        (["fixture_child_permission", "fixture_spawn_any"], ["*"], True),
        (["fixture_child_permission"], ["fixture-guarded"], False),
        (["fixture_child_permission", "fixture_spawn_any"], ["fixture-guarded"], True),
    ],
)
def test_spawner_capabilities_gate_wildcard_and_finite_callers(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caller_capabilities: list[str],
    spawnable: list[str],
    expected: bool,
) -> None:
    project_id = str(sample_project["id"])
    sessions, caller = _caller(
        temp_db, project_id, caller_capabilities, spawnable_agents=spawnable, spawned=True
    )
    _define(
        AgentDefinitionManager(temp_db),
        "fixture-guarded",
        {
            "capabilities": ["fixture_child_permission"],
            "spawner_capabilities": ["fixture_spawn_any"],
        },
        project_id=project_id,
    )
    assert spawn_capabilities_allowed(sessions, caller, "fixture-guarded", None, caller) is expected


def test_spawner_capabilities_hold_on_a_fallback_hop(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A guarded definition reached through a fallback is refused like a direct target."""
    project_id = str(sample_project["id"])
    sessions, caller = _caller(
        temp_db, project_id, ["fixture_child_permission"], spawnable_agents=["*"], spawned=True
    )
    _define(
        AgentDefinitionManager(temp_db),
        "fixture-guarded",
        {
            "capabilities": ["fixture_child_permission"],
            "spawner_capabilities": ["fixture_spawn_any"],
        },
        project_id=project_id,
    )
    _define(
        AgentDefinitionManager(temp_db),
        "fixture-entry",
        {"capabilities": ["fixture_child_permission"], "fallback_agent": "fixture-guarded"},
        project_id=project_id,
    )
    assert not spawn_capabilities_allowed(sessions, caller, "fixture-entry", None, caller)
