"""
Installation commands for hooks.
"""

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

from ._detectors import (
    _is_claude_code_installed,
    _is_codex_cli_installed,
    _is_droid_cli_installed,
    _is_gemini_cli_installed,
    _is_qwen_cli_installed,
)
from ._install_prompts import (
    _API_KEY_PROMPTS,
    _echo_install_details,
    _echo_install_summary,
    _echo_migration_notice,
    _echo_uninstall_details,
    _echo_uninstall_summary,
    _prompt_api_keys,
    _run_embedding_install,
    _run_falkordb_install,
    _run_falkordb_uninstall,
    _run_git_hooks_install,
    _run_qdrant_install,
    _run_standard_cli_install,
    _run_standard_cli_uninstall,
    _run_voice_install,
)
from .install_setup import ensure_daemon_config, run_daemon_setup
from .installers import (
    install_claude,
    install_codex,
    install_droid,
    install_embedding,
    install_falkordb,
    install_gemini,
    install_git_hooks,
    install_qdrant,
    install_qwen,
    uninstall_claude,
    uninstall_codex,
    uninstall_droid,
    uninstall_falkordb,
    uninstall_gemini,
    uninstall_qwen,
)
from .utils import get_install_dir, load_full_config_from_db

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility (tests import from here)
_ensure_daemon_config = ensure_daemon_config

# Re-exports from extracted modules (tests import these from gobby.cli.install)
__all__ = [
    "_is_claude_code_installed",
    "_is_codex_cli_installed",
    "_is_droid_cli_installed",
    "_is_gemini_cli_installed",
    "_is_qwen_cli_installed",
    "_echo_install_details",
    "_echo_uninstall_details",
    "_API_KEY_PROMPTS",
    "_prompt_api_keys",
    "_ensure_daemon_config",
    "install",
    "uninstall",
]


_GRAPH_BACKEND_REMOVED_MESSAGE = """--neo4j / --neo4j-password has been removed in 0.4.0.

The knowledge graph backend has been replaced with FalkorDB.
- Install (auto-runs as part of gobby install; tune with): gobby install [--falkordb-password <pw>] (or service-only: gobby install --falkordb)
- Uninstall: gobby uninstall --falkordb
- Migration notes: see CHANGELOG.md for the full upgrade path."""


def _raise_graph_backend_removed() -> None:
    raise click.UsageError(_GRAPH_BACKEND_REMOVED_MESSAGE)


@click.command("install")
@click.option(
    "--claude",
    "claude_flag",
    is_flag=True,
    help="Install Claude Code hooks only",
)
@click.option(
    "--gemini",
    "gemini_flag",
    is_flag=True,
    help="Install Gemini CLI hooks only",
)
@click.option(
    "--codex",
    "codex_flag",
    is_flag=True,
    help="Configure Codex notify integration (interactive Codex)",
)
@click.option(
    "--droid",
    "droid_flag",
    is_flag=True,
    help="Install Droid CLI hooks only",
)
@click.option(
    "--qwen",
    "qwen_flag",
    is_flag=True,
    help="Install Qwen CLI hooks only",
)
@click.option(
    "--hooks",
    "--git-hooks",
    "hooks_flag",
    is_flag=True,
    help="Install Git hooks for verification, JSONL export, and code indexing",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Install hooks for all detected CLIs (default behavior when no flags specified)",
)
@click.option(
    "--no-ext-services",
    "no_ext_services_flag",
    is_flag=True,
    help="Skip Docker service installation (Qdrant, FalkorDB)",
)
@click.option(
    "--falkordb",
    "falkordb_flag",
    is_flag=True,
    default=False,
    help="Install only the FalkorDB service",
)
@click.option(
    "--falkordb-password",
    "falkordb_password",
    default=None,
    help="Set a custom FalkorDB password (default: auto-generated or reused from existing config)",
)
@click.option(
    "--neo4j-password",
    "deprecated_neo4j_password",
    default=None,
    hidden=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: _raise_graph_backend_removed()
    if value is not None
    else None,
)
@click.option(
    "--neo4j",
    "neo4j_flag",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: _raise_graph_backend_removed() if value else None,
)
@click.option(
    "--project",
    "project_flag",
    is_flag=True,
    help="Install hooks per-project instead of globally (legacy behavior)",
)
@click.option(
    "--voice",
    "voice_flag",
    is_flag=True,
    help="Install voice chat dependencies (STT + TTS with voice cloning)",
)
@click.option(
    "--embedding-url",
    "embedding_url",
    default=None,
    help="Override the embedding provider's API base URL (e.g. for LM Studio on a LAN IP)",
)
@click.option(
    "--embedding-provider",
    "embedding_provider",
    type=click.Choice(["lmstudio", "ollama", "openai-compatible"]),
    default=None,
    help=(
        "Compatibility mode for --embedding-url: lmstudio uses LM Studio-compatible "
        "defaults, ollama uses Ollama-compatible defaults, openai-compatible uses "
        "generic OpenAI-compatible embedding APIs."
    ),
)
@click.option(
    "--embedding-model",
    "embedding_model",
    default=None,
    help="Override the embedding model id (e.g. text-embedding-qwen3-embedding-4b)",
)
@click.option(
    "--embedding-dim",
    "embedding_dim",
    type=click.IntRange(min=1),
    default=None,
    help="Override the embedding dimension. Omit to auto-detect via /v1/embeddings probe.",
)
@click.option(
    "--no-interactive",
    "no_interactive_flag",
    is_flag=True,
    help="Skip interactive prompts (for CI/automation)",
)
@click.option(
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Target directory (default: current directory)",
)
def install(
    claude_flag: bool,
    gemini_flag: bool,
    codex_flag: bool,
    droid_flag: bool,
    qwen_flag: bool,
    hooks_flag: bool,
    all_flag: bool,
    no_ext_services_flag: bool,
    falkordb_flag: bool,
    falkordb_password: str | None,
    voice_flag: bool,
    project_flag: bool,
    embedding_url: str | None,
    embedding_provider: str | None,
    embedding_model: str | None,
    embedding_dim: int | None,
    no_interactive_flag: bool,
    working_dir: Path | None,
) -> None:
    """Install Gobby hooks to AI coding CLIs and Git.

    By default (no flags), installs hooks globally (one-time setup).
    Use --project to install per-project instead (legacy behavior).
    Use --claude, --gemini, --qwen, --codex, or --droid to install only to specific CLIs.
    Use --hooks to install Git hooks for verification, JSONL export, and code indexing.
    """
    if embedding_provider and not embedding_url:
        raise click.UsageError("--embedding-provider requires --embedding-url.")

    if falkordb_flag:
        service_results: dict[str, dict[str, Any]] = {}
        _run_falkordb_install(install_falkordb, falkordb_password, service_results)
        if not _echo_install_summary(service_results, no_interactive_flag):
            sys.exit(1)
        return

    project_path = working_dir.resolve() if working_dir else Path.cwd()
    mode = "project" if project_flag else "global"

    if (
        not claude_flag
        and not gemini_flag
        and not qwen_flag
        and not codex_flag
        and not droid_flag
        and not hooks_flag
        and not falkordb_flag
        and not all_flag
    ):
        all_flag = True
    is_full_install = all_flag

    # Build list of CLIs to install
    clis_to_install: list[str] = []

    # Local copy of hooks_flag — mutated by auto-detection below
    install_hooks = hooks_flag

    if all_flag:
        # Auto-detect installed CLIs
        if _is_claude_code_installed():
            clis_to_install.append("claude")
        if _is_gemini_cli_installed():
            clis_to_install.append("gemini")
        if _is_qwen_cli_installed():
            clis_to_install.append("qwen")
        if _is_codex_cli_installed():
            clis_to_install.append("codex")
        if _is_droid_cli_installed():
            clis_to_install.append("droid")

        # Check for git
        if (project_path / ".git").exists():
            install_hooks = True

        if not clis_to_install and not install_hooks:
            click.echo("No supported AI coding CLIs detected.")
            click.echo("\nSupported CLIs:")
            click.echo("  - Claude Code: npm install -g @anthropic-ai/claude-code")
            click.echo("  - Gemini CLI:  npm install -g @google/gemini-cli")
            click.echo("  - Qwen CLI:    npm install -g @qwen-code/qwen-code")
            click.echo("  - Codex CLI:   npm install -g @openai/codex")
            click.echo("  - Droid CLI:   curl -fsSL https://app.factory.ai/cli | sh")
            click.echo(
                "\nYou can still install manually with --claude, --gemini, --qwen, --codex, "
                "or --droid flags."
            )
            sys.exit(1)
    else:
        if claude_flag:
            clis_to_install.append("claude")
        if gemini_flag:
            clis_to_install.append("gemini")
        if qwen_flag:
            clis_to_install.append("qwen")
        if codex_flag:
            clis_to_install.append("codex")
        if droid_flag:
            clis_to_install.append("droid")

    # Get install directory info
    install_dir = get_install_dir()
    is_dev_mode = "src" in str(install_dir)

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Installation")
    click.echo("=" * 60)
    if mode == "global":
        click.echo("\nScope: Global (hooks installed to ~/.gobby/hooks/)")
    else:
        click.echo(f"\nScope: Project ({project_path})")
    if is_dev_mode:
        click.echo("Mode: Development (using source directory)")

    # Phase 1: daemon config, database, bundled content, MCP servers, IDE config
    config_result = _ensure_daemon_config()
    if config_result["created"]:
        click.echo(f"Created daemon config: {config_result['path']}")
    run_daemon_setup(project_path)

    toggles = list(clis_to_install)
    if install_hooks:
        toggles.append("git-hooks")

    click.echo(f"Components to configure: {', '.join(toggles)}")
    click.echo("")

    # Track results
    results: dict[str, dict[str, Any]] = {}
    db: HubDatabase | None = None
    secret_store: SecretStore | None = None

    try:
        from gobby.storage.hub.runtime import open_runtime_hub_database

        load_full_config_from_db()
        db = open_runtime_hub_database(apply_migrations=False)
        secret_store = SecretStore(db)
    except (FileNotFoundError, PermissionError, OSError, RuntimeError, ValueError) as exc:
        # Missing config file, unavailable hub, malformed config values.
        # The orchestration proceeds with db/secret_store=None — downstream
        # steps open their own DB via _ensure_db_and_secrets if they need it.
        logger.warning(
            "Failed to initialize install database/secret store (%s): %s",
            type(exc).__name__,
            exc,
        )

    try:
        # Standard CLIs (claude, gemini, qwen, codex, droid)
        _standard_installers: dict[str, Callable[..., dict[str, Any]]] = {
            "claude": install_claude,
            "gemini": install_gemini,
            "qwen": install_qwen,
            "codex": install_codex,
            "droid": install_droid,
        }
        for cli_name, installer_fn in _standard_installers.items():
            if cli_name in clis_to_install:
                _run_standard_cli_install(cli_name, installer_fn, project_path, mode, results)

        # Git hooks
        if install_hooks:
            _run_git_hooks_install(install_git_hooks, project_path, results)

        # Embedding provider setup runs only for full installs. Targeted hook installs
        # should not depend on local embedding or Docker service health.
        selected_embedding_provider = "none"
        if is_full_install:
            selected_embedding_provider = _run_embedding_install(
                install_embedding,
                results,
                no_interactive=no_interactive_flag,
                api_base_override=embedding_url,
                model_override=embedding_model,
                dim_override=embedding_dim,
                provider_override=embedding_provider,
            )

        # Voice chat (optional — installs ~500MB of deps including PyTorch)
        _run_voice_install(
            results,
            voice_flag=voice_flag,
            no_interactive=no_interactive_flag,
            db=db,
            secret_store=secret_store,
        )

        # Docker services (Qdrant + FalkorDB, installed by default if Docker available)
        # Skipped if user chose "none" for embeddings (no semantic search = no vector store needed)
        if is_full_install:
            if not no_ext_services_flag and selected_embedding_provider != "none":
                _run_qdrant_install(install_qdrant, results)
                _run_falkordb_install(install_falkordb, falkordb_password, results)
            elif selected_embedding_provider == "none":
                click.echo("Skipping Qdrant/FalkorDB install (embeddings disabled)")
                click.echo("")

        # Migration detection
        if mode == "global":
            _echo_migration_notice(project_path)

        # Summary, next steps, API key prompts
        all_success = _echo_install_summary(
            results,
            no_interactive_flag,
            db=db,
            secret_store=secret_store,
        )
        if not all_success:
            sys.exit(1)
    finally:
        if db is not None:
            db.close()


@click.command("uninstall")
@click.option(
    "--claude",
    "claude_flag",
    is_flag=True,
    help="Uninstall Claude Code hooks only",
)
@click.option(
    "--gemini",
    "gemini_flag",
    is_flag=True,
    help="Uninstall Gemini CLI hooks only",
)
@click.option(
    "--codex",
    "codex_flag",
    is_flag=True,
    help="Uninstall Codex notify integration",
)
@click.option(
    "--droid",
    "droid_flag",
    is_flag=True,
    help="Uninstall Droid CLI hooks only",
)
@click.option(
    "--qwen",
    "qwen_flag",
    is_flag=True,
    help="Uninstall Qwen CLI hooks only",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Uninstall hooks from all CLIs (default behavior when no flags specified)",
)
@click.option(
    "--falkordb",
    "falkordb_flag",
    is_flag=True,
    help="Uninstall FalkorDB knowledge graph backend",
)
@click.option(
    "--neo4j",
    "neo4j_flag",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: _raise_graph_backend_removed() if value else None,
)
@click.option(
    "--volumes",
    "volumes_flag",
    is_flag=True,
    help="Also remove Docker volumes (data loss, use with --falkordb)",
)
@click.option(
    "--project",
    "project_flag",
    is_flag=True,
    help="Uninstall per-project hooks from current directory (instead of global)",
)
@click.option(
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Target directory (default: current directory)",
)
@click.confirmation_option(prompt="Are you sure you want to uninstall Gobby hooks?")
def uninstall(
    claude_flag: bool,
    gemini_flag: bool,
    codex_flag: bool,
    droid_flag: bool,
    qwen_flag: bool,
    all_flag: bool,
    falkordb_flag: bool,
    volumes_flag: bool,
    project_flag: bool,
    working_dir: Path | None,
) -> None:
    """Uninstall Gobby hooks from AI coding CLIs.

    By default (no flags), uninstalls global hooks from CLI settings and ~/.gobby/hooks/.
    Use --project to uninstall per-project hooks from the current directory.
    Use --claude, --gemini, --qwen, or --codex to uninstall only from specific CLIs.
    """
    project_path = working_dir.resolve() if working_dir else Path.cwd()

    # Determine which CLIs to uninstall
    if (
        not claude_flag
        and not gemini_flag
        and not qwen_flag
        and not codex_flag
        and not droid_flag
        and not all_flag
        and not falkordb_flag
    ):
        all_flag = True

    # Build list of CLIs to uninstall
    clis_to_uninstall: list[str] = []

    if all_flag:
        if project_flag:
            claude_settings = project_path / ".claude" / "settings.json"
            gemini_settings = project_path / ".gemini" / "settings.json"
            qwen_settings = project_path / ".qwen" / "settings.json"
            codex_hooks = project_path / ".codex" / "hooks.json"
            droid_hooks = project_path / ".factory" / "hooks" / "hooks.json"
        else:
            claude_settings = Path.home() / ".claude" / "settings.json"
            gemini_settings = Path.home() / ".gemini" / "settings.json"
            qwen_settings = Path.home() / ".qwen" / "settings.json"
            codex_hooks = Path.home() / ".codex" / "hooks.json"
            droid_hooks = Path.home() / ".factory" / "hooks" / "hooks.json"

        if claude_settings.exists():
            clis_to_uninstall.append("claude")
        if gemini_settings.exists():
            clis_to_uninstall.append("gemini")
        if qwen_settings.exists():
            clis_to_uninstall.append("qwen")
        if codex_hooks.exists():
            clis_to_uninstall.append("codex")
        if droid_hooks.exists():
            clis_to_uninstall.append("droid")

        if not clis_to_uninstall:
            click.echo("No Gobby hooks found to uninstall.")
            if project_flag:
                click.echo(f"\nChecked: {project_path / '.claude'}")
                click.echo(f"         {project_path / '.gemini'}")
                click.echo(f"         {project_path / '.qwen'}")
                click.echo(f"         {project_path / '.codex'}")
                click.echo(f"         {project_path / '.factory'}")
            else:
                click.echo(f"\nChecked: {Path.home() / '.claude'}")
                click.echo(f"         {Path.home() / '.gemini'}")
                click.echo(f"         {Path.home() / '.qwen'}")
                click.echo(f"         {Path.home() / '.codex'}")
                click.echo(f"         {Path.home() / '.factory'}")
            sys.exit(0)
    else:
        if claude_flag:
            clis_to_uninstall.append("claude")
        if gemini_flag:
            clis_to_uninstall.append("gemini")
        if qwen_flag:
            clis_to_uninstall.append("qwen")
        if codex_flag:
            clis_to_uninstall.append("codex")
        if droid_flag:
            clis_to_uninstall.append("droid")

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Uninstallation")
    click.echo("=" * 60)
    if project_flag:
        click.echo(f"\nScope: Project ({project_path})")
    else:
        click.echo("\nScope: Global")
    click.echo(f"CLIs to uninstall from: {', '.join(clis_to_uninstall)}")
    click.echo("")

    # For global uninstall, use Path.home() so uninstallers find ~/.{cli}/
    uninstall_base = project_path if project_flag else Path.home()

    # Track results
    results: dict[str, dict[str, Any]] = {}

    # Standard CLIs (claude, gemini, qwen, codex, droid)
    _standard_uninstallers: dict[str, Callable[..., dict[str, Any]]] = {
        "claude": uninstall_claude,
        "gemini": uninstall_gemini,
        "qwen": uninstall_qwen,
        "codex": uninstall_codex,
        "droid": uninstall_droid,
    }
    for cli_name, uninstaller_fn in _standard_uninstallers.items():
        if cli_name in clis_to_uninstall:
            uninstall_kwargs: dict[str, Any] = {}
            if cli_name in {"qwen", "droid"}:
                uninstall_kwargs["mode"] = "project" if project_flag else "global"
            _run_standard_cli_uninstall(
                cli_name,
                uninstaller_fn,
                uninstall_base,
                results,
                **uninstall_kwargs,
            )

    # Remove global hooks directory for global uninstall
    if not project_flag and all_flag:
        global_hooks_dir = Path(
            os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks"))
        )
        for fname in ("hook_dispatcher.py", "validate_settings.py"):
            fpath = global_hooks_dir / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                except OSError as e:
                    click.echo(f"  Warning: could not remove {fpath}: {e}", err=True)
        click.echo("Removed global hook dispatchers from ~/.gobby/hooks/")
        click.echo("")

    # FalkorDB
    if falkordb_flag:
        _run_falkordb_uninstall(uninstall_falkordb, volumes_flag, results)

    # Summary
    all_success = _echo_uninstall_summary(results)
    if not all_success:
        sys.exit(1)
