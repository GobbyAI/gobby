"""Version utility for reading the installed package version."""

from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    """
    Get the package version from installed package metadata.

    This reads the version from the installed package metadata (pyproject.toml),
    ensuring a single source of truth for the version string.

    Returns:
        str: The package version (e.g., "0.1.0")

    Raises:
        PackageNotFoundError: If the package is not installed
    """
    try:
        return version("gobby")
    except PackageNotFoundError:
        # Development checkouts may not be installed as a package yet.
        from gobby import __version__

        return __version__
