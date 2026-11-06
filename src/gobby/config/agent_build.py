"""Agent build-output location configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from gobby.utils.local_token import account_home

AGENT_BUILD_ROOT_KEY = "agent_build.build_root"


class AgentBuildConfig(BaseModel):
    """Where agent Cargo build folders are written."""

    build_root: str = Field(
        default="",
        description=(
            "Absolute, canonical, existing directory that holds agent Cargo build folders. "
            "Empty uses the Gobby cache. Takes effect at daemon startup."
        ),
    )


def validate_build_root(value: str | None) -> Path | None:
    """Return the canonical build root, or None when the setting is empty.

    One validator gates every place the setting is accepted: the config write, the
    restart preflight, and daemon startup. The root must be an existing absolute
    directory spelled canonically, so a symlink never redirects a sandbox write grant.
    It must not be a filesystem root or an account home, which a sandbox grant would
    then cover wholesale. Raises ValueError with the reason.
    """
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"agent build root must be absolute: {value}")
    if path.resolve(strict=False) != path:
        raise ValueError(f"agent build root must be canonical, with no symlink components: {value}")
    if not path.is_dir():
        raise ValueError(f"agent build root does not exist or is not a directory: {value}")
    # The passwd home is the account's real home; `$HOME` alone could name another directory.
    accounts = {Path.home(), account_home()}
    if path == Path(path.anchor) or path in accounts:
        raise ValueError(f"agent build root is too broad: {value}")
    return path
