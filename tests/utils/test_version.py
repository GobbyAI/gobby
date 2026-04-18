from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from gobby.utils.version import get_version


def test_get_version_uses_package_metadata() -> None:
    with patch("gobby.utils.version.version", return_value="1.2.3"):
        assert get_version() == "1.2.3"


def test_get_version_falls_back_to_module_version() -> None:
    with patch(
        "gobby.utils.version.version",
        side_effect=PackageNotFoundError("Package metadata is unavailable"),
    ):
        assert get_version() == "0.4.0"
