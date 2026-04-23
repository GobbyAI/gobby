"""Compatibility smoke tests for the split runner test suite."""

import pytest

pytestmark = pytest.mark.unit


def test_runner_split_modules_importable() -> None:
    """Keep the historical test path runnable after the runner suite split."""
    import tests.test_runner_init
    import tests.test_runner_lifecycle
    import tests.test_runner_shutdown

    assert tests.test_runner_init is not None
    assert tests.test_runner_lifecycle is not None
    assert tests.test_runner_shutdown is not None
