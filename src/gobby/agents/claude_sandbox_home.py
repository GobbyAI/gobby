"""Private Claude reviewer housekeeping with preserved MCP and transcript state."""

from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.sandbox_control_paths import CLAUDE_CONTROL_FILES
from gobby.agents.trust import encode_claude_project_path


@dataclass(frozen=True)
class ClaudeSandboxHome:
    environment: dict[str, str]
    protected_write_paths: tuple[str, ...]


def prepare_claude_reviewer_home(
    cache: Path, env: Mapping[str, str], *, workspace: str, auto_approve: bool
) -> ClaudeSandboxHome:
    """Create private mutable state; retain only authorized configuration scopes."""
    original_config = env.get("CLAUDE_CONFIG_DIR", "")
    host_config = Path(original_config).expanduser() if original_config else Path.home() / ".claude"
    if not host_config.is_absolute():
        host_config = Path(workspace) / host_config
    source_global = (
        host_config / ".claude.json" if original_config else Path.home() / ".claude.json"
    )
    if host_config.is_symlink() or source_global.is_symlink() or cache.is_symlink():
        raise ValueError("Claude reviewer state must use real configuration directories")
    seed: dict[str, object] = {
        "hasCompletedOnboarding": True,
        "bypassPermissionsModeAccepted": auto_approve,
    }
    if source_global.is_file():
        source = json.loads(source_global.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise ValueError("Claude global configuration must be an object")
        if isinstance(servers := source.get("mcpServers"), dict):
            # User helpers run from CLAUDE_CONFIG_DIR; keep their original cwd.
            user_servers = {}
            for name, server in servers.items():
                if (
                    isinstance(server, dict)
                    and isinstance(helper := server.get("headersHelper"), str)
                    and helper
                ):
                    server = {
                        **server,
                        "headersHelper": f"cd {shlex.quote(str(host_config.resolve()))} && (\n{helper}\n)",
                    }
                user_servers[name] = server
            seed["mcpServers"] = user_servers
        projects = source.get("projects", {})
        project = projects.get(workspace, {}) if isinstance(projects, dict) else {}
        if isinstance(project, dict) and isinstance(servers := project.get("mcpServers"), dict):
            seed["projects"] = {workspace: {"mcpServers": servers}}
    config = cache / "claude"
    # Refuse redirected or reused state before writing, including failed setup retries.
    config.mkdir(mode=0o700, parents=True)
    for name in ("settings.json", "settings.local.json"):
        source_settings = host_config / name
        if source_settings.is_symlink():
            raise ValueError("Claude reviewer settings must be regular files")
        if source_settings.is_file():
            target = config / name
            shutil.copyfile(source_settings, target)
            target.chmod(0o600)
    # Only this workspace's transcripts use the existing default Claude write grant.
    encoded = encode_claude_project_path(workspace)
    host_root = Path.home() / ".claude"
    host_projects = host_root / "projects"
    if host_root.is_symlink() or host_projects.is_symlink():
        raise ValueError("Claude transcripts must remain inside the existing provider grant")
    host_projects = host_projects.resolve()
    transcript = host_projects / encoded
    if transcript.is_symlink() or not transcript.resolve().is_relative_to(host_projects):
        raise ValueError("Claude transcript directory escaped the existing provider grant")
    if auto_approve:
        transcript.mkdir(mode=0o700, parents=True, exist_ok=True)
    if transcript.is_dir():
        (config / "projects").mkdir(mode=0o700)
        (config / "projects" / encoded).symlink_to(transcript, target_is_directory=True)
    global_config = config / ".claude.json"
    global_config.write_text(json.dumps(seed), encoding="utf-8")
    global_config.chmod(0o600)
    environment = {
        "CLAUDE_CONFIG_DIR": str(config),
        "XDG_STATE_HOME": str(config / "state"),
        # An empty selector preserves the default, un-hashed Keychain namespace.
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": env.get(
            "CLAUDE_SECURESTORAGE_CONFIG_DIR", original_config
        ),
    }
    protected = tuple(str(config / name) for name in CLAUDE_CONTROL_FILES)
    return ClaudeSandboxHome(environment, protected)
