"""Default bundled MCP server installation."""

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

import psycopg

from gobby.config.mcp import DEFAULT_MCP_CONFIG_PATH

from .mcp_config_shared import _facade_attr, _facade_logger

DEFAULT_MCP_SERVERS: list[dict[str, Any]] = [
    {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "$secret:github_personal_access_token"},
        "description": "GitHub API integration for issues, PRs, repos, and code search",
    },
    {
        "name": "linear",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-linear"],
        "env": {"LINEAR_API_KEY": "$secret:linear_api_key"},
        "description": "Linear issue tracking integration",
    },
    {
        "name": "brave-search",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@brave/brave-search-mcp-server"],
        "env": {"BRAVE_API_KEY": "$secret:brave_api_key"},
        "description": "Brave Search API for web search, local search, and news",
    },
    {
        "name": "context7",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "optional_secret_args": {"context7_api_key": ["--api-key"]},
        "description": (
            "Context7 library documentation lookup (set context7_api_key secret for private repos)"
        ),
    },
    {
        "name": "playwright",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "description": "Playwright MCP for browser automation and inspection",
    },
    {
        "name": "chrome-devtools",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "chrome-devtools-mcp@0.21.0", "--no-usage-statistics"],
        "description": "Chrome DevTools MCP for browser debugging and automation",
    },
]


def _default_mcp_servers() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _facade_attr("DEFAULT_MCP_SERVERS", DEFAULT_MCP_SERVERS))


def _default_mcp_config_path() -> str:
    return cast(str, _facade_attr("DEFAULT_MCP_CONFIG_PATH", DEFAULT_MCP_CONFIG_PATH))


def install_default_mcp_servers() -> dict[str, Any]:
    """Install default external MCP servers to ~/.gobby/mcp-servers.json.

    Adds bundled external MCP servers if not already configured. Also syncs to
    the database so the daemon proxy can serve them. These servers pull API
    keys from environment variables where applicable.

    Returns:
        Dict with 'success', 'servers_added', 'servers_skipped', and 'error' keys
    """
    logger = _facade_logger()
    result: dict[str, Any] = {
        "success": False,
        "servers_added": [],
        "servers_skipped": [],
        "error": None,
    }

    mcp_config_path = Path(_default_mcp_config_path()).expanduser()

    # Ensure parent directory exists
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config or create empty
    existing_config: dict[str, Any] = {"servers": []}
    if mcp_config_path.exists():
        try:
            with open(mcp_config_path) as f:
                content = f.read()
                if content.strip():
                    existing_config = json.loads(content)
                    if "servers" not in existing_config:
                        existing_config["servers"] = []
        except (json.JSONDecodeError, OSError) as e:
            result["error"] = f"Failed to read MCP config: {e}"
            return result

    # Get existing server names
    existing_names = {s.get("name") for s in existing_config["servers"]}

    # Repair misconfigured servers: reconcile canonical fields with defaults
    default_servers = _default_mcp_servers()
    servers_repaired: list[str] = []
    default_by_name = {s["name"]: s for s in default_servers}

    def existing_args_have_secret_refs(args: Any) -> bool:
        return isinstance(args, list) and any(
            isinstance(arg, str) and "$secret:" in arg for arg in args
        )

    for existing_server in existing_config["servers"]:
        name = existing_server.get("name")
        default = default_by_name.get(name) if name else None
        if not default:
            continue
        repaired = False
        canonical_fields = {
            "transport": default.get("transport"),
            "command": default.get("command"),
            "url": default.get("url"),
            "args": list(default.get("args") or []) or None,
            "env": dict(default["env"]) if "env" in default else None,
        }
        for field, canonical_value in canonical_fields.items():
            if field == "args" and existing_args_have_secret_refs(existing_server.get("args")):
                continue
            if canonical_value is None:
                if field in existing_server:
                    existing_server.pop(field, None)
                    repaired = True
            elif existing_server.get(field) != canonical_value:
                existing_server[field] = canonical_value
                repaired = True
        if repaired:
            servers_repaired.append(name)
    result["servers_repaired"] = servers_repaired

    # Resolve optional_secret_args via secret store (lazy init)
    secret_store = None
    secret_store_db = None
    secret_store_stack = ExitStack()
    secret_store_init_failed = False

    # Add default servers if not already present
    try:
        for server in default_servers:
            if server["name"] in existing_names:
                result["servers_skipped"].append(server["name"])
            else:
                # Build args list, adding optional secret-dependent args
                args = list(server.get("args") or [])
                optional_secret_args = server.get("optional_secret_args", {})
                for secret_name, extra_args in optional_secret_args.items():
                    if secret_store is None and not secret_store_init_failed:
                        try:
                            from gobby.storage.hub.runtime import runtime_hub_database
                            from gobby.storage.secrets import SecretStore

                            secret_store_db = secret_store_stack.enter_context(
                                runtime_hub_database(apply_migrations=False)
                            )
                            secret_store = SecretStore(secret_store_db)
                        except (ImportError, OSError, RuntimeError, psycopg.Error):
                            secret_store_init_failed = True
                            logger.warning(
                                "Failed to initialize secret store for optional MCP args",
                                exc_info=True,
                            )
                        except Exception:
                            # Unexpected failures should keep their traceback; expected optional
                            # secret-store errors are handled above and allow installation to continue.
                            logger.exception(
                                "Unexpected error initializing secret store for optional MCP args"
                            )
                            raise
                    if secret_store is not None:
                        try:
                            if secret_store.exists(secret_name):
                                args.extend(extra_args + [f"$secret:{secret_name}"])
                        except (OSError, RuntimeError, psycopg.Error):
                            logger.warning(
                                "Failed to read optional MCP secret",
                                exc_info=True,
                            )
                        except Exception:
                            # Unexpected secret lookup failures are logged with traceback before
                            # re-raising; expected storage errors are handled above.
                            logger.exception("Unexpected error reading optional MCP secret")
                            raise

                existing_config["servers"].append(
                    {
                        "name": server["name"],
                        "enabled": True,
                        "transport": server["transport"],
                        "command": server.get("command"),
                        "args": args if args else None,
                        "env": server.get("env"),
                        "description": server.get("description"),
                    }
                )
                result["servers_added"].append(server["name"])
    finally:
        secret_store_stack.close()

    # Write updated config if any servers were added or repaired
    if result["servers_added"] or servers_repaired:
        try:
            with open(mcp_config_path, "w") as f:
                json.dump(existing_config, f, indent=2)
            # Set restrictive permissions
            mcp_config_path.chmod(0o600)
        except OSError as e:
            result["error"] = f"Failed to write MCP config: {e}"
            return result

    if servers_repaired:
        logger.info("Repaired MCP server configs: %s", ", ".join(servers_repaired))

    # Sync .mcp.json to database so the daemon proxy can serve them
    try:
        from gobby.storage.hub.runtime import runtime_hub_database
        from gobby.storage.mcp import LocalMCPManager
        from gobby.storage.projects import GLOBAL_PROJECT_ID

        with runtime_hub_database(apply_migrations=False) as db:
            mcp_db = LocalMCPManager(db)
            imported = mcp_db.import_from_mcp_json(mcp_config_path, project_id=GLOBAL_PROJECT_ID)
            if imported:
                logger.info("Synced %s MCP servers to database", imported)
    except Exception as e:
        logger.warning("Failed to sync MCP servers to database: %s", e)

    result["success"] = True
    return result
