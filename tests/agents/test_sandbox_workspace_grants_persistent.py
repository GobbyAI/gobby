"""A persistent owner's resume grants its stable live folder, keeping history read-only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import resume_executor
from gobby.agents.agent_workspaces import session_workspace_dir
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.srt_settings import render_srt_settings
from gobby.agents.workspace_bindings import bind_session_workspace
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.test_resume_executor import (
    _original_run,
    _patch_common,
    _resume_metadata,
    _runner,
)
from tests.agents.test_sandbox_workspace_grants import _assert_only_own_folders, _covered, _seq
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, make_team_world

pytestmark = pytest.mark.unit

ARCHIVIST = "fixture-definition-599e0437"


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    """A project with real session rows; Gobby home sits under the test's HOME."""
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    home = (tmp_path / "home").resolve()
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    return world


def _persistent_seat(db: HubDatabase, session_id: str) -> None:
    """Bind the session to the persistent fixture definition."""
    SessionVariableManager(db).merge_variables(session_id, {"_agent_type": ARCHIVIST})


@pytest.mark.asyncio
async def test_resume_of_a_new_owner_grants_its_stable_folder(
    world: TeamWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed persistent owner receives only its stable live folder as a write grant."""
    db = world.case.db
    project = world.case.project_id
    AgentDefinitionManager(db).upsert_with_steps(
        ARCHIVIST,
        {
            "name": ARCHIVIST,
            "surfaces": ["persona", "spawn"],
            "prompts": {"persona": "Seat.", "agent": "Work."},
            "persistent_workspace": True,
            "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        },
        {"steps": [{"name": "work"}]},
        source="custom",
    )
    predecessor = world.case.session(ARCHIVIST)
    successor = world.case.session(ARCHIVIST)
    _persistent_seat(db, predecessor)
    _persistent_seat(db, successor)
    first = bind_session_workspace(db, predecessor)
    SessionManager(db).update_status_if_non_terminal(predecessor, "expired")
    inherited = bind_session_workspace(db, successor)
    own = session_workspace_dir(project, ARCHIVIST, _seq(db, successor))
    # The premise: the successor binds the lineage's folder, not one for its own number.
    assert first is not None
    assert inherited is not None
    assert inherited == first
    assert inherited != own
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _patch_common(monkeypatch, spawner=MagicMock(), finalize=AsyncMock())
    spawn_context = SimpleNamespace(session_id=successor, env_vars={"GOBBY_SESSION_ID": successor})
    monkeypatch.setattr(
        resume_executor, "prepare_terminal_resume", MagicMock(return_value=spawn_context)
    )
    prepare = AsyncMock(side_effect=OSError("stop after the replayed config"))
    monkeypatch.setattr(resume_executor, "prepare_sandbox_launch", prepare)
    metadata = _resume_metadata()
    metadata["cwd"] = str(workspace)
    metadata["agent_slug"] = ARCHIVIST
    metadata["sandbox_config"] = SandboxConfig(enabled=True, backend="srt").model_dump(mode="json")
    runner = _runner(storage=MagicMock())
    runner.run_storage.db = db

    with patch("gobby.agents.sandbox_gate.verify_srt_installation_for_launch"):
        await resume_executor.resume_agent_run(
            _original_run(),
            resume_metadata=metadata,
            runner=runner,
            session_manager=MagicMock(),
        )

    assert prepare.await_args is not None
    config = SandboxConfig.model_validate(prepare.await_args.kwargs["config"])
    assert config.extra_write_paths.count(str(inherited)) == 1
    paths = await compute_sandbox_paths(
        config, str(workspace), provider="codex", env=spawn_context.env_vars
    )
    filesystem = render_srt_settings(paths)["filesystem"]
    assert _covered(filesystem["allowWrite"], inherited)
    assert not _covered(filesystem["allowWrite"], own)
    _assert_only_own_folders(filesystem, paths, {inherited})
