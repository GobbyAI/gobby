"""
CLI installation modules for Gobby hooks.

This package contains per-CLI installation logic extracted from the main install.py
using the strangler fig pattern for incremental migration.
"""

from .agy import install_agy, uninstall_agy
from .claude import install_claude, uninstall_claude
from .codex import install_codex, uninstall_codex
from .droid import install_droid, uninstall_droid
from .embedding import install_embedding
from .falkor import install_falkordb
from .git_hooks import install_git_hooks
from .grok import install_grok, uninstall_grok
from .postgres import install_postgres
from .qdrant import install_qdrant
from .qwen import install_qwen, uninstall_qwen
from .service import get_service_status, install_service, uninstall_service
from .shared import (
    clean_project_hooks,
    install_cli_content,
    install_global_hooks,
    install_shared_content,
)

__all__ = [
    # Shared
    "clean_project_hooks",
    "install_shared_content",
    "install_cli_content",
    "install_global_hooks",
    # AGY
    "install_agy",
    "uninstall_agy",
    # Claude
    "install_claude",
    "uninstall_claude",
    # Grok
    "install_grok",
    "uninstall_grok",
    # Qwen
    "install_qwen",
    "uninstall_qwen",
    # Droid
    "install_droid",
    "uninstall_droid",
    # Codex
    "install_codex",
    "uninstall_codex",
    # Git Hooks
    "install_git_hooks",
    # Embedding
    "install_embedding",
    # FalkorDB
    "install_falkordb",
    # Qdrant
    "install_qdrant",
    # PostgreSQL
    "install_postgres",
    # Service (OS-level daemon)
    "install_service",
    "uninstall_service",
    "get_service_status",
]
