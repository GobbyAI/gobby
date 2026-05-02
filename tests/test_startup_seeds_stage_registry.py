"""Red test for startup stage-registry sync wiring."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from gobby.runner import GobbyRunner
from gobby.storage.tasks._stage_registry_loader import StageRegistrySyncResult
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


def test_registry_populated_after_startup(mock_config) -> None:
    patches = create_base_patches(mock_config=mock_config)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        sync = stack.enter_context(
            patch(
                "gobby.storage.tasks._stage_registry_loader.StageRegistryLoader.sync",
                autospec=True,
            )
        )
        sync.return_value = StageRegistrySyncResult(
            upserted=0,
            skipped=11,
            bundled_hash="hash",
        )

        runner = GobbyRunner()

    sync.assert_called_once()
    assert sync.call_args.args[1] is runner.database
