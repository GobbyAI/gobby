"""Tests for MCP template registry storage."""

from typing import Any

import pytest

from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID, LocalProjectManager

pytestmark = pytest.mark.unit


def _definition(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "transport": "stdio",
        "command": "uvx",
        "args": ["awslabs.openapi-mcp-server"],
        "enabled": True,
        "runtime_hook": "chrome-devtools",
    }
    body.update(overrides)
    return body


class TestMCPTemplateStorageMixin:
    def test_first_creation_applies_definition_enabled(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict[str, Any],
    ) -> None:
        row = mcp_manager.upsert_template(
            name="openapi",
            project_id=sample_project["id"],
            owner="user",
            definition=_definition(enabled=False),
            source_path=".gobby/mcp/templates/openapi.yaml",
        )

        assert row.name == "openapi"
        assert row.project_id == sample_project["id"]
        assert row.owner == "user"
        assert row.enabled is False
        assert row.source_path == ".gobby/mcp/templates/openapi.yaml"
        assert row.definition["command"] == "uvx"
        assert row.definition_hash

        loaded = mcp_manager.get_template("openapi", project_id=sample_project["id"])
        assert loaded is not None
        assert loaded.id == row.id
        assert loaded.enabled is False

    def test_identical_reupsert_skips_rewrite_and_updated_at_churn(
        self,
        mcp_manager: LocalMCPManager,
    ) -> None:
        first = mcp_manager.upsert_template(
            name="openapi",
            project_id=GLOBAL_PROJECT_ID,
            owner="user",
            definition=_definition(),
            source_path="~/.gobby/mcp/templates/openapi.yaml",
        )
        second = mcp_manager.upsert_template(
            name="openapi",
            project_id=GLOBAL_PROJECT_ID,
            owner="user",
            definition=_definition(),
            source_path="~/.gobby/mcp/templates/openapi.yaml",
        )
        assert second.id == first.id
        assert second.definition_hash == first.definition_hash
        assert second.updated_at == first.updated_at

        changed = mcp_manager.upsert_template(
            name="openapi",
            project_id=GLOBAL_PROJECT_ID,
            owner="user",
            definition=_definition(command="npx"),
            source_path="~/.gobby/mcp/templates/openapi.yaml",
        )
        assert changed.id == first.id
        assert changed.definition["command"] == "npx"
        assert changed.updated_at > first.updated_at

    def test_gobby_drift_refresh_preserves_stored_enabled(
        self,
        mcp_manager: LocalMCPManager,
    ) -> None:
        created = mcp_manager.upsert_template(
            name="github",
            project_id=GLOBAL_PROJECT_ID,
            owner="gobby",
            definition=_definition(command="npx"),
        )
        mcp_manager.upsert_template(
            name="github",
            project_id=GLOBAL_PROJECT_ID,
            owner="gobby",
            definition=_definition(command="npx"),
            enabled=False,
        )
        refreshed = mcp_manager.upsert_template(
            name="github",
            project_id=GLOBAL_PROJECT_ID,
            owner="gobby",
            definition=_definition(command="uvx", args=["@github/mcp"]),
            source_path="src/gobby/install/shared/mcp/templates/github.yaml",
        )

        assert refreshed.id == created.id
        assert refreshed.enabled is False
        assert refreshed.definition["command"] == "uvx"
        assert refreshed.definition["args"] == ["@github/mcp"]
        assert refreshed.source_path == "src/gobby/install/shared/mcp/templates/github.yaml"
        assert refreshed.definition_hash != created.definition_hash

    def test_get_template_never_crosses_projects_and_returns_disabled_row(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict[str, Any],
        project_manager: LocalProjectManager,
    ) -> None:
        other = project_manager.create(name="other-mcp-project", repo_path="/tmp/other-mcp")
        global_row = mcp_manager.upsert_template(
            name="openapi",
            project_id=GLOBAL_PROJECT_ID,
            owner="gobby",
            definition=_definition(),
        )
        project_row = mcp_manager.upsert_template(
            name="openapi",
            project_id=sample_project["id"],
            owner="user",
            definition=_definition(enabled=False, command="npx"),
        )

        shadowed = mcp_manager.get_template("openapi", project_id=sample_project["id"])
        assert shadowed is not None
        assert shadowed.id == project_row.id
        assert shadowed.enabled is False

        from_other = mcp_manager.get_template("openapi", project_id=other.id)
        assert from_other is not None
        assert from_other.id == global_row.id

        exact_other = mcp_manager.get_template("missing", project_id=other.id)
        assert exact_other is None

        listed = mcp_manager.list_templates(project_id=sample_project["id"], enabled_only=False)
        names = [row.name for row in listed]
        assert names.count("openapi") == 1
        assert listed[0].id == project_row.id

        enabled_only = mcp_manager.list_templates(
            project_id=sample_project["id"],
            enabled_only=True,
        )
        assert all(row.name != "openapi" for row in enabled_only)

    def test_delete_template_nulls_instance_template_id(
        self,
        mcp_manager: LocalMCPManager,
        sample_project: dict[str, Any],
    ) -> None:
        template = mcp_manager.upsert_template(
            name="openapi",
            project_id=GLOBAL_PROJECT_ID,
            owner="gobby",
            definition=_definition(),
        )
        instance = mcp_manager.upsert(
            name="petstore",
            transport="stdio",
            command="uvx",
            project_id=sample_project["id"],
            template_id=template.id,
            template_values={"api_name": "pets"},
            runtime_hook="chrome-devtools",
        )

        attached = mcp_manager.list_template_instances(template.id)
        assert [row.id for row in attached] == [instance.id]
        assert attached[0].template == "openapi"

        assert mcp_manager.delete_template("openapi", project_id=GLOBAL_PROJECT_ID) is True

        detached = mcp_manager.get_server_by_id(instance.id)
        assert detached is not None
        assert detached.template_id is None
        assert detached.template is None
        assert detached.runtime_hook == "chrome-devtools"
        assert detached.command == "uvx"
        assert mcp_manager.list_template_instances(template.id) == []
        assert mcp_manager.get_template_by_id(template.id) is None
