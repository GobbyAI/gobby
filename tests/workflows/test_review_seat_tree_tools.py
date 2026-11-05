"""Review seats cannot remove an author's worktree or clone, under any bundled definition."""

from typing import Any

import pytest
import yaml

from gobby.paths import get_install_dir

pytestmark = pytest.mark.unit

# Each tool deletes a tree or clone, and with it the author's uncommitted or unmerged work.
DESTRUCTIVE_TREE_TOOLS = frozenset(
    {
        "gobby-worktrees:delete_worktree",
        "gobby-worktrees:cleanup_stale_worktrees",
        "gobby-clones:delete_clone",
        "gobby-clones:cleanup_stale_clones",
    }
)


def _review_seat_definitions() -> list[tuple[str, dict[str, Any]]]:
    """Bundled agent definitions that write independent-review attestations."""
    agents_dir = get_install_dir() / "shared" / "workflows" / "agents"
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(agents_dir.glob("*.yaml")):
        data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        writes = data.get("task_attestation_writes")
        if isinstance(writes, list) and "independent-review" in writes:
            found.append((path.name, data))
    return found


def test_bundled_review_seats_block_tree_and_clone_deletion() -> None:
    seats = _review_seat_definitions()
    assert seats, "no bundled definition writes independent-review attestations"
    for name, data in seats:
        blocked = set(data.get("blocked_mcp_tools") or [])
        missing = DESTRUCTIVE_TREE_TOOLS - blocked
        assert not missing, f"{name} leaves tree or clone deletion callable: {sorted(missing)}"
