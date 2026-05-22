"""FalkorDB installer helpers shared by service status checks."""

from pathlib import Path


def _resolve_falkordb_db_path(gobby_home: Path) -> Path:
    """Resolve the config-store DB path for a FalkorDB install scoped to a home."""
    bootstrap_path = gobby_home / "bootstrap.yaml"
    if not bootstrap_path.exists():
        return gobby_home / "gobby-hub.db"

    from gobby.config.bootstrap import load_bootstrap

    bootstrap = load_bootstrap(str(bootstrap_path))
    return Path(bootstrap.database_path).expanduser()
