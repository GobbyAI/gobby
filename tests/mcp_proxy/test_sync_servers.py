"""Tests for MCP instance YAML sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _write_template(directory: Path, name: str, *, enabled: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    enabled_line = "true" if enabled else "false"
    path.write_text(
        "\n".join(
            [
                f"name: {name}",
                f"description: Template {name}",
                "version: 1",
                f"enabled: {enabled_line}",
                "transport: stdio",
                "command: npx",
                f'args: ["-y", "{name}-pkg"]',
                "params:",
                "  - name: token",
                f"    env: {name.upper()}_TOKEN",
                "    required: true",
                "    secret: true",
                f"    default_secret: {name}_token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_instance(
    directory: Path,
    filename: str,
    *,
    template: str,
    name: str | None = None,
    token: str = "$secret:demo_token",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    lines = [f"template: {template}", "enabled: true"]
    if name is not None:
        lines.insert(0, f"name: {name}")
    lines.extend(["values:", f"  token: {token}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_instance_yaml_syncs_scoped_rows_with_affected_ids_and_needs_configuration(
    temp_db: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    sample_project: dict[str, Any],
) -> None:
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    templates = tmp_path / "templates"
    _write_template(templates, "demo")
    sync_bundled_mcp_templates(temp_db, templates, tag="gobby")

    global_servers = tmp_path / "global-servers"
    project_servers = tmp_path / "project-servers"
    _write_instance(global_servers, "global-demo.yaml", template="demo", name="global-demo")
    _write_instance(project_servers, "project-demo.yaml", template="demo", name="project-demo")

    with caplog.at_level("INFO"):
        result = sync_mcp_server_files(
            temp_db,
            [project_servers, global_servers],
            project_id=sample_project["id"],
            project_root=project_servers,
            secret_store=SecretStore(temp_db),
        )

    assert result["errors"] == []
    assert result["synced"] == 2
    assert len(result["affected_ids"]) == 2
    assert result["needs_configuration"]["global-demo"] == ["demo_token"]
    assert result["needs_configuration"]["project-demo"] == ["demo_token"]
    assert "gobby secrets set demo_token --global" in caplog.text
    assert "gobby secrets set demo_token" in caplog.text

    global_row = manager.get_server("global-demo", project_id=GLOBAL_PROJECT_ID)
    project_row = manager.get_server("project-demo", project_id=sample_project["id"])
    assert global_row is not None
    assert project_row is not None
    assert global_row.template == "demo"
    assert project_row.project_id == sample_project["id"]
    assert {global_row.id, project_row.id} == set(result["affected_ids"])


def test_removed_instance_file_does_not_delete_row(temp_db: Any, tmp_path: Path) -> None:
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    templates = tmp_path / "templates"
    _write_template(templates, "demo")
    sync_bundled_mcp_templates(temp_db, templates, tag="gobby")
    servers = tmp_path / "servers"
    _write_instance(servers, "keep.yaml", template="demo", name="keep")
    first = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=tmp_path / "no-project",
        secret_store=SecretStore(temp_db),
    )
    (servers / "keep.yaml").unlink()
    second = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=tmp_path / "no-project",
        secret_store=SecretStore(temp_db),
    )
    row = manager.get_server("keep", project_id=GLOBAL_PROJECT_ID)
    assert row is not None
    assert row.enabled is True
    assert row.id == first["affected_ids"][0]
    assert second["affected_ids"] == []


def test_disabled_template_blocks_instance_sync(temp_db: Any, tmp_path: Path) -> None:
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    templates = tmp_path / "templates"
    _write_template(templates, "demo", enabled=False)
    sync_bundled_mcp_templates(temp_db, templates, tag="gobby")
    servers = tmp_path / "servers"
    _write_instance(servers, "blocked.yaml", template="demo", name="blocked")
    result = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=tmp_path / "no-project",
        secret_store=SecretStore(temp_db),
    )
    assert result["synced"] == 0
    assert any("template_disabled" in error for error in result["errors"])
    assert any("demo" in error for error in result["errors"])
    assert manager.get_server("blocked", project_id=GLOBAL_PROJECT_ID) is None


def test_instance_name_and_template_name_are_independent(temp_db: Any, tmp_path: Path) -> None:
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    templates = tmp_path / "templates"
    _write_template(templates, "openapi")
    sync_bundled_mcp_templates(temp_db, templates, tag="gobby")
    servers = tmp_path / "servers"
    _write_instance(servers, "fancy.yaml", template="openapi", name="storefront")
    result = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=tmp_path / "no-project",
        secret_store=SecretStore(temp_db),
    )
    assert result["synced"] == 1
    row = manager.get_server("storefront", project_id=GLOBAL_PROJECT_ID)
    assert row is not None
    assert row.name == "storefront"
    assert row.template == "openapi"
    assert manager.get_server("fancy", project_id=GLOBAL_PROJECT_ID) is None
    assert manager.get_server("openapi", project_id=GLOBAL_PROJECT_ID) is None
