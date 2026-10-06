"""Credential roots that managed sandbox grants cannot expose."""

from pathlib import Path

from gobby.paths import get_gobby_home
from gobby.utils.break_glass import break_glass_path, break_glass_staging_path
from gobby.utils.local_token import daemon_bootstrap_path


def credential_roots() -> list[Path]:
    """Keep install credentials and the bound daemon startup files denied."""
    home = get_gobby_home()
    return [
        home / "bootstrap.yaml",
        home / ".secret_kek",
        home / "local_cli_token",
        home / "tools" / "srt",
        daemon_bootstrap_path(),
        break_glass_path(),
        break_glass_staging_path(),
    ]


def gcode_runtime_root() -> Path:
    """Return the parent of every workspace's generated gcode home."""
    return get_gobby_home() / "gcode-runtime"
