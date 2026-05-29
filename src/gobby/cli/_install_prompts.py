"""Interactive prompt and UI helpers for install/uninstall commands."""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from ._install_embedding_prompts import (
    _infer_embedding_provider_from_url,
    _run_embedding_install,
)

__all__ = (
    "_API_KEY_PROMPTS",
    "_echo_install_details",
    "_echo_install_summary",
    "_echo_migration_notice",
    "_echo_uninstall_details",
    "_echo_uninstall_summary",
    "_ensure_db_and_secrets",
    "_infer_embedding_provider_from_url",
    "_prompt_api_keys",
    "_prompt_hub_api_keys",
    "_run_codex_uninstall",
    "_run_embedding_install",
    "_run_git_hooks_install",
    "_run_falkordb_install",
    "_run_falkordb_uninstall",
    "_run_qdrant_install",
    "_run_standard_cli_install",
    "_run_standard_cli_uninstall",
    "_run_voice_install",
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.secrets import SecretStore

logger = logging.getLogger(__name__)


@contextmanager
def _ensure_db_and_secrets(
    db: HubDatabase | None,
    secret_store: SecretStore | None,
) -> Generator[tuple[HubDatabase, SecretStore]]:
    """Yield (db, secret_store), opening the DB only when this call created it.

    When the caller passes a pre-built ``db`` we use it as-is and do NOT close
    it in the finally block — ownership stays with the caller. When ``db`` is
    None we resolve the configured path and open a fresh handle, closing it
    on exit. ``SecretStore`` is constructed on the caller's behalf when not
    provided.

    Imports are deferred to avoid circular-import cycles with the rest of the
    ``gobby.cli`` package, mirroring the pattern used by the original
    in-function bootstrap blocks.
    """
    from gobby.storage.hub.runtime import open_runtime_hub_database
    from gobby.storage.secrets import SecretStore as _SecretStore

    owns_db = False
    local_db: HubDatabase | None = db
    local_store: SecretStore | None = secret_store
    try:
        if local_db is None:
            local_db = open_runtime_hub_database(apply_migrations=False)
            owns_db = True
        if local_store is None:
            local_store = _SecretStore(local_db)
        yield local_db, local_store
    finally:
        if owns_db and local_db is not None:
            local_db.close()


def _echo_install_details(
    result: dict[str, Any],
    mcp_config_path: str | None = None,
    config_path: str | None = None,
) -> None:
    """Print common install result details (hooks, workflows, agents, commands, plugins, MCP)."""
    click.echo(f"Installed {len(result['hooks_installed'])} hooks")
    for hook in result["hooks_installed"]:
        click.echo(f"  - {hook}")

    for key, label in [
        ("workflows_installed", "workflows"),
        ("agents_installed", "agents"),
        ("commands_installed", "skills/commands"),
    ]:
        items = result.get(key)
        if items:
            click.echo(f"Installed {len(items)} {label}")
            for item in items:
                click.echo(f"  - {item}")

    plugins = result.get("plugins_installed")
    if plugins:
        click.echo(f"Installed {len(plugins)} plugins to .gobby/plugins/")
        for plugin in plugins:
            click.echo(f"  - {plugin}")

    if mcp_config_path:
        if result.get("mcp_configured"):
            click.echo(f"Configured MCP server: {mcp_config_path}")
        elif result.get("mcp_already_configured"):
            click.echo(f"MCP server already configured: {mcp_config_path}")

    if config_path:
        click.echo(f"Configuration: {config_path}")


def _echo_uninstall_details(
    result: dict[str, Any],
    label: str = "hooks from settings",
) -> None:
    """Print common uninstall result details (hooks removed, files removed)."""
    if result["hooks_removed"]:
        click.echo(f"Removed {len(result['hooks_removed'])} {label}")
        for hook in result["hooks_removed"]:
            click.echo(f"  - {hook}")
    if result["files_removed"]:
        click.echo(f"Removed {len(result['files_removed'])} files")
    if not result["hooks_removed"] and not result["files_removed"]:
        click.echo("  (no hooks found to remove)")


_API_KEY_PROMPTS = [
    {
        "secret_name": "github_personal_access_token",
        "env_var": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "label": "GitHub Personal Access Token",
        "category": "mcp_server",
        "description": "GitHub MCP server authentication",
    },
    {
        "secret_name": "linear_api_key",
        "env_var": "LINEAR_API_KEY",
        "label": "Linear API Key",
        "category": "mcp_server",
        "description": "Linear MCP server authentication",
    },
    {
        "secret_name": "brave_api_key",
        "env_var": "BRAVE_API_KEY",
        "label": "Brave Search API Key",
        "category": "mcp_server",
        "description": "Brave Search MCP server authentication",
    },
    {
        "secret_name": "openai_api_key",
        "env_var": "OPENAI_API_KEY",
        "label": "OpenAI API Key",
        "category": "llm",
        "description": "OpenAI embeddings and LLM execution",
    },
    {
        "secret_name": "context7_api_key",
        "env_var": "CONTEXT7_API_KEY",
        "label": "Context7 API Key",
        "category": "mcp_server",
        "description": "Context7 library docs (private repos)",
    },
]


def _prompt_hub_api_keys(
    no_interactive: bool = False,
    *,
    db: HubDatabase | None = None,
    secret_store: SecretStore | None = None,
) -> dict[str, Any]:
    """Prompt for skill-hub API keys driven by resolved SkillsConfig.hubs.

    Iterates hubs whose ``auth_key_name`` is set and whose secret isn't already
    stored in SecretStore. Prompts with hidden input, persists with category
    ``integration``. Env vars are never consulted (hub auth resolves from
    SecretStore only).

    Returns:
        Dict with ``stored`` / ``skipped`` / ``already_configured`` counts and
        ``unresolved``: list of ``(hub_name, auth_key_name)`` tuples for hubs
        whose secret is still not stored after this call. Callers can use
        ``unresolved`` to drive follow-up help text.
    """
    result: dict[str, Any] = {
        "stored": 0,
        "skipped": 0,
        "already_configured": 0,
        "unresolved": [],  # list[tuple[str, str]] of (hub_name, auth_key_name)
    }

    try:
        from gobby.cli.utils import load_full_config_from_db

        skills_cfg = load_full_config_from_db().skills
    except Exception as e:
        click.echo(f"  Warning: Could not initialize hub key prompt: {e}")
        return result

    try:
        with _ensure_db_and_secrets(db, secret_store) as (_db, resolved_store):
            # Flatten to (hub_name, auth_key_name) so mypy sees auth_key_name as str.
            auth_hubs: list[tuple[str, str]] = [
                (name, cfg.auth_key_name)
                for name, cfg in skills_cfg.hubs.items()
                if cfg.auth_key_name
            ]
            pending: list[tuple[str, str]] = [
                (name, key) for name, key in auth_hubs if not resolved_store.exists(key)
            ]
            result["already_configured"] = len(auth_hubs) - len(pending)

            if no_interactive:
                result["unresolved"] = list(pending)
                return result

            if not pending:
                return result

            click.echo("")
            click.echo("-" * 40)
            click.echo("Skill Hub API Keys (optional)")
            click.echo("-" * 40)
            click.echo("These enable authenticated skill hubs. Press Enter to skip.")
            click.echo("")

            for idx, (hub_name, auth_key_name) in enumerate(pending):
                try:
                    value = click.prompt(
                        f"  {hub_name} ({auth_key_name})",
                        default="",
                        hide_input=True,
                        show_default=False,
                    )
                except (click.Abort, EOFError) as abort_exc:
                    click.echo("")
                    # Record the abort so CI / non-interactive pipelines have a
                    # breadcrumb pointing at the exact hub that was pending when
                    # the user (or upstream) interrupted.
                    logger.debug(
                        "Hub key prompt aborted; remaining=%d",
                        len(pending) - idx,
                        exc_info=abort_exc,
                    )
                    # Remaining pending hubs become unresolved.
                    for rh_name, rh_key in pending[idx:]:
                        result["unresolved"].append((rh_name, rh_key))
                    break

                if value.strip():
                    try:
                        resolved_store.set(
                            name=auth_key_name,
                            plaintext_value=value.strip(),
                            category="integration",
                            description=f"API key for {hub_name} skill hub",
                        )
                        click.echo(f"    Stored credential for {hub_name} ({auth_key_name})")
                        result["stored"] += 1
                    except Exception as e:
                        logger.warning("Failed to store hub credential", exc_info=e)
                        click.echo("    Warning: Failed to store credential")
                        result["unresolved"].append((hub_name, auth_key_name))
                else:
                    result["skipped"] += 1
                    result["unresolved"].append((hub_name, auth_key_name))
            return result
    except Exception as e:
        logger.debug("Could not initialize hub key prompt", exc_info=e)
        click.echo("  Warning: Could not initialize hub key prompt")
        return result


def _prompt_api_keys(
    no_interactive: bool = False,
    *,
    db: HubDatabase | None = None,
    secret_store: SecretStore | None = None,
) -> dict[str, Any]:
    """Prompt for API keys and store them in the secret store.

    Skips keys that are already stored or found in environment variables.
    In non-interactive mode, skips all prompts.

    Returns:
        Dict with stored, skipped, env_found counts.
    """
    result: dict[str, Any] = {"stored": 0, "skipped": 0, "env_found": 0, "already_configured": 0}

    if no_interactive:
        return result

    try:
        with _ensure_db_and_secrets(db, secret_store) as (_db, resolved_store):
            click.echo("")
            click.echo("-" * 40)
            click.echo("API Keys (optional)")
            click.echo("-" * 40)
            click.echo("These enable external integrations. Press Enter to skip any.")
            click.echo("")

            for key_info in _API_KEY_PROMPTS:
                secret_name = key_info["secret_name"]
                env_var = key_info["env_var"]
                label = key_info["label"]

                # Check if already stored in secret store
                if resolved_store.exists(secret_name):
                    click.echo(f"  {label}: (already configured)")
                    result["already_configured"] += 1
                    continue

                # Check if set in environment
                if os.environ.get(env_var):
                    click.echo(f"  {label}: (found in environment)")
                    result["env_found"] += 1
                    continue

                # Prompt for value
                try:
                    value = click.prompt(
                        f"  {label}", default="", hide_input=True, show_default=False
                    )
                except (click.Abort, EOFError):
                    click.echo("")
                    break

                if value.strip():
                    try:
                        resolved_store.set(
                            name=secret_name,
                            plaintext_value=value.strip(),
                            category=key_info["category"],
                            description=key_info["description"],
                        )
                        click.echo("    Stored credential")
                        result["stored"] += 1
                    except Exception as e:
                        logger.warning("Failed to store prompted credential", exc_info=e)
                        click.echo("    Warning: Failed to store credential")
                else:
                    result["skipped"] += 1

            return result
    except Exception as e:
        logger.debug("Could not initialize secret store prompt", exc_info=e)
        click.echo("  Warning: Could not initialize secret store")
        return result


# ---------------------------------------------------------------------------
# Per-CLI install/uninstall orchestration helpers
# ---------------------------------------------------------------------------

# Mapping: cli_name -> (display_name, global_config, project_config_subpath, mcp_config_path)
_CLI_INSTALL_META: dict[str, tuple[str, str, str, str | None]] = {
    "claude": ("Claude Code", "~/.claude/settings.json", ".claude/settings.json", "~/.claude.json"),
    "gemini": (
        "Gemini CLI (deprecated)",
        "~/.gemini/settings.json",
        ".gemini/settings.json",
        "~/.gemini/settings.json",
    ),
    "grok": (
        "Grok CLI",
        "~/.grok/hooks/gobby.json",
        ".grok/hooks/gobby.json",
        None,
    ),
    "qwen": (
        "Qwen CLI",
        "~/.qwen/settings.json",
        ".qwen/settings.json",
        "~/.qwen/settings.json",
    ),
    "codex": ("Codex", "~/.codex/hooks.json", ".codex/hooks.json", None),
    "droid": (
        "Droid CLI",
        "~/.factory/hooks/hooks.json",
        ".factory/hooks/hooks.json",
        "~/.factory/mcp.json",
    ),
}


def _run_standard_cli_install(
    cli_name: str,
    installer: Callable[..., dict[str, Any]],
    project_path: Path,
    mode: str,
    results: dict[str, dict[str, Any]],
) -> None:
    """Run install + echo for a standard CLI (claude, gemini, qwen, codex, droid)."""
    display_name, global_config, project_subpath, mcp_path = _CLI_INSTALL_META[cli_name]

    click.echo("-" * 40)
    click.echo(display_name)
    click.echo("-" * 40)

    result = installer(project_path, mode=mode)
    results[cli_name] = result

    if result["success"]:
        config = global_config if mode == "global" else str(project_path / project_subpath)
        _echo_install_details(result, mcp_config_path=mcp_path, config_path=config)
    else:
        click.echo(f"Failed: {result['error']}", err=True)
    click.echo("")


def _run_git_hooks_install(
    installer: Callable[..., dict[str, Any]],
    project_path: Path,
    results: dict[str, dict[str, Any]],
) -> None:
    """Run install + echo for Git hooks."""
    click.echo("-" * 40)
    click.echo("Git Hooks (Verification + JSONL Export)")
    click.echo("-" * 40)

    result = installer(project_path)
    results["git-hooks"] = result

    if result["success"]:
        if result.get("installed"):
            click.echo("Installed git hooks:")
            for hook in result["installed"]:
                click.echo(f"  - {hook}")
        if result.get("skipped"):
            click.echo("Skipped:")
            for hook in result["skipped"]:
                click.echo(f"  - {hook}")
        if result.get("removed_legacy_imports"):
            click.echo("Removed legacy import hooks:")
            for hook in result["removed_legacy_imports"]:
                click.echo(f"  - {hook}")
        if not result.get("installed") and not result.get("skipped"):
            click.echo("No hooks to install")
    else:
        click.echo(f"Failed: {result['error']}", err=True)
    click.echo("")


def _run_qdrant_install(
    installer: Callable[..., dict[str, Any]],
    results: dict[str, dict[str, Any]],
) -> None:
    """Run install + echo for Qdrant (default, Docker-gated)."""
    if not shutil.which("docker"):
        click.echo("Docker not found — Qdrant will run in embedded mode")
        return

    click.echo("-" * 40)
    click.echo("Qdrant Vector Database")
    click.echo("-" * 40)

    result = installer()
    results["qdrant"] = result

    if result["success"]:
        click.echo("Qdrant installed")
        click.echo(f"  URL: {result['qdrant_url']}")
    else:
        click.echo(f"Failed: {result['error']}", err=True)
    click.echo("")


def _run_voice_install(
    results: dict[str, dict[str, Any]],
    voice_flag: bool = False,
    no_interactive: bool = False,
    *,
    db: HubDatabase | None = None,
    secret_store: SecretStore | None = None,
) -> None:
    """Interactive voice chat setup.

    Installs voice dependencies (faster-whisper, chatterbox-tts) and enables
    voice in daemon config. Skipped by default in non-interactive mode.

    Args:
        results: Results dict to accumulate install outcomes
        voice_flag: If True, install voice deps without prompting
        no_interactive: If True, skip the prompt (only install if voice_flag is set)
    """
    install_voice = voice_flag

    if not install_voice and not no_interactive:
        click.echo("-" * 40)
        click.echo("Voice Chat (Optional)")
        click.echo("-" * 40)
        click.echo("Voice adds speech-to-text and text-to-speech with voice cloning.")
        click.echo("Requires a local Whisper + Chatterbox TTS stack.")
        click.echo("")

        try:
            install_voice = click.confirm("Enable voice chat?", default=False)
        except (click.Abort, EOFError):
            click.echo("")
            install_voice = False

        click.echo("")

    if not install_voice:
        return

    click.echo("-" * 40)
    click.echo("Installing Voice Dependencies")
    click.echo("-" * 40)

    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "uv",
                "pip",
                "install",
                "faster-whisper>=1.0.0",
                "chatterbox-tts",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )

        if proc.returncode == 0:
            click.echo("Voice packages installed successfully")
            results["voice"] = {"success": True}

            # Enable voice in daemon config
            try:
                from gobby.storage.config_store import ConfigStore

                with _ensure_db_and_secrets(db, None) as (_db, _store):
                    ConfigStore(_db).set("voice.enabled", True)
                click.echo("Voice enabled in daemon config")
            except Exception as e:
                logger.warning(f"Failed to update daemon config: {e}")
                click.echo(f"  Warning: Could not enable voice in config: {e}")
                click.echo("  Enable manually: set voice.enabled=true in config")

            click.echo("")
            click.echo("Next: place a 10-20s voice reference WAV at:")
            click.echo("  ~/.gobby/voice/reference.wav")
            click.echo("")
            click.echo("See docs/guides/voice.md for how to sample from YouTube.")
        else:
            click.echo("Failed to install voice packages:", err=True)
            click.echo(proc.stderr[:500] if proc.stderr else "(no error output)", err=True)
            results["voice"] = {"success": False, "error": "pip install failed"}
    except subprocess.TimeoutExpired:
        click.echo("Voice package installation timed out (10 min limit)", err=True)
        results["voice"] = {"success": False, "error": "install timeout"}
    except Exception as e:
        click.echo(f"Failed: {e}", err=True)
        results["voice"] = {"success": False, "error": str(e)}

    click.echo("")


def _run_falkordb_install(
    installer: Callable[..., dict[str, Any]],
    falkordb_password: str | None,
    results: dict[str, dict[str, Any]],
) -> None:
    """Run install + echo for FalkorDB."""
    click.echo("-" * 40)
    click.echo("FalkorDB Knowledge Graph")
    click.echo("-" * 40)

    try:
        result = installer(password=falkordb_password)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    results["falkordb"] = result

    if result["success"]:
        click.echo("FalkorDB installed (docker mode)")
        source = result.get("password_source")
        if source == "generated" and result.get("password"):
            click.echo(f"  Generated FalkorDB password: {result['password']}")
        elif source == "provided":
            click.echo("  Using provided FalkorDB password (not displayed)")
        elif source == "reused":
            click.echo("  Reusing existing FalkorDB password from config_store")
        click.echo(f"  Redis: {result['url']}")
        click.echo(f"  Browser: {result['browser_url']}")
        if result.get("compose_file"):
            click.echo(f"  Compose: {result['compose_file']}")
        click.echo("\nRestart the daemon to apply: gobby restart")
    else:
        click.echo(f"Failed: {result['error']}", err=True)
    click.echo("")


def _echo_migration_notice(project_path: Path) -> None:
    """Detect and warn about per-project hooks that can be cleaned up."""
    per_project_hooks = []
    for cli_name, cli_dir in [
        ("claude", ".claude"),
        ("gemini", ".gemini"),
        ("codex", ".codex"),
    ]:
        hooks_dir = project_path / cli_dir / "hooks"
        if (hooks_dir / "hook_dispatcher.py").exists():
            per_project_hooks.append(cli_name)

    if per_project_hooks:
        click.echo("-" * 40)
        click.echo("Migration Notice")
        click.echo("-" * 40)
        click.echo(f"Per-project hooks detected for: {', '.join(per_project_hooks)}")
        click.echo("Run 'gobby uninstall --project' to clean up per-project hooks.")
        click.echo("")


def _echo_install_summary(
    results: dict[str, dict[str, Any]],
    no_interactive_flag: bool,
    *,
    db: HubDatabase | None = None,
    secret_store: SecretStore | None = None,
) -> bool:
    """Print install summary, next steps, and API key prompts. Returns True if all succeeded."""
    click.echo("=" * 60)
    click.echo("  Summary")
    click.echo("=" * 60)

    all_success = all(r.get("success", False) for r in results.values())

    if all_success:
        click.echo("\nInstallation completed successfully!")
    else:
        failed = [cli for cli, r in results.items() if not r.get("success", False)]
        click.echo(f"\nSome installations failed: {', '.join(failed)}")

    click.echo("\nNext steps:")
    click.echo("  1. Ensure the Gobby daemon is running:")
    click.echo("     gobby start")
    click.echo("  2. Start a new session in your AI coding CLI")
    click.echo("  3. Your sessions will now be tracked locally")

    api_key_result = _prompt_api_keys(
        no_interactive=no_interactive_flag,
        db=db,
        secret_store=secret_store,
    )
    hub_key_result = _prompt_hub_api_keys(
        no_interactive=no_interactive_flag,
        db=db,
        secret_store=secret_store,
    )

    mcp_nothing_configured = (
        api_key_result["stored"] == 0
        and api_key_result["already_configured"] == 0
        and api_key_result["env_found"] == 0
    )
    if no_interactive_flag or mcp_nothing_configured:
        click.echo("\nMCP Servers (via Gobby proxy):")
        click.echo("  Configure API keys to enable external integrations:")
        click.echo("    gobby secrets set github_personal_access_token")
        click.echo("    gobby secrets set linear_api_key")
        click.echo("    gobby secrets set openai_api_key")
        click.echo("    gobby secrets set context7_api_key")
        click.echo("  Or set environment variables (GITHUB_PERSONAL_ACCESS_TOKEN, etc.)")
        click.echo("  Restart the daemon after setting: gobby restart")

    if hub_key_result["unresolved"]:
        click.echo("\nSkill hubs with missing API keys:")
        for hub_name, auth_key_name in hub_key_result["unresolved"]:
            click.echo(f"    gobby secrets set {auth_key_name}    # for {hub_name} hub")
        click.echo("  Restart the daemon after setting: gobby restart")

    return all_success


def _echo_uninstall_summary(results: dict[str, dict[str, Any]]) -> bool:
    """Print uninstall summary. Returns True if all succeeded."""
    click.echo("=" * 60)
    click.echo("  Summary")
    click.echo("=" * 60)

    all_success = all(r.get("success", False) for r in results.values())

    if all_success:
        click.echo("\nUninstallation completed successfully!")
    else:
        failed = [cli for cli, r in results.items() if not r.get("success", False)]
        click.echo(f"\nSome uninstallations failed: {', '.join(failed)}")

    return all_success


# Uninstall CLI meta: cli_name -> (display_name, uninstall_label)
_CLI_UNINSTALL_META: dict[str, tuple[str, str]] = {
    "claude": ("Claude Code", "hooks from settings"),
    "gemini": ("Gemini CLI", "hooks from settings"),
    "qwen": ("Qwen CLI", "hooks from settings"),
    "codex": ("Codex", "hooks from settings"),
    "droid": ("Droid CLI", "hooks from settings"),
}


def _run_standard_cli_uninstall(
    cli_name: str,
    uninstaller: Callable[..., dict[str, Any]],
    uninstall_base: Path,
    results: dict[str, dict[str, Any]],
    **kwargs: Any,
) -> None:
    """Run uninstall + echo for a standard CLI."""
    display_name, label = _CLI_UNINSTALL_META[cli_name]

    click.echo("-" * 40)
    click.echo(display_name)
    click.echo("-" * 40)

    result = uninstaller(uninstall_base, **kwargs)
    results[cli_name] = result

    if result["success"]:
        _echo_uninstall_details(result, label=label)
    else:
        click.echo(f"Failed: {result['error']}", err=True)
    click.echo("")


def _run_codex_uninstall(
    uninstaller: Callable[..., dict[str, Any]],
    results: dict[str, dict[str, Any]],
) -> None:
    """Run uninstall + echo for Codex notify integration."""
    click.echo("-" * 40)
    click.echo("Codex")
    click.echo("-" * 40)

    result = uninstaller()
    results["codex"] = result

    if result["success"]:
        if result["files_removed"]:
            click.echo(f"Removed {len(result['files_removed'])} files")
            for f in result["files_removed"]:
                click.echo(f"  - {f}")
        if result.get("config_updated"):
            click.echo("Updated: ~/.codex/config.toml (removed `notify = ...`)")
        if not result["files_removed"] and not result.get("config_updated"):
            click.echo("  (no codex integration found to remove)")
    else:
        click.echo(f"Failed: {result['error']}", err=True)
    click.echo("")


def _run_falkordb_uninstall(
    uninstaller: Callable[..., dict[str, Any]],
    volumes_flag: bool,
    results: dict[str, dict[str, Any]],
) -> None:
    """Run uninstall + echo for FalkorDB."""
    click.echo("-" * 40)
    click.echo("FalkorDB Knowledge Graph")
    click.echo("-" * 40)

    result = uninstaller(purge=volumes_flag)
    results["falkordb"] = result

    if result["success"]:
        if result.get("already_uninstalled"):
            click.echo("FalkorDB was not installed")
        else:
            click.echo("FalkorDB services removed")
            if result.get("data_removed"):
                click.echo("  Docker volumes removed (data deleted)")
        click.echo("\nRestart the daemon to apply: gobby restart")
    else:
        click.echo(f"Failed: {result['error']}", err=True)
    click.echo("")
