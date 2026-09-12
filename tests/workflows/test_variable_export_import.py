"""Variable auto-exports must reload through the user-definition import path."""

from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.workflows._variables import create_variable, update_variable
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.imports import sync_imported_workflow_file

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("value", [False, None, ["first"], {"nested": [1]}, "hello"])
def test_auto_export_reloads_typed_value(
    temp_db: HubDatabase, tmp_path: Path, value: object
) -> None:
    manager = SessionVariableDefaultManager(temp_db)
    result = create_variable(
        manager, "example_value", value, "Example default", project_path=tmp_path
    )
    assert result["success"] is True
    exported = tmp_path / ".gobby/workflows/variables/example_value.yaml"
    original = manager.get_by_name("example_value")
    assert original is not None
    manager.hard_delete(original.id)

    sync_imported_workflow_file(temp_db, exported, None)
    restored = manager.get_by_name("example_value")
    assert restored is not None
    assert restored.default_value == value
    assert restored.description == "Example default"

    updated = update_variable(manager, "example_value", {"updated": True}, project_path=tmp_path)
    assert updated["success"] is True
    manager.update(restored.id, default_value="stale")
    sync_imported_workflow_file(temp_db, exported, None)
    assert manager.get(restored.id).default_value == {"updated": True}
