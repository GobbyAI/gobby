"""
Git hooks installation for Gobby automation.

This module handles installing git hooks for verification, code indexing,
and JSONL backup export.

Features:
- Backs up existing hooks before modification
- Chains with existing hooks (doesn't overwrite)
- Integrates with pre-commit framework when available
- Supports clean uninstallation
"""

import logging
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from .wiki_branch_setup import default_wiki_setup_result, setup_wiki_branch

logger = logging.getLogger(__name__)

# Markers for identifying Gobby hook sections
GOBBY_HOOK_START = "# >>> GOBBY HOOK START >>>"
GOBBY_HOOK_END = "# <<< GOBBY HOOK END <<<"

_CODE_INDEX_REINDEX_BODY = r"""
if [ -n "$CHANGED_FILES" ]; then
    GCODE="$HOME/.gobby/bin/gcode"
    if [ -x "$GCODE" ]; then
        (
            echo "$CHANGED_FILES" | tr '\n' '\0' | xargs -0 "$GCODE" index --quiet --skip-if-locked --files >/dev/null 2>&1
        ) &
    fi
fi
"""


def _code_index_reindex_hook(event_name: str, changed_files_script: str) -> str:
    return (
        f"# Gobby incremental code indexing after {event_name}\n"
        f"{changed_files_script.strip()}\n"
        f"{_CODE_INDEX_REINDEX_BODY.strip()}"
    )


# Hook script templates - these get wrapped with markers
HOOK_TEMPLATES = {
    "pre-commit": r"""
# Gobby smart pre-commit wrapper
# - Runs gobby verification commands (if configured)
# - Runs pre-commit framework if available
# - Auto-commits formatting fixes separately
# - Task/memory JSONL sync moved to pre-push

# Run Gobby verification commands for pre-commit stage
if command -v gobby >/dev/null 2>&1; then
    gobby hooks run pre-commit 2>/dev/null
    GOBBY_EXIT=$?
    if [ $GOBBY_EXIT -ne 0 ]; then
        echo "Gobby pre-commit verification failed"
        exit $GOBBY_EXIT
    fi
fi

# Record which files have unstaged changes before pre-commit runs
UNSTAGED_BEFORE=$(git diff --name-only 2>/dev/null | sort)

# Run pre-commit if available and config exists
if command -v pre-commit >/dev/null 2>&1 && [ -f .pre-commit-config.yaml ]; then
    pre-commit run --hook-stage pre-commit
    PRECOMMIT_EXIT=$?

    if [ $PRECOMMIT_EXIT -ne 0 ]; then
        # Check if files were auto-fixed (new unstaged changes appeared)
        UNSTAGED_AFTER=$(git diff --name-only 2>/dev/null | sort)

        if [ "$UNSTAGED_BEFORE" != "$UNSTAGED_AFTER" ]; then
            # Find files that were auto-fixed (newly unstaged)
            AUTO_FIXED=$(comm -13 <(echo "$UNSTAGED_BEFORE") <(echo "$UNSTAGED_AFTER") 2>/dev/null)

            if [ -n "$AUTO_FIXED" ]; then
                echo ""
                echo "Pre-commit auto-fixed files. Creating separate commit..."

                # Stage only the auto-fixed files (handle filenames with spaces/special chars)
                echo "$AUTO_FIXED" | while IFS= read -r file; do
                    [ -n "$file" ] && git add -- "$file"
                done

                # Commit them with --no-verify to skip hooks
                git commit --no-verify -m "style: auto-format (pre-commit)" >/dev/null

                echo "Auto-format committed. Please run 'git commit' again for your changes."
                exit 1
            fi
        fi

        # Pre-commit failed for other reasons
        exit $PRECOMMIT_EXIT
    fi
fi
""",
    "pre-push": """
# Gobby verification runner for pre-push
# Runs configured verification commands (type_check, unit_tests, security, etc.)

# Capture pre-push refs once so delete checks, verification, and wiki publishing
# all use the same stdin payload.
PUSH_REFS=$(cat)
ZERO_SHA="0000000000000000000000000000000000000000"

DELETE_ONLY=false
WIKI_ONLY=false
if [ -n "$PUSH_REFS" ]; then
    DELETE_ONLY=true
    WIKI_ONLY=true
    while read -r local_ref local_sha remote_ref remote_sha; do
        if [ -z "$local_ref" ]; then
            continue
        fi

        if [ "$local_sha" != "$ZERO_SHA" ]; then
            DELETE_ONLY=false
        fi

        branch="${local_ref#refs/heads/}"
        if [ "$branch" = "$local_ref" ]; then
            branch="${remote_ref#refs/heads/}"
        fi
        if [ "$branch" != "wiki" ]; then
            WIKI_ONLY=false
        fi
    done <<< "$PUSH_REFS"
fi
if [ "$DELETE_ONLY" = true ] || [ "$WIKI_ONLY" = true ]; then
    exit 0
fi

REMOTE_NAME="${1:-origin}"
DEFAULT_BRANCH=$(
    git symbolic-ref --quiet --short "refs/remotes/${REMOTE_NAME}/HEAD" 2>/dev/null \
        | sed "s#^${REMOTE_NAME}/##"
)
if [ -z "$DEFAULT_BRANCH" ]; then
    if git show-ref --verify --quiet refs/heads/main \
        || git show-ref --verify --quiet "refs/remotes/${REMOTE_NAME}/main"; then
        DEFAULT_BRANCH="main"
    elif git show-ref --verify --quiet refs/heads/master \
        || git show-ref --verify --quiet "refs/remotes/${REMOTE_NAME}/master"; then
        DEFAULT_BRANCH="master"
    else
        DEFAULT_BRANCH=$(git branch --show-current 2>/dev/null || true)
    fi
fi

PUBLISH_WIKI=false
if [ -n "$DEFAULT_BRANCH" ] && [ "$DEFAULT_BRANCH" != "wiki" ] && [ -n "$PUSH_REFS" ]; then
    while read -r local_ref local_sha remote_ref remote_sha; do
        if [ -z "$local_ref" ] || [ "$local_sha" = "$ZERO_SHA" ]; then
            continue
        fi

        branch="${local_ref#refs/heads/}"
        if [ "$branch" = "$local_ref" ]; then
            branch="${remote_ref#refs/heads/}"
        fi
        if [ "$branch" = "$DEFAULT_BRANCH" ]; then
            PUBLISH_WIKI=true
            break
        fi
    done <<< "$PUSH_REFS"
fi

# Gobby backup — snapshot tasks and memories outside the repository before push
# Skip for spawned agents to avoid JSONL contamination in worktrees
if [ -z "$GOBBY_AGENT_RUN_ID" ] && command -v gobby >/dev/null 2>&1; then
    gobby tasks backup --quiet 2>/dev/null || true
    gobby memory backup --quiet 2>/dev/null || true
fi

if command -v gobby >/dev/null 2>&1; then
    gobby hooks run pre-push 2>/dev/null
    GOBBY_EXIT=$?
    if [ $GOBBY_EXIT -ne 0 ]; then
        echo "Gobby pre-push verification failed"
        exit $GOBBY_EXIT
    fi
fi

if [ "$PUBLISH_WIKI" = true ]; then
    REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)

    WIKI_VAULT=""
    if [ -n "$REPO_ROOT" ] && command -v gobby >/dev/null 2>&1; then
        WIKI_VAULT=$(gobby hooks resolve-wiki-vault "$REPO_ROOT" 2>/dev/null || true)
    fi

    if [ -n "$WIKI_VAULT" ]; then
        REPO_NAME=$(basename "$REPO_ROOT")
        REPO_PARENT=$(cd "$REPO_ROOT/.." && pwd)
        WIKI_WORKTREE="$REPO_PARENT/${REPO_NAME}-wiki"

        if [ ! -d "$WIKI_WORKTREE" ]; then
            echo "gobby: wiki publish skipped; missing worktree at $WIKI_WORKTREE" >&2
            echo "gobby: run 'gobby install git-hooks' from $REPO_ROOT to configure it." >&2
        elif ! git -C "$WIKI_WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            echo "gobby: wiki publish skipped; $WIKI_WORKTREE is not a Git worktree" >&2
        elif [ "$(git -C "$WIKI_WORKTREE" branch --show-current 2>/dev/null)" != "wiki" ]; then
            echo "gobby: wiki publish skipped; $WIKI_WORKTREE is not on branch wiki" >&2
        else
            find "$WIKI_WORKTREE" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
            if command -v rsync >/dev/null 2>&1; then
                rsync -a --delete --exclude .git "$WIKI_VAULT"/ "$WIKI_WORKTREE"/
            else
                (cd "$WIKI_VAULT" && tar --exclude .git -cf - .) \
                    | (cd "$WIKI_WORKTREE" && tar -xf -)
            fi

            git -C "$WIKI_WORKTREE" add -A
            if ! git -C "$WIKI_WORKTREE" diff --cached --quiet --exit-code; then
                if ! git -C "$WIKI_WORKTREE" commit -m "gobby: sync wiki vault" --no-verify; then
                    echo "gobby: wiki publish warning; failed to commit wiki vault" >&2
                fi
            fi

            if ! git -C "$WIKI_WORKTREE" push "$REMOTE_NAME" wiki; then
                echo "gobby: wiki publish warning; failed to push branch wiki" >&2
            fi
        fi
    fi
fi
""",
    "pre-merge-commit": """
# Gobby verification runner for pre-merge-commit
# Skip when Gobby itself is performing a merge (e.g. merge_clone)
if [ "$GOBBY_MERGE" = "1" ]; then
    exit 0
fi
# Runs configured verification commands (code_review, integration tests, etc.)
if command -v gobby >/dev/null 2>&1; then
    gobby hooks run pre-merge
    GOBBY_EXIT=$?
    if [ $GOBBY_EXIT -ne 0 ]; then
        echo "Gobby pre-merge-commit verification failed"
        exit $GOBBY_EXIT
    fi
fi
""",
    "post-commit": _code_index_reindex_hook(
        "commit",
        r"""
CHANGED_FILES=$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null)
""",
    ),
    "post-checkout": _code_index_reindex_hook(
        "checkout",
        r"""
if [ "$3" = "1" ] && [ -n "$1" ] && [ -n "$2" ]; then
    if git rev-parse -q --verify "$1^{commit}" >/dev/null 2>&1; then
        CHANGED_FILES=$(git diff --name-only "$1" "$2" 2>/dev/null)
    else
        CHANGED_FILES=$(git diff-tree --no-commit-id --name-only -r "$2" 2>/dev/null)
    fi
else
    CHANGED_FILES=
fi
""",
    ),
    "post-merge": _code_index_reindex_hook(
        "merge",
        r"""
if git rev-parse -q --verify ORIG_HEAD >/dev/null 2>&1; then
    WORKTREE_CHANGED=$(git diff --name-only HEAD 2>/dev/null)
    if [ -n "$WORKTREE_CHANGED" ]; then
        CHANGED_FILES="$WORKTREE_CHANGED"
    elif [ "$(git rev-parse ORIG_HEAD 2>/dev/null)" != "$(git rev-parse HEAD 2>/dev/null)" ]; then
        CHANGED_FILES=$(git diff --name-only ORIG_HEAD HEAD 2>/dev/null)
    else
        CHANGED_FILES=$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null)
    fi
else
    CHANGED_FILES=$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null)
fi
""",
    ),
    "post-rewrite": _code_index_reindex_hook(
        "rewrite",
        r"""
CHANGED_FILES=$(
    while read -r OLD_REV NEW_REV; do
        if [ -n "$OLD_REV" ] && [ -n "$NEW_REV" ]; then
            git diff --name-only "$OLD_REV" "$NEW_REV" 2>/dev/null
        fi
    done | sort -u
)
""",
    ),
}


def _resolve_git_hooks_dir(project_path: Path) -> Path | None:
    """Resolve hooks dir for normal repos and linked git worktrees."""
    git_entry = project_path / ".git"
    if git_entry.is_dir():
        return git_entry / "hooks"
    if not git_entry.is_file():
        return None

    try:
        content = git_entry.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not content.startswith("gitdir:"):
        return None

    git_dir = Path(content.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (project_path / git_dir).resolve()

    common_dir_file = git_dir / "commondir"
    if common_dir_file.exists():
        try:
            common_dir = Path(common_dir_file.read_text(encoding="utf-8").strip())
        except OSError:
            common_dir = git_dir
        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
        return common_dir / "hooks"

    return git_dir / "hooks"


def _backup_hook(hook_path: Path, hooks_dir: Path) -> str | None:
    """Create a timestamped backup of an existing hook.

    Args:
        hook_path: Path to the hook file
        hooks_dir: Directory containing hooks

    Returns:
        Backup path if created, None otherwise
    """
    if not hook_path.exists():
        return None

    timestamp = int(time.time())
    backup_path = hooks_dir / f"{hook_path.name}.{timestamp}.backup"

    try:
        shutil.copy2(hook_path, backup_path)
        logger.debug("Backed up %s to %s", hook_path.name, backup_path.name)
        return str(backup_path)
    except OSError as e:
        logger.warning("Failed to backup %s: %s", hook_path.name, e)
        return None


def _has_gobby_hook(content: str) -> bool:
    """Check if content already contains Gobby hook markers."""
    return GOBBY_HOOK_START in content


def _is_precommit_framework_hook(content: str) -> bool:
    """Check if this is a hook generated by the pre-commit framework."""
    return "File generated by pre-commit" in content or "pre_commit" in content


def _wrap_gobby_section(script: str) -> str:
    """Wrap a script section with Gobby markers."""
    return f"{GOBBY_HOOK_START}\n{script.strip()}\n{GOBBY_HOOK_END}\n"


def _extract_gobby_section(content: str) -> str | None:
    """Return the complete managed Gobby hook section, if present."""
    start = content.find(GOBBY_HOOK_START)
    if start == -1:
        return None

    end = content.find(GOBBY_HOOK_END, start)
    if end == -1:
        return None

    end += len(GOBBY_HOOK_END)
    return content[start:end].strip()


def _replace_gobby_section(content: str, gobby_section: str) -> str:
    """Replace the complete managed Gobby hook section in place."""
    start = content.find(GOBBY_HOOK_START)
    if start == -1:
        return content

    end = content.find(GOBBY_HOOK_END, start)
    if end == -1:
        return content

    end += len(GOBBY_HOOK_END)
    line_end = content.find("\n", end)
    replace_end = len(content) if line_end == -1 else line_end + 1
    return f"{content[:start]}{gobby_section}{content[replace_end:]}"


def _clean_hook_content(content: str) -> str:
    """Normalize blank lines after removing managed hook content."""
    cleaned = content
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")

    return cleaned.strip() + "\n" if cleaned.strip() else ""


def _remove_gobby_section(content: str) -> str:
    """Remove Gobby hook section from content."""
    lines = content.split("\n")
    result = []
    in_gobby_section = False

    for line in lines:
        if GOBBY_HOOK_START in line:
            in_gobby_section = True
            continue
        if GOBBY_HOOK_END in line:
            in_gobby_section = False
            continue
        if not in_gobby_section:
            result.append(line)

    return _clean_hook_content("\n".join(result))


def _check_precommit_installed() -> bool:
    """Check if pre-commit framework is installed and configured."""
    return shutil.which("pre-commit") is not None


def _has_precommit_config(project_path: Path) -> bool:
    """Check if project has a .pre-commit-config.yaml."""
    return (project_path / ".pre-commit-config.yaml").exists()


def install_git_hooks(
    project_path: Path,
    *,
    force: bool = False,
    setup_precommit: bool = True,
) -> dict[str, Any]:
    """Install Gobby git hooks to the current repository.

    Safely installs hooks by:
    1. Backing up existing hooks
    2. Chaining with existing hooks (appending Gobby section)
    3. Optionally setting up pre-commit framework

    Args:
        project_path: Path to the project root
        force: If True, reinstall even if already present
        setup_precommit: If True, run `pre-commit install` if config exists

    Returns:
        Dict with installation results including:
        - success: bool
        - installed: list of installed hook names
        - skipped: list of skipped hooks with reasons
        - backups: list of backup file paths
        - precommit_installed: bool if pre-commit was set up
        - error: error message if failed
    """
    result: dict[str, Any] = {
        "success": False,
        "installed": [],
        "skipped": [],
        "backups": [],
        "precommit_installed": False,
        "wiki_setup": default_wiki_setup_result(),
        "error": None,
    }

    hooks_dir = _resolve_git_hooks_dir(project_path)
    if hooks_dir is None:
        result["error"] = "Not a git repository (no .git directory found)"
        return result

    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Install each hook
    for hook_name, gobby_script in HOOK_TEMPLATES.items():
        hook_path = hooks_dir / hook_name
        gobby_section = _wrap_gobby_section(gobby_script)

        if hook_path.exists():
            content = hook_path.read_text()
            existing_gobby_section = _extract_gobby_section(content)

            # Check if already installed
            if existing_gobby_section == gobby_section.strip() and not force:
                result["skipped"].append(f"{hook_name} (already installed)")
                continue

            # Backup existing hook
            backup_path = _backup_hook(hook_path, hooks_dir)
            if backup_path:
                result["backups"].append(backup_path)

            # If this is a pre-commit framework hook for pre-commit stage,
            # replace it entirely with our wrapper (which calls pre-commit)
            if (
                hook_name == "pre-commit"
                and existing_gobby_section is None
                and _is_precommit_framework_hook(content)
            ):
                new_content = f"#!/usr/bin/env bash\n\n{gobby_section}"
                hook_path.write_text(new_content)
                logger.info("Replaced pre-commit framework hook with Gobby wrapper")
            elif existing_gobby_section is not None:
                new_content = _replace_gobby_section(content, gobby_section)
                hook_path.write_text(new_content)
                logger.info("Refreshed Gobby hook section in existing %s", hook_name)
            else:
                # Append Gobby section to existing hook
                if content.strip():
                    # Ensure shebang is preserved at top
                    if content.startswith("#!"):
                        lines = content.split("\n", 1)
                        shebang = lines[0]
                        rest = lines[1] if len(lines) > 1 else ""
                        new_content = f"{shebang}\n\n{gobby_section}\n{rest.strip()}\n"
                    else:
                        new_content = f"#!/usr/bin/env bash\n\n{gobby_section}\n{content}"
                else:
                    new_content = f"#!/usr/bin/env bash\n\n{gobby_section}"

                hook_path.write_text(new_content)
                logger.info("Appended Gobby hook to existing %s", hook_name)

        else:
            # Create new hook (use bash for pre-commit process substitution)
            new_content = f"#!/usr/bin/env bash\n\n{gobby_section}"
            hook_path.write_text(new_content)
            logger.info("Created new %s hook", hook_name)

        # Ensure executable
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        result["installed"].append(hook_name)

    # Note: We intentionally DON'T run `pre-commit install` here.
    # Our smart pre-commit hook wrapper calls `pre-commit run` directly,
    # which allows us to handle auto-fixes by creating separate commits.
    # Running `pre-commit install` would overwrite our wrapper.
    #
    # We also don't run `pre-commit install --hook-type pre-push` because
    # our pre-push hook now runs gobby verification commands first, and
    # the pre-commit framework's hook would overwrite ours.
    if setup_precommit and _has_precommit_config(project_path) and _check_precommit_installed():
        result["precommit_installed"] = True
        logger.info(
            "Pre-commit detected - gobby hooks will run verification first, then pre-commit framework"
        )

    result["wiki_setup"] = setup_wiki_branch(project_path)
    result["success"] = True
    return result


def uninstall_git_hooks(project_path: Path) -> dict[str, Any]:
    """Remove Gobby sections from git hooks.

    Safely removes only Gobby-added sections, preserving other hook functionality.

    Args:
        project_path: Path to the project root

    Returns:
        Dict with uninstallation results
    """
    result: dict[str, Any] = {
        "success": False,
        "removed": [],
        "not_found": [],
        "error": None,
    }

    hooks_dir = _resolve_git_hooks_dir(project_path)
    if hooks_dir is None:
        result["error"] = "Not a git repository"
        return result

    if not hooks_dir.exists():
        result["success"] = True
        return result

    for hook_name in HOOK_TEMPLATES:
        hook_path = hooks_dir / hook_name

        if not hook_path.exists():
            result["not_found"].append(hook_name)
            continue

        content = hook_path.read_text()

        if not _has_gobby_hook(content):
            result["not_found"].append(hook_name)
            continue

        # Remove Gobby section
        new_content = _remove_gobby_section(content)

        if new_content.strip():
            # Hook still has content, keep it
            hook_path.write_text(new_content)
        else:
            # Hook is now empty, remove it
            hook_path.unlink()

        result["removed"].append(hook_name)
        logger.info("Removed Gobby section from %s", hook_name)

    result["success"] = True
    return result
