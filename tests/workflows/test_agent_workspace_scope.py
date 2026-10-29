"""Agent workspace write and read scope rules (addendum 9.2)."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from gobby.agents.agent_workspaces import predecessor_folder_readable
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.idle_declaration import native_variable_write_error
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import seed_seat

pytestmark = pytest.mark.unit

RULES_DIR = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules"
PROJECT = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
TEMPLATE = "~/.gobby/agents_workspaces/<project_id>/<definition>/<session>/"


def test_orchestrator_definition_excludes_agent_workspace_read_scope() -> None:
    path = RULES_DIR.parent / "agents" / "orchestrator.yaml"
    definition = yaml.safe_load(path.read_text())
    assert "name:agent-workspace-read-scope" in definition["workflows"]["rule_selectors"]["exclude"]


@pytest.mark.parametrize("agent_type", ["orchestrator", "developer"])
@pytest.mark.parametrize("reader", ["native", "shell"])
async def test_only_orchestrator_reads_another_seats_session_report(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    agent_type: str,
    reader: str,
) -> None:
    definition = yaml.safe_load((RULES_DIR.parent / "agents" / f"{agent_type}.yaml").read_text())
    selectors = definition["workflows"]["rule_selectors"]
    seed_seat(
        temp_db,
        agent_type,
        include=selectors.get("include", []),
        exclude=selectors.get("exclude", []),
    )
    project_id: str = sample_project["id"]
    anchor = root / project_id
    variables = _seat(
        anchor, agent_type, 50, persistent=definition.get("persistent_workspace", False)
    )
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type=agent_type,
        folder=Path(variables["_agent_workspace_dir"]),
        persistent=definition.get("persistent_workspace", False),
    )
    report_folder = anchor / "researcher" / "session-51"
    _register(
        temp_db,
        project_id,
        seq=51,
        status="active",
        agent_type="researcher",
        folder=report_folder,
        persistent=False,
    )
    report = report_folder / "report.md"
    report_folder.mkdir(parents=True)
    report.write_text("# Research report\nPrimary evidence.\n")
    project = tmp_path / "checkout"
    project.mkdir()
    data = _read(report, project) if reader == "native" else _shell(f"cat {report}", project)
    response = await _decide(engine, data, variables, caller)
    expected = "allow" if agent_type == "orchestrator" else "block"
    assert response.decision == expected, response.reason
    if agent_type != "orchestrator":
        assert "agent-workspace-read-scope" in (response.reason or "")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The workspaces root under a temporary home, so ``~/`` paths resolve to it."""
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GOBBY_HOME", raising=False)
    # The test home sits under the OS temp root; move that root so plan mode's temp
    # allowance does not cover the workspace.
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(os_temp))
    monkeypatch.setattr("gobby.workflows.condition_helpers_paths._PROVIDER_TEMP_ROOT", os_temp)
    return home / ".gobby" / "agents_workspaces"


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    manager = RuleDefinitionManager(temp_db)
    for rule_file in sorted([*RULES_DIR.glob("roles/*.yaml"), *RULES_DIR.glob("plan-mode/*.yaml")]):
        document = yaml.safe_load(rule_file.read_text())
        for name, rule_data in document["rules"].items():
            body_data, metadata = split_rule_definition_data(rule_data)
            manager.create(
                name=name,
                definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
                priority=metadata["priority"],
                enabled=metadata["enabled"],
                tags=document["tags"],
            )
    return RuleEngine(temp_db)


@pytest.fixture
def workspace_write_engine(temp_db: HubDatabase) -> RuleEngine:
    document = yaml.safe_load((RULES_DIR / "roles" / "agent-workspace-scope.yaml").read_text())
    name = "agent-workspace-write-scope"
    body_data, metadata = split_rule_definition_data(document["rules"][name])
    RuleDefinitionManager(temp_db).create(
        name=name,
        definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
        priority=metadata["priority"],
        enabled=metadata["enabled"],
        tags=document["tags"],
    )
    return RuleEngine(temp_db)


@pytest.fixture
def workspace_read_engine(temp_db: HubDatabase) -> RuleEngine:
    document = yaml.safe_load((RULES_DIR / "roles" / "agent-workspace-scope.yaml").read_text())
    name = "agent-workspace-read-scope"
    body_data, metadata = split_rule_definition_data(document["rules"][name])
    RuleDefinitionManager(temp_db).create(
        name=name,
        definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
        priority=metadata["priority"],
        enabled=metadata["enabled"],
        tags=document["tags"],
    )
    return RuleEngine(temp_db)


def _event(data: dict[str, Any], session_id: str) -> HookEvent:
    # The platform session id rides in metadata, as the hook handlers stamp it in production.
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": session_id},
    )


async def _decide(
    engine: RuleEngine,
    data: dict[str, Any],
    variables: dict[str, Any],
    session_id: str = SESSION_ID,
) -> HookResponse:
    return await engine.evaluate(
        _event(data, session_id), session_id=session_id, variables=variables
    )


def _write(path: Path, project: Path) -> dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": "x"},
        "cwd": str(project),
        "project_path": str(project),
    }


def _read(path: Path, project: Path) -> dict[str, Any]:
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": str(path)},
        "cwd": str(project),
        "project_path": str(project),
    }


def _grep(path: Path, project: Path) -> dict[str, Any]:
    return {
        "tool_name": "Grep",
        "tool_input": {"pattern": "lesson", "path": str(path)},
        "cwd": str(project),
        "project_path": str(project),
    }


def _shell(command: str, project: Path) -> dict[str, Any]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(project),
        "project_path": str(project),
    }


def _seat(
    anchor: Path, agent_type: str | None, seq: int, *, persistent: bool = False
) -> dict[str, Any]:
    definition = agent_type or "default"
    variables: dict[str, Any] = {
        "_agent_workspace_dir": str(anchor / definition / f"session-{seq}"),
        "_agent_workspace_epic_dir": str(anchor / "epics" / "epic-5"),
        "_agent_workspace_persistent": persistent,
    }
    if agent_type is not None:
        variables["_agent_type"] = agent_type
    return variables


def _register(
    db: HubDatabase,
    project_id: str,
    *,
    seq: int,
    status: str,
    agent_type: str,
    folder: Path,
    persistent: bool,
) -> str:
    """A session of ``agent_type`` numbered ``seq`` whose bound folder is ``folder``."""
    session_id = str(uuid4())
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, title,
                              status, agent_depth, seq_num)
        VALUES (%s, %s, %s, 'test', %s, %s, %s, 0, %s)
        """,
        (
            session_id,
            f"ext-{session_id}",
            require_machine_id(),
            project_id,
            agent_type,
            status,
            seq,
        ),
    )
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            "_agent_type": agent_type,
            "_agent_workspace_dir": str(folder),
            "_agent_workspace_persistent": persistent,
        },
    )
    return session_id


SEATS = ("researcher", "assistant", "archivist", "developer", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", SEATS, ids=lambda seat: seat or "plan-mode")
async def test_write_isolation(
    engine: RuleEngine, root: Path, tmp_path: Path, agent_type: str | None
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, agent_type, 12)
    if agent_type is None:
        variables["plan_mode"] = True
    own = Path(variables["_agent_workspace_dir"])
    epic = Path(variables["_agent_workspace_epic_dir"])

    for allowed in (own / "notes.md", own / "nested" / "draft.md", epic / "decisions.md"):
        response = await _decide(engine, _write(allowed, project), dict(variables))
        assert response.decision == "allow", (allowed, response.reason)

    for refused in (
        root / PROJECT / (agent_type or "default") / "session-99" / "notes.md",
        root / PROJECT / "epics" / "epic-6" / "decisions.md",
        root / PROJECT / "README.md",
        root / f"{PROJECT}.git" / "config",
        own / ".git" / "config",
        own / ".gobby" / "project.json",
        epic / ".gobby" / "state.json",
    ):
        response = await _decide(engine, _write(refused, project), dict(variables))
        assert response.decision == "block", refused


@pytest.mark.asyncio
async def test_plan_documents_in_owned_worktrees_are_writable(
    engine: RuleEngine, root: Path, tmp_path: Path
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    worktree = own / "task-25298"
    plans = worktree / ".gobby" / "plans"
    plans.mkdir(parents=True)
    (plans / "config.md").symlink_to(worktree / ".gobby" / "project.json")

    for path, expected in (
        (plans / "runbook-devteam-addendum.md", "allow"),
        (plans / "nested" / "design.md", "allow"),
        (plans / "config.md", "block"),
        (plans / ".." / "project.json", "block"),
        (plans / "settings.json", "block"),
        (plans / ".git" / "config.md", "block"),
        (plans / ".gobby" / "config.md", "block"),
        (root / PROJECT / "developer" / "session-99" / ".gobby" / "plans" / "a.md", "block"),
    ):
        event = {
            "tool_name": "apply_patch",
            "canonical_tool_kind": "write",
            "tool_input": {"file_path": str(path), "content": "# Plan"},
            "cwd": str(worktree),
            "project_path": str(project),
        }
        response = await _decide(engine, event, dict(variables))
        assert response.decision == expected, (path, response.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["write", "commit"])
@pytest.mark.parametrize("folder", ["own", "epic"])
async def test_workspace_metadata_names_ignore_case(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, tool: str, folder: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    key = "_agent_workspace_dir" if folder == "own" else "_agent_workspace_epic_dir"
    directory = Path(variables[key])
    plans = directory / ".gobby" / "plans"
    plans.mkdir(parents=True)
    (plans / "linked.MD").symlink_to(directory / ".GoBbY" / "project.json")
    for relative, decision in (
        (".GOBBY/project.json", "block"),
        (".GoBbY/settings.json", "block"),
        (".GIT/config", "block"),
        (".GiT/config", "block"),
        (".gobby/plans/.GIT/config.md", "block"),
        (".GOBBY/PLANS/.GoBbY/config.MD", "block"),
        (".gobby/plans/linked.MD", "block"),
        (".GOBBY/PLANS/plan.MD", "allow"),
    ):
        path = directory / relative
        data = (
            _write(path, project)
            if tool == "write"
            else _shell(f"git commit -m plan --only -- {path}", project)
        )
        normalize_tool_fields(data)
        response = await _decide(workspace_write_engine, data, variables)
        assert response.decision == decision, (path, tool, response.reason)
        if decision == "block":
            assert response.reason is not None
            assert "agent-workspace-write-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("ancestor", ["gobby", "root", "definition"])
@pytest.mark.parametrize("tool", ["write", "commit"])
async def test_case_alias_of_workspace_root_does_not_bypass_write_scope(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, ancestor: str, tool: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    directory = {"gobby": root.parent, "root": root, "definition": own.parent}[ancestor]
    alias = directory.with_name(directory.name.upper())
    if not alias.exists():
        pytest.skip("Filesystem distinguishes workspace root casing")
    assert alias.samefile(directory)
    for target, decision in (
        (own / ".GIT" / "config", "block"),
        (own / ".gobby" / "project.json", "block"),
        (own.parent / "session-99" / "notes.md", "block"),
        (own / "notes.md", "allow"),
    ):
        path = alias / target.relative_to(directory)
        data = (
            _write(path, project)
            if tool == "write"
            else _shell(f"git commit -m plan --only -- {path}", project)
        )
        normalize_tool_fields(data)
        response = await _decide(workspace_write_engine, data, variables)
        assert response.decision == decision, (path, tool, response.reason)
        if decision == "block":
            assert response.reason is not None
            assert "agent-workspace-write-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["own", "outside"])
@pytest.mark.parametrize("pattern", ["*.md", "*/notes.md", "**/*.md"])
@pytest.mark.parametrize("tool", ["Bash", "Glob"])
async def test_glob_matches_check_resolved_seat(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    location: str,
    pattern: str,
    tool: str,
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    safe = own / "target"
    foreign = own.parent / "session-99"
    for directory in (safe, foreign):
        directory.mkdir(parents=True)
        (directory / "notes.md").write_text("Fixture notes")
    search = own if location == "own" else tmp_path / "repository"
    search.mkdir(exist_ok=True)
    file_link = pattern == "*.md"
    link = search / ("leak.md" if file_link else "leak")
    link.symlink_to(foreign / "notes.md" if file_link else foreign)
    data = (
        _shell(f"cat {search / pattern}", tmp_path)
        if tool == "Bash"
        else {
            "tool_name": "Glob",
            "tool_input": {"path": str(search), "pattern": pattern},
            "cwd": str(tmp_path),
        }
    )
    refused = await _decide(workspace_read_engine, data, variables)
    assert refused.decision == "block"
    assert "agent-workspace-read-scope" in (refused.reason or "")
    link.unlink()
    link.symlink_to(safe / "notes.md" if file_link else safe)
    allowed = await _decide(workspace_read_engine, data, variables)
    assert allowed.decision == "allow", allowed.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["entries", "depth", "permission"])
async def test_glob_inspection_fails_closed_when_incomplete(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from gobby.workflows import condition_helpers_sessions as helpers

    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    for number in range(3):
        (own / f"notes-{number}.md").write_text("Fixture notes")
    with monkeypatch.context() as scope:
        if failure == "entries":
            scope.setattr(helpers, "_WORKSPACE_GLOB_BUDGET", 2)
        elif failure == "depth":
            scope.setattr(helpers, "_WORKSPACE_GLOB_MAX_DEPTH", 0)
        else:

            def unavailable(path: Path) -> None:
                raise PermissionError("Cannot inspect pattern entries")

            scope.setattr(helpers.os, "scandir", unavailable)
        response = await _decide(
            workspace_read_engine, _shell(f"cat {own}/*.md", tmp_path), variables
        )
    assert response.decision == "block"
    assert "agent-workspace-read-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["log*/..", "logs/{..,other}", "logs/{.,other}{.,other}"])
async def test_read_expansion_cannot_enter_workspace_from_outside(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, prefix: str
) -> None:
    (root.parent / "logs").mkdir(parents=True)
    variables = _seat(root / PROJECT, "developer", 12)
    target = root.parent / prefix / root.name / PROJECT / "developer/session-99/notes.md"
    response = await _decide(workspace_read_engine, _shell(f"cat {target}", tmp_path), variables)
    assert response.decision == "block"
    assert "agent-workspace-read-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["different", "unavailable"])
async def test_case_alias_cannot_borrow_unproven_seat_ownership(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    alias = own.with_name(own.name.upper())

    def samefile(path: Path, other: Path) -> bool:
        if identity == "unavailable":
            raise PermissionError("Cannot inspect folder identity")
        return False

    monkeypatch.setattr(Path, "samefile", samefile)
    response = await _decide(
        workspace_write_engine, _write(alias / "notes.md", tmp_path), variables
    )
    assert response.decision == "block"
    assert "agent-workspace-write-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("ancestor", ["gobby", "root", "definition"])
@pytest.mark.parametrize("tool", ["read", "grep", "shell"])
async def test_case_alias_reads_preserve_workspace_isolation(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, ancestor: str, tool: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    directory = {"gobby": root.parent, "root": root, "definition": own.parent}[ancestor]
    alias = directory.with_name(directory.name.upper())
    if not alias.exists():
        pytest.skip("Filesystem distinguishes workspace root casing")
    assert alias.samefile(directory)
    for target, decision in (
        (own.parent / "session-99" / "notes.md", "block"),
        (own / "notes.md", "allow"),
    ):
        path = alias / target.relative_to(directory)
        if tool == "shell":
            data = _shell(f"cat {path}", project)
        elif tool == "grep":
            data = _grep(path, project)
        else:
            data = _read(path, project)
        response = await _decide(workspace_read_engine, data, variables)
        assert response.decision == decision, (path, tool, response.reason)
        if decision == "block":
            assert response.reason is not None
            assert "agent-workspace-read-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["shell", "glob"])
@pytest.mark.parametrize(
    "pattern",
    ["agents_work*", "agents_work?paces", "agents_[w]orkspaces", "agents_{workspaces,other}"],
)
async def test_workspace_root_patterns_cannot_expose_foreign_reads(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, tool: str, pattern: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    target = root.parent / pattern / PROJECT / "developer" / "session-99" / "notes.md"
    data = (
        _shell(f"cat {target}", project)
        if tool == "shell"
        else {"tool_name": "Glob", "tool_input": {"pattern": str(target)}, "cwd": str(project)}
    )
    response = await _decide(workspace_read_engine, data, variables)
    assert response.decision == "block", (target, response.reason)
    assert response.reason is not None
    assert "agent-workspace-read-scope" in response.reason


@pytest.mark.asyncio
async def test_read_patterns_keep_an_unambiguous_owned_boundary(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    for target, decision in (
        (own / "*.md", "allow"),
        (own / "**" / "*.md", "allow"),
        (Path(variables["_agent_workspace_epic_dir"]) / "*.md", "allow"),
        (project / "src" / "*.py", "allow"),
        (own.parent / "session-*" / "notes.md", "block"),
        (own.parent / "*" / "notes.md", "block"),
        (own / "*" / ".." / ".." / "session-99" / "notes.md", "block"),
    ):
        response = await _decide(workspace_read_engine, _shell(f"cat {target}", project), variables)
        assert response.decision == decision, (target, response.reason)
        if decision == "block":
            assert response.reason is not None
            assert "agent-workspace-read-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["own", "epic"])
async def test_nested_task_worktree_plan_allowance_stays_bounded(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, folder: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    variable = "_agent_workspace_dir" if folder == "own" else "_agent_workspace_epic_dir"
    worktree = Path(variables[variable]) / "task-25298"
    for relative, decision in (
        (".gobby/plans/runbook-devteam-addendum.md", "allow"),
        (".gobby/plans/research/notes.MD", "allow"),
        (".gobby/project.json", "block"),
        (".git/config", "block"),
        (".gobby/plans/.git/notes.md", "block"),
        (".gobby/plans/nested/.gobby/plans/notes.md", "block"),
    ):
        response = await _decide(
            workspace_write_engine, _write(worktree / relative, project), variables
        )
        assert response.decision == decision, (relative, response.reason)
        if decision == "block":
            assert response.reason is not None
            assert "agent-workspace-write-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative", ["{..,.}/session-99/notes.md", "{../session-99,notes}/notes.md"]
)
async def test_brace_expansion_cannot_escape_owned_read_folder(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, relative: str
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    target = Path(variables["_agent_workspace_dir"]) / relative
    response = await _decide(workspace_read_engine, _shell(f"cat {target}", tmp_path), variables)
    assert response.decision == "block"
    assert "agent-workspace-read-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["different", "unavailable"])
async def test_read_alias_requires_proven_seat_identity(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])

    def samefile(path: Path, other: Path) -> bool:
        if identity == "unavailable":
            raise PermissionError("Cannot inspect folder identity")
        return False

    monkeypatch.setattr(Path, "samefile", samefile)
    response = await _decide(
        workspace_read_engine,
        _read(own.with_name(own.name.upper()) / "notes.md", tmp_path),
        variables,
    )
    assert response.decision == "block"
    assert "agent-workspace-read-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["plan", "templates"])
@pytest.mark.parametrize("command_field", [False, True])
async def test_apply_patch_workspace_targets_use_the_actual_headers(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    shape: str,
    command_field: bool,
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    worktree = Path(variables["_agent_workspace_dir"]) / "task-25298"
    targets = (
        [worktree / ".gobby/plans/runbook-devteam-addendum.md"]
        if shape == "plan"
        else [
            worktree / "src/gobby/install/shared" / name
            for name in (
                "workflows/agents/orchestrator.yaml",
                "workflows/agents/triage.yaml",
                "workflows/agents/assistant.yaml",
                "workflows/agents/archivist.yaml",
                "workflows/agents/release-manager.yaml",
                "AGENTS.md",
            )
        ]
    )
    patch = (
        "*** Begin Patch\n"
        + "".join(f"*** Update File: {path}\n@@\n-before\n+after\n" for path in targets)
        + "*** End Patch\n"
    )
    # Reduced from a refused plan patch; escapes expose the shell punctuation.
    patch = patch.replace("+after", "+Each leaf\x27s \x60implementation_domain\x60 selects skills.")
    data: dict[str, Any] = {
        "tool_name": "apply_patch",
        "tool_input": {"command": patch} if command_field else patch,
        "cwd": str(tmp_path),
    }
    normalize_tool_fields(data)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == [str(path) for path in targets]
    response = await _decide(workspace_write_engine, data, variables)
    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
async def test_predecessor_case_alias_keeps_live_holder_and_sequence_guards(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_id: str = sample_project["id"]
    anchor = root / project_id
    own = anchor / "assistant" / "session-40"
    own.mkdir(parents=True)
    alias_root = root.with_name(root.name.upper())
    if not alias_root.exists():
        pytest.skip("Filesystem distinguishes workspace root casing")
    assert alias_root.samefile(root)
    variables = _seat(anchor, "assistant", 40, persistent=True)
    caller = _register(
        temp_db,
        project_id,
        seq=40,
        status="active",
        agent_type="assistant",
        folder=own,
        persistent=True,
    )
    (anchor / "README.md").write_text("Shared workspace guide")
    readme = await _decide(
        workspace_read_engine,
        _read(alias_root / project_id / "readme.md", tmp_path),
        variables,
        caller,
    )
    assert readme.decision == "allow", readme.reason
    for seq, status, decision in (
        (30, "expired", "allow"),
        (20, "active", "block"),
        (60, "expired", "block"),
    ):
        folder = own.parent / f"session-{seq}"
        folder.mkdir()
        _register(
            temp_db,
            project_id,
            seq=seq,
            status=status,
            agent_type="assistant",
            folder=folder,
            persistent=True,
        )
        alias = alias_root / project_id / "ASSISTANT" / f"SESSION-{seq}" / "notes.md"
        response = await _decide(workspace_read_engine, _read(alias, tmp_path), variables, caller)
        assert response.decision == decision, (seq, response.reason)
        if decision == "block":
            assert "agent-workspace-read-scope" in (response.reason or "")


@pytest.mark.asyncio
async def test_read_scope_by_persistence(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    project_id: str = sample_project["id"]
    anchor = root / project_id

    def tilde(path: Path) -> str:
        return "~/" + str(path.relative_to(root.parents[1]))

    # A non-persistent developer: its own folder, its epic folder and the README.
    developer = _seat(anchor, "developer", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="developer",
        folder=anchor / "developer" / "session-50",
        persistent=False,
    )
    ended_earlier = anchor / "developer" / "session-10"
    _register(
        temp_db,
        project_id,
        seq=10,
        status="expired",
        agent_type="developer",
        folder=ended_earlier,
        persistent=False,
    )
    for allowed in (
        anchor / "developer" / "session-50" / "notes.md",
        anchor / "epics" / "epic-5" / "plan.md",
        anchor / "README.md",
    ):
        for data in (
            _read(allowed, project),
            _grep(allowed, project),
            _shell(f"cat {allowed}", project),
            _shell(f"cat {tilde(allowed)}", project),
        ):
            response = await _decide(engine, data, dict(developer), caller)
            assert response.decision == "allow", (data, response.reason)
    for refused in (
        ended_earlier / "notes.md",
        anchor / "developer" / "session-51" / "notes.md",
        anchor / "orchestrator" / "session-3" / "notes.md",
        anchor / "developer",
    ):
        for data in (
            _read(refused, project),
            _grep(refused, project),
            _shell(f"cat {refused}", project),
            _shell(f"grep -r lesson {tilde(refused)}", project),
        ):
            response = await _decide(engine, data, dict(developer), caller)
            assert response.decision == "block", data

    # A persistent seat also reads its definition's eligible predecessor folders.
    assistant_folder = anchor / "assistant" / "session-40"
    assistant = _seat(anchor, "assistant", 40, persistent=True)
    successor = _register(
        temp_db,
        project_id,
        seq=40,
        status="active",
        agent_type="assistant",
        folder=assistant_folder,
        persistent=True,
    )
    predecessor = anchor / "assistant" / "session-30"
    _register(
        temp_db,
        project_id,
        seq=30,
        status="expired",
        agent_type="assistant",
        folder=predecessor,
        persistent=True,
    )
    for data in (
        _read(predecessor / "lessons.md", project),
        _grep(predecessor, project),
        _shell(f"cat {predecessor / 'lessons.md'}", project),
        _shell(f"cat {tilde(predecessor / 'lessons.md')}", project),
    ):
        response = await _decide(engine, data, dict(assistant), successor)
        assert response.decision == "allow", (data, response.reason)
    # Never the whole definition directory, and never another definition's folders.
    for refused in (anchor / "assistant", anchor / "orchestrator" / "session-30" / "notes.md"):
        for data in (_grep(refused, project), _shell(f"cat {refused}", project)):
            response = await _decide(engine, data, dict(assistant), successor)
            assert response.decision == "block", data


@pytest.mark.asyncio
async def test_exemptions_and_variable_protection(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])

    # Workspace writes sit outside every checkout: no repo mutation, so no edit-ledger entry.
    for data in (
        _write(own / "notes.md", project),
        _shell(f"echo x > {own / 'notes.md'}", project),
    ):
        event = _event(data, SESSION_ID)
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=dict(variables))
        assert response.decision == "allow", (data, response.reason)
        assert not event.data.get("canonical_repo_mutation"), data

    # No session can repoint its own scope through native set_variable.
    caller = _register(
        temp_db,
        sample_project["id"],
        seq=7,
        status="active",
        agent_type="assistant",
        folder=root / sample_project["id"] / "assistant" / "session-7",
        persistent=True,
    )
    sessions = SessionManager(temp_db)
    for role in ("assistant", "orchestrator", "default"):
        for key in (
            "_agent_workspace_dir",
            "_agent_workspace_epic_dir",
            "_agent_workspace_persistent",
        ):
            error = native_variable_write_error(
                "mcp__gobby__set_variable",
                {"name": key, "value": "/elsewhere", "session_id": caller},
                caller,
                {"_agent_type": role},
                sessions,
            )
            assert error is not None and key in error, (role, key)
        ordinary = native_variable_write_error(
            "mcp__gobby__set_variable",
            {"name": "note", "value": "kept", "session_id": caller},
            caller,
            {"_agent_type": role},
            sessions,
        )
        assert ordinary is None, role


def test_predecessor_eligibility(
    root: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id: str = sample_project["id"]
    anchor = root / project_id
    sessions = SessionManager(temp_db)

    def folder(definition: str, seq: int) -> Path:
        return anchor / definition / f"session-{seq}"

    def register(definition: str, seq: int, status: str, bound: Path | None = None) -> str:
        return _register(
            temp_db,
            project_id,
            seq=seq,
            status=status,
            agent_type=definition,
            folder=bound or folder(definition, seq),
            persistent=definition != "developer",
        )

    caller = register("assistant", 40, "active")
    register("assistant", 30, "expired")
    register("assistant", 35, "active")
    register("assistant", 45, "expired")
    resumed = register("assistant", 32, "active")
    register("assistant", 28, "expired")
    # A live clear successor inherited the session-28 lineage folder.
    register("assistant", 39, "active", bound=folder("assistant", 28))
    register("orchestrator", 20, "expired")

    def readable(path: Path, who: str = caller) -> bool:
        return predecessor_folder_readable(sessions, who, str(path))

    assert readable(folder("assistant", 30)) is True
    assert readable(folder("assistant", 35)) is False, "a live sibling is not a predecessor"
    assert readable(folder("assistant", 45)) is False, "a later lineage is not a predecessor"
    assert readable(folder("assistant", 32)) is False, "a resumed predecessor is live"
    assert readable(folder("assistant", 28)) is False, "a live successor holds the lineage"
    assert readable(folder("orchestrator", 20)) is False, "another definition's folder"
    assert readable(anchor / "assistant") is False, "never the whole definition directory"
    assert readable(folder("assistant", 30) / ".." / "session-35") is False

    sessions.update_status(resumed, "expired")
    assert readable(folder("assistant", 32)) is True

    # A non-persistent seat reads no predecessor folder at all.
    developer = register("developer", 50, "active")
    register("developer", 10, "expired")
    assert readable(folder("developer", 10), developer) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            f'git commit -m "docs: describe the agent workspace layout" -m "Anchor: {TEMPLATE}"',
            id="template-in-second-message",
        ),
        pytest.param(
            f'git commit -m "{TEMPLATE} layout notes"',
            id="message-starts-with-template",
        ),
        pytest.param(
            "git commit -F - <<'EOF'\n"
            "docs: describe the agent workspace layout\n"
            "\n"
            f"Anchor: {TEMPLATE}\n"
            "EOF",
            id="heredoc-body-line",
        ),
    ],
)
async def test_placeholder_template_in_commit_message_is_not_a_read(
    command: str,
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    project_id: str = sample_project["id"]
    anchor = root / project_id
    developer = _seat(anchor, "developer", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="developer",
        folder=anchor / "developer" / "session-50",
        persistent=False,
    )
    # The template is documentation text inside a message, not a path the seat reads.
    response = await _decide(engine, _shell(command, project), dict(developer), caller)
    assert response.decision == "allow", (command, response.reason)


@pytest.mark.asyncio
async def test_concrete_reads_of_other_session_folders_stay_refused(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    project_id: str = sample_project["id"]
    anchor = root / project_id
    developer = _seat(anchor, "developer", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="developer",
        folder=anchor / "developer" / "session-50",
        persistent=False,
    )
    # Concrete paths carrying real ids: a sibling session, and a folder of another project.
    sibling = anchor / "developer" / "session-51" / "notes.md"
    other_project = root / "22222222-2222-4222-8222-222222222222"
    foreign = other_project / "developer" / "session-9" / "notes.md"
    for refused in (sibling, foreign):
        for data in (
            _read(refused, project),
            _shell(f"ls {refused.parent}", project),
            _shell(f"cat {refused}", project),
        ):
            response = await _decide(engine, data, dict(developer), caller)
            assert response.decision == "block", (data, response.reason)
            assert "agent workspace folder is outside" in (response.reason or ""), data
    # A placeholder inside a commit message does not excuse a concrete read in the same command.
    data = _shell(f'git commit -m "Anchor: {TEMPLATE}" && cat {sibling}', project)
    response = await _decide(engine, data, dict(developer), caller)
    assert response.decision == "block", (data, response.reason)
    assert "agent workspace folder is outside" in (response.reason or ""), data


@pytest.mark.asyncio
async def test_own_and_predecessor_folders_stay_readable(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    project_id: str = sample_project["id"]
    anchor = root / project_id
    assistant = _seat(anchor, "assistant", 40, persistent=True)
    successor = _register(
        temp_db,
        project_id,
        seq=40,
        status="active",
        agent_type="assistant",
        folder=anchor / "assistant" / "session-40",
        persistent=True,
    )
    predecessor = anchor / "assistant" / "session-30"
    _register(
        temp_db,
        project_id,
        seq=30,
        status="expired",
        agent_type="assistant",
        folder=predecessor,
        persistent=True,
    )
    for allowed in (anchor / "assistant" / "session-40" / "notes.md", predecessor / "lessons.md"):
        for data in (
            _read(allowed, project),
            _shell(f"ls {allowed.parent}", project),
            _shell(f"cat {allowed}", project),
        ):
            response = await _decide(engine, data, dict(assistant), successor)
            assert response.decision == "allow", (data, response.reason)


@pytest.mark.asyncio
async def test_shell_wrapped_reads_of_other_session_folders_stay_refused(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    project_id: str = sample_project["id"]
    anchor = root / project_id
    developer = _seat(anchor, "developer", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="developer",
        folder=anchor / "developer" / "session-50",
        persistent=False,
    )
    # A shell wrapper runs its command string, so the paths inside that string are reads too.
    sibling = anchor / "developer" / "session-51" / "notes.md"
    for command in (
        f'bash -c "cat {sibling}"',
        f"sh -c 'ls {sibling.parent}'",
        f'zsh -lc "cat {sibling}"',
        f'bash -o pipefail -c "cat {sibling}"',
        f'eval "cat {sibling}"',
    ):
        data = _shell(command, project)
        response = await _decide(engine, data, dict(developer), caller)
        assert response.decision == "block", (command, response.reason)
        assert "agent workspace folder is outside" in (response.reason or ""), command
    # The caller's own folder stays readable through a wrapper.
    own = anchor / "developer" / "session-50" / "notes.md"
    data = _shell(f'bash -c "cat {own}"', project)
    response = await _decide(engine, data, dict(developer), caller)
    assert response.decision == "allow", (data, response.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        "cat <<'EOF' > {target}",
        "cat <<'EOF' >> {target}",
        "tee {target} <<'EOF'",
        "git commit --only -F - -- {target} <<'EOF'",
    ],
)
@pytest.mark.parametrize("target_kind", ["config", "foreign"])
async def test_quoted_data_heredoc_still_checks_real_write_targets(
    engine: RuleEngine, root: Path, tmp_path: Path, header: str, target_kind: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    target = (
        own / ".gobby" / "project.json"
        if target_kind == "config"
        else root / PROJECT / "developer" / "session-99" / "notes.md"
    )
    command = header.format(target=target) + "\nLiteral commit or document content\nEOF"
    data = _shell(command, project)
    normalize_tool_fields(data)

    response = await _decide(engine, data, variables)

    assert response.decision == "block", (command, response.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "decision"),
    [
        ("--only -- .gobby/project.json", "block"),
        ("--only .gobby/project.json", "block"),
        ("-o -- .git/config", "block"),
        ("--only -- ../session-99/notes.md", "block"),
        ("--only -- .gobby/plans/plan.md", "allow"),
        ("--only -- notes.md", "allow"),
        ("-m '.gobby/project.json' -- notes.md", "allow"),
        ("-m.git/config -- notes.md", "allow"),
        ("--only -- ':(top).gobby/project.json'", "block"),
        ("--only -- '.gobby/*'", "block"),
        ("--pathspec-from-file=paths.txt", "block"),
        ("--only -- '$TARGET'", "block"),
    ],
)
async def test_git_commit_pathspec_workspace_scope(
    engine: RuleEngine, root: Path, tmp_path: Path, arguments: str, decision: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    message_option = "" if arguments.startswith("-m") else "-F - "
    command = f"git -C {own} commit {message_option}{arguments} <<'EOF'\nLiteral .git/config\nEOF"
    data = _shell(command, project)
    normalize_tool_fields(data)

    response = await _decide(engine, data, variables)

    assert response.decision == decision, (command, response.reason)


@pytest.mark.asyncio
async def test_quoted_heredoc_bodies_and_quoted_text_judge_concrete_foreign_paths(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    project_id: str = sample_project["id"]
    anchor = root / project_id
    developer = _seat(anchor, "developer", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="developer",
        folder=anchor / "developer" / "session-50",
        persistent=False,
    )
    # A shell that reads a quoted heredoc body runs it, so the body is judged as shell code.
    # Literal heredoc data does not read the paths it describes.
    sibling = anchor / "developer" / "session-51" / "notes.md"
    for command in (
        f"bash <<'EOF'\ncat {sibling}\nEOF",
        f"sh -s <<'EOF'\ncat {sibling}\nEOF",
        f"cat <<'EOF' | bash\ncat {sibling}\nEOF",
        f"python - <<'EOF'\npath = '{sibling}'\nprint(open(path).read())\nEOF",
        f"ruby <<'EOF'\npath = '{sibling}'\nputs File.read(path)\nEOF",
        f"cat <<'EOF' | python -\npath = '{sibling}'\nprint(open(path).read())\nEOF",
        f"cat <<'DATA'\nliteral\nDATA\npython - <<'PY'\npath = '{sibling}'\nprint(open(path).read())\nPY",
        f'git commit -m "see {sibling}"',
    ):
        data = _shell(command, project)
        response = await _decide(engine, data, dict(developer), caller)
        assert response.decision == "block", (command, response.reason)
        assert "agent workspace folder is outside" in (response.reason or ""), command
    # The caller's own folder stays readable in the same positions.
    own = anchor / "developer" / "session-50" / "notes.md"
    for command in (
        f"git commit -F - <<'EOF'\nAnchor: {sibling}\nEOF",
        f"cat > {own} <<'EOF'\n# Handoff\nPrior evidence: {sibling}\nEOF",
    ):
        response = await _decide(engine, _shell(command, project), dict(developer), caller)
        assert response.decision != "block", (command, response.reason)
    for command in (
        f"bash <<'EOF'\ncat {own}\nEOF",
        f"git commit -F - <<'EOF'\nAnchor: {own}\nEOF",
    ):
        data = _shell(command, project)
        response = await _decide(engine, data, dict(developer), caller)
        assert response.decision == "allow", (command, response.reason)
