"""Pre-approve workspace trust for CLI agents and installer-owned paths.

When spawning agents in clone or worktree directories, CLIs show interactive
trust prompts that block headless execution. This module pre-approves
directories so those prompts never appear. During install, it also seeds trust
for the configured Gobby home so Gobby-owned files are trusted before agents
start using them.

Each CLI has a different trust mechanism:
- Claude Code: projects[<path>].hasTrustDialogAccepted in the active global JSON config
- Codex CLI: ~/.codex/config.toml [projects."<path>"] trust_level = "trusted"
- Droid CLI: --auto high handles spawned-agent permissions, no trust database needed
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import psycopg
import tomlkit
import yaml
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from gobby.claude_config import claude_config_home, claude_config_path, update_claude_config
from gobby.paths import get_gobby_home

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

_CLAUDE_COMPATIBLE_CLIS = frozenset({"claude"})
_TRUST_STORE_LOCKS: dict[str, threading.Lock] = {}
_TRUST_STORE_LOCKS_LOCK = threading.Lock()
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
# (table, key, value) entries that Codex writes to config.toml on its first interactive
# launch. Seeding them before launch is meant to leave that launch nothing to rewrite;
# the live steward run is the check. The NUX value is a Codex exposure count, and Codex
# rewrites the file until the count reaches 4. The key names one catalog-announced model:
# a future announcement adds a key that Codex rewrites once. No supported switch is known.
_CODEX_FIRST_RUN_TUI: tuple[tuple[str, str, Any], ...] = (
    ("tui", "screen_reader_detection_done", True),
)

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


def encode_claude_project_path(directory: PathValue) -> str:
    """Encode a directory path into Claude's project directory name.

    Claude Code replaces every character outside ASCII letters and digits
    with a dash, including separators, punctuation, spaces, and Unicode.

    Example: /Users/josh/.gobby/clones/foo -> -Users-josh--gobby-clones-foo
    """
    path = os.fspath(directory)
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def pre_approve_directory(cli: str, directory: PathValue, codex_models: Iterable[str] = ()) -> None:
    """Pre-approve a directory for the given CLI so trust prompts are skipped.

    Resolves symlinks (e.g. /tmp -> /private/tmp on macOS) to match what
    the CLI sees at runtime, and creates trust entries for both the original
    and resolved paths to cover all cases.

    Args:
        cli: CLI name (claude, codex, droid)
        directory: Absolute path to the workspace directory
        codex_models: Codex model ids from the active project policy, added to the
            packaged defaults when seeding Codex first-run acknowledgements
    """
    seed_cli_trust(cli, directory, codex_models)


def seed_gobby_home_trust(
    cli: str, gobby_home: PathValue | None = None, codex_models: Iterable[str] = ()
) -> dict[str, Any]:
    """Seed install-time trust for the configured Gobby home directory.

    The Gobby home defaults to ``$GOBBY_HOME`` or ``Path.home() / ".gobby"``.
    Trust is seeded for the configured path and its resolved real path when
    they differ.
    """
    home = gobby_home if gobby_home is not None else get_gobby_home()
    return seed_cli_trust(cli, home, codex_models).as_dict()


def seed_cli_trust(
    cli: str, directory: PathValue, codex_models: Iterable[str] = ()
) -> TrustSeedResult:
    """Seed trust for a CLI and return structured details about the writes."""
    with _TRUST_STORE_LOCKS_LOCK:
        lock = _TRUST_STORE_LOCKS.setdefault(cli, threading.Lock())

    with lock:
        return _seed_cli_trust_unlocked(cli, directory, codex_models)


def _seed_cli_trust_unlocked(
    cli: str, directory: PathValue, codex_models: Iterable[str]
) -> TrustSeedResult:
    """Seed trust for a CLI. Caller must hold the per-CLI trust-store lock."""
    paths = _trust_path_strings(directory)
    result = TrustSeedResult(cli=cli, paths=paths)

    try:
        if cli in _CLAUDE_COMPATIBLE_CLIS:
            _seed_claude_trust(paths, result)
        elif cli == "codex":
            _seed_codex_trust(paths, result, codex_models)
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


def _seed_claude_trust(paths: list[str], result: TrustSeedResult) -> None:
    """Seed Claude Code persisted workspace trust for all paths."""
    for path in paths:
        _pre_approve_claude(path, result=result)


def _pre_approve_claude(directory: PathValue, result: TrustSeedResult | None = None) -> None:
    """Pre-approve a directory for Claude Code.

    Persist hasTrustDialogAccepted in the global config Claude reads. Transcript
    directory existence does not imply trust on current Claude versions.
    """
    directory_text = os.fspath(directory)
    claude_home = claude_config_home()
    config_path = claude_config_path()
    encoded = encode_claude_project_path(directory_text)
    project_dir = claude_home / "projects" / encoded

    try:

        def approve(config: dict[str, Any]) -> bool:
            projects = config.setdefault("projects", {})
            if not isinstance(projects, dict):
                raise ValueError("Claude projects configuration must be an object")
            project = projects.setdefault(directory_text, {})
            if not isinstance(project, dict):
                raise ValueError("Claude workspace configuration must be an object")
            if project.get("hasTrustDialogAccepted") is True:
                return False
            project["hasTrustDialogAccepted"] = True
            return True

        changed = update_claude_config(config_path, approve)
        if changed:
            if result is not None:
                result.add_file_written(config_path)

        existing = project_dir.exists()
        project_dir.mkdir(parents=True, exist_ok=True)
        if result is not None:
            result.add_entry(
                store="claude_config",
                target=directory_text,
                status="updated" if changed else "existing",
            )
            result.add_entry(
                store="claude_projects",
                target=os.fspath(project_dir),
                status="existing" if existing else "created",
            )
        logger.info("Pre-approved Claude workspace trust for %s", directory_text)
    except (OSError, ValueError) as exc:
        message = f"Failed to pre-approve Claude trust for {directory_text}: {exc}"
        if result is not None:
            result.add_error(message)
            result.add_entry(
                store="claude_config",
                target=os.fspath(config_path),
                status="error",
                message=str(exc),
            )
        logger.warning(message, exc_info=True)


def _seed_codex_trust(
    paths: list[str], result: TrustSeedResult, codex_models: Iterable[str]
) -> None:
    """Seed Codex [projects] trust and first-run [tui] keys in ~/.codex/config.toml."""
    codex_home = Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / "config.toml"
    content = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    parsed = tomllib.loads(content) if content.strip() else {}
    raw_projects = parsed.get("projects")
    projects: Mapping[str, Any] = raw_projects if isinstance(raw_projects, Mapping) else {}
    pending: list[tuple[str, TrustEntryStatus]] = []
    for path in paths:
        entry = projects.get(path)
        if isinstance(entry, Mapping) and entry.get("trust_level") == "trusted":
            result.add_entry(store="codex_projects", target=path, status="existing")
            continue
        pending.append((path, "created" if entry is None else "updated"))
    tui_pending = _codex_first_run_tui_pending(parsed, codex_models)

    if not pending and not tui_pending:
        return

    if pending:
        content = _prune_stale_codex_projects(content, projects, keep_paths=set(paths))
    direct_append = (
        all(status == "created" for _, status in pending)
        and not re.search(r"(?m)^\s*projects\s*=", content)
        and ("tui" not in parsed or not tui_pending)
    )
    if direct_append:
        for path, status in pending:
            content = _append_codex_trust_entry(content, path)
            result.add_entry(store="codex_projects", target=path, status=status)
        if tui_pending:
            content = _append_codex_tui_block(content, tui_pending)
        tomllib.loads(content)
        _atomic_write_text(config_path, content)
        result.add_file_written(config_path)
        return

    config = _load_toml_config(content)
    if pending:
        mutable_projects = config.get("projects")
        if not isinstance(mutable_projects, Table):
            # An inline ``projects = {...}`` keeps its entries when it becomes a table.
            replacement = tomlkit.table()
            if isinstance(mutable_projects, Mapping):
                replacement.update(mutable_projects)
            mutable_projects = replacement
            config["projects"] = mutable_projects

        for path, status in pending:
            entry = mutable_projects.get(path)
            project_config = entry if isinstance(entry, Table) else tomlkit.table()
            project_config["trust_level"] = "trusted"
            mutable_projects[path] = project_config
            result.add_entry(store="codex_projects", target=path, status=status)

    _set_codex_tui_entries(config, tui_pending)
    _atomic_write_text(config_path, tomlkit.dumps(config))
    result.add_file_written(config_path)


_PROJECT_POLICY_RULE = "project-operating-policy"


def codex_model_ids_in_policy(defaults: Any) -> set[str]:
    """Return the Codex model ids named anywhere under one launch-defaults tree.

    A ``model`` or ``*_model`` value counts when its ``*_provider`` sibling is ``codex``,
    or when no provider is set and the id starts with ``gpt-``.
    """
    models: set[str] = set()
    if not isinstance(defaults, Mapping):
        return models
    for key, value in defaults.items():
        if isinstance(value, Mapping):
            models |= codex_model_ids_in_policy(value)
        elif (key == "model" or str(key).endswith("_model")) and isinstance(value, str):
            value = value.strip()
            if not value or value == "inherit":
                continue
            provider = defaults.get(str(key).removesuffix("model") + "provider")
            if provider == "codex" or (provider in (None, "inherit") and value.startswith("gpt-")):
                models.add(value)
    return models


@cache
def packaged_codex_launch_models() -> frozenset[str]:
    """Read the static package seed baseline once per process.

    Install and workspace seeding reuse this baseline. Spawn and resume union
    it with the DB-active project policy, which is read live on each call.
    The packaged launch snapshot is outside the rule templates and never syncs
    into the DB. Package upgrades take effect when the process restarts.
    """
    from gobby.agents.sync import get_bundled_agents_path

    models: set[str] = set()
    defaults = files("gobby").joinpath("data/codex_launch_defaults.json")
    try:
        models.update(codex_model_ids_in_policy(json.loads(defaults.read_text(encoding="utf-8"))))
    except (OSError, ValueError):
        logger.warning("Could not read packaged Codex launch defaults", exc_info=True)
    for path in get_bundled_agents_path().glob("*.yaml"):
        try:
            definition = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            logger.warning("Could not read bundled agent defaults from %s", path, exc_info=True)
            continue
        models.update(codex_model_ids_in_policy(definition))
    return frozenset(models)


def active_codex_launch_models(db: HubDatabase | None, project_id: str | None) -> tuple[str, ...]:
    """Return the Codex model ids in the DB-active project operating policy.

    Reads the ``project-operating-policy`` rule row and walks each ``project_policy``
    effect value, so a live policy change is seeded without a checkout edit. Returns
    an empty tuple when there is no database or the row cannot be read: a database
    error, or a malformed row, must not block a spawn.
    """
    from gobby.storage.definitions.rules import RuleDefinitionManager

    if db is None:
        return ()
    try:
        rule = RuleDefinitionManager(db).get_by_name(_PROJECT_POLICY_RULE, project_id)
    except (psycopg.Error, ValueError):
        logger.warning(
            "Active project policy unreadable; using packaged Codex defaults", exc_info=True
        )
        return ()
    if rule is None:
        return ()
    models: set[str] = set()
    for effect in rule.definition_json.get("effects") or []:
        if isinstance(effect, Mapping) and effect.get("variable") == "project_policy":
            models |= codex_model_ids_in_policy(effect.get("value"))
    return tuple(sorted(models))


def _codex_first_run_tui_pending(
    parsed: Mapping[str, Any], codex_models: Iterable[str]
) -> list[tuple[str, str, Any]]:
    """Return the first-run ``[tui]`` entries that ``parsed`` lacks or must raise.

    A present key keeps its value, the operator's own record, except an exposure count
    below its seed: Codex would rewrite the file again on the next launch.
    """
    pending: list[tuple[str, str, Any]] = []
    nux_entries = [
        ("tui.model_availability_nux", model, 4)
        for model in sorted(packaged_codex_launch_models().union(codex_models))
    ]
    for table, key, value in (*_CODEX_FIRST_RUN_TUI, *nux_entries):
        section: Any = parsed
        for part in table.split("."):
            section = section.get(part) if isinstance(section, Mapping) else None
        if not (isinstance(section, Mapping) and key in section):
            pending.append((table, key, value))
        elif _exposure_count_below(section[key], value):
            pending.append((table, key, value))
    return pending


def _exposure_count_below(present: Any, seed: Any) -> bool:
    """Return True for an integer exposure count below an integer seed.

    Booleans are excluded on both sides, so a present flag never counts as a count.
    """
    return (
        isinstance(seed, int)
        and not isinstance(seed, bool)
        and isinstance(present, int)
        and not isinstance(present, bool)
        and present < seed
    )


def _append_codex_tui_block(content: str, pending: list[tuple[str, str, Any]]) -> str:
    blocks: list[str] = []
    for table in dict.fromkeys(table for table, _, _ in pending):
        entries = "".join(
            f"{json.dumps(key)} = {json.dumps(value)}\n"
            for entry_table, key, value in pending
            if entry_table == table
        )
        blocks.append(f"[{table}]\n{entries}")
    prefix = f"{content.rstrip()}\n\n" if content.strip() else ""
    return prefix + "\n".join(blocks)


def ensure_codex_project_trust(config: TOMLDocument, directory: PathValue) -> None:
    """Trust ``directory``'s Codex project paths in a parsed config, in place.

    The document-level counterpart of ``_seed_codex_trust``: the caller writes the
    config, so trust rides in that write instead of a second text write. Like the
    text path, stale generated projects are pruned only when a path is pending, and
    the first-run [tui] keys ride in the same write.
    """
    paths = _trust_path_strings(directory)
    raw_projects = config.get("projects")
    existing: Mapping[str, Any] = raw_projects if isinstance(raw_projects, Mapping) else {}
    pending: list[str] = []
    for path in paths:
        entry = existing.get(path)
        if not (isinstance(entry, Mapping) and entry.get("trust_level") == "trusted"):
            pending.append(path)
    tui_pending = _codex_first_run_tui_pending(config, ())
    if not pending and not tui_pending:
        return

    if pending:
        projects = raw_projects if isinstance(raw_projects, Table) else tomlkit.table()
        if projects is not raw_projects:
            # An inline ``projects = {...}`` keeps its entries when it becomes a table.
            projects.update(existing)
            config["projects"] = projects
        for path in list(projects):
            if path not in paths and is_stale_generated_path(path):
                del projects[path]
        for path in pending:
            entry = projects.get(path)
            project_config = entry if isinstance(entry, Table) else tomlkit.table()
            project_config["trust_level"] = "trusted"
            projects[path] = project_config
    _set_codex_tui_entries(config, tui_pending)


def _set_codex_tui_entries(config: Any, pending: list[tuple[str, str, Any]]) -> None:
    for table, key, value in pending:
        section: Any = config
        for part in table.split("."):
            child = section.get(part)
            if child is None:
                child = tomlkit.table()
                section[part] = child
            section = child
        section[key] = value


def _append_codex_trust_entry(content: str, path: str) -> str:
    prefix = f"{content.rstrip()}\n\n" if content.strip() else ""
    quoted_path = json.dumps(path, ensure_ascii=False)
    return f'{prefix}[projects.{quoted_path}]\ntrust_level = "trusted"\n'


def _prune_stale_codex_projects(
    content: str,
    projects: Mapping[str, Any],
    *,
    keep_paths: set[str],
) -> str:
    stale_paths = {
        path for path in projects if path not in keep_paths and is_stale_generated_path(path)
    }
    if not stale_paths:
        return content

    stale_headers = {f"[projects.{json.dumps(path, ensure_ascii=False)}]" for path in stale_paths}
    retained: list[str] = []
    skipping = False
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped in stale_headers:
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            retained.append(line)
    return "".join(retained)


def is_stale_generated_path(path: str) -> bool:
    """Return whether ``path`` is a deleted Gobby-generated workspace path.

    Generated paths live under ``~/.gobby/worktrees``, ``~/.gobby/clones``, the
    process temp dir, or a ``gobby-*`` directory directly under ``/tmp``. A root
    that is itself missing (unmounted or not yet created) marks nothing stale.
    """
    if _is_windows_absolute_path(path) or Path(path).exists():
        return False
    resolved = _realpath(path)
    gobby_home = Path.home() / ".gobby"
    roots = (
        _realpath(str(gobby_home / "worktrees")),
        _realpath(str(gobby_home / "clones")),
        _realpath(tempfile.gettempdir()),
    )
    if any(_path_is_within(resolved, root) and os.path.isdir(root) for root in roots):
        return True
    for tmp_root in {_realpath("/tmp"), "/private/tmp"}:
        if _path_is_within(resolved, tmp_root) and resolved != tmp_root:
            first = os.path.relpath(resolved, tmp_root).split(os.sep, 1)[0]
            if first.startswith("gobby-"):
                return True
    return False


def _path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _load_toml_config(content: str) -> TOMLDocument:
    """Parse TOML content into a mutable document."""
    if not content.strip():
        return tomlkit.document()
    parsed = tomlkit.parse(content)
    return parsed


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
