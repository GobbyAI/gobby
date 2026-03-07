"""
Git hooks installation for Gobby task sync.

This module handles installing git hooks for automatic task
synchronization on commit, merge, and checkout operations.

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

logger = logging.getLogger(__name__)

# Markers for identifying Gobby hook sections
GOBBY_HOOK_START = "# >>> GOBBY HOOK START >>>"
GOBBY_HOOK_END = "# <<< GOBBY HOOK END <<<"

# Hook script templates - these get wrapped with markers
HOOK_TEMPLATES = {
    "pre-commit": r"""
# Gobby smart pre-commit wrapper
# - Runs gobby verification commands (if configured)
# - Runs pre-commit framework if available
# - Auto-commits formatting fixes separately
# - Syncs tasks before commit

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

# Gobby task sync - export tasks before commit
if command -v gobby >/dev/null 2>&1; then
    gobby tasks sync --export --quiet 2>/dev/null || true
    # Stage task file if modified by export
    if git diff --name-only 2>/dev/null | grep -q "\.gobby/tasks.jsonl"; then
        git add .gobby/tasks.jsonl 2>/dev/null || true
    fi
fi
""",
    "pre-push": """
# Gobby verification runner for pre-push
# Runs configured verification commands (type_check, unit_tests, security, etc.)

# Skip verification for branch deletions (no code being pushed)
DELETE_ONLY=true
while read local_ref local_sha remote_ref remote_sha; do
    if [ "$local_sha" != "0000000000000000000000000000000000000000" ]; then
        DELETE_ONLY=false
        break
    fi
done
if [ "$DELETE_ONLY" = true ]; then
    exit 0
fi

if command -v gobby >/dev/null 2>&1; then
    gobby hooks run pre-push 2>/dev/null
    GOBBY_EXIT=$?
    if [ $GOBBY_EXIT -ne 0 ]; then
        echo "Gobby pre-push verification failed"
        exit $GOBBY_EXIT
    fi
fi
""",
    "pre-merge-commit": """
# Gobby verification runner for pre-merge-commit
# Runs configured verification commands (code_review, integration tests, etc.)
if command -v gobby >/dev/null 2>&1; then
    gobby hooks run pre-merge-commit 2>/dev/null
    GOBBY_EXIT=$?
    if [ $GOBBY_EXIT -ne 0 ]; then
        echo "Gobby pre-merge-commit verification failed"
        exit $GOBBY_EXIT
    fi
fi
""",
    "post-merge": """
# Gobby task sync - import tasks after merge/pull
if command -v gobby >/dev/null 2>&1; then
    gobby tasks sync --import --quiet 2>/dev/null || true
fi
""",
    "post-checkout": """
# Gobby task sync - import tasks on branch switch
# $3 is 1 if this was a branch checkout (vs file checkout)
if [ "$3" = "1" ]; then
    if command -v gobby >/dev/null 2>&1; then
        gobby tasks sync --import --quiet 2>/dev/null || true
    fi
fi
""",
    "post-commit": """
# Gobby task sync + incremental code indexing after commit
if command -v gobby >/dev/null 2>&1; then
    gobby tasks sync --export --quiet 2>/dev/null || true

    # Incremental code index: send changed files to daemon
    CHANGED_FILES=$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null)
    if [ -n "$CHANGED_FILES" ]; then
        PROJECT_ID=""
        if [ -f .gobby/project.json ]; then
            PROJECT_ID=$(python3 -c "import json; print(json.load(open('.gobby/project.json')).get('project_id',''))" 2>/dev/null || echo "")
        fi
        if [ -n "$PROJECT_ID" ]; then
            FILES_JSON=$(echo "$CHANGED_FILES" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip().split('\\n')))" 2>/dev/null)
            if [ -n "$FILES_JSON" ]; then
                curl -s -X POST "http://localhost:60887/api/code-index/incremental" \
                    -H "Content-Type: application/json" \
                    -d "{\\\"project_id\\\": \\\"$PROJECT_ID\\\", \\\"files\\\": $FILES_JSON}" \
                    >/dev/null 2>&1 || true
            fi
        fi
    fi
fi
""",
}


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
        logger.debug(f"Backed up {hook_path.name} to {backup_path.name}")
        return str(backup_path)
    except OSError as e:
        logger.warning(f"Failed to backup {hook_path.name}: {e}")
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

    # Clean up multiple blank lines
    cleaned = "\n".join(result)
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")

    return cleaned.strip() + "\n" if cleaned.strip() else ""


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
        "error": None,
    }

    git_dir = project_path / ".git"
    if not git_dir.exists():
        result["error"] = "Not a git repository (no .git directory found)"
        return result

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Install each hook
    for hook_name, gobby_script in HOOK_TEMPLATES.items():
        hook_path = hooks_dir / hook_name
        gobby_section = _wrap_gobby_section(gobby_script)

        if hook_path.exists():
            content = hook_path.read_text()

            # Check if already installed
            if _has_gobby_hook(content) and not force:
                result["skipped"].append(f"{hook_name} (already installed)")
                continue

            # Backup existing hook
            backup_path = _backup_hook(hook_path, hooks_dir)
            if backup_path:
                result["backups"].append(backup_path)

            # If this is a pre-commit framework hook for pre-commit stage,
            # replace it entirely with our wrapper (which calls pre-commit)
            if hook_name == "pre-commit" and _is_precommit_framework_hook(content):
                new_content = f"#!/usr/bin/env bash\n\n{gobby_section}"
                hook_path.write_text(new_content)
                logger.info("Replaced pre-commit framework hook with Gobby wrapper")
            else:
                # Remove old Gobby section if force reinstalling
                if force and GOBBY_HOOK_START in content:
                    content = _remove_gobby_section(content)

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
                logger.info(f"Appended Gobby hook to existing {hook_name}")

        else:
            # Create new hook (use bash for pre-commit process substitution)
            new_content = f"#!/usr/bin/env bash\n\n{gobby_section}"
            hook_path.write_text(new_content)
            logger.info(f"Created new {hook_name} hook")

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

    git_dir = project_path / ".git"
    if not git_dir.exists():
        result["error"] = "Not a git repository"
        return result

    hooks_dir = git_dir / "hooks"
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
        logger.info(f"Removed Gobby section from {hook_name}")

    result["success"] = True
    return result
