from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest

import gobby
from gobby.utils.version import get_version


@pytest.mark.unit
def test_get_version_uses_package_metadata() -> None:
    with patch("gobby.utils.version.version", return_value="1.2.3"):
        assert get_version() == "1.2.3"


def test_get_version_falls_back_to_module_version() -> None:
    with patch(
        "gobby.utils.version.version",
        side_effect=PackageNotFoundError("Package metadata is unavailable"),
    ):
        assert get_version() == gobby.__version__
