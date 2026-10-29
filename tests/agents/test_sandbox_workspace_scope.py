"""Workspace read isolation is enforced beneath shell/path parsing."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.agents.agent_workspaces import grant_seat_workspace_paths
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat._session_launch import (
    SessionLaunchContext,
    start_hydrated_session,
)
from gobby.servers.websocket.chat.runtime_manager import SandboxPolicySnapshot
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_unbound_sandbox_denies_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    root = home / "agents_workspaces"
    root.mkdir(parents=True)
    (tmp_path / "checkout").mkdir()
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt"), str(tmp_path / "checkout")
    )
    assert str(root.resolve()) in paths.deny_read_paths
    assert not any(root.resolve().is_relative_to(Path(path)) for path in paths.read_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["root", "foreign", "ancestor", "alias", "case"])
async def test_config_read_grant_cannot_override_workspace_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grant: str
) -> None:
    home = tmp_path / "gobby"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    root = home / "agents_workspaces"
    foreign = root / "project" / "other" / "session-1"
    foreign.mkdir(parents=True)
    (tmp_path / "checkout").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(foreign, target_is_directory=True)
    requested = {
        "root": root,
        "foreign": foreign,
        "ancestor": home,
        "alias": alias,
        "case": home / "AGENTS_WORKSPACES" / "project" / "other" / "session-1",
    }[grant]
    if grant == "ancestor":
        with pytest.raises(ValueError, match="contains sensitive root"):
            await compute_sandbox_paths(
                SandboxConfig(enabled=True, backend="srt", extra_read_paths=[str(requested)]),
                str(tmp_path / "checkout"),
            )
        return
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", extra_read_paths=[str(requested)]),
        str(tmp_path / "checkout"),
    )
    assert str(root.resolve()) in paths.deny_read_paths
    assert not any(foreign.resolve().is_relative_to(Path(path)) for path in paths.read_paths)
    if grant == "case":
        assert str(requested) not in paths.read_paths


def _definition(db: HubDatabase, *, persistent: bool, read_all: bool = False) -> None:
    AgentDefinitionManager(db).upsert_with_steps(
        "custom-seat",
        {
            "name": "custom-seat",
            "prompts": {"agent": "Work."},
            "persistent_workspace": persistent,
            "read_all_agent_workspaces": read_all,
            "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        },
        {"steps": [{"name": "work"}]},
        source="custom",
    )


def _session(db: HubDatabase, project: str, seq: int, folder: Path | None = None) -> str:
    session_id = str(uuid4())
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "status, agent_depth, seq_num) VALUES (%s, %s, %s, 'test', %s, 'active', 0, %s)",
        (session_id, str(uuid4()), require_machine_id(), project, seq),
    )
    if folder is not None:
        SessionVariableManager(db).merge_variables(
            session_id, {"_agent_workspace_dir": str(folder)}
        )
    return session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_seat_scope_uses_definition_and_preserves_predecessor_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    persistent: bool,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby"))
    root = tmp_path / "gobby" / "agents_workspaces"
    project: str = sample_project["id"]
    anchor = root / project
    own = anchor / "custom-seat" / "session-40"
    prior = own.parent / "session-10"
    live = own.parent / "session-20"
    future = own.parent / "session-50"
    foreign = anchor / "other-seat" / "session-5"
    for folder in (own, prior, live, future, foreign):
        folder.mkdir(parents=True)
    (anchor / "README.md").write_text("Shared guidance")
    _definition(temp_db, persistent=persistent)
    caller = _session(temp_db, project, 40)
    _session(temp_db, project, 30, live)  # A live clear successor holds the old folder.
    config = grant_seat_workspace_paths(
        SandboxConfig(enabled=True, backend="srt"), temp_db, caller, "custom-seat"
    )
    paths = await compute_sandbox_paths(config, str(own))
    assert str(own) in paths.read_paths
    assert str(own) in paths.write_paths
    assert str(anchor / "README.md") in paths.read_paths
    assert (str(prior) in paths.read_paths) is persistent
    assert not {str(live), str(future), str(foreign)}.intersection(paths.read_paths)
    assert str(root) in paths.deny_read_paths


@pytest.mark.asyncio
async def test_managed_persona_scope_is_available_before_backend_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby"))
    root = tmp_path / "gobby" / "agents_workspaces"
    root.mkdir(parents=True)
    _definition(temp_db, persistent=False, read_all=True)
    caller = _session(temp_db, sample_project["id"], 10)
    observed: list[str] = []

    async def start(model: str | None = None) -> None:
        assert model is None
        assert session.sandbox_config is not None
        paths = await compute_sandbox_paths(session.sandbox_config, str(tmp_path))
        observed.extend(paths.read_paths)

    session = cast(
        ChatSessionProtocol,
        SimpleNamespace(
            start=start,
            db_session_id=caller,
            resume_session_id=None,
            sandbox_metadata={},
            model=None,
        ),
    )
    owner = SimpleNamespace(_chat_sessions={}, clients={})
    manager = SimpleNamespace(db=temp_db, update=MagicMock(return_value=None))
    await start_hydrated_session(
        owner,
        session,
        SessionLaunchContext(
            sandbox=SandboxPolicySnapshot(
                config=SandboxConfig(enabled=True, backend="srt"),
                policy_hash="proof",
            ),
            workspace_path=str(tmp_path),
        ),
        session_key="proof",
        effective_model=None,
        persona_selected=True,
        pending_agent=None,
        pending_mode="plan",
        agent_name="custom-seat",
        provider_name="agy",
        session_manager=manager,
        existing_db_session=None,
        project_context_changed=False,
        effective_pid=sample_project["id"],
    )
    assert str(root) in observed
    assert owner._chat_sessions["proof"] is session


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before-grant", "before-render"])
async def test_symlinked_own_binding_cannot_authorize_foreign_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    when: str,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby"))
    anchor = tmp_path / "gobby" / "agents_workspaces" / sample_project["id"]
    own = anchor / "custom-seat" / "session-10"
    foreign = anchor / "other-seat" / "session-20"
    own.parent.mkdir(parents=True)
    foreign.mkdir(parents=True)
    _definition(temp_db, persistent=False)
    caller = _session(temp_db, sample_project["id"], 10)
    config = SandboxConfig(enabled=True, backend="srt")
    if when == "before-render":
        config = grant_seat_workspace_paths(config, temp_db, caller, "custom-seat")
    own.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match="scope follows an alias"):
        if when == "before-grant":
            grant_seat_workspace_paths(config, temp_db, caller, "custom-seat")
        else:
            await compute_sandbox_paths(config, str(foreign))


@pytest.mark.asyncio
async def test_read_all_is_definition_authority_and_does_not_survive_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby"))
    root = tmp_path / "gobby" / "agents_workspaces"
    root.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _definition(temp_db, persistent=False, read_all=True)
    caller = _session(temp_db, sample_project["id"], 10)
    config = grant_seat_workspace_paths(
        SandboxConfig(enabled=True, backend="srt"), temp_db, caller, "custom-seat"
    )
    paths = await compute_sandbox_paths(config, str(checkout))
    assert str(root) in paths.read_paths
    restored = SandboxConfig.model_validate(config.model_dump())
    _definition(temp_db, persistent=False, read_all=False)
    refreshed = grant_seat_workspace_paths(restored, temp_db, caller, "custom-seat")
    paths = await compute_sandbox_paths(refreshed, str(checkout))
    assert str(root) not in paths.read_paths
    assert str(root) in paths.deny_read_paths
