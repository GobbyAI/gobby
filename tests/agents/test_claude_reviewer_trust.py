import json
from pathlib import Path

import pytest

from gobby.agents.claude_sandbox_home import prepare_claude_reviewer_home


@pytest.mark.parametrize("auto_approve", [False, True])
@pytest.mark.parametrize("aliased", [False, True])
def test_reviewer_private_home_seeds_only_authorized_workspace_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    auto_approve: bool,
    aliased: bool,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launch_path = workspace
    if aliased:
        launch_path = tmp_path / "workspace-alias"
        launch_path.symlink_to(workspace, target_is_directory=True)
    host_global = home / ".claude.json"
    host_global.write_text(
        json.dumps({"projects": {str(tmp_path): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    host_before = host_global.read_bytes()

    result = prepare_claude_reviewer_home(
        tmp_path / "cache", {}, workspace=str(launch_path), auto_approve=auto_approve
    )

    private = Path(result.environment["CLAUDE_CONFIG_DIR"])
    config = json.loads((private / ".claude.json").read_text(encoding="utf-8"))
    projects = config.get("projects", {})
    if auto_approve:
        # Claude checks persisted project acceptance, not transcript-directory existence.
        assert projects.get(str(workspace.resolve()), {}).get("hasTrustDialogAccepted") is True
        assert projects.get(str(launch_path), {}).get("hasTrustDialogAccepted") is True
        assert set(projects) == {str(launch_path), str(workspace.resolve())}
    else:
        assert projects == {}
    assert str(private / "settings.json") in result.protected_write_paths
    assert host_global.read_bytes() == host_before
