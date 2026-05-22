"""
CLI installation modules for Gobby hooks.

This package contains per-CLI installation logic extracted from the main install.py
using the strangler fig pattern for incremental migration.
"""

from .claude import install_claude, uninstall_claude
from .codex import install_codex, install_codex_notify, uninstall_codex, uninstall_codex_notify
from .droid import install_droid, uninstall_droid
from .embedding import install_embedding
from .falkor import install_falkordb, uninstall_falkordb
from .gemini import install_gemini, uninstall_gemini
from .git_hooks import install_git_hooks
from .mcp_config import install_default_mcp_servers
from .neo4j import install_neo4j, uninstall_neo4j
from .postgres import install_postgres, uninstall_postgres
from .qdrant import install_qdrant, uninstall_qdrant
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
    "install_default_mcp_servers",
    # Claude
    "install_claude",
    "uninstall_claude",
    # Gemini
    "install_gemini",
    "uninstall_gemini",
    # Qwen
    "install_qwen",
    "uninstall_qwen",
    # Droid
    "install_droid",
    "uninstall_droid",
    # Codex
    "install_codex",
    "uninstall_codex",
    "install_codex_notify",  # backward-compat alias
    "uninstall_codex_notify",  # backward-compat alias
    # Git Hooks
    "install_git_hooks",
    # Embedding
    "install_embedding",
    # Neo4j
    "install_neo4j",
    "uninstall_neo4j",
    # FalkorDB
    "install_falkordb",
    "uninstall_falkordb",
    # Qdrant
    "install_qdrant",
    "uninstall_qdrant",
    # PostgreSQL
    "install_postgres",
    "uninstall_postgres",
    # Service (OS-level daemon)
    "install_service",
    "uninstall_service",
    "get_service_status",
]
