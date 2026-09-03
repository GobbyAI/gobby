"""Contract tests for isolated-checkout helpers."""

from collections.abc import Callable

import pytest

from gobby.agents import launcher_session
from gobby.storage import workspace_machine_scope
from gobby.utils import machine_id as machine_id_utils
from tests.fixtures.isolated_checkout import patch_local_machine_id


def test_patch_local_machine_id_preserves_require_machine_id_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned_machine_id = "9d969add-1896-4c92-8a73-c41774352666"
    utility_binding = machine_id_utils.require_machine_id
    workspace_binding: Callable[[], str] = vars(workspace_machine_scope)["require_machine_id"]
    launcher_binding: Callable[[], str] = vars(launcher_session)["require_machine_id"]

    assert workspace_binding is utility_binding
    assert launcher_binding is utility_binding

    patch_local_machine_id(monkeypatch, pinned_machine_id)

    assert machine_id_utils.require_machine_id is utility_binding
    assert vars(workspace_machine_scope)["require_machine_id"] is workspace_binding
    assert vars(launcher_session)["require_machine_id"] is launcher_binding
    assert machine_id_utils.require_machine_id() == pinned_machine_id
    assert workspace_binding() == pinned_machine_id
    assert launcher_binding() == pinned_machine_id
