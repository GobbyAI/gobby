"""Read roots that agent shell commands need beyond the policy defaults."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

from gobby.agents.sandbox_policy import canonical_paths


def gobby_read_exceptions(env: Mapping[str, str]) -> list[str]:
    """Return Gobby state, runtime, and prompt resources needed by agents."""
    # Gobby home stays readable apart from sensitive_roots(), which deny
    # bootstrap.yaml: agents reach the daemon through GOBBY_DAEMON_URL and the
    # hub through their managed grant (GOBBY_MANAGED_EXECUTION_BOOTSTRAP), which
    # these exceptions re-admit alongside other per-run resources.
    home = Path.home()
    paths = [
        # Codex scrubs the environment of agent shell commands, so uv falls back
        # to its default cache roots. Shared cache contents remain readable while
        # writable cache state is routed through a per-run extra_write_path.
        home / ".cache" / "uv",
        home / "Library" / "Caches" / "uv",
    ]
    runtime_home = env.get("GOBBY_CODE_INDEX_RUNTIME_HOME")
    if runtime_home:
        paths.append(Path(runtime_home))
    managed_bootstrap = env.get("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
    if managed_bootstrap:
        paths.append(Path(managed_bootstrap))
    prompt_file = env.get("GOBBY_PROMPT_FILE")
    if prompt_file:
        paths.append(Path(prompt_file))
    # Workspace tooling: agent shell `uv run` commands need the uv binary and its
    # managed interpreters (venv symlink targets). Both live outside the
    # workspace and stay read-only.
    uv_binary = shutil.which("uv", path=env.get("PATH"))
    if uv_binary:
        uv_path = Path(uv_binary).expanduser().absolute()
        paths.extend([uv_path, uv_path.resolve(strict=False)])
    paths.append(Path("~/.local/share/uv").expanduser())
    return canonical_paths([str(path) for path in paths])
