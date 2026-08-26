"""Cross-CLI regressions for stateless ambient MCP session identity."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from gobby.cli.installers.shared import sync_bundled_content_to_db
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV, issue_agent_api_token
from gobby.workflows.state_manager import SessionVariableManager
from tests.e2e.conftest import CLIEventSimulator, DaemonInstance, daemon_health_unavailable

pytestmark = pytest.mark.e2e

PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
TARGET_SERVER = "gobby-tasks"
TARGET_TOOL = "list_ready_tasks"

# Matches the identity seeded into the isolated daemon home by e2e_config, so
# test-process registrations resolve the same machine as daemon-side ones.
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    yield


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: HubDatabase) -> None:
    """Install the production rule and variable definitions in isolated storage."""
    result = sync_bundled_content_to_db(postgres_db)
    assert result["errors"] == []


def _terminal_context(label: str) -> dict[str, Any]:
    suffix = f"{label}-{uuid.uuid4()}"
    return {
        "parent_pid": 49230,
        "tmux_pane": f"%{suffix}",
        "tmux_socket_path": f"/tmp/{suffix}.sock",
        "tty": f"/dev/pts/{suffix}",
        "term_program": "pytest",
        "term_session_id": suffix,
    }


def _session_for(
    manager: SessionManager,
    external_id: str,
    cli_source: str,
) -> Session:
    session = manager.find_by_external_id(
        external_id,
        PROJECT_ID,
        cli_source,
    )
    assert session is not None
    return session


def _schema_hook_input(cli_source: str) -> dict[str, Any]:
    target = {"server_name": TARGET_SERVER, "tool_name": TARGET_TOOL}
    if cli_source == "qwen":
        return {
            "function_name": "mcp_gobby_get_tool_schema",
            "parameters": target,
        }
    if cli_source == "droid":
        return {
            "toolName": "gobby___get_tool_schema",
            "toolArgs": target,
        }
    return {
        "tool_name": "mcp__gobby__get_tool_schema",
        "tool_input": target,
    }


def _new_proxy(
    monkeypatch: pytest.MonkeyPatch,
    daemon_instance: DaemonInstance,
    terminal_context: dict[str, Any],
) -> DaemonProxy:
    monkeypatch.setenv("GOBBY_HOME", str(daemon_instance.gobby_home))
    monkeypatch.setenv("GOBBY_PROJECT_ID", PROJECT_ID)
    monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
        lambda: terminal_context,
    )
    return DaemonProxy(daemon_instance.http_port)


def test_daemon_instance_restarts_with_same_ports(daemon_instance: DaemonInstance) -> None:
    """The isolated daemon fixture can stop and restart in place."""
    original_pid = daemon_instance.pid

    daemon_instance.stop()

    assert not daemon_instance.is_alive()
    assert daemon_health_unavailable(daemon_instance.http_port)

    daemon_instance.restart()

    assert daemon_instance.is_alive()
    assert daemon_instance.pid != original_pid
    assert daemon_instance.http_url.endswith(str(daemon_instance.http_port))
    assert daemon_instance.ws_url.endswith(str(daemon_instance.ws_port))


def test_session_start_accepts_distinct_cli_and_lifecycle_sources(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
) -> None:
    terminal_context = {
        "parent_pid": 49230,
        "tty": "/dev/pts/gobby-e2e-simulator",
        "term_session_id": f"simulator-{uuid.uuid4()}",
    }
    external_a = f"simulator-a-{uuid.uuid4()}"
    external_b = f"simulator-b-{uuid.uuid4()}"

    project_result = cli_events.register_test_project(
        project_id=PROJECT_ID,
        name="E2E Test Project",
        repo_path=str(daemon_instance.project_dir),
    )
    assert project_result["status"] in {"success", "already_exists"}

    start_a = cli_events.session_start(
        external_a,
        cli_source="codex",
        session_start_source="startup",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    prompt_a = cli_events.user_prompt_submit(
        external_a,
        prompt="hello",
        source="codex",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    start_b = cli_events.session_start(
        external_b,
        cli_source="codex",
        session_start_source="clear",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    prompt_b = cli_events.user_prompt_submit(
        external_b,
        prompt="hello",
        source="codex",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )

    manager = SessionManager(postgres_db)
    session_a = manager.find_by_external_id(
        external_a,
        PROJECT_ID,
        "codex",
    )
    session_b = manager.find_by_external_id(
        external_b,
        PROJECT_ID,
        "codex",
    )

    assert start_a.get("continue") is True
    assert prompt_a.get("continue") is True
    assert start_b.get("continue") is True
    assert prompt_b.get("continue") is True
    assert session_a is not None
    assert session_a.status == "expired"
    assert session_b is not None
    assert session_b.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_source", ["claude", "codex", "qwen", "droid", "grok"])
async def test_ambient_proxy_follows_clear_and_attributes_schema_lease(
    cli_source: str,
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_context = _terminal_context(cli_source)
    external_a = f"{cli_source}-ambient-a-{uuid.uuid4()}"
    external_b = f"{cli_source}-ambient-b-{uuid.uuid4()}"
    project_result = cli_events.register_test_project(
        project_id=PROJECT_ID,
        name="E2E Test Project",
        repo_path=str(daemon_instance.project_dir),
    )
    assert project_result["status"] in {"success", "already_exists"}

    start_a = cli_events.session_start(
        external_a,
        cli_source=cli_source,
        session_start_source="startup",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    assert start_a.get("continue") is True
    prompt_a = cli_events.user_prompt_submit(
        external_a,
        prompt="hello",
        source=cli_source,
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    assert prompt_a.get("continue") is True

    manager = SessionManager(postgres_db)
    variable_manager = SessionVariableManager(postgres_db)
    audit_manager = WorkflowAuditManager(postgres_db)
    session_a = _session_for(manager, external_a, cli_source)
    proxy = _new_proxy(monkeypatch, daemon_instance, terminal_context)

    try:
        ambient_a = await proxy.list_tools(TARGET_SERVER)
        assert ambient_a.get("success") is True
        assert TARGET_SERVER in variable_manager.get_variables(session_a.id)["listed_servers"]

        start_b = cli_events.session_start(
            external_b,
            cli_source=cli_source,
            session_start_source="clear",
            project_id=PROJECT_ID,
            cwd=str(daemon_instance.project_dir),
            terminal_context=terminal_context,
        )
        assert start_b.get("continue") is True
        prompt_b = cli_events.user_prompt_submit(
            external_b,
            prompt="hello",
            source=cli_source,
            project_id=PROJECT_ID,
            cwd=str(daemon_instance.project_dir),
            terminal_context=terminal_context,
        )
        assert prompt_b.get("continue") is True

        session_a = _session_for(manager, external_a, cli_source)
        session_b = _session_for(manager, external_b, cli_source)
        assert session_a.status == "expired"
        assert session_b.status == "active"
        expired_a = session_a.to_dict()

        ambient_b = await proxy.list_tools(TARGET_SERVER)
        assert ambient_b.get("success") is True
        assert TARGET_SERVER in variable_manager.get_variables(session_b.id)["listed_servers"]
        refreshed_a = manager.get(session_a.id)
        assert refreshed_a is not None
        assert refreshed_a.to_dict() == expired_a

        variable_manager.set_variable(session_b.id, "enforce_tool_schema_check", True)
        variable_manager.set_variable(session_b.id, "unlocked_tools", [])
        blocked = await proxy.call_tool(
            TARGET_SERVER,
            TARGET_TOOL,
            {},
            preflight_enabled=False,
        )
        assert blocked.get("success") is False

        b_blocks = audit_manager.get_entries(
            session_id=session_b.id,
            event_type="rule_eval",
            result="block",
        )
        a_blocks = audit_manager.get_entries(
            session_id=session_a.id,
            event_type="rule_eval",
            result="block",
        )
        assert any(
            entry.rule_id == "require-current-context-schema-before-call" for entry in b_blocks
        )
        assert a_blocks == []

        schema = await proxy.get_tool_schema(TARGET_SERVER, TARGET_TOOL)
        assert schema.get("success") is True
        post_tool = cli_events.post_tool_use(
            external_b,
            cli_source=cli_source,
            input_data=_schema_hook_input(cli_source),
            project_id=PROJECT_ID,
        )
        assert post_tool.get("continue") is True

        unlocked = variable_manager.get_variables(session_b.id)["unlocked_tools"]
        assert f"{TARGET_SERVER}:{TARGET_TOOL}" in unlocked, post_tool

        repeated = await proxy.call_tool(
            TARGET_SERVER,
            TARGET_TOOL,
            {},
            preflight_enabled=False,
        )
        assert repeated.get("success") is True

        explicit_a = await proxy.list_mcp_servers(session_id=session_a.id)
        assert explicit_a.get("success") is True
        refreshed_a = manager.get(session_a.id)
        assert refreshed_a is not None
        assert refreshed_a.status == "expired"
        assert refreshed_a.to_dict() == expired_a
    finally:
        await proxy.aclose()


@pytest.mark.asyncio
async def test_daemon_binds_schema_lease_to_child_identity_env(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon-side non-regression: a child-identity env resolves leases to the child.

    The identity env is seeded directly via monkeypatch, so this does not
    exercise the spawn-side propagation (the Codex override/env_vars scrub
    model — covered by tests/agents/test_spawn_executor.py::
    test_scrubbed_child_env_reaches_daemon_proxy_identity). It pins what the
    daemon does once that env is in place: the schema lease and the following
    call bind to the child session, never the parent.
    """
    project_result = cli_events.register_test_project(
        project_id=PROJECT_ID,
        name="E2E Test Project",
        repo_path=str(daemon_instance.project_dir),
    )
    assert project_result["status"] in {"success", "already_exists"}

    session_manager = SessionManager(postgres_db)
    parent = session_manager.register(
        external_id=f"managed-parent-{uuid.uuid4()}",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=PROJECT_ID,
    )
    child_external_id = f"managed-child-{uuid.uuid4()}"
    child = session_manager.register(
        external_id=child_external_id,
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=PROJECT_ID,
        parent_session_id=parent.id,
        agent_depth=1,
    )
    run_manager = LocalAgentRunManager(postgres_db)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        claimed_session_id=child.id,
        provider="codex",
        prompt="Exercise progressive discovery",
        run_id=str(uuid.uuid4()),
    )
    run_manager.start(run.id)

    variable_manager = SessionVariableManager(postgres_db)
    variable_manager.set_variable(child.id, "enforce_tool_schema_check", True)
    variable_manager.set_variable(child.id, "unlocked_tools", [])
    variable_manager.set_variable(parent.id, "unlocked_tools", [])

    monkeypatch.setenv("GOBBY_HOME", str(daemon_instance.gobby_home))
    monkeypatch.setenv("GOBBY_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("GOBBY_SESSION_ID", child.id)
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", run.id)
    operator_token = (daemon_instance.gobby_home / "local_cli_token").read_text().strip()
    monkeypatch.setenv(
        GOBBY_AGENT_API_TOKEN_ENV,
        issue_agent_api_token(
            operator_token,
            agent_run_id=run.id,
            session_id=child.id,
            project_id=PROJECT_ID,
        ),
    )
    proxy = DaemonProxy(daemon_instance.http_port)

    try:
        schema = await proxy.get_tool_schema(TARGET_SERVER, TARGET_TOOL)
        assert schema.get("success") is True
        post_tool = cli_events.post_tool_use(
            child_external_id,
            cli_source="codex",
            input_data=_schema_hook_input("codex"),
            project_id=PROJECT_ID,
        )
        assert post_tool.get("continue") is True

        result = await proxy.call_tool(
            TARGET_SERVER,
            TARGET_TOOL,
            {},
            preflight_enabled=False,
        )
        assert result.get("success") is True
        lease = f"{TARGET_SERVER}:{TARGET_TOOL}"
        assert lease in variable_manager.get_variables(child.id)["unlocked_tools"]
        assert lease not in variable_manager.get_variables(parent.id)["unlocked_tools"]
    finally:
        await proxy.aclose()


@pytest.mark.asyncio
async def test_hookless_terminal_requires_session_before_tool_dispatch(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_result = cli_events.register_test_project(
        project_id=PROJECT_ID,
        name="E2E Test Project",
        repo_path=str(daemon_instance.project_dir),
    )
    assert project_result["status"] in {"success", "already_exists"}

    row = postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM tasks WHERE project_id = %s",
        (PROJECT_ID,),
    )
    assert row is not None
    task_count_before = int(row["count"])
    proxy = _new_proxy(monkeypatch, daemon_instance, _terminal_context("hookless"))

    try:
        result = await proxy.call_tool(
            "gobby-tasks",
            "create_task",
            {
                "title": f"must-not-dispatch-{uuid.uuid4()}",
                "category": "manual",
            },
            preflight_enabled=False,
        )

        assert result["success"] is False
        assert result["error_code"] == "SESSION_REQUIRED"
        assert result["terminal_context_seen"] is True
        row = postgres_db.fetchone(
            "SELECT COUNT(*) AS count FROM tasks WHERE project_id = %s",
            (PROJECT_ID,),
        )
        assert row is not None
        assert int(row["count"]) == task_count_before
    finally:
        await proxy.aclose()


@pytest.mark.asyncio
async def test_same_proxy_resolves_existing_session_after_daemon_restart(
    cli_events: CLIEventSimulator,
    daemon_instance: DaemonInstance,
    postgres_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_result = cli_events.register_test_project(
        project_id=PROJECT_ID,
        name="E2E Test Project",
        repo_path=str(daemon_instance.project_dir),
    )
    assert project_result["status"] in {"success", "already_exists"}

    # No tmux keys: startup tmux repair detaches sessions bound to a missing
    # tmux server, and this test's fabricated socket never exists. A tty-only
    # terminal identity survives the daemon restart.
    suffix = f"daemon-restart-{uuid.uuid4()}"
    terminal_context = {
        "parent_pid": 49230,
        "tty": f"/dev/pts/{suffix}",
        "term_program": "pytest",
        "term_session_id": suffix,
    }
    external_id = f"codex-daemon-restart-{uuid.uuid4()}"
    start = cli_events.session_start(
        external_id,
        cli_source="codex",
        session_start_source="startup",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    assert start.get("continue") is True
    prompt = cli_events.user_prompt_submit(
        external_id,
        prompt="hello",
        source="codex",
        project_id=PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
        terminal_context=terminal_context,
    )
    assert prompt.get("continue") is True

    manager = SessionManager(postgres_db)
    variable_manager = SessionVariableManager(postgres_db)
    session = _session_for(manager, external_id, "codex")
    proxy = _new_proxy(monkeypatch, daemon_instance, terminal_context)

    try:
        initial = await proxy.list_tools(TARGET_SERVER)
        assert initial.get("success") is True
        assert TARGET_SERVER in variable_manager.get_variables(session.id)["listed_servers"]
        before_outage = manager.get(session.id)
        assert before_outage is not None
        before_outage_data = before_outage.to_dict()

        daemon_instance.stop()
        unavailable = await proxy.list_tools(TARGET_SERVER)
        assert unavailable["success"] is False
        assert unavailable["error_code"] == "DAEMON_UNAVAILABLE"

        during_outage = manager.get(session.id)
        assert during_outage is not None
        assert during_outage.to_dict() == before_outage_data

        daemon_instance.restart()
        after_restart = await proxy.list_tools(TARGET_SERVER)
        assert after_restart.get("success") is True
        resolved = manager.get(session.id)
        assert resolved is not None
        assert resolved.status == "active"
        assert TARGET_SERVER in variable_manager.get_variables(session.id)["listed_servers"]
    finally:
        await proxy.aclose()
