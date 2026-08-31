"""Tests for MCP instance YAML sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.mcp_proxy.templates import MCPServerTemplate, get_bundled_templates_path
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _write_template(
    directory: Path,
    name: str,
    *,
    enabled: bool = True,
    runtime_hook: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    enabled_line = "true" if enabled else "false"
    hook_lines = [f"runtime_hook: {runtime_hook}"] if runtime_hook else []
    path.write_text(
        "\n".join(
            [
                f"name: {name}",
                f"description: Template {name}",
                "version: 1",
                f"enabled: {enabled_line}",
                *hook_lines,
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
    assert first["errors"] == []
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


def test_repointing_instance_at_hookless_template_clears_runtime_hook(
    temp_db: Any, tmp_path: Path
) -> None:
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    templates = tmp_path / "templates"
    _write_template(templates, "hooked", runtime_hook="chrome_executable_path")
    _write_template(templates, "hookless")
    sync_bundled_mcp_templates(temp_db, templates, tag="gobby")

    servers = tmp_path / "servers"
    _write_instance(servers, "browser.yaml", template="hooked", name="browser")
    result = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=None,
        secret_store=SecretStore(temp_db),
    )
    assert result["errors"] == []
    row = manager.get_server("browser", project_id=GLOBAL_PROJECT_ID)
    assert row is not None
    assert row.template == "hooked"
    assert row.runtime_hook == "chrome_executable_path"

    _write_instance(servers, "browser.yaml", template="hookless", name="browser")
    result = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=None,
        secret_store=SecretStore(temp_db),
    )
    assert result["errors"] == []
    assert result["updated"] == 1
    repointed = manager.get_server("browser", project_id=GLOBAL_PROJECT_ID)
    assert repointed is not None
    assert repointed.id == row.id
    assert repointed.template == "hookless"
    assert repointed.runtime_hook is None


def test_yaml_bool_values_expand_lower_case_for_choices(temp_db: Any, tmp_path: Path) -> None:
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "boolean.yaml").write_text(
        "\n".join(
            [
                "name: boolean",
                "description: Template boolean",
                "version: 1",
                "enabled: true",
                "transport: stdio",
                "command: npx",
                'args: ["-y", "boolean-pkg"]',
                "params:",
                "  - name: insecure",
                "    env: INSECURE",
                "    required: true",
                '    choices: ["true", "false"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sync_bundled_mcp_templates(temp_db, templates, tag="gobby")

    servers = tmp_path / "servers"
    servers.mkdir(parents=True, exist_ok=True)
    (servers / "bool-demo.yaml").write_text(
        "template: boolean\nenabled: true\nvalues:\n  insecure: true\n",
        encoding="utf-8",
    )
    result = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=None,
        secret_store=SecretStore(temp_db),
    )

    assert result["errors"] == []
    assert result["synced"] == 1
    row = manager.get_server("bool-demo", project_id=GLOBAL_PROJECT_ID)
    assert row is not None
    assert row.env == {"INSECURE": "true"}
    assert row.template_values == {"insecure": "true"}


def test_non_bool_enabled_is_a_sync_error(temp_db: Any, tmp_path: Path) -> None:
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    templates = tmp_path / "templates"
    _write_template(templates, "demo")
    sync_bundled_mcp_templates(temp_db, templates, tag="gobby")

    servers = tmp_path / "servers"
    servers.mkdir(parents=True, exist_ok=True)
    (servers / "bad-enabled.yaml").write_text(
        "template: demo\nenabled: 1\nvalues:\n  token: $secret:demo_token\n",
        encoding="utf-8",
    )
    result = sync_mcp_server_files(
        temp_db,
        [servers],
        project_id=None,
        project_root=None,
        secret_store=SecretStore(temp_db),
    )

    assert result["synced"] == 0
    assert any("invalid enabled value" in error for error in result["errors"])
    assert manager.get_server("bad-enabled", project_id=GLOBAL_PROJECT_ID) is None


_BUNDLED_TEMPLATE_NAMES = sorted(path.stem for path in get_bundled_templates_path().glob("*.yaml"))


def _required_values(template: MCPServerTemplate) -> dict[str, str]:
    """Minimal instance values satisfying a template's required and one-of rules."""
    needed = {param.name for param in template.params if param.required}
    needed.update(group[0] for group in template.require_one_of)
    values: dict[str, str] = {}
    for param in template.params:
        if param.name not in needed:
            continue
        if param.secret:
            values[param.name] = f"$secret:{param.name}_test"
        elif param.choices:
            values[param.name] = param.choices[0]
        else:
            values[param.name] = f"{param.name}-value"
    return values


@pytest.mark.parametrize("template_name", _BUNDLED_TEMPLATE_NAMES)
def test_project_override_adds_env_param_for_every_bundled_template(
    temp_db: Any, tmp_path: Path, sample_project: dict[str, Any], template_name: str
) -> None:
    """A project-level override can add an env-backed param to any bundled template."""
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates

    manager = LocalMCPManager(temp_db)
    bundled_sync = sync_bundled_mcp_templates(temp_db, get_bundled_templates_path(), tag="gobby")
    assert bundled_sync["errors"] == []

    bundled_path = get_bundled_templates_path() / f"{template_name}.yaml"
    override = yaml.safe_load(bundled_path.read_text(encoding="utf-8"))
    override["override"] = True
    override.setdefault("params", []).append(
        {"name": "extra_flag", "env": "EXTRA_FLAG", "default": "off", "choices": ["on", "off"]}
    )
    project_templates = tmp_path / "project" / "templates"
    project_templates.mkdir(parents=True)
    (project_templates / f"{template_name}.yaml").write_text(
        yaml.safe_dump(override), encoding="utf-8"
    )
    template_sync = sync_bundled_mcp_templates(
        temp_db,
        [project_templates],
        tag="user",
        project_id=sample_project["id"],
        project_root=project_templates,
    )
    assert template_sync["errors"] == []
    project_template = manager.get_template(template_name, project_id=sample_project["id"])
    assert project_template is not None
    assert project_template.project_id == sample_project["id"]
    assert project_template.owner == "user"

    values = _required_values(MCPServerTemplate.from_definition(project_template.definition))
    values["extra_flag"] = "on"
    project_servers = tmp_path / "project" / "servers"
    project_servers.mkdir(parents=True)
    instance_name = f"{template_name}-inst"
    (project_servers / f"{instance_name}.yaml").write_text(
        yaml.safe_dump({"name": instance_name, "template": template_name, "values": values}),
        encoding="utf-8",
    )
    server_sync = sync_mcp_server_files(
        temp_db,
        [project_servers],
        project_id=sample_project["id"],
        project_root=project_servers,
        secret_store=SecretStore(temp_db),
    )
    assert server_sync["errors"] == []

    row = manager.get_server(instance_name, project_id=sample_project["id"])
    assert row is not None
    assert row.template_id == project_template.id
    assert row.env is not None
    assert row.env["EXTRA_FLAG"] == "on"
