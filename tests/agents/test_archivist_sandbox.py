"""Archivist digest capability across spawn policy and resume metadata."""

from fnmatch import fnmatchcase
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.sandbox_gate import resolve_resume_sandbox
from gobby.agents.spawn_executor_providers import _prepare_provider_sandbox
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.srt_runtime import SandboxLaunch, render_srt_settings
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", ["archivist", "developer", "log-monitor", None])
async def test_spawn_never_grants_desktop_digests(
    agent_name: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SandboxConfig(enabled=True, backend="srt")
    metadata: dict[str, object] = {}
    context = prepared_spawn(env_vars={"GOBBY_SESSION_ID": "digest-policy-test", "PATH": ""})
    request = SpawnRequest(
        prompt="Maintain digest",
        cwd=str(workspace),
        provider="claude",
        session_id=context.session_id,
        run_id=context.agent_run_id,
        parent_session_id="parent",
        project_id="project",
        agent_name=agent_name,
        sandbox_config=config,
        resume_metadata_json=metadata,
        prepared_spawn=context,
    )
    launch = SandboxLaunch(backend="srt", enforced=True)
    with (
        patch(
            "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
            AsyncMock(return_value=launch),
        ) as prepare,
        patch("gobby.agents.spawn_executor_providers._record_actual_sandbox_enforcement"),
    ):
        result = await _prepare_provider_sandbox(request, context, "claude", context.env_vars)

    assert result is launch
    assert prepare.await_args is not None
    resolved = SandboxConfig.model_validate(prepare.await_args.kwargs["config"])
    paths = await compute_sandbox_paths(
        resolved, str(workspace), provider="claude", env=context.env_vars
    )
    writes = render_srt_settings(paths)["filesystem"]["allowWrite"]
    digest_glob = str(home / "Desktop/gobby-digest-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md")
    assert digest_glob not in writes
    assert str(home / "Desktop") not in writes
    for filename in (
        "gobby-digest-2026-10-10.md",
        "gobby-digest-2026-10-11.md",
        "gobby-digest-2026-10-10.md.tmp.1.d4c9fe167b3e",
        "gobby-digest-2026-10-10.md.tmp.29592.7e53ef56480f",
        "gobby-digest-2026-10-11.md.tmp.2147483647.012345abcdef",
    ):
        path = str(home / "Desktop" / filename)
        assert not any(fnmatchcase(path, pattern) for pattern in writes)
    for filename in (
        "notes.md",
        "gobby-digest.md",
        "gobby-digest-2026-1-10.md",
        "gobby-digest-2026-١٠-10.md",
        "gobby-digest-2026-10-10.md.bak",
        "gobby-digest-2026-10-10.md/child",
        "gobby-digest-2026-10-10.md.tmp.29592.d4c9fe167b3e/child",
        "gobby-digest-2026-10-10.md.tmp..d4c9fe167b3e",
        "gobby-digest-2026-10-10.md.tmp.1x.d4c9fe167b3e",
        "gobby-digest-2026-10-10.md.tmp.12345678901.d4c9fe167b3e",
        "gobby-digest-2026-10-10.md.tmp.29592.",
        "gobby-digest-2026-10-10.md.tmp.29592.d4c9fe167b3",
        "gobby-digest-2026-10-10.md.tmp.29592.d4c9fe167b3ef",
        "gobby-digest-2026-10-10.md.tmp.29592.d4c9fe167b3g",
        "gobby-digest-2026-10-10.md.tmp.29592.D4C9FE167B3E",
        "gobby-digest-2026-10-10.md.tmp.29592.d4c9fe167b3e.bak",
    ):
        path = str(home / "Desktop" / filename)
        assert not any(fnmatchcase(path, pattern) for pattern in writes)
    with patch("gobby.agents.sandbox_gate.verify_srt_installation") as verify_installation:
        persisted = resolve_resume_sandbox(metadata)
    verify_installation.assert_called_once_with()
    assert persisted.extra_write_paths == resolved.extra_write_paths
    assert config.extra_write_paths == []
