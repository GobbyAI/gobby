"""Startup must leave registry network I/O to the background refresh."""

from collections.abc import Iterator
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from gobby.app_context import clear_app_context
from gobby.runner import GobbyRunner
from tests.runner_helpers import create_base_patches

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_context() -> Iterator[None]:
    # Constructing a runner installs the process-wide app context. Clear it so
    # later tests in the same process do not inherit this runner's mocks.
    clear_app_context()
    yield
    clear_app_context()


def test_runner_construction_never_runs_synchronous_metadata_population() -> None:
    with ExitStack() as stack:
        for base_patch in create_base_patches():
            stack.enter_context(base_patch)
        # Keep construction bounded even on the broken implementation. A real
        # hanging populate here prevents HTTP admission from ever starting.
        populate = stack.enter_context(
            patch("gobby.storage.model_metadata.ModelMetadataStore.populate", return_value=0)
        )
        runner = GobbyRunner()
        assert runner.database is not None
        assert populate.call_count == 0, "startup synchronously waits for registry network I/O"
