"""Tests for bundled, global, and project MCP template YAML sync."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.bundled import resolve_runtime_stdio_args
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID

pytestmark = pytest.mark.unit


def _write_plain(
    directory: Path,
    name: str,
    *,
    override: bool = False,
    runtime_hook: str | None = None,
    command_pkg: str | None = None,
    description: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    pkg = command_pkg or f"{name}-pkg"
    lines = [
        f"name: {name}",
        f"description: {description or f'Template {name}'}",
        "version: 1",
        "enabled: true",
        "transport: stdio",
        "command: npx",
        f'args: ["-y", "{pkg}"]',
    ]
    if runtime_hook:
        lines.append(f"runtime_hook: {runtime_hook}")
    if override:
        lines.append("override: true")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_secret_template(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    env = f"{name.upper().replace('-', '_')}_TOKEN"
    secret = f"{name.replace('-', '_')}_token"
    path = directory / f"{name}.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            name: {name}
            description: Template {name}
            version: 1
            enabled: true
            transport: stdio
            command: npx
            args: ["-y", "{name}-pkg"]
            params:
              - name: token
                env: {env}
                required: true
                secret: true
                default_secret: {secret}
            """
        ),
        encoding="utf-8",
    )
    return path


def test_sync_bundled_global_and_project_templates_with_override_guard(
    temp_db: Any, tmp_path: Path, sample_project: dict[str, Any]
) -> None:
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates
    from gobby.workflows.pipeline_loader import is_bundled_template

    manager = LocalMCPManager(temp_db)
    bundled = tmp_path / "bundled"
    _write_plain(bundled, "playwright")
    result = sync_bundled_mcp_templates(temp_db, bundled, tag="gobby")
    assert result["synced"] == 1
    row = manager.get_template("playwright", project_id=GLOBAL_PROJECT_ID)
    assert row is not None
    assert row.owner == "gobby"
    assert row.project_id == GLOBAL_PROJECT_ID
    assert is_bundled_template(row) is True

    global_user = tmp_path / "global-user"
    _write_plain(global_user, "playwright")
    conflicted = sync_bundled_mcp_templates(
        temp_db, [global_user], tag="user", project_id=None, project_root=tmp_path / "no-proj"
    )
    assert any("override: true" in error for error in conflicted["errors"])
    still = manager.get_template("playwright", project_id=GLOBAL_PROJECT_ID)
    assert still is not None
    assert still.owner == "gobby"

    _write_plain(global_user, "playwright", override=True, command_pkg="user-playwright")
    overridden = sync_bundled_mcp_templates(
        temp_db, [global_user], tag="user", project_id=None, project_root=tmp_path / "no-proj"
    )
    assert overridden["errors"] == []
    user_row = manager.get_template("playwright", project_id=GLOBAL_PROJECT_ID)
    assert user_row is not None
    assert user_row.owner == "user"

    project_root = tmp_path / "project-templates"
    _write_plain(project_root, "playwright", override=True)
    project_sync = sync_bundled_mcp_templates(
        temp_db,
        [project_root, tmp_path / "empty-global"],
        tag="user",
        project_id=sample_project["id"],
        project_root=project_root,
    )
    assert project_sync["errors"] == []
    assert project_sync["synced"] >= 1
    project_row = manager.get_template("playwright", project_id=sample_project["id"])
    assert project_row is not None
    assert project_row.project_id == sample_project["id"]
    assert project_row.owner == "user"


def test_sync_adopts_only_exact_legacy_bundled_rows(temp_db: Any, tmp_path: Path) -> None:
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    bundled = tmp_path / "bundled"
    _write_secret_template(bundled, "demo")
    _write_plain(bundled, "plain")
    _write_secret_template(bundled, "demo-extra-args")
    _write_secret_template(bundled, "demo-secret")
    sync_bundled_mcp_templates(temp_db, bundled, tag="gobby")

    exact = manager.upsert(
        name="demo",
        transport="stdio",
        command="npx",
        args=["-y", "demo-pkg"],
        env={"DEMO_TOKEN": "$secret:demo_token"},
        project_id=GLOBAL_PROJECT_ID,
        connect_timeout=30.0,
    )
    extra_env = manager.upsert(
        name="plain",
        transport="stdio",
        command="npx",
        args=["-y", "plain-pkg"],
        env={"EXTRA": "1"},
        project_id=GLOBAL_PROJECT_ID,
        connect_timeout=30.0,
    )
    extra_args = manager.upsert(
        name="demo-extra-args",
        transport="stdio",
        command="npx",
        args=["-y", "demo-pkg", "--flag"],
        env={"DEMO_EXTRA_ARGS_TOKEN": "$secret:demo_extra_args_token"},
        project_id=GLOBAL_PROJECT_ID,
        connect_timeout=30.0,
    )
    different_secret = manager.upsert(
        name="demo-secret",
        transport="stdio",
        command="npx",
        args=["-y", "demo-pkg"],
        env={"DEMO_SECRET_TOKEN": "$secret:other_token"},
        project_id=GLOBAL_PROJECT_ID,
        connect_timeout=30.0,
    )

    result = sync_bundled_mcp_templates(temp_db, bundled, tag="gobby")
    adopted = manager.get_server_by_id(exact.id)
    assert adopted is not None
    assert adopted.template_id is not None
    assert adopted.template_values is not None
    assert adopted.template_values["token"] == "$secret:demo_token"
    assert adopted.args == ["-y", "demo-pkg"]
    assert adopted.env == {"DEMO_TOKEN": "$secret:demo_token"}

    skipped_env = manager.get_server_by_id(extra_env.id)
    skipped_args = manager.get_server_by_id(extra_args.id)
    skipped_secret = manager.get_server_by_id(different_secret.id)
    assert skipped_env is not None and skipped_env.template_id is None
    assert skipped_args is not None and skipped_args.template_id is None
    assert skipped_secret is not None and skipped_secret.template_id is None
    assert result["adoption_skipped"]["plain"] == "env"
    assert result["adoption_skipped"]["demo-extra-args"] == "args"
    assert result["adoption_skipped"]["demo-secret"] == "env"


def test_removed_template_file_prunes_row_and_restores_bundled_definition(
    temp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.cli.installers.shared import _sync_user_templates_to_db
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    bundled = tmp_path / "bundled"
    _write_plain(bundled, "github")
    sync_bundled_mcp_templates(temp_db, bundled, tag="gobby")
    original = manager.get_template("github", project_id=GLOBAL_PROJECT_ID)
    assert original is not None
    original_hash = original.definition_hash

    global_templates = tmp_path / "user-global-templates"
    _write_plain(
        global_templates,
        "github",
        override=True,
        command_pkg="user-github",
        description="User override",
    )
    project_templates = tmp_path / "user-project-templates"
    project_templates.mkdir()
    empty = tmp_path / "empty"
    monkeypatch.setattr("gobby.mcp_proxy.templates.get_bundled_templates_path", lambda: bundled)
    monkeypatch.setattr(
        "gobby.mcp_proxy.sync_templates.get_bundled_templates_path", lambda: bundled
    )
    monkeypatch.setattr(
        "gobby.paths.get_project_mcp_templates_dir", lambda _path: project_templates
    )
    monkeypatch.setattr("gobby.paths.get_global_mcp_templates_dir", lambda: global_templates)
    monkeypatch.setattr("gobby.paths.get_project_mcp_servers_dir", lambda _path: empty)
    monkeypatch.setattr("gobby.paths.get_global_mcp_servers_dir", lambda: empty)
    monkeypatch.setattr("gobby.paths.get_project_rules_dir", lambda _path: empty)
    monkeypatch.setattr("gobby.paths.get_global_rules_dir", lambda: empty)
    monkeypatch.setattr("gobby.paths.get_project_variables_dir", lambda _path: empty)
    monkeypatch.setattr("gobby.paths.get_global_variables_dir", lambda: empty)
    monkeypatch.setattr("gobby.utils.project_context.get_project_context", lambda cwd=None: None)

    _sync_user_templates_to_db(temp_db)
    overridden = manager.get_template("github", project_id=GLOBAL_PROJECT_ID)
    assert overridden is not None
    assert overridden.owner == "user"
    assert overridden.definition["description"] == "User override"

    (global_templates / "github.yaml").unlink()
    _sync_user_templates_to_db(temp_db)
    restored = manager.get_template("github", project_id=GLOBAL_PROJECT_ID)
    assert restored is not None
    assert restored.owner == "gobby"
    assert restored.definition_hash == original_hash
    assert restored.definition["args"] == ["-y", "github-pkg"]


def test_last_template_deletion_prunes_and_missing_root_does_not(
    temp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_project: dict[str, Any]
) -> None:
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    global_root = tmp_path / "global-templates"
    project_root = tmp_path / "project-templates"
    _write_plain(global_root, "keep-global")
    _write_plain(project_root, "keep-project")
    sync_bundled_mcp_templates(
        temp_db,
        [project_root, global_root],
        tag="user",
        project_id=sample_project["id"],
        project_root=project_root,
    )

    (global_root / "keep-global.yaml").unlink()
    missing_project = tmp_path / "does-not-exist"
    result = sync_bundled_mcp_templates(
        temp_db,
        [missing_project, global_root],
        tag="user",
        project_id=sample_project["id"],
        project_root=missing_project,
    )
    assert result["orphaned"] == 1
    assert manager.get_template("keep-global", project_id=GLOBAL_PROJECT_ID) is None
    project_row = manager.get_template("keep-project", project_id=sample_project["id"])
    assert project_row is not None
    assert project_row.project_id == sample_project["id"]

    _write_plain(global_root, "keep-global")
    unreadable = tmp_path / "unreadable"
    _write_plain(unreadable, "doomed")
    sync_bundled_mcp_templates(
        temp_db, [unreadable], tag="user", project_id=None, project_root=tmp_path / "no-project"
    )
    (unreadable / "doomed.yaml").unlink()
    real_rglob = Path.rglob

    def _rglob(self: Path, pattern: str) -> Any:
        if self.resolve() == unreadable.resolve():
            raise PermissionError("unreadable root")
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", _rglob)
    blocked = sync_bundled_mcp_templates(
        temp_db,
        [unreadable, global_root],
        tag="user",
        project_id=None,
        project_root=tmp_path / "no-project",
    )
    assert blocked["orphaned"] == 0
    assert manager.get_template("doomed", project_id=GLOBAL_PROJECT_ID) is not None
    assert blocked["errors"]


def test_detached_instance_keeps_runtime_hook_after_reconnect(temp_db: Any, tmp_path: Path) -> None:
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    bundled = tmp_path / "bundled"
    _write_plain(bundled, "chrome-devtools", runtime_hook="chrome_executable_path")
    sync_bundled_mcp_templates(temp_db, bundled, tag="gobby")
    template = manager.get_template("chrome-devtools", project_id=GLOBAL_PROJECT_ID)
    assert template is not None
    instance = manager.upsert(
        name="chrome-devtools",
        transport="stdio",
        command="npx",
        args=["-y", "chrome-devtools-pkg"],
        project_id=GLOBAL_PROJECT_ID,
        template_id=template.id,
        runtime_hook="chrome_executable_path",
    )
    (bundled / "chrome-devtools.yaml").unlink()
    result = sync_bundled_mcp_templates(temp_db, bundled, tag="gobby")
    assert result["orphaned"] == 1
    detached = manager.get_server_by_id(instance.id)
    assert detached is not None
    assert detached.template_id is None
    assert detached.runtime_hook == "chrome_executable_path"
    with patch(
        "gobby.mcp_proxy.bundled.resolve_chrome_devtools_executable_path",
        return_value="/tmp/chrome-bin",
    ):
        hooked_args = resolve_runtime_stdio_args(detached.runtime_hook, detached.args)
    assert "--executable-path=/tmp/chrome-bin" in hooked_args
