"""Repair and provider config helpers for isolated agent workspaces."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from gobby.agents.isolation_git_hygiene import apply_isolation_git_hygiene
from gobby.agents.python_env_seed import preseed_isolated_python_environment

logger = logging.getLogger("gobby.agents.isolation")


async def repair_isolation_environment(
    *,
    main_repo_path: str,
    isolated_path: str,
    provider: str,
) -> None:
    """Repair hooks, project metadata, and MCP config for an isolated workspace."""
    await _copy_cli_hooks(
        source_path=main_repo_path,
        target_path=isolated_path,
        provider=provider,
    )

    from gobby.utils.project_context import ensure_project_json_for_isolation

    await asyncio.to_thread(
        ensure_project_json_for_isolation,
        main_repo_path,
        isolated_path,
    )
    seed_result = await preseed_isolated_python_environment(isolated_path)
    if seed_result.attempted and not seed_result.success:
        logger.warning(
            "Failed to pre-seed isolated Python environment for %s: %s",
            isolated_path,
            seed_result.error,
        )
    await _patch_mcp_config_for_isolation(
        main_repo_path=main_repo_path,
        isolated_path=isolated_path,
        provider=provider,
    )
    try:
        await asyncio.to_thread(
            apply_isolation_git_hygiene,
            isolated_path,
            main_repo_path=main_repo_path,
        )
    except Exception:
        logger.warning(
            "Failed to apply isolation git hygiene for %s",
            isolated_path,
            exc_info=True,
        )


async def _copy_cli_hooks(
    source_path: str,
    target_path: str,
    provider: str,
) -> None:
    """Copy CLI-specific hook directories to an isolated environment.

    Without these hooks, the spawned agent won't trigger SessionStart
    and other lifecycle hooks, breaking Gobby integration.

    Claude and Codex use copied project directories, Droid generates its hook
    files separately, and Qwen relies on its installer/global hook flow.

    Args:
        source_path: Path to the source repository
        target_path: Path to the isolated environment (worktree or clone)
        provider: CLI provider (claude, qwen, codex, droid)
    """
    import shutil

    if provider == "droid":
        await _copy_droid_hooks_for_isolation(target_path)
        return

    cli_dirs = {
        "claude": ".claude",
        "codex": ".codex",
    }

    cli_dir = cli_dirs.get(provider)
    if not cli_dir:
        logger.debug(f"No CLI hooks directory defined for provider: {provider}")
        return

    src_path = Path(source_path) / cli_dir
    dst_path = Path(target_path) / cli_dir

    if not src_path.exists():
        logger.debug(f"CLI hooks directory not found in source repo: {src_path}")
        return

    try:
        await asyncio.to_thread(shutil.copytree, src_path, dst_path, dirs_exist_ok=True)
        logger.info(f"Copied CLI hooks from {src_path} to {dst_path}")
    except shutil.Error:
        logger.warning(
            f"Failed to copy CLI hooks: provider={provider}, src={src_path}, dst={dst_path}",
            exc_info=True,
        )
    except OSError:
        logger.warning(
            f"Filesystem error copying CLI hooks: provider={provider}, src={src_path}, dst={dst_path}",
            exc_info=True,
        )


async def _copy_droid_hooks_for_isolation(target_path: str) -> None:
    """Ensure isolated Droid sessions have Gobby hooks.

    Droid normally reads user-global ``~/.factory/hooks/hooks.json`` regardless
    of ``--cwd``, but project-local Factory config can shadow that file and
    global inheritance is hard to prove in automated isolation tests. Writing
    Gobby-owned hook entries into the isolated worktree or clone gives spawned
    Droid agents deterministic lifecycle hooks while preserving user entries.
    """
    hooks_path = Path(target_path) / ".factory" / "hooks" / "hooks.json"
    try:
        await asyncio.to_thread(_write_droid_isolation_hooks, hooks_path)
        logger.info(f"Wrote Droid isolation hooks to {hooks_path}")
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        logger.warning(
            f"Failed to write Droid isolation hooks: target={hooks_path}",
            exc_info=True,
        )


def _write_droid_isolation_hooks(hooks_path: Path) -> None:
    existing_settings = _load_json_object(hooks_path)
    gobby_settings = _load_droid_isolation_hooks_template()
    updated_settings = _merge_droid_isolation_hooks(existing_settings, gobby_settings)
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(updated_settings, indent=2) + "\n")


def _load_droid_isolation_hooks_template() -> dict[str, Any]:
    from gobby.cli.installers.hook_commands import rewrite_hook_template_commands
    from gobby.paths import get_install_dir

    template_path = get_install_dir() / "droid" / "hooks-template.json"
    if not template_path.exists():
        raise FileNotFoundError(f"Missing Droid hooks template: {template_path}")

    template = _load_json_object(template_path)
    hooks_dir = Path(os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks")))
    rewrite_hook_template_commands(
        template,
        cli_name="droid",
        hooks_dir=hooks_dir.expanduser(),
    )
    return template


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open() as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _merge_droid_isolation_hooks(
    existing_settings: dict[str, Any],
    gobby_settings: dict[str, Any],
) -> dict[str, Any]:
    from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
    from gobby.cli.installers.hook_commands import config_contains_gobby_hook

    updated = deepcopy(existing_settings)
    hooks = updated.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        updated["hooks"] = hooks

    gobby_hooks = gobby_settings.get("hooks")
    if not isinstance(gobby_hooks, dict):
        raise ValueError("Droid hooks template does not contain a hooks object")

    for hook_type in DROID_PASCAL_HOOK_NAMES:
        hook_config = gobby_hooks.get(hook_type)
        if not isinstance(hook_config, list) or not hook_config:
            raise ValueError(f"Droid hooks template missing hook type: {hook_type}")

        current_config = hooks.get(hook_type)
        preserved: list[Any] = []
        if isinstance(current_config, list):
            preserved = [
                deepcopy(entry) for entry in current_config if not config_contains_gobby_hook(entry)
            ]
        hooks[hook_type] = preserved + deepcopy(hook_config)

    return updated


async def _patch_mcp_config_for_isolation(
    main_repo_path: str,
    isolated_path: str,
    provider: str,
) -> None:
    """Patch MCP server config so isolated agents use the main repo's gobby code.

    Without this, ``uv run gobby mcp-server`` resolves from the isolated
    environment's ``pyproject.toml``/``uv.lock``, which may be on an old
    branch and missing recent fixes.

    Writes a ``.mcp.json`` in the isolated directory that forces
    ``--project <main_repo_path>`` so ``uv`` resolves from the main repo.

    For Claude provider, also patches ``~/.claude.json`` to register the
    isolated path as a project with the correct MCP server config.
    """

    mcp_config = {
        "mcpServers": {
            "gobby": {
                "command": "uv",
                "args": [
                    "run",
                    "--project",
                    main_repo_path,
                    "gobby",
                    "mcp-server",
                ],
            }
        }
    }

    mcp_json_path = Path(isolated_path) / ".mcp.json"
    try:
        await asyncio.to_thread(mcp_json_path.write_text, json.dumps(mcp_config, indent=2) + "\n")
        logger.info(f"Wrote MCP config to {mcp_json_path}")
    except OSError as e:
        logger.warning(f"Failed to write .mcp.json to {isolated_path}: {e}")
        return

    # For Claude provider, register the isolated path in ~/.claude.json
    if provider == "claude":
        claude_json_path = Path.home() / ".claude.json"
        try:

            def _patch_claude_json() -> None:
                import tempfile

                data: dict[str, Any] = {}
                if claude_json_path.exists():
                    try:
                        data = json.loads(claude_json_path.read_text())
                    except json.JSONDecodeError:
                        logger.warning("Malformed ~/.claude.json, re-initializing")
                        data = {}

                projects = data.setdefault("projects", {})
                project_config = projects.setdefault(isolated_path, {})
                project_config["mcpServers"] = mcp_config["mcpServers"]

                # Atomic write via tempfile + os.replace to avoid TOCTOU race
                fd, tmp_path = tempfile.mkstemp(dir=str(claude_json_path.parent), suffix=".tmp")
                try:
                    os.write(fd, (json.dumps(data, indent=2) + "\n").encode())
                    os.close(fd)
                    os.replace(tmp_path, str(claude_json_path))
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise

            await asyncio.to_thread(_patch_claude_json)
            logger.info(f"Registered isolated path in ~/.claude.json: {isolated_path}")
        except Exception as e:
            logger.warning(f"Failed to patch ~/.claude.json for {isolated_path}: {e}")


def provider_mcp_config_error(isolated_path: str, provider: str) -> str | None:
    """Return a compact preflight error if isolated MCP config is missing."""
    if provider == "droid":
        return None

    isolated = Path(isolated_path)
    mcp_json_path = isolated / ".mcp.json"
    if not mcp_json_path.exists():
        return f"provider_mcp_config_missing:{mcp_json_path}"
    try:
        data = json.loads(mcp_json_path.read_text())
        gobby_server = data["mcpServers"]["gobby"]
        args = gobby_server.get("args", [])
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        return f"provider_mcp_config_invalid:{mcp_json_path}:{exc}"
    if gobby_server.get("command") != "uv" or "mcp-server" not in args:
        return f"provider_mcp_config_invalid:{mcp_json_path}:gobby server not configured"

    if provider != "claude":
        return None

    claude_json_path = Path.home() / ".claude.json"
    if not claude_json_path.exists():
        return f"provider_mcp_config_missing:{claude_json_path}"
    try:
        claude_data = json.loads(claude_json_path.read_text())
        projects = claude_data.get("projects", {})
        keys = {str(isolated), str(isolated.resolve())}
        project_config = next(
            (projects[key] for key in keys if isinstance(projects.get(key), dict)),
            None,
        )
        if not project_config or "gobby" not in project_config.get("mcpServers", {}):
            return f"provider_mcp_config_missing:{claude_json_path}:projects[{isolated}]"
    except (OSError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        return f"provider_mcp_config_invalid:{claude_json_path}:{exc}"
    return None
