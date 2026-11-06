"""Credential roots that managed sandbox grants cannot expose."""

from pathlib import Path

from gobby.paths import get_gobby_home
from gobby.utils.break_glass import break_glass_path, break_glass_staging_path
from gobby.utils.local_token import daemon_bootstrap_path, operator_bootstrap_path


def credential_roots() -> list[Path]:
    """Keep install credentials and the bound daemon startup files denied.

    The operator's canonical bootstrap is denied too: a daemon started under a GOBBY_HOME override
    keeps its bound file elsewhere, and the canonical file is still the operator's credential.
    """
    home = get_gobby_home()
    roots = [
        home / "bootstrap.yaml",
        home / ".secret_kek",
        home / "grants",
        home / "tools" / "srt",
        daemon_bootstrap_path(),
        break_glass_path(),
        break_glass_staging_path(),
    ]
    canonical = operator_bootstrap_path()
    if canonical is None:
        # Without the passwd home the operator's real bootstrap cannot be denied, so no
        # sandbox may launch with a credential list that silently lacks it.
        raise RuntimeError(
            "the operator's passwd home is unknown; refusing to build sandbox credential roots"
        )
    roots.append(canonical)
    return roots


def gcode_runtime_root() -> Path:
    """Return the parent of every workspace's generated gcode home."""
    return get_gobby_home() / "gcode-runtime"
