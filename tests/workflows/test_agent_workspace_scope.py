"""Agent workspace write and read scope rules (addendum 9.2)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import pytest

from gobby.agents.agent_workspaces import predecessor_folder_readable
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.condition_helpers_sessions import agent_workspace_read_violation
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.idle_declaration import native_variable_write_error
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import seed_seat
from tests.fixtures.isolated_checkout import init_git_checkout

pytestmark = pytest.mark.unit

PROJECT = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
TEMPLATE = "~/.gobby/agents_workspaces/<project_id>/<definition>/<session>/"


@pytest.mark.parametrize("exempt", [True, False])
@pytest.mark.parametrize("reader", ["native", "shell"])
async def test_read_scope_selector_can_exempt_a_synthetic_seat(
    engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    exempt: bool,
    reader: str,
) -> None:
    agent_type = "scope-exempt" if exempt else "scope-limited"
    seed_seat(
        temp_db,
        agent_type,
        include=["name:fixture-read-scope"],
        exclude=["name:fixture-read-scope"] if exempt else [],
    )
    project_id: str = sample_project["id"]
    anchor = root / project_id
    variables = _seat(anchor, agent_type, 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type=agent_type,
        folder=Path(variables["_agent_workspace_dir"]),
        persistent=False,
    )
    report_folder = anchor / "fixture-definition-3588bb72" / "session-51"
    _register(
        temp_db,
        project_id,
        seq=51,
        status="active",
        agent_type="fixture-definition-3588bb72",
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
    expected = "allow" if exempt else "block"
    assert response.decision == expected, response.reason
    if not exempt:
        assert "fixture-read-scope" in (response.reason or "")


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


def _scope_engine(db: HubDatabase, modes: tuple[str, ...]) -> RuleEngine:
    for mode in modes:
        expression = (
            f"agent_workspace_{mode}_violation(event.data, tool_input, variables, "
            "event.metadata.get('_platform_session_id'))"
        )
        RuleDefinitionManager(db).create(
            f"fixture-{mode}-scope",
            {
                "event": "before_tool",
                "when": expression,
                "effects": [{"type": "block", "reason": "Synthetic workspace scope refused"}],
            },
            tags=["default", "runbook"],
        )
    # A named seat must resolve its definition; an unresolved one fails closed.
    for seat in ("fixture-seat", *(name for name in SEATS if name is not None)):
        seed_seat(db, seat, include=["tag:runbook"])
    return RuleEngine(db)


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    return _scope_engine(temp_db, ("read", "write"))


@pytest.mark.asyncio
@pytest.mark.parametrize("command_field", [False, True])
@pytest.mark.parametrize(
    ("relative", "decision"),
    [
        (".gobby/plans/runbook.md", "allow"),
        ("src/gobby/install/shared/AGENTS.md", "allow"),
        (".gobby/project.json", "block"),
        (".git/config", "block"),
    ],
)
async def test_patch_prose_checks_destinations_without_executing_shell_text(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command_field: bool,
    relative: str,
    decision: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    target = Path(variables["_agent_workspace_dir"]) / "task-25298" / relative
    # Reduced from the refused plan patch, preserving its shell punctuation.
    patch = (
        f"*** Begin Patch\n*** Update File: {target}\n@@\n-before\n"
        "+Each leaf\x27s \x60implementation_domain\x60 selects skills.\n*** End Patch\n"
    )
    data: dict[str, Any] = {
        "tool_name": "apply_patch",
        "tool_input": {"command": patch} if command_field else patch,
        "cwd": str(tmp_path),
    }
    normalize_tool_fields(data)
    assert data["canonical_file_paths"] == [str(target)]
    response = await _decide(workspace_write_engine, data, variables)
    assert response.decision == decision, response.reason
    if decision == "block":
        assert "fixture-write-scope" in (response.reason or "")


@pytest.fixture
def workspace_write_engine(temp_db: HubDatabase) -> RuleEngine:
    return _scope_engine(temp_db, ("write",))


@pytest.fixture
def workspace_read_engine(temp_db: HubDatabase) -> RuleEngine:
    return _scope_engine(temp_db, ("read",))


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


@pytest.mark.parametrize("operation", ["write", "commit"])
@pytest.mark.parametrize(
    ("target_kind", "expected"),
    [
        ("tracked_baseline", "allow"),
        ("new_repo_config", "allow"),
        ("bare_session_config", "block"),
        ("git_marker", "block"),
        ("git_config", "block"),
        ("symlink_to_session_config", "block"),
        ("checkout_unavailable", "block"),
    ],
)
async def test_owned_task_worktree_repository_content_keeps_metadata_boundary(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    target_kind: str,
    expected: str,
) -> None:
    project = tmp_path / "checkout"
    (project / ".gobby").mkdir(parents=True)
    (project / ".gobby/test-types-baseline.json").write_text("{}\n", encoding="utf-8")
    init_git_checkout(project)
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    worktree = own / "task-25133"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree)],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    session_config = own / ".gobby/project.json"
    session_config.parent.mkdir()
    session_config.write_text("{}\n", encoding="utf-8")
    alias = worktree / ".gobby/session-config.json"
    alias.symlink_to(session_config)
    target = {
        "tracked_baseline": worktree / ".gobby/test-types-baseline.json",
        "new_repo_config": worktree / ".gobby/project.json",
        "bare_session_config": session_config,
        "git_marker": worktree / ".git",
        "git_config": worktree / ".git/config",
        "symlink_to_session_config": alias,
        "checkout_unavailable": worktree / ".gobby/test-types-baseline.json",
    }[target_kind]
    if target_kind == "checkout_unavailable":

        def unavailable(_path: Path) -> Path | None:
            raise OSError("checkout inspection unavailable")

        monkeypatch.setattr("gobby.workflows.condition_helpers_paths.checkout_root", unavailable)
    if operation == "commit":
        event = _shell(f"git commit --only -m 'task change' -- {target}", worktree)
    else:
        event = _write(target, worktree)
    normalize_tool_fields(event)

    response = await _decide(workspace_write_engine, event, variables)

    assert response.decision == expected, (target_kind, operation, response.reason)
    if expected == "block":
        assert "fixture-write-scope" in (response.reason or "")


SEATS = (
    "fixture-definition-3588bb72",
    "fixture-helper",
    "fixture-definition-599e0437",
    "fixture-worker",
    None,
)


@pytest.mark.parametrize(
    "command",
    [
        "tar -xf archive.tar -C {destination}",
        "tar xf archive.tar -C{destination}",
        "tar --extract --file=archive.tar --directory={destination}",
        "bsdtar -xzf archive.tgz -C {destination}",
        "gtar -xf archive.tar -C {destination}",
        "git archive HEAD | tar -x -C {destination}",
        "cd {destination} && tar -xf archive.tar",
        "sh -c 'tar -xf archive.tar -C {destination}'",
        "env -C {destination} tar -xf archive.tar",
        'for output in {destination}; do tar -xf archive.tar -C "$output"; done',
        "tar -xf archive.tar -C {destination} && tar -xf other.tar -C {scratch}",
        "tar -xf other.tar -C {scratch} && tar -xf archive.tar -C {destination}",
        "unzip archive.zip -d {destination}",
        "unzip -oq -d{destination} archive.zip",
        "unzip -oqd {destination} archive.zip",
        "unzip -P -p archive.zip -d {destination}",
        "cd {destination} && unzip archive.zip",
        "7z x archive.7z -o{destination}",
        "7zz e archive.7z -o{destination}",
        "7z x -o{destination} -- -so",
        "unrar x archive.rar {destination}/",
        "unrar x -op{destination} archive.rar",
        "rar e -op{destination} archive.rar",
        "ditto -x -k archive.zip {destination}",
        "ditto -xk archive.zip {destination}",
        "ditto -kx archive.zip {destination}",
        "ditto -Vxk archive.zip {destination}",
        "ditto -V -xk archive.zip {destination}",
        "ditto --rsrc -xk archive.zip {destination}",
        "ditto -xk --sequesterRsrc archive.zip {destination}",
        "cd {destination} && jar xf archive.jar",
        "cd {destination} && jar xf --dir=/tmp/out",
    ],
)
@pytest.mark.parametrize("target", ["seat", "checkout", "scratch"])
async def test_archive_extraction_requires_a_checkout_in_seat_folders(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command: str,
    target: str,
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    own = Path(variables["_agent_workspace_dir"])
    checkout = own / "task-checkout"
    (checkout / ".git").mkdir(parents=True)
    destinations = {
        "seat": own / "candidate",
        "checkout": checkout / "unpacked",
        "scratch": tmp_path / "scratch",
    }
    destination = destinations[target]
    destination.mkdir(parents=True)
    response = await _decide(
        workspace_write_engine,
        _shell(command.format(destination=destination, scratch=tmp_path / "other"), project),
        variables,
    )
    assert response.decision == ("block" if target == "seat" else "allow"), response.reason


@pytest.mark.parametrize(
    "command",
    [
        "xargs touch {destination}",
        "xargs -0 -I ITEM touch {destination}/ITEM",
        "sudo touch {destination}",
        "sudo -u root -- touch {destination}",
        "sudo -D /tmp touch {destination}",
        "sudo xargs touch {destination}",
        "sh -c 'sudo touch {destination}'",
        "env xargs touch {destination}",
        "printf x | xargs touch {destination}",
        "sudo touch {destination} && cat README.md",
        "cat README.md && xargs touch {destination}",
        "xargs time -o {destination} cat README.md",
        "xargs env time -o {destination} cat README.md",
    ],
)
@pytest.mark.parametrize("metadata", [".git", ".gobby"])
async def test_unresolved_wrapped_writes_cannot_plant_seat_metadata(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command: str,
    metadata: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    destination = Path(variables["_agent_workspace_dir"]) / metadata / "x"
    response = await _decide(
        workspace_write_engine,
        _shell(command.format(destination=destination), tmp_path),
        variables,
    )
    assert response.decision == "block", response.reason
    assert "fixture-write-scope" in (response.reason or "")


@pytest.mark.parametrize("flags", ["", "-V", "--rsrc", "--bom -xk", "--arch arm64"])
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (".git/x", "block"),
        (".gobby/x", "block"),
        ("candidate", "block"),
        ("task-checkout/unpacked", "allow"),
        ("scratch", "allow"),
    ],
)
async def test_ditto_copy_enforces_destination_and_unknown_member_scope(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    flags: str,
    target: str,
    expected: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    own = Path(variables["_agent_workspace_dir"])
    (own / "task-checkout/.git").mkdir(parents=True)
    destination = tmp_path / "scratch" if target == "scratch" else own / target
    event = _shell(f"ditto {flags} source {destination}", tmp_path)
    normalize_tool_fields(event)
    assert event["canonical_tool_kind"] == "write"
    assert event["canonical_file_paths"] == [str(destination)]
    assert event["canonical_write_file_paths"] == [str(destination)]

    response = await _decide(workspace_write_engine, event, variables)
    assert response.decision == expected, response.reason


@pytest.mark.parametrize("flags", ["-ck", "-c -k", "-Vck"])
async def test_ditto_archive_creation_allows_an_ordinary_seat_file(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, flags: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    destination = Path(variables["_agent_workspace_dir"]) / "candidate.zip"
    event = _shell(f"ditto {flags} source {destination}", tmp_path)
    normalize_tool_fields(event)
    assert event["canonical_tool_kind"] == "write"
    assert event["canonical_file_paths"] == [str(destination)]

    response = await _decide(workspace_write_engine, event, variables)
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize("option", ["--outBom", "--keepBinariesList"])
async def test_ditto_auxiliary_outputs_cannot_write_seat_metadata(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, option: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    destination = Path(variables["_agent_workspace_dir"]) / ".gobby/x"
    response = await _decide(
        workspace_write_engine,
        _shell(f"ditto -ck {option} {destination} source {tmp_path}/archive.zip", tmp_path),
        variables,
    )
    assert response.decision == "block", response.reason


@pytest.mark.parametrize("command", ["sudo --help", "xargs --help", "xargs cat", "cat README.md"])
async def test_proven_reads_and_wrapper_help_keep_workspace_access(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, command: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    response = await _decide(workspace_write_engine, _shell(command, tmp_path), variables)
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize(
    ("command", "read_only"),
    [
        ("git ls-files | xargs grep -l foo", True),
        ("echo a b | xargs echo", True),
        ("rg --pre cat foo", True),
        ("sudo touch report.txt", False),
        ("xargs touch report.txt", False),
        ("xargs /usr/bin/true", True),
        ("xargs opaque-command report.txt", False),
        ("xargs -I ITEM unzip ITEM -d unpacked", False),
        ('xargs -I ITEM unzip ITEM -d "$DEST"', False),
    ],
)
@pytest.mark.parametrize("has_seat", [True, False])
@pytest.mark.parametrize("location", ["outside", "nested_checkout", "seat"])
async def test_unresolved_execution_preserves_access_outside_protected_seat_folders(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command: str,
    read_only: bool,
    has_seat: bool,
    location: str,
) -> None:
    seat = _seat(root / PROJECT, "fixture-seat", 12)
    variables = seat if has_seat else {}
    project = tmp_path / "project"
    if location == "seat":
        project = Path(seat["_agent_workspace_dir"])
        project.mkdir(parents=True)
    elif location == "nested_checkout":
        project = Path(seat["_agent_workspace_dir"]) / "task-checkout"
        (project / ".git").mkdir(parents=True)
    else:
        project.mkdir()
    response = await _decide(workspace_write_engine, _shell(command, project), variables)
    # Writes into a checkout without ownership variables target another seat;
    # read operands are checked by the separate workspace-read rule.
    expected = (
        "block"
        if not read_only
        and (location == "seat" or (location == "nested_checkout" and not has_seat))
        else "allow"
    )
    assert response.decision == expected, response.reason


@pytest.mark.parametrize(
    "command",
    [
        "cd {directory} && xargs touch {name}",
        "env -C {directory} xargs touch {name}",
        "sudo -D {directory} touch {name}",
        "sudo -D{directory} touch {name}",
        "sudo -nD{directory} touch {name}",
        "sudo --chdir={directory} touch {name}",
        "xargs env -C {directory} touch {name}",
    ],
)
@pytest.mark.parametrize("location", ["seat", "checkout"])
@pytest.mark.parametrize("name", [".git/x", "report.txt"])
async def test_unresolved_execution_uses_literal_wrapper_directories(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command: str,
    location: str,
    name: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    own = Path(variables["_agent_workspace_dir"])
    (own / "checkout/.git").mkdir(parents=True)
    directory = own if location == "seat" else own / "checkout"
    response = await _decide(
        workspace_write_engine,
        _shell(command.format(directory=directory, name=name), tmp_path),
        variables,
    )
    expected = "allow" if location == "checkout" and name == "report.txt" else "block"
    assert response.decision == expected, response.reason


@pytest.mark.parametrize("argument", ["-o{path}", "-ao{path}", "--output={path}", "-o {path}"])
async def test_unresolved_execution_keeps_relative_wrapper_output_paths(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, argument: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    own = Path(variables["_agent_workspace_dir"])
    relative = own.relative_to(tmp_path) / ".git/x"
    response = await _decide(
        workspace_write_engine,
        _shell(f"xargs time {argument.format(path=relative)} cat README.md", tmp_path),
        variables,
    )
    assert response.decision == "block", response.reason


async def test_unresolved_stdin_is_not_assumed_to_be_a_literal_write_target(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    own = Path(variables["_agent_workspace_dir"])
    event = _shell(f"printf '%s' {own}/.git/x | xargs touch", tmp_path)
    normalize_tool_fields(event)
    assert event["canonical_unresolved_execution"] is True
    assert event["canonical_repo_mutation_scope_unknown"] is True
    response = await _decide(workspace_write_engine, event, variables)
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize("wrapper", ["sudo -D", "sudo --chdir", "xargs env -C"])
async def test_literal_wrapper_directory_replaces_the_bare_seat_cwd(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, wrapper: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    response = await _decide(
        workspace_write_engine,
        _shell(f"{wrapper} {tmp_path} /usr/bin/true", own),
        variables,
    )
    assert response.decision == "allow", response.reason


async def test_wrapper_output_does_not_attribute_read_sources_as_mutations(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    source = Path(variables["_agent_workspace_dir"]) / ".gobby/report.txt"
    output = tmp_path / "timing.txt"
    event = _shell(f"xargs time -o {output} cat {source}", tmp_path)
    normalize_tool_fields(event)
    assert event["canonical_file_paths"] == [str(output)]
    assert event["canonical_write_file_paths"] == [str(output)]
    response = await _decide(workspace_write_engine, event, variables)
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize(
    "command",
    [
        "tar -tf archive.tar",
        "tar -xOf archive.tar",
        "bsdtar -xOf archive.tar",
        "unzip -l archive.zip",
        "unzip -p archive.zip",
        "7z l archive.7z",
        "unrar l archive.rar",
    ],
)
async def test_archive_inspection_keeps_seat_folder_access(
    workspace_write_engine: RuleEngine, root: Path, command: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    response = await _decide(workspace_write_engine, _shell(command, own), variables)
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize(
    "command",
    [
        "tar -xPf archive.tar",
        "unzip -: archive.zip",
        "7z x -spf archive.7z",
        "unrar x -ep3 archive.rar",
        "unrar x -opoutput archive.rar other/",
        "cpio -id < archive.cpio",
        "cpio -id -F -t",
        "cpio -id --no-absolute-filenames < archive.cpio",
        "pax -r -f archive.pax",
        "pax -r -f -w",
        "pax -r -s ',^,../,' -f archive.pax",
    ],
)
@pytest.mark.parametrize("location", ["outside", "seat", "nested_checkout"])
async def test_archive_extraction_uncertainty_obeys_workspace_containment(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command: str,
    location: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    project = tmp_path
    if location == "seat":
        project = Path(variables["_agent_workspace_dir"])
        project.mkdir(parents=True)
    elif location == "nested_checkout":
        project = Path(variables["_agent_workspace_dir"]) / "task-checkout"
        (project / ".git").mkdir(parents=True)
    event = _shell(command, project)
    normalize_tool_fields(event)
    assert event["canonical_repo_mutation_scope_unknown"] is True
    response = await _decide(workspace_write_engine, event, variables)
    assert response.decision == ("block" if location == "seat" else "allow"), response.reason


@pytest.mark.parametrize(
    "command",
    [
        'tar -xf a.tar -C "$OUT"',
        'unzip a.zip -d "$OUT"',
        'D=$(mktemp -d) && tar -xf a.tar -C "$D"',
        "cpio -id < a.cpio",
        "pax -r -f a.pax",
        "tar -xPf a.tar",
    ],
)
@pytest.mark.parametrize("has_seat", [True, False])
@pytest.mark.parametrize("location", ["outside", "seat", "nested_checkout"])
async def test_unresolved_archive_destinations_follow_workspace_scope(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command: str,
    has_seat: bool,
    location: str,
) -> None:
    seat = _seat(root / PROJECT, "fixture-seat", 12)
    project = tmp_path
    if location == "seat":
        project = Path(seat["_agent_workspace_dir"])
        project.mkdir(parents=True)
    elif location == "nested_checkout":
        project = Path(seat["_agent_workspace_dir"]) / "task-checkout"
        (project / ".git").mkdir(parents=True)
    event = _shell(command, project)
    normalize_tool_fields(event)
    assert event["canonical_repo_mutation_scope_unknown"] is True
    response = await _decide(workspace_write_engine, event, seat if has_seat else {})
    expected = (
        "block"
        if location == "seat" or (location == "nested_checkout" and not has_seat)
        else "allow"
    )
    assert response.decision == expected, response.reason


@pytest.mark.parametrize("command", ['tar -xf a.tar -C "$OUT"', 'unzip a.zip -d "$OUT"'])
@pytest.mark.parametrize("inside", [True, False])
@pytest.mark.parametrize("nested", [True, False])
async def test_unresolved_archive_destinations_use_effective_shell_cwd(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    command: str,
    inside: bool,
    nested: bool,
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    seat = Path(variables["_agent_workspace_dir"])
    seat.mkdir(parents=True)
    initial, effective = (tmp_path, seat) if inside else (seat, tmp_path)
    command = f"cd {effective} && {command}"
    if nested:
        command = f"sh -c '{command}'"
    event = _shell(command, initial)
    response = await _decide(workspace_write_engine, event, variables)
    assert response.decision == ("block" if inside else "allow"), response.reason


async def test_unresolved_archive_cwd_does_not_include_a_sibling_extraction_cwd(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    seat = Path(variables["_agent_workspace_dir"])
    seat.mkdir(parents=True)
    command = f'tar -xf a.tar -C {tmp_path} && cd {tmp_path} && unzip b.zip -d "$OUT"'
    response = await _decide(workspace_write_engine, _shell(command, seat), variables)
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize(
    "command", ["tar -xPf archive.tar -C {destination}", "unzip -: archive.zip -d {destination}"]
)
async def test_unknown_archive_members_keep_literal_outside_destination_scope(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, command: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    seat = Path(variables["_agent_workspace_dir"])
    seat.mkdir(parents=True)
    event = _shell(command.format(destination=tmp_path), seat)
    normalize_tool_fields(event)
    assert event["canonical_repo_mutation_scope_unknown"] is True
    response = await _decide(workspace_write_engine, event, variables)
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize(
    "command",
    [
        "find src | xargs grep -l foo",
        "rg --pre cat foo {seat}/cand",
        "sudo cat {seat}/cand/pyproject.toml",
    ],
)
@pytest.mark.parametrize("has_seat", [True, False])
async def test_read_only_children_do_not_supply_workspace_write_evidence(
    workspace_write_engine: RuleEngine,
    root: Path,
    command: str,
    has_seat: bool,
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    seat = Path(variables["_agent_workspace_dir"])
    (seat / "cand").mkdir(parents=True)
    event = _shell(command.format(seat=seat), seat)
    response = await _decide(workspace_write_engine, event, variables if has_seat else {})
    assert response.decision == "allow", response.reason


@pytest.mark.parametrize("preprocessor", ["touch", "/usr/bin/touch", "opaque-preprocessor"])
@pytest.mark.parametrize("attached", [True, False])
@pytest.mark.parametrize("explicit_path", [True, False])
@pytest.mark.parametrize("prefix", ["", "true && "])
async def test_rg_preprocessor_write_or_opaque_scope_is_preserved(
    workspace_write_engine: RuleEngine,
    root: Path,
    preprocessor: str,
    attached: bool,
    explicit_path: bool,
    prefix: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    seat = Path(variables["_agent_workspace_dir"])
    (seat / ".gobby").mkdir(parents=True)
    target = seat / ".gobby/config.yaml"
    target.write_text("foo: bar\n")
    option = f"--pre={preprocessor}" if attached else f"--pre {preprocessor}"
    command = f"{prefix}rg {option} foo" + (f" {target}" if explicit_path else "")
    response = await _decide(workspace_write_engine, _shell(command, seat), variables)
    assert response.decision == "block", response.reason


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("--pre=cat foo {target}", "allow"),
        # A binary invoked by path has no readable-script proof; keep it opaque.
        ("--pre /usr/bin/cat foo {target}", "block"),
        ("--pre touch --no-pre foo {target}", "allow"),
        ("--no-pre --pre touch foo {target}", "block"),
        ("--pre touch --pre cat foo {target}", "allow"),
        ("--pre cat --pre touch foo {target}", "block"),
        ("-e --pre {target}", "allow"),
        ("-- --pre {target}", "allow"),
        ("--pre= foo {target}", "allow"),
    ],
)
async def test_rg_preprocessor_proof_follows_option_values_and_order(
    workspace_write_engine: RuleEngine, root: Path, arguments: str, expected: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-seat", 12)
    seat = Path(variables["_agent_workspace_dir"])
    seat.mkdir(parents=True)
    command = "rg " + arguments.format(target=seat / ".gobby/config.yaml")
    response = await _decide(workspace_write_engine, _shell(command, seat), variables)
    assert response.decision == expected, response.reason


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
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
        (root / PROJECT / "fixture-worker" / "session-99" / ".gobby" / "plans" / "a.md", "block"),
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
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
            assert "fixture-write-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("ancestor", ["gobby", "root", "definition"])
@pytest.mark.parametrize("tool", ["write", "commit"])
async def test_case_alias_of_workspace_root_does_not_bypass_write_scope(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, ancestor: str, tool: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
            assert "fixture-write-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["own", "outside"])
@pytest.mark.parametrize("tool", ["Bash", "Glob"])
async def test_glob_literal_prefix_checks_resolved_seat(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    location: str,
    tool: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    safe = own / "target"
    foreign = own.parent / "session-99"
    for directory in (safe, foreign):
        directory.mkdir(parents=True)
        (directory / "notes.md").write_text("Fixture notes")
    search = own if location == "own" else tmp_path / "repository"
    search.mkdir(exist_ok=True)
    link = search / "leak"
    link.symlink_to(foreign)
    pattern = "leak/*.md"
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
    assert "fixture-read-scope" in (refused.reason or "")
    link.unlink()
    link.symlink_to(safe)
    allowed = await _decide(workspace_read_engine, data, variables)
    assert allowed.decision == "allow", allowed.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern",
    [
        "{leak,notes}.md",
        "{x,{leak,notes}}.md",
    ],
)
async def test_bare_shell_patterns_check_resolved_seat(
    workspace_read_engine: RuleEngine, root: Path, pattern: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    own.mkdir(parents=True)
    foreign.mkdir()
    (foreign / "notes.md").write_text("Foreign notes")
    link = own / "leak.md"
    link.symlink_to(foreign / "notes.md")
    data = _shell(f"cat {pattern}", own)
    refused = await _decide(workspace_read_engine, data, variables)
    assert refused.decision == "block"
    link.unlink()
    link.write_text("Owned notes")
    allowed = await _decide(workspace_read_engine, data, variables)
    assert allowed.decision == "allow", allowed.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["{a,b}.md", "[ab].md"])
@pytest.mark.parametrize("tool", ["Read", "Bash"])
async def test_literal_pattern_filename_cannot_hide_foreign_symlink(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, name: str, tool: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    own.mkdir(parents=True)
    foreign.mkdir()
    target = foreign / "notes.md"
    target.write_text("Foreign notes")
    link = own / name
    link.symlink_to(target)
    data = _read(link, tmp_path) if tool == "Read" else _shell(f"cat '{link}'", tmp_path)
    refused = await _decide(workspace_read_engine, data, variables)
    assert refused.decision == "block"
    link.unlink()
    link.write_text("Owned notes")
    allowed = await _decide(workspace_read_engine, data, variables)
    assert allowed.decision == "allow", allowed.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["{a,b}", "[ab]", "*"])
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
async def test_file_tool_paths_are_literal_not_patterns(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, name: str, tool: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    directory = own / name
    foreign = own.parent / "session-99"
    for folder in (directory, foreign):
        folder.mkdir(parents=True)
        (folder / "notes.md").write_text("Fixture notes")
    (own / "a").symlink_to(foreign)
    data = {
        "Read": _read(directory / "notes.md", tmp_path),
        "Grep": _grep(directory, tmp_path),
        "Glob": {
            "tool_name": "Glob",
            "tool_input": {"path": str(directory), "pattern": "*.md"},
        },
    }[tool]
    response = await _decide(workspace_read_engine, data, variables)
    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", [".[.]", ".[^a]", ".[[:punct:]]", ".*", "@(.|..)"])
@pytest.mark.parametrize("tool", ["Bash", "Glob"])
async def test_shell_dot_patterns_cannot_traverse_to_foreign_sibling(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, pattern: str, tool: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    own.mkdir(parents=True)
    foreign.mkdir()
    (foreign / "notes.md").write_text("Foreign notes")
    relative = f"{pattern}/session-99/notes.md"
    data = (
        _shell(f"cat {own / relative}", tmp_path)
        if tool == "Bash"
        else {"tool_name": "Glob", "tool_input": {"path": str(own), "pattern": relative}}
    )
    response = await _decide(workspace_read_engine, data, variables)
    assert response.decision == ("block" if tool == "Bash" else "allow"), response.reason


@pytest.mark.parametrize("location", ["own", "outside"])
def test_large_regular_trees_keep_glob_reads_fast_and_allowed(
    root: Path, tmp_path: Path, location: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    directory = (
        Path(variables["_agent_workspace_dir"]) if location == "own" else tmp_path / "repository"
    )
    for number in range(90):
        package = directory / "src" / "gobby" / f"package-{number}"
        package.mkdir(parents=True)
        for item in range(100):
            (package / f"module-{item}.py").touch()
    for pattern in ("**/*.py", "src/gobby/**/*.py"):
        data: dict[str, Any] = {
            "tool_name": "Glob",
            "tool_input": {"path": str(directory), "pattern": pattern},
        }
        started = perf_counter()
        refused = agent_workspace_read_violation(None, data, data["tool_input"], variables, None)
        elapsed = perf_counter() - started
        assert not refused, (location, pattern)
        assert elapsed < 0.5, (location, pattern, elapsed)
    foreign = Path(variables["_agent_workspace_dir"]).parent / "session-99" / "notes.py"
    foreign.parent.mkdir(parents=True)
    foreign.touch()
    (directory / "src/gobby/package-89/leak.py").symlink_to(foreign)
    # Literal symlinks remain checked; wildcard-selected symlinks are the
    # explicitly deferred SRT policy decision in #25307.
    data["tool_input"]["pattern"] = "src/gobby/package-89/leak.py"
    assert agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    ("tool", "expression", "directory"),
    [
        ("Glob", "**/*.py", "."),
        ("Glob", "src/**/*.py", "."),
        ("Glob", "src/gobby/**/*.py", "."),
        ("Glob", "**/*.{ts,tsx}", "web"),
        ("Bash", "ls src/**/*.py", "."),
        ("Bash", "git diff -- '**/*.py'", "."),
    ],
)
def test_real_checkout_glob_regressions_remain_allowed(
    root: Path, tool: str, expression: str, directory: str
) -> None:
    checkout = Path(__file__).resolve().parents[2]
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    data = (
        _shell(expression, checkout)
        if tool == "Bash"
        else {
            "tool_name": "Glob",
            "tool_input": {"path": str(checkout / directory), "pattern": expression},
        }
    )
    started = perf_counter()
    refused = agent_workspace_read_violation(None, data, data["tool_input"], variables, None)
    elapsed = perf_counter() - started
    assert not refused, (tool, expression)
    assert elapsed < 0.5, (tool, expression, elapsed)


@pytest.mark.parametrize("command", ["ls ~/*", "cat ~/*.md", "ls /*"])
def test_ancestor_glob_does_not_imply_workspace_read(
    root: Path, tmp_path: Path, command: str
) -> None:
    root.mkdir(parents=True)
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    data = _shell(command, tmp_path)
    assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize("tool", ["Bash", "Glob"])
def test_glob_scope_checks_do_not_enumerate_directories(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    data = (
        _shell("git diff -- '**/*.py'", own)
        if tool == "Bash"
        else {"tool_name": "Glob", "tool_input": {"path": str(own), "pattern": "**/*.py"}}
    )

    def forbidden_scan(path: object) -> None:
        raise AssertionError(f"Read-scope hooks must not enumerate {path}")

    with monkeypatch.context() as scope:
        scope.setattr(os, "scandir", forbidden_scan)
        assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize("location", ["own", "outside"])
@pytest.mark.parametrize(
    "command",
    [
        "grep -E 'src/(a|b)?(/x)' README.md",
        r"sed -E 's/a*(b\/c)/d/' README.md",
        "grep -E '(foo|bar)+(/baz)' README.md",
        "rg 'x@(a/b)' src",
        r"grep -E 'import (os|sys)*(\.path/)' src/x.py",
        "grep -E '(a|b)*(c)' README.md",
        "grep -E 'v?(1|2)' README.md",
        'rg -n "(_agent_type|agent_name|agent_type|agent_definition|\\.name|\\.role|audience)'
        ".*(==|!=| in | not in ).*(fixture-definition-0f916789|fixture-coordinator|dev-manager|fixture-helper|fixture-worker|"
        'fixture-definition-3588bb72|reviewer|fixture-definition-c02389e4)|(_agent_type|agent_name|agent_type).*get\\(" '
        "src/gobby/agents src/gobby/workflows src/gobby/mcp_proxy src/gobby/tasks "
        "src/gobby/install/shared/workflows --glob '*.py' --glob '*.yaml' -m 50",
    ],
)
def test_regex_data_operands_are_not_extglob_paths(
    root: Path, tmp_path: Path, location: str, command: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    directory = own if location == "own" else tmp_path
    data = _shell(command, directory)
    assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)
    foreign = own.parent / "session-99" / "notes.md"
    data = _shell(f"{command} '{foreign}'", directory)
    assert agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    "command",
    [
        "grep -efoo",
        "grep --regexp=foo",
        "rg --regexp=foo",
        "grep -nefoo",
        "grep -ffile",
        "rg --file=file",
        "sed -nffile",
        "grep -- -efoo",
        "grep -e --regexp",
        "rg -e --regexp",
        "sed -e --expression",
    ],
)
def test_explicit_regex_option_keeps_first_file_operand(
    root: Path, tmp_path: Path, command: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    foreign = Path(variables["_agent_workspace_dir"]).parent / "session-99/notes.md"
    data = _shell(f"{command} '{foreign}'", tmp_path)
    assert agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


def test_reported_project_grep_is_allowed_from_checkout(root: Path, tmp_path: Path) -> None:
    # The reported command, verbatim, run from the project checkout. Its leading cd narrows
    # the plan read to the project; nothing it resolves is a foreign seat folder.
    variables = _seat(root / PROJECT, "developer", 12)
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    command = (
        r"cd /Users/josh/Projects/gobby && grep -n -E "
        r"'^#{1,4} |^`kind:|^\*\*(Plan ID|Granularity|Acceptance)|^Targets:|"
        r"^Consumers unchanged:' .gobby/plans/task-25460.md | head -120"
    )
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(checkout),
        "project_path": str(checkout),
    }
    assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


def test_reported_grep_from_a_workspace_cwd_follows_its_leading_cd(
    root: Path, tmp_path: Path
) -> None:
    # The reported grep with its cd target an existing checkout, run from a workspace folder
    # that is not the seat's own. The pre-fix rule took the grep's relative file operand from
    # that cwd and refused it; the leading cd moves the operand to the checkout.
    variables = _seat(root / PROJECT, "developer", 12)
    definition = root / PROJECT / "plan-writer"
    definition.mkdir(parents=True)
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    command = (
        f"cd {checkout} && grep -n -E "
        r"'^#{1,4} |^`kind:|^\*\*(Plan ID|Granularity|Acceptance)|^Targets:|"
        r"^Consumers unchanged:' .gobby/plans/task-25460.md | head -120"
    )
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(definition),
        "project_path": str(checkout),
    }
    assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        # Bash and zsh fold `..` on the logical path: `hop/..` is the developer folder.
        ("cd {hop}/.. && cat session-99/notes.md", True),
        ("cd {hop}/..; cat session-99/notes.md", True),
        ("cd {hop}/..\ncat session-99/notes.md", True),
        ("cd {hop} && cd .. && cat session-99/notes.md", True),
        ("cd {foreign_link} && cat notes.md", True),
        # Bash falls back to the physical path when the logical one does not exist. The
        # link sits outside the workspaces root, so `deep_hop/../x` is a missing folder there,
        # and bash lands in own/inner/deep/x, four levels below the developer folder.
        ("cd {deep_hop}/../x && cat ../../../../session-99/notes.md", True),
        ("cd {deep_hop}/../x; cat ../../../../session-99/notes.md", True),
        # `set -P` (or zsh `chase_links`) makes cd physical even where the logical path exists.
        ("set -P; cd {phys_hop}/../x && cat ../../../../session-99/notes.md", True),
        # zsh `cd old new` swaps `old` for `new` in the current directory.
        ("cd 12 99; cat notes.md", True),
        ("cd 12 99 && cat notes.md", True),
        # A trailing `&` runs the whole and-or list in the background, so the shell stays put.
        ("cd inner && true &\ncat ../session-99/notes.md", True),
        ("cd inner && true & cat ../session-99/notes.md", True),
        ("cd inner && sleep 0 & wait; cat ../session-99/notes.md", True),
        ("cd inner && (true; true) &\ncat ../session-99/notes.md", True),
        # A newline after `&&`, `||` or `|` continues the list up to its `&`.
        ("cd inner &&\ntrue &\ncat ../session-99/notes.md", True),
        ("cd inner/deep &&\ncd .. & cat ../session-99/notes.md", True),
        ("cd inner &&\ntrue ||\ntrue & cat ../session-99/notes.md", True),
        ("cd {hop} &&\n(true) & cat ../session-99/notes.md", True),
        # A comment before the newline does not end the joined list.
        ("cd inner && # c\ntrue & cat ../session-99/notes.md", True),
        ("cd inner/deep && # c\ncd .. &\ncat ../session-99/notes.md", True),
        ("cd {hop} && # c\n(true) & cat ../session-99/notes.md", True),
        ("cd inner && true || # c; d\ntrue & cat ../session-99/notes.md", True),
        ("cd inner && true | # c\ncat & cat ../session-99/notes.md", True),
        # A quoted or escaped `#` is a word, so the join after it still continues the list.
        ('cd inner && echo "#" &&\ntrue & cat ../session-99/notes.md', True),
        ("cd inner && echo \\# &&\ntrue & cat ../session-99/notes.md", True),
        ('cd inner && echo "#x" |\ncat & cat ../session-99/notes.md', True),
        ('cd {hop} && echo "#" && (\ntrue\n) & cat ../session-99/notes.md', True),
        ("cd inner && # c )\ntrue & cat ../session-99/notes.md", True),
        # A `;` or newline inside a keyword compound or backticks does not end the list.
        ("cd inner && if true; then true; fi & cat ../session-99/notes.md", True),
        ("cd inner && if true\nthen true\nfi & cat ../session-99/notes.md", True),
        ("cd inner && while false\ndo :\ndone & cat ../session-99/notes.md", True),
        ("cd inner && echo `\ntrue` & cat ../session-99/notes.md", True),
        ("cd inner &&\ntrue\ncat ../notes.md", False),
        ("cd inner && true # c\ncat ../notes.md", False),
        ("cd inner && true; cat ../notes.md", False),
        # A redirection after the target is not a second operand.
        ("cd inner 2>/dev/null && cat notes.md", False),
        ("cd inner >/dev/null && cat notes.md", False),
        # A link to the seat's own folder stays readable.
        ("cd {own_link} && cat notes.md", False),
    ],
)
def test_symlinked_cd_reads_follow_the_shell_path(
    root: Path, tmp_path: Path, command: str, refused: bool
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    (own / "inner" / "deep" / "deeper").mkdir(parents=True)
    (own / "inner" / "deep" / "x").mkdir()
    foreign.mkdir()
    (foreign / "notes.md").write_text("held\n")
    (own / "notes.md").write_text("own\n")
    outside = tmp_path / "outside"
    (outside / "phys" / "x").mkdir(parents=True)
    links = {
        "hop": (own.parent / "hop", own / "inner"),
        "deep_hop": (outside / "deep_hop", own / "inner" / "deep" / "deeper"),
        "phys_hop": (outside / "phys" / "hop", own / "inner" / "deep" / "deeper"),
        "foreign_link": (own.parent / "foreign_link", foreign),
        "own_link": (own.parent / "own_link", own),
    }
    for link, target in links.values():
        link.symlink_to(target)
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": command.format(**{name: link for name, (link, _) in links.items()}),
        },
        "cwd": str(own),
        "project_path": str(checkout),
    }
    assert (
        agent_workspace_read_violation(None, data, data["tool_input"], variables, None) is refused
    )


@pytest.mark.parametrize("hook_cwd", ["definition", "root"])
def test_leading_cd_anchors_relative_reads_to_its_target(
    root: Path, tmp_path: Path, hook_cwd: str
) -> None:
    # The hook reports the shell cwd from before the command. A shell left inside the
    # workspace must still judge a relative operand against the checkout its `cd` enters.
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    cwd = own.parent if hook_cwd == "definition" else root
    command = (
        f"cd {checkout} && grep -n -E '^#{{1,4}} |^`kind:|^\\*\\*(Plan ID|Granularity)|^Targets:' "
        ".gobby/plans/task-25460.md | head -120"
    )
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
        "project_path": str(checkout),
    }
    assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    "command",
    [
        "cd {checkout} && cat {foreign}/notes.md",
        "cd {own} && cat ../session-99/notes.md",
        "cd {foreign} && cat notes.md",
        "cd {foreign} && sh -c 'cat notes.md'",
    ],
)
def test_leading_cd_still_refuses_foreign_workspace_reads(
    root: Path, tmp_path: Path, command: str
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    own.mkdir(parents=True)
    foreign.mkdir()
    checkout = tmp_path / "gobby"
    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": command.format(checkout=checkout, own=own, foreign=foreign),
        },
        "cwd": str(checkout),
        "project_path": str(checkout),
    }
    assert agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    "command",
    [
        "false || cd {own} && cat ../session-99/notes.md",
        "false || cd {own}; cat ../session-99/notes.md",
        "{{ cd {own}; cat ../session-99/notes.md; }}",
        "cat {own}/notes.md\ncd {own}\ncat ../session-99/notes.md",
        "if true; then cd {own}; cat ../session-99/notes.md; fi",
    ],
)
def test_cd_after_other_separators_still_refuses_foreign_reads(
    root: Path, tmp_path: Path, command: str
) -> None:
    # A cd reached through `||`, a brace group or a new line runs in the current shell, so the
    # operands after it resolve from its target, not from the hook cwd.
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    own.mkdir(parents=True)
    foreign.mkdir()
    checkout = tmp_path / "gobby"
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(own=own)},
        "cwd": str(checkout),
        "project_path": str(checkout),
    }
    assert agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    "command",
    [
        "false && cd {checkout}; cat ../session-99/notes.md",
        "cd {checkout} | cat ../session-99/notes.md",
        "{{ cd {checkout}; }} | cat ../session-99/notes.md",
        "FOREIGN={foreign}; cd $FOREIGN; cat notes.md",
    ],
)
def test_skipped_piped_or_dynamic_cd_keeps_foreign_reads_refused(
    root: Path, tmp_path: Path, command: str
) -> None:
    # Run from the own folder: a cd that may be skipped, runs in a pipeline subshell or has an
    # unknown target must not move the judged place off the own folder.
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    own.mkdir(parents=True)
    foreign.mkdir()
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(checkout=checkout, foreign=foreign)},
        "cwd": str(own),
        "project_path": str(checkout),
    }
    assert agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    "command",
    [
        "true & cd {checkout}; cat ../session-99/notes.md",
        "command cd {checkout}; cat ../session-99/notes.md",
        "builtin cd {checkout}; cat ../session-99/notes.md",
        "time cd {checkout}; cat ../session-99/notes.md",
        "echo x | cd {checkout}; cat ../session-99/notes.md",
        "{{ cd {checkout}; }} | cat ../session-99/notes.md",
        "cd {checkout} |& cat; cat ../session-99/notes.md",
        "pushd {checkout}; cat ../session-99/notes.md",
        "pushd {checkout}; popd; cat ../session-99/notes.md",
        "cd -P {checkout}; cat ../session-99/notes.md",
        "CDPATH={own_parent} cd session-99; cat notes.md",
        'eval "cd {checkout}"; cat ../session-99/notes.md',
        "source ./lib.sh; cat notes.md",
        "( cd {checkout} ); cat ../session-99/notes.md",
        "noglob eval 'cd ..'; cat session-99/notes.md",
        "noglob source ./lib.sh; cat session-99/notes.md",
        "nocorrect . ./lib.sh; cat session-99/notes.md",
        "- source ./lib.sh; cat session-99/notes.md",
        "if false; then true; cd {checkout}; fi; cat ../session-99/notes.md",
        "while false; do true; cd {checkout}; done; cat ../session-99/notes.md",
        "until true; do true; cd {checkout}; done; cat ../session-99/notes.md",
        "for f in a; do true; cd {checkout}; done; cat ../session-99/notes.md",
        "case x in x) true; cd {checkout};; esac; cat ../session-99/notes.md",
        "f() {{ true; cd {checkout}; }}; cat ../session-99/notes.md",
        'echo ";" cd /tmp; cat ../session-99/notes.md',
        "cd /tmp < /nonexistent-25578; cat ../session-99/notes.md",
    ],
)
def test_widened_cwd_forms_keep_foreign_reads_refused(
    root: Path, tmp_path: Path, command: str
) -> None:
    # Run from the own folder. A cd, pushd, popd or evaluator that is not narrowed adds its target
    # or an unknown place, and the own folder stays reachable, so a relative operand is judged from
    # every place the shell may be in and a foreign seat folder is refused.
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    foreign = own.parent / "session-99"
    own.mkdir(parents=True)
    foreign.mkdir()
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    data = {
        "tool_name": "Bash",
        "tool_input": {
            "command": command.format(checkout=checkout, own_parent=own.parent),
        },
        "cwd": str(own),
        "project_path": str(checkout),
    }
    assert agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        # Each mover runs after the leading cd narrowed the shell to the checkout.
        ("cd {checkout}; chdir {own}; cat ../session-99/notes.md", True),
        ("cd {checkout} && chdir {own} && cat ../session-99/notes.md", True),
        ("cd {checkout}; trap 'cd {own}' DEBUG; cat ../session-99/notes.md", True),
        # zsh AUTO_CD enters a directory named as a command.
        ("cd {checkout}; setopt autocd; {own}; cat ../session-99/notes.md", True),
        # An assignment prefix keeps the next word in command position.
        ('cd {checkout}; FOO=1 trap "cd {own}" DEBUG; cat ../session-99/notes.md', True),
        ('cd {checkout}; a[1]=x trap "cd {own}" DEBUG; cat ../session-99/notes.md', True),
        # A substituted, quoted or variable command name may be `cd`.
        ("cd {checkout}; `echo cd` {own}; cat ../session-99/notes.md", True),
        ("cd {checkout}; $'cd' {own}; cat ../session-99/notes.md", True),
        ("cd {checkout}; $'chdir' {own}; cat ../session-99/notes.md", True),
        ("cd {checkout}; c=cd; $c {own}; cat ../session-99/notes.md", True),
        # zsh `-J` is AUTO_CD.
        ("cd {checkout}; set -J; ln -s {own} lnk; lnk; cat ../session-99/notes.md", True),
        ("cd {checkout}; setopt -J; ln -s {own} lnk; lnk; cat ../session-99/notes.md", True),
        # A test bracket is a plain command and leaves the narrowed checkout alone.
        ("cd {checkout} && [ -d .gobby ] && grep -n x .gobby/plans/task-25460.md", False),
        # AUTO_CD enters a link made in the same command, which does not exist yet.
        ("cd {checkout}; setopt autocd; ln -s {own} lnk; lnk; cat ../session-99/notes.md", True),
        (
            "cd {checkout}; setopt AUTO_CD; mkdir d && ln -s {own} d/l; d/l; "
            "cat ../session-99/notes.md",
            True,
        ),
        # zsh `cd +N` rotates the directory stack.
        ("cd {checkout}; cd +1; cat ../session-99/notes.md", True),
        # Command words that name no directory leave the narrowed checkout alone.
        ("cd {checkout} && ls && grep -n x .gobby/plans/task-25460.md", False),
    ],
)
def test_movers_after_a_narrowing_cd_keep_foreign_reads_refused(
    root: Path, tmp_path: Path, command: str, refused: bool
) -> None:
    # Run from the own folder, where the bare relative read already reaches session-99.
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    (own.parent / "session-99").mkdir(parents=True)
    own.mkdir()
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(checkout=checkout, own=own)},
        "cwd": str(own),
        "project_path": str(checkout),
    }
    assert (
        agent_workspace_read_violation(None, data, data["tool_input"], variables, None) is refused
    )


def test_subshell_cd_leaves_own_folder_reads_allowed(root: Path, tmp_path: Path) -> None:
    # A `( cd ... )` subshell changes nothing for the shell, so a bare own-folder read after it
    # still resolves from the own folder.
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    (own / "notes.md").write_text("Own notes")
    checkout = tmp_path / "gobby"
    checkout.mkdir()
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": f"( cd {checkout} ); cat notes.md"},
        "cwd": str(own),
        "project_path": str(checkout),
    }
    assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.parametrize(
    "command",
    [
        "cd {own} && cat ./notes.md",
        "cd {own} && cat notes.md",
        "cd {own} && cat sub/notes.md",
        "false || cd {own}; cat ./notes.md",
        "{{ cd {own}; cat notes.md; }}",
        "cat {own}/notes.md\ncd {own}\ncat notes.md",
    ],
)
def test_leading_cd_into_own_folder_keeps_own_reads_allowed(
    root: Path, tmp_path: Path, command: str
) -> None:
    variables = _seat(root / PROJECT, "developer", 12)
    own = Path(variables["_agent_workspace_dir"])
    (own / "sub").mkdir(parents=True)
    (own / "notes.md").write_text("Own notes")
    (own / "sub" / "notes.md").write_text("Own nested notes")
    checkout = tmp_path / "gobby"
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": command.format(own=own)},
        "cwd": str(checkout),
        "project_path": str(checkout),
    }
    assert not agent_workspace_read_violation(None, data, data["tool_input"], variables, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["permission", "braces"])
async def test_unreadable_directory_and_brace_limit_are_distinct(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from gobby.workflows import workspace_read_targets as helpers

    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    own.mkdir(parents=True)
    for number in range(3):
        (own / f"notes-{number}.md").write_text("Fixture notes")
    with monkeypatch.context() as scope:
        if failure == "braces":
            scope.setattr(helpers, "_READ_EXPANSION_LIMIT", 2)
        else:

            def unavailable(path: Path) -> None:
                raise PermissionError("Cannot inspect pattern entries")

            scope.setattr(os, "scandir", unavailable)
        pattern = "{notes-0,notes-1}.md" if failure == "braces" else "*.md"
        response = await _decide(
            workspace_read_engine, _shell(f"cat {own / pattern}", tmp_path), variables
        )
    assert response.decision == ("allow" if failure == "permission" else "block")
    if failure == "braces":
        assert "fixture-read-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["own", "repository"])
@pytest.mark.parametrize(
    ("tool", "expression"),
    [
        ("Bash", "ls src/*.{py,md}"),
        ("Bash", "ls src/{a,b}.py"),
        ("Bash", "awk '/foo/{print}' README.md"),
        ("Bash", "sed -n '/^def /{p}' src/x.py"),
        ("Bash", "ls src/*/../tests"),
        ("Glob", "**/*.{ts,tsx}"),
        ("Glob", "**/*.{py,md}"),
    ],
)
async def test_safe_braces_and_parent_patterns_remain_readable(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    location: str,
    tool: str,
    expression: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    directory = (
        Path(variables["_agent_workspace_dir"]) if location == "own" else tmp_path / "repository"
    )
    (directory / "src" / "tests").mkdir(parents=True)
    (directory / "web").mkdir()
    for relative in ("src/a.py", "src/b.py", "src/a.md", "web/a.ts", "web/b.tsx"):
        (directory / relative).write_text("Fixture source")
    data = (
        _shell(expression, directory)
        if tool == "Bash"
        else {
            "tool_name": "Glob",
            "tool_input": {"path": str(directory), "pattern": expression},
            "cwd": str(directory),
        }
    )
    response = await _decide(workspace_read_engine, data, variables)
    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["log*/..", "logs/{..,other}", "logs/{.,other}{.,other}"])
async def test_read_expansion_cannot_enter_workspace_from_outside(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, prefix: str
) -> None:
    (root.parent / "logs").mkdir(parents=True)
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    target = root.parent / prefix / root.name / PROJECT / "fixture-worker/session-99/notes.md"
    response = await _decide(workspace_read_engine, _shell(f"cat {target}", tmp_path), variables)
    assert response.decision == "block"
    assert "fixture-read-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["different", "unavailable"])
async def test_case_alias_cannot_borrow_unproven_seat_ownership(
    workspace_write_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
    assert "fixture-write-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("ancestor", ["gobby", "root", "definition"])
@pytest.mark.parametrize("tool", ["read", "grep", "shell"])
async def test_case_alias_reads_preserve_workspace_isolation(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, ancestor: str, tool: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
            assert "fixture-read-scope" in response.reason


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
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    target = root.parent / pattern / PROJECT / "fixture-worker" / "session-99" / "notes.md"
    data = (
        _shell(f"cat {target}", project)
        if tool == "shell"
        else {"tool_name": "Glob", "tool_input": {"pattern": str(target)}, "cwd": str(project)}
    )
    response = await _decide(workspace_read_engine, data, variables)
    assert response.decision == "block", (target, response.reason)
    assert response.reason is not None
    assert "fixture-read-scope" in response.reason


@pytest.mark.asyncio
async def test_read_patterns_keep_an_unambiguous_owned_boundary(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
            assert "fixture-read-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["own", "epic"])
async def test_nested_task_worktree_plan_allowance_stays_bounded(
    workspace_write_engine: RuleEngine, root: Path, tmp_path: Path, folder: str
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
            assert "fixture-write-scope" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative", ["{..,.}/session-99/notes.md", "{../session-99,notes}/notes.md"]
)
async def test_brace_expansion_cannot_escape_owned_read_folder(
    workspace_read_engine: RuleEngine, root: Path, tmp_path: Path, relative: str
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    target = Path(variables["_agent_workspace_dir"]) / relative
    response = await _decide(workspace_read_engine, _shell(f"cat {target}", tmp_path), variables)
    assert response.decision == "block"
    assert "fixture-read-scope" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["different", "unavailable"])
async def test_read_alias_requires_proven_seat_identity(
    workspace_read_engine: RuleEngine,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
) -> None:
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
    assert "fixture-read-scope" in (response.reason or "")


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
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    worktree = Path(variables["_agent_workspace_dir"]) / "task-25298"
    targets = (
        [worktree / ".gobby/plans/runbook-devteam-addendum.md"]
        if shape == "plan"
        else [
            worktree / "src/gobby/install/shared" / name
            for name in (
                "workflows/agents/example-first.yaml",
                "workflows/agents/example-second.yaml",
                "workflows/agents/example-third.yaml",
                "workflows/agents/example-fourth.yaml",
                "workflows/agents/example-fifth.yaml",
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
    own = anchor / "fixture-helper" / "session-40"
    own.mkdir(parents=True)
    alias_root = root.with_name(root.name.upper())
    if not alias_root.exists():
        pytest.skip("Filesystem distinguishes workspace root casing")
    assert alias_root.samefile(root)
    variables = _seat(anchor, "fixture-helper", 40, persistent=True)
    caller = _register(
        temp_db,
        project_id,
        seq=40,
        status="active",
        agent_type="fixture-helper",
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
            agent_type="fixture-helper",
            folder=folder,
            persistent=True,
        )
        alias = alias_root / project_id / "FIXTURE-HELPER" / f"SESSION-{seq}" / "notes.md"
        response = await _decide(workspace_read_engine, _read(alias, tmp_path), variables, caller)
        assert response.decision == decision, (seq, response.reason)
        if decision == "block":
            assert "fixture-read-scope" in (response.reason or "")


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
    developer = _seat(anchor, "fixture-worker", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="fixture-worker",
        folder=anchor / "fixture-worker" / "session-50",
        persistent=False,
    )
    ended_earlier = anchor / "fixture-worker" / "session-10"
    _register(
        temp_db,
        project_id,
        seq=10,
        status="expired",
        agent_type="fixture-worker",
        folder=ended_earlier,
        persistent=False,
    )
    for allowed in (
        anchor / "fixture-worker" / "session-50" / "notes.md",
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
        anchor / "fixture-worker" / "session-51" / "notes.md",
        anchor / "fixture-coordinator" / "session-3" / "notes.md",
        anchor / "fixture-worker",
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
    assistant_folder = anchor / "fixture-helper" / "session-40"
    assistant = _seat(anchor, "fixture-helper", 40, persistent=True)
    successor = _register(
        temp_db,
        project_id,
        seq=40,
        status="active",
        agent_type="fixture-helper",
        folder=assistant_folder,
        persistent=True,
    )
    predecessor = anchor / "fixture-helper" / "session-30"
    _register(
        temp_db,
        project_id,
        seq=30,
        status="expired",
        agent_type="fixture-helper",
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
    for refused in (
        anchor / "fixture-helper",
        anchor / "fixture-coordinator" / "session-30" / "notes.md",
    ):
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
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
        agent_type="fixture-helper",
        folder=root / sample_project["id"] / "fixture-helper" / "session-7",
        persistent=True,
    )
    sessions = SessionManager(temp_db)
    for role in ("fixture-helper", "fixture-coordinator", "default"):
        AgentDefinitionManager(temp_db).create(
            role,
            {
                "name": role,
                "prompts": {"agent": "Maintain assigned session state."},
                "workflows": {"rule_selectors": {"include": []}},
                "capabilities": ["write_session_variables"],
            },
            project_id=sample_project["id"],
        )
        SessionVariableManager(temp_db).set_variable(caller, "_agent_type", role)
        for key in (
            "project_policy",
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
            persistent=definition != "fixture-worker",
        )

    caller = register("fixture-helper", 40, "active")
    register("fixture-helper", 30, "expired")
    register("fixture-helper", 35, "active")
    register("fixture-helper", 45, "expired")
    resumed = register("fixture-helper", 32, "active")
    register("fixture-helper", 28, "expired")
    # A live clear successor inherited the session-28 lineage folder.
    register("fixture-helper", 39, "active", bound=folder("fixture-helper", 28))
    register("fixture-coordinator", 20, "expired")

    def readable(path: Path, who: str = caller) -> bool:
        return predecessor_folder_readable(sessions, who, str(path))

    assert readable(folder("fixture-helper", 30)) is True
    assert readable(folder("fixture-helper", 35)) is False, "a live sibling is not a predecessor"
    assert readable(folder("fixture-helper", 45)) is False, "a later lineage is not a predecessor"
    assert readable(folder("fixture-helper", 32)) is False, "a resumed predecessor is live"
    assert readable(folder("fixture-helper", 28)) is False, "a live successor holds the lineage"
    assert readable(folder("fixture-coordinator", 20)) is False, "another definition's folder"
    assert readable(anchor / "fixture-helper") is False, "never the whole definition directory"
    assert readable(folder("fixture-helper", 30) / ".." / "session-35") is False

    sessions.update_status(resumed, "expired")
    assert readable(folder("fixture-helper", 32)) is True

    # A non-persistent seat reads no predecessor folder at all.
    developer = register("fixture-worker", 50, "active")
    register("fixture-worker", 10, "expired")
    assert readable(folder("fixture-worker", 10), developer) is False


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
    developer = _seat(anchor, "fixture-worker", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="fixture-worker",
        folder=anchor / "fixture-worker" / "session-50",
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
    developer = _seat(anchor, "fixture-worker", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="fixture-worker",
        folder=anchor / "fixture-worker" / "session-50",
        persistent=False,
    )
    # Concrete paths carrying real ids: a sibling session, and a folder of another project.
    sibling = anchor / "fixture-worker" / "session-51" / "notes.md"
    other_project = root / "22222222-2222-4222-8222-222222222222"
    foreign = other_project / "fixture-worker" / "session-9" / "notes.md"
    for refused in (sibling, foreign):
        for data in (
            _read(refused, project),
            _shell(f"ls {refused.parent}", project),
            _shell(f"cat {refused}", project),
        ):
            response = await _decide(engine, data, dict(developer), caller)
            assert response.decision == "block", (data, response.reason)
            assert "fixture-read-scope" in (response.reason or ""), data
    # A placeholder inside a commit message does not excuse a concrete read in the same command.
    data = _shell(f'git commit -m "Anchor: {TEMPLATE}" && cat {sibling}', project)
    response = await _decide(engine, data, dict(developer), caller)
    assert response.decision == "block", (data, response.reason)
    assert "fixture-read-scope" in (response.reason or ""), data


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
    assistant = _seat(anchor, "fixture-helper", 40, persistent=True)
    successor = _register(
        temp_db,
        project_id,
        seq=40,
        status="active",
        agent_type="fixture-helper",
        folder=anchor / "fixture-helper" / "session-40",
        persistent=True,
    )
    predecessor = anchor / "fixture-helper" / "session-30"
    _register(
        temp_db,
        project_id,
        seq=30,
        status="expired",
        agent_type="fixture-helper",
        folder=predecessor,
        persistent=True,
    )
    for allowed in (
        anchor / "fixture-helper" / "session-40" / "notes.md",
        predecessor / "lessons.md",
    ):
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
    developer = _seat(anchor, "fixture-worker", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="fixture-worker",
        folder=anchor / "fixture-worker" / "session-50",
        persistent=False,
    )
    # A shell wrapper runs its command string, so the paths inside that string are reads too.
    sibling = anchor / "fixture-worker" / "session-51" / "notes.md"
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
        assert "fixture-read-scope" in (response.reason or ""), command
    # The caller's own folder stays readable through a wrapper.
    own = anchor / "fixture-worker" / "session-50" / "notes.md"
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
    variables = _seat(root / PROJECT, "fixture-worker", 12)
    own = Path(variables["_agent_workspace_dir"])
    target = (
        own / ".gobby" / "project.json"
        if target_kind == "config"
        else root / PROJECT / "fixture-worker" / "session-99" / "notes.md"
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
    variables = _seat(root / PROJECT, "fixture-worker", 12)
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
    developer = _seat(anchor, "fixture-worker", 50)
    caller = _register(
        temp_db,
        project_id,
        seq=50,
        status="active",
        agent_type="fixture-worker",
        folder=anchor / "fixture-worker" / "session-50",
        persistent=False,
    )
    # A shell that reads a quoted heredoc body runs it, so the body is judged as shell code.
    # Literal heredoc data does not read the paths it describes.
    sibling = anchor / "fixture-worker" / "session-51" / "notes.md"
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
        assert "fixture-read-scope" in (response.reason or ""), command
    # The caller's own folder stays readable in the same positions.
    own = anchor / "fixture-worker" / "session-50" / "notes.md"
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
