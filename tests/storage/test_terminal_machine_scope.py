"""Machine-scoping checks for durable terminal rows."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import MachineOwnershipMismatchError
from tests.storage.test_terminals import (
    LOCAL_MACHINE_ID,
    _manager,
    _tmux_key,
    _tmux_locator,
)

pytestmark = pytest.mark.unit

FOREIGN_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _machine() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_attach_locator_rejects_foreign_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    manager = _manager(temp_db)
    locator = _tmux_locator(pane_id="%foreign")
    foreign = manager.upsert_external(
        machine_id=FOREIGN_MACHINE_ID,
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=_tmux_key(locator),
        session_name="foreign",
        window_id="@2",
        title="foreign",
    )

    with pytest.raises(MachineOwnershipMismatchError, match="machine_ownership_mismatch"):
        manager.attach_locator(
            foreign.id,
            live_host_epoch=str(uuid.uuid4()),
            socket_dir=tmp_path,
        )
