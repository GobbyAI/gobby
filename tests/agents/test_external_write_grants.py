"""External grants are explicit, canonical, bounded and stable across resume."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents.external_write_grants import (
    GRANT_KEY,
    apply_write_grant,
    authorize_write_grant,
    canonical_write_roots,
    revalidate_write_grant,
)

pytestmark = pytest.mark.unit


async def test_rendered_policy_preserves_workspace_and_sensitive_denials(
    tmp_path: Path,
    authority: dict[str, Any],
) -> None:
    from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
    from gobby.agents.sandbox_policy import sensitive_write_roots
    from gobby.agents.srt_runtime import render_srt_settings

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    sibling = tmp_path / "external-other"
    for path in [workspace, external, sibling]:
        path.mkdir()
    grant = authorize_write_grant([str(external)], "authorized", **authority)
    config = apply_write_grant(SandboxConfig(enabled=True), grant)
    paths = await compute_sandbox_paths(config, str(workspace), 60887)
    filesystem = render_srt_settings(paths)["filesystem"]
    assert str(workspace.resolve()) in filesystem["allowWrite"]
    assert str(external.resolve()) in filesystem["allowWrite"]
    assert str(sibling.resolve()) not in filesystem["allowWrite"]
    assert set(sensitive_write_roots()).issubset(filesystem["denyWrite"])


@pytest.fixture
def authority() -> dict[str, Any]:
    sessions = MagicMock()
    sessions.get.return_value = SimpleNamespace(
        id="caller", agent_run_id=None, parent_session_id=None
    )
    runs = MagicMock()
    runs.get_by_session.return_value = None
    return {
        "caller_session_id": "caller",
        "parent_session_id": "parent",
        "session_manager": sessions,
        "run_storage": runs,
    }


@pytest.mark.parametrize(
    "paths,reason",
    [
        ("/tmp", "why"),
        ([None], "why"),
        (["relative"], "why"),
        (["/"], "why"),
        ([str(Path.home())], "why"),
        (["/definitely-missing-gobby-root"], "why"),
    ],
)
def test_invalid_root_inputs(paths: object, reason: object) -> None:
    with pytest.raises(ValueError):
        canonical_write_roots(paths, reason)


@pytest.mark.parametrize("reason", [None, "", "  ", 12])
def test_nonempty_roots_need_reason(tmp_path: Path, reason: object) -> None:
    with pytest.raises(ValueError, match="reason"):
        canonical_write_roots([str(tmp_path)], reason)


def test_file_is_not_a_directory(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(ValueError, match="directory"):
        canonical_write_roots([str(file)], "authorized")


def test_canonical_deduplication_and_audit(tmp_path: Path, authority: dict[str, Any]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    requested = [str(root), str(alias), str(root / ".." / "root")]
    grant = authorize_write_grant(requested, "  authorized task workspace  ", **authority)
    assert grant is not None
    assert grant["requested_roots"] == requested
    assert grant["canonical_roots"] == [str(root.resolve())]
    assert grant["reason"] == "authorized task workspace"
    assert grant["asserting_session_id"] == "caller"
    assert grant["parent_run_id"] is None
    assert grant["asserted_at"]
    assert revalidate_write_grant({GRANT_KEY: grant}) == [str(root.resolve())]


@pytest.mark.parametrize("relation", ["same", "parent", "child", "symlink"])
def test_protected_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relation: str) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    child = protected / "nested"
    child.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    monkeypatch.setattr(
        "gobby.agents.external_write_grants.sensitive_roots", lambda: [str(protected)]
    )
    requested = {"same": protected, "parent": tmp_path, "child": child, "symlink": alias}[relation]
    with pytest.raises(ValueError, match="protected"):
        canonical_write_roots([str(requested)], "authorized")


def test_child_narrows_recorded_grant_and_cannot_spoof_parent(
    tmp_path: Path, authority: dict[str, Any]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "nested"
    child.mkdir()
    sibling = tmp_path / "root-other"
    sibling.mkdir()
    grant = authorize_write_grant([str(root)], "authorized", **authority)
    authority["session_manager"].get.return_value.agent_run_id = "run"
    authority["run_storage"].get.return_value = SimpleNamespace(
        id="run", child_session_id="caller", resume_metadata_json={GRANT_KEY: grant}
    )
    narrowed = authorize_write_grant([str(child)], "delegate tests", **authority)
    assert narrowed is not None
    assert narrowed["canonical_roots"] == [str(child.resolve())]
    assert narrowed["parent_run_id"] == "run"
    for path in [sibling, tmp_path]:
        with pytest.raises(ValueError, match="only delegate"):
            authorize_write_grant([str(path)], "claimed authorization", **authority)
    authority["session_manager"].get.assert_called_with("caller")
    assert authorize_write_grant(None, None, **authority) is None
    assert authorize_write_grant([], None, **authority) is None


def test_child_without_grant_cannot_create_one(tmp_path: Path, authority: dict[str, Any]) -> None:
    authority["run_storage"].get_by_session.return_value = SimpleNamespace(
        id="run", child_session_id="caller", resume_metadata_json={}
    )
    with pytest.raises(ValueError, match="only delegate"):
        authorize_write_grant([str(tmp_path)], "claimed authorization", **authority)


@pytest.mark.parametrize("missing", ["session", "run", "relationship"])
def test_missing_or_inconsistent_identity_fails(
    tmp_path: Path, authority: dict[str, Any], missing: str
) -> None:
    if missing == "session":
        authority["session_manager"].get.return_value = None
    elif missing == "run":
        authority["session_manager"].get.return_value.agent_run_id = "lost"
        authority["run_storage"].get.return_value = None
    else:
        authority["run_storage"].get_by_session.return_value = SimpleNamespace(
            child_session_id="other"
        )
    with pytest.raises(ValueError):
        authorize_write_grant([str(tmp_path)], "authorized", **authority)


@pytest.mark.parametrize("change", ["retarget", "remove", "protected"])
def test_resume_rejects_changed_grants(
    tmp_path: Path, authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    grant = authorize_write_grant([str(alias)], "authorized", **authority)
    if change == "retarget":
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
    elif change == "remove":
        root.rmdir()
    else:
        monkeypatch.setattr(
            "gobby.agents.external_write_grants.sensitive_roots", lambda: [str(root)]
        )
    with pytest.raises(ValueError):
        revalidate_write_grant({GRANT_KEY: grant})


def test_no_grant_never_reads_session_or_inherits(authority: dict[str, Any]) -> None:
    assert authorize_write_grant(None, None, **authority) is None
    authority["session_manager"].get.assert_not_called()
    assert revalidate_write_grant({}) == []
