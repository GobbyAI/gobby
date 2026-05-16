"""Pre-approve workspace trust for CLI agents and installer-owned paths.

When spawning agents in clone or worktree directories, CLIs show interactive
trust prompts that block headless execution. This module pre-approves
directories so those prompts never appear. During install, it also seeds trust
for the configured Gobby home so Gobby-owned files are trusted before agents
start using them.

Each CLI has a different trust mechanism:
- Claude Code: ~/.claude/projects/<encoded-path>/ (directory existence = trust)
- Gemini/Qwen CLI: ~/.gemini|.qwen/trustedFolders.json + projects.json
- Codex CLI: ~/.codex/config.toml [projects."<path>"] trust_level = "trusted"
- Droid CLI: --auto high handles spawned-agent permissions, no trust database needed
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomlkit
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_CLAUDE_COMPATIBLE_CLIS = frozenset({"claude"})
_GEMINI_COMPATIBLE_CLIS = frozenset({"gemini", "qwen"})
_MODEL_DISCOVERY_TRUST_LOCKS: dict[str, asyncio.Lock] = {}
_LOCK_DICT_LOCK = asyncio.Lock()
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

type PathValue = str | os.PathLike[str]
type TrustEntryStatus = Literal["created", "updated", "existing", "skipped", "error"]


@dataclass
class TrustSeedResult:
    """Structured result returned by installer trust setup."""

    cli: str
    paths: list[str]
    entries: list[dict[str, str]] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str | None = None

    @property
    def success(self) -> bool:
        """Return whether all attempted trust store updates succeeded."""
        return not self.errors

    def add_entry(
        self,
        *,
        store: str,
        target: str,
        status: TrustEntryStatus,
        message: str | None = None,
    ) -> None:
        """Record a trust store entry operation."""
        entry = {"store": store, "target": target, "status": status}
        if message:
            entry["message"] = message
        self.entries.append(entry)

    def add_file_written(self, path: Path) -> None:
        """Record a file write without duplicating paths."""
        path_text = os.fspath(path)
        if path_text not in self.files_written:
            self.files_written.append(path_text)

    def add_error(self, message: str) -> None:
        """Record an error for installer output."""
        self.errors.append(message)

    def as_dict(self) -> dict[str, Any]:
        """Serialize to plain data for installer result dictionaries."""
        return {
            "cli": self.cli,
            "success": self.success,
            "paths": self.paths,
            "entries": self.entries,
            "files_written": self.files_written,
            "errors": self.errors,
            "skipped": self.skipped,
            "reason": self.reason,
        }


def _encode_claude_project_path(directory: PathValue) -> str:
    """Encode a directory path into Claude's project directory name.

    Claude Code replaces path separators and dots with dashes. Gobby also
    normalizes Windows drive colons and backslashes so seeded trust is
    deterministic across platforms.

    Example: /Users/josh/.gobby/clones/foo -> -Users-josh--gobby-clones-foo
    """
    path = os.fspath(directory)
    return path.replace("/", "-").replace("\\", "-").replace(":", "-").replace(".", "-")


def pre_approve_directory(cli: str, directory: PathValue) -> None:
    """Pre-approve a directory for the given CLI so trust prompts are skipped.

    Resolves symlinks (e.g. /tmp -> /private/tmp on macOS) to match what
    the CLI sees at runtime, and creates trust entries for both the original
    and resolved paths to cover all cases.

    Args:
        cli: CLI name (claude, gemini, qwen, codex, droid)
        directory: Absolute path to the workspace directory
    """
    if cli == "codex":
        logger.debug(
            "Codex runtime workspace trust pre-approval is a no-op for %s; "
            "install-time Gobby home trust is seeded by `gobby install`.",
            os.fspath(directory),
        )
        return

    seed_cli_trust(cli, directory)


def seed_gobby_home_trust(cli: str, gobby_home: PathValue | None = None) -> dict[str, Any]:
    """Seed install-time trust for the configured Gobby home directory.

    The Gobby home defaults to ``$GOBBY_HOME`` or ``Path.home() / ".gobby"``.
    Trust is seeded for the configured path and its resolved real path when
    they differ.
    """
    home = gobby_home if gobby_home is not None else get_gobby_home()
    return seed_cli_trust(cli, home, respect_folder_trust_setting=True).as_dict()


async def authorize_model_discovery_trust(cli: str, directory: PathValue) -> TrustSeedResult:
    """Authorize provider-owned ACP model discovery paths.

    Model discovery only needs Gemini-compatible trust stores. Runtime workspace
    trust stays on ``pre_approve_directory`` so these authorization paths remain
    separate.
    """
    if cli not in _GEMINI_COMPATIBLE_CLIS:
        result = TrustSeedResult(cli=cli, paths=_trust_path_strings(directory))
        result.skipped = True
        supported = ", ".join(sorted(_GEMINI_COMPATIBLE_CLIS))
        result.reason = (
            f"Unsupported CLI for model discovery trust: {cli}; supported CLIs: {supported}"
        )
        return result

    async with _LOCK_DICT_LOCK:
        lock = _MODEL_DISCOVERY_TRUST_LOCKS.setdefault(cli, asyncio.Lock())
        await lock.acquire()

    try:
        return await asyncio.to_thread(
            seed_cli_trust,
            cli,
            directory,
            respect_folder_trust_setting=True,
        )
    finally:
        lock.release()


def seed_cli_trust(
    cli: str,
    directory: PathValue,
    *,
    respect_folder_trust_setting: bool = False,
) -> TrustSeedResult:
    """Seed trust for a CLI and return structured details about the writes."""
    paths = _trust_path_strings(directory)
    result = TrustSeedResult(cli=cli, paths=paths)

    try:
        if cli in _CLAUDE_COMPATIBLE_CLIS:
            _seed_claude_trust(paths, result)
        elif cli in _GEMINI_COMPATIBLE_CLIS:
            _seed_gemini_compatible_trust(
                cli,
                paths,
                result,
                respect_folder_trust_setting=respect_folder_trust_setting,
            )
        elif cli == "codex":
            _seed_codex_trust(paths, result)
        elif cli == "droid":
            result.skipped = True
            result.reason = (
                "Droid has no documented trusted-folder store; spawned agents use "
                "`droid exec --auto high` for permission handling."
            )
            logger.debug(
                "Droid workspace trust pre-approval is a no-op for %s; spawned "
                "agents use `droid exec --auto high` for permission handling.",
                ", ".join(paths),
            )
        else:
            result.skipped = True
            result.reason = f"Unsupported CLI for trust seeding: {cli}"
    except Exception as exc:
        message = f"Failed to seed {cli} trust for {', '.join(paths)}: {exc}"
        result.add_error(message)
        logger.warning(message, exc_info=True)

    return result


def _trust_path_strings(directory: PathValue) -> list[str]:
    """Return configured and resolved path strings, preserving order."""
    configured = os.path.expanduser(os.fspath(directory))
    resolved = _realpath(configured)

    paths: list[str] = []
    for path in (configured, resolved):
        if path not in paths:
            paths.append(path)
    return paths


def _realpath(path: str) -> str:
    """Resolve symlinks without mangling Windows absolute paths on POSIX."""
    if os.name != "nt" and _is_windows_absolute_path(path):
        return path
    return os.path.realpath(path)


def _is_windows_absolute_path(path: str) -> bool:
    """Return whether a string is a Windows absolute path."""
    return bool(_WINDOWS_ABSOLUTE_RE.match(path)) or path.startswith("\\\\")


def _path_name(path: str) -> str:
    """Return a final path component for POSIX and Windows-style strings."""
    trimmed = path.rstrip("/\\")
    if not trimmed:
        return path
    return re.split(r"[/\\]", trimmed)[-1]


def _seed_claude_trust(paths: list[str], result: TrustSeedResult) -> None:
    """Seed Claude Code project-directory trust for all paths."""
    for path in paths:
        _pre_approve_claude(path, result=result)


def _pre_approve_claude(directory: PathValue, result: TrustSeedResult | None = None) -> None:
    """Pre-approve a directory for Claude Code.

    Creates the project directory under ~/.claude/projects/ if it doesn't
    exist. Claude treats directory existence as implicit trust.
    """
    directory_text = os.fspath(directory)
    claude_home = Path.home() / ".claude" / "projects"
    encoded = _encode_claude_project_path(directory_text)
    project_dir = claude_home / encoded

    if project_dir.exists():
        if result is not None:
            result.add_entry(
                store="claude_projects",
                target=os.fspath(project_dir),
                status="existing",
            )
        return

    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        if result is not None:
            result.add_entry(
                store="claude_projects",
                target=os.fspath(project_dir),
                status="created",
            )
        logger.info("Pre-approved Claude workspace trust for %s", directory_text)
    except OSError as exc:
        message = f"Failed to pre-approve Claude trust for {directory_text}: {exc}"
        if result is not None:
            result.add_error(message)
            result.add_entry(
                store="claude_projects",
                target=os.fspath(project_dir),
                status="error",
                message=str(exc),
            )
        logger.warning(message, exc_info=True)


def _seed_gemini_compatible_trust(
    cli: str,
    paths: list[str],
    result: TrustSeedResult,
    *,
    respect_folder_trust_setting: bool = False,
) -> None:
    """Pre-approve paths for a Gemini-compatible CLI."""
    cli_home = Path.home() / f".{cli}"
    cli_home.mkdir(parents=True, exist_ok=True)

    try:
        _seed_gemini_projects(cli, cli_home, paths, result)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        message = f"Failed to update {cli.title()} projects.json for {', '.join(paths)}: {exc}"
        result.add_error(message)
        logger.warning(message, exc_info=True)

    if respect_folder_trust_setting and not _folder_trust_enabled(cli_home / "settings.json"):
        for path in paths:
            result.add_entry(
                store="trusted_folders",
                target=path,
                status="skipped",
                message="security.folderTrust is disabled",
            )
        return

    try:
        _seed_gemini_trusted_folders(cli, cli_home, paths, result)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        message = (
            f"Failed to update {cli.title()} trustedFolders.json for {', '.join(paths)}: {exc}"
        )
        result.add_error(message)
        logger.warning(message, exc_info=True)


def _pre_approve_gemini_compatible(cli: str, directory: PathValue) -> None:
    """Backward-compatible wrapper for runtime Gemini/Qwen workspace trust."""
    result = TrustSeedResult(cli=cli, paths=_trust_path_strings(directory))
    _seed_gemini_compatible_trust(
        cli,
        result.paths,
        result,
        respect_folder_trust_setting=False,
    )


def _seed_gemini_projects(
    cli: str,
    cli_home: Path,
    paths: list[str],
    result: TrustSeedResult,
) -> None:
    """Register Gemini/Qwen project paths in projects.json."""
    projects_file = cli_home / "projects.json"
    data = _load_json_object(projects_file, reset_label=f"{cli.title()} projects.json")

    projects_raw = data.get("projects")
    if isinstance(projects_raw, dict):
        projects = projects_raw
    else:
        if projects_raw is not None:
            logger.warning("%s projects field is not a dict, resetting", cli.title())
        projects = {}
        data["projects"] = projects

    changed = False
    for path in paths:
        if path in projects:
            result.add_entry(store="projects_json", target=path, status="existing")
            continue

        projects[path] = _path_name(path)
        result.add_entry(store="projects_json", target=path, status="created")
        changed = True

    if changed:
        _atomic_write_json(projects_file, data)
        result.add_file_written(projects_file)


def _seed_gemini_trusted_folders(
    cli: str,
    cli_home: Path,
    paths: list[str],
    result: TrustSeedResult,
) -> None:
    """Register Gemini/Qwen TRUST_PARENT paths in trustedFolders.json."""
    trust_file = cli_home / "trustedFolders.json"
    trusted = _load_json_object(trust_file, reset_label=f"{cli.title()} trustedFolders.json")

    changed = False
    for path in paths:
        current = trusted.get(path)
        if current == "TRUST_PARENT":
            result.add_entry(store="trusted_folders", target=path, status="existing")
            continue

        status: TrustEntryStatus = "created" if current is None else "updated"
        trusted[path] = "TRUST_PARENT"
        result.add_entry(store="trusted_folders", target=path, status=status)
        changed = True
        logger.info("Pre-approved %s folder trust for %s", cli.title(), path)

    if changed:
        _atomic_write_json(trust_file, trusted)
        result.add_file_written(trust_file)


def _folder_trust_enabled(settings_file: Path) -> bool:
    """Return whether Gemini/Qwen folder trust is active in settings."""
    if not settings_file.exists():
        return True

    settings = _load_json_object(settings_file, reset_label=settings_file.name)
    security = settings.get("security")
    if isinstance(security, dict) and security.get("folderTrust") is False:
        return False
    return True


def _seed_codex_trust(paths: list[str], result: TrustSeedResult) -> None:
    """Seed Codex [projects] trust entries in ~/.codex/config.toml."""
    codex_home = Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / "config.toml"

    if config_path.exists():
        config = _load_toml_config(config_path.read_text(encoding="utf-8"))
    else:
        config = tomlkit.document()

    projects = config.get("projects")
    if not isinstance(projects, Table):
        projects = tomlkit.table()
        config["projects"] = projects

    changed = False
    for path in paths:
        entry = projects.get(path)
        if isinstance(entry, Table):
            project_config = entry
        else:
            project_config = tomlkit.table()

        if project_config.get("trust_level") == "trusted":
            result.add_entry(store="codex_projects", target=path, status="existing")
            continue

        status: TrustEntryStatus = "created" if entry is None else "updated"
        project_config["trust_level"] = "trusted"
        projects[path] = project_config
        result.add_entry(store="codex_projects", target=path, status=status)
        changed = True

    if changed:
        _atomic_write_text(config_path, tomlkit.dumps(config))
        result.add_file_written(config_path)


def _load_toml_config(content: str) -> TOMLDocument:
    """Parse TOML content into a mutable document."""
    if not content.strip():
        return tomlkit.document()
    parsed = tomlkit.parse(content)
    return parsed


def _load_json_object(path: Path, *, reset_label: str) -> dict[str, Any]:
    """Load a JSON object, resetting non-object roots to an empty object."""
    if not path.exists():
        return {}

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data

    logger.warning("%s root is not a dict, resetting: %s", reset_label, path)
    return {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically with fsync and same-directory rename."""
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically with fsync and same-directory rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=os.fspath(path.parent),
        suffix=".tmp",
        prefix=f"{path.stem}_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
