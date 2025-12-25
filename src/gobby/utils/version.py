"""Version utility for reading package version from metadata."""

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
        return version("gobby-platform")
    except PackageNotFoundError:
        # Fallback for development environments where package may not be installed
        return "0.1.1-dev"
