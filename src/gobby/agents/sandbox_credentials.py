"""Credential roots that managed sandbox grants cannot expose."""

from pathlib import Path

from gobby.paths import get_gobby_home
from gobby.utils.break_glass import break_glass_path, break_glass_staging_path
from gobby.utils.capability_signing_key import (
    CAPABILITY_SIGNING_KEY_FILENAME,
    capability_signing_key_staging_path,
)
from gobby.utils.local_token import daemon_bootstrap_path, operator_bootstrap_path
from gobby.utils.session_credential_record import STORE_DIR as SESSION_CREDENTIAL_STORE_DIR


def credential_roots() -> list[Path]:
    """Keep install credentials and the bound daemon startup files denied.

    The operator's canonical bootstrap is denied too: a daemon started under a GOBBY_HOME override
    keeps its bound file elsewhere, and the canonical file is still the operator's credential.
    The capability signing key (beside the bound bootstrap, with its staging directory) can forge
    any session token, and the session-credentials store holds live session tokens. A sandboxed
    seat presents its own run token, so it needs neither.
    """
    home = get_gobby_home()
    bound = daemon_bootstrap_path()
    roots = [
        home / "bootstrap.yaml",
        home / ".secret_kek",
        home / "grants",
        home / "tools" / "srt",
        # The runtime home's pair is the no-hub stand-in for the bound pair. It stays denied
        # when the daemon is bound elsewhere.
        home / "break_glass",
        home / ".break_glass-staging",
        bound,
        break_glass_path(),
        break_glass_staging_path(),
        home / SESSION_CREDENTIAL_STORE_DIR,
    ]
    for key in (
        home / CAPABILITY_SIGNING_KEY_FILENAME,
        bound.with_name(CAPABILITY_SIGNING_KEY_FILENAME),
    ):
        for path in (key, capability_signing_key_staging_path(key)):
            if path not in roots:
                roots.append(path)
    canonical = operator_bootstrap_path()
    if canonical is not None:
        roots.extend(
            [
                canonical,
                break_glass_path(canonical.parent),
                break_glass_staging_path(canonical.parent),
            ]
        )
    return roots


def gcode_runtime_root() -> Path:
    """Return the parent of every workspace's generated gcode home."""
    return get_gobby_home() / "gcode-runtime"
