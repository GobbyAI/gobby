"""Hub-local one-shot files_home migrate campaign."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from gobby.config.bootstrap import load_bootstrap
from gobby.paths import (
    FilesHomeNotOnThisDaemonError,
    assert_held_files_home_identity,
    ensure_files_home_descendant_dir,
    files_home_root_fd,
    get_gobby_home,
    publish_files_home_descendant,
    require_files_home,
    unlink_files_home_descendant,
)
from gobby.runner_pid_file import claim_pid_file
from gobby.storage.projects import PERSONAL_PROJECT_ID, ensure_personal_project_identity

_HANDLED_PERSONAL = frozenset({"USER.md", ".gobby", "wiki", "attachments"})
_RESERVED_TOPICS = frozenset({"personal", "_personal", "wiki"})
_CLASS_ORDER = (
    "profile",
    "personal_marker",
    "personal_wiki",
    "leftover",
    "topics",
    "attachments",
    "registry",
)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC


class FilesMigrateError(Exception):
    """Typed migrate refusal or publication failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class FilesMigratePartialError(FilesMigrateError):
    """Recognized partial after at least one class published."""

    def __init__(self, message: str, *, published: tuple[str, ...] = ()) -> None:
        super().__init__("partial", message)
        self.published = published


@dataclass
class FilesMigrateHooks:
    """Test injection points. Production CLI leaves this empty."""

    after_class: str | None = None
    before_registry_publish: bool = False
    before_scope_rewrite: bool = False
    before_locator_rewrite: bool = False
    force_exdev: bool = False
    fail_exdev_verify: bool = False
    fail_exdev_truncate: bool = False
    on_claimed: Callable[[object], None] | None = None
    swap_after_class: str | None = None
    swap_fn: Callable[[], None] | None = None
    before_first_publish: Callable[[], None] | None = None
    db: object | None = None


@dataclass(frozen=True)
class FilesMigrateReport:
    status: str
    published: tuple[str, ...]
    skipped: tuple[str, ...]
    seeded: tuple[str, ...]
    rewritten_attachments: int
    unrewritten_attachments: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "published": list(self.published),
            "skipped": list(self.skipped),
            "seeded": list(self.seeded),
            "rewritten_attachments": self.rewritten_attachments,
            "unrewritten_attachments": list(self.unrewritten_attachments),
        }


@dataclass
class _Pair:
    class_id: str
    source: Path
    dest_rel: Path
    retire_only: bool = False


@dataclass
class _Held:
    path: Path
    fd: int
    identity: tuple[int, int]


def special_file_reason(stat_result: os.stat_result) -> str | None:
    """Return a refusal token for a symlink, FIFO, socket, device, or hard link."""
    mode = stat_result.st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    if stat.S_ISREG(mode) and stat_result.st_nlink > 1:
        return "nlink"
    if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
        return "special"
    return None


def validate_topic_name(name: str) -> str:
    value = name.strip()
    invalid = (
        not value
        or value in {".", ".."}
        or any(char in value for char in {":", "/", "\\"})
        or any(ord(char) < 32 for char in value)
        or value in _RESERVED_TOPICS
    )
    if invalid:
        raise FilesMigrateError("invalid_topic", f"invalid or reserved topic name `{value}`")
    return value


def run_files_migrate(*, hooks: FilesMigrateHooks | None = None) -> FilesMigrateReport:
    """Discover, preflight, publish, then seed still-missing baseline children."""
    hooks = hooks or FilesMigrateHooks()
    config = load_bootstrap()
    if config.datastore_mode == "remote":
        raise FilesHomeNotOnThisDaemonError("files_home is not on this remote-mode daemon")
    claim = claim_pid_file(get_gobby_home() / "gobby.pid", role="maintenance")
    if claim is None:
        raise FilesMigrateError(
            "daemon_running",
            "cannot migrate while the hub daemon or another maintenance campaign is running",
        )
    held: list[_Held] = []
    published: list[str] = []
    skipped: list[str] = []
    seeded: list[str] = []
    rewritten = 0
    unrewritten: list[str] = []
    try:
        if hooks.on_claimed is not None:
            hooks.on_claimed(claim)
        files_home = require_files_home()
        _refuse_filesystem_root(files_home)
        pairs = _discover(files_home)
        _refuse_overlap(files_home, [pair.source for pair in pairs])
        _parse_registry_if_present(pairs, files_home)
        held = _prewalk([pair.source for pair in pairs if pair.source.exists()])
        _classify_destinations(files_home, pairs)
        _assert_dest_graph(pairs)
        _inspect_existing_dest(files_home, pairs)
        if hooks.before_first_publish is not None:
            hooks.before_first_publish()
        _assert_identities(held, allow_partial=False)
        assert_held_files_home_identity()
        for class_id in _CLASS_ORDER:
            _maybe_swap(hooks, class_id, published, held)
            class_pairs = [pair for pair in pairs if pair.class_id == class_id]
            if class_id == "registry" and class_pairs and hooks.before_registry_publish:
                raise FilesMigratePartialError(
                    "injected crash before registry publication",
                    published=tuple(published),
                )
            for pair in class_pairs:
                if pair.retire_only:
                    _retire_source(pair.source)
                    skipped.append(f"{class_id}:{pair.dest_rel}")
                    continue
                if not pair.source.exists():
                    skipped.append(f"{class_id}:{pair.dest_rel}")
                    continue
                dest = files_home / pair.dest_rel
                if class_id == "registry":
                    _publish_registry(pair, files_home)
                    published.append(f"{class_id}:{pair.dest_rel}")
                    continue
                if dest.exists():
                    if _same_payload(pair.source, dest):
                        _retire_source(pair.source)
                        skipped.append(f"{class_id}:{pair.dest_rel}")
                        continue
                    raise FilesMigrateError(
                        "divergent",
                        f"divergent both-present pair {pair.source} -> {pair.dest_rel}",
                    )
                _publish_pair(pair, hooks)
                published.append(f"{class_id}:{pair.dest_rel}")
            if class_id == "attachments":
                rewritten, unrewritten = _rewrite_locators(hooks, files_home)
            if hooks.after_class == class_id:
                raise FilesMigratePartialError(
                    f"injected failure after {class_id}",
                    published=tuple(published),
                )
        if hooks.before_scope_rewrite:
            raise FilesMigratePartialError(
                "injected crash before scope rewrite",
                published=tuple(published),
            )
        _rewrite_scopes(files_home)
        seeded.extend(_seed_baseline(files_home))
        return FilesMigrateReport(
            status="success",
            published=tuple(published),
            skipped=tuple(skipped),
            seeded=tuple(seeded),
            rewritten_attachments=rewritten,
            unrewritten_attachments=tuple(unrewritten),
        )
    finally:
        _close_held(held)
        claim.release()


def _refuse_filesystem_root(files_home: Path) -> None:
    if files_home.parent == files_home:
        raise FilesMigrateError("root", "files_home must not be a filesystem root")


def _discover(files_home: Path) -> list[_Pair]:
    del files_home
    pairs: list[_Pair] = []
    personal = get_gobby_home() / "personal"
    projects = get_gobby_home() / "projects"
    topics = Path.home() / "wiki" / "topics"
    registry = Path.home() / "wiki" / "wikis.json"
    _add_if_present(pairs, "profile", personal / "USER.md", Path("USER.md"))
    _add_if_present(pairs, "personal_marker", personal / ".gobby", Path("_personal/.gobby"))
    _add_if_present(pairs, "personal_wiki", personal / "wiki", Path("wiki/personal"))
    if personal.is_dir():
        for child in sorted(personal.iterdir(), key=lambda path: path.name):
            if child.name in _HANDLED_PERSONAL:
                continue
            pairs.append(_Pair("leftover", child, Path("_personal") / child.name))
    if topics.is_dir():
        for child in sorted(topics.iterdir(), key=lambda path: path.name):
            if child.name.startswith("."):
                continue
            validate_topic_name(child.name)
            pairs.append(_Pair("topics", child, Path("wiki") / child.name))
    pairs.extend(_discover_attachments(personal / "attachments", projects))
    _add_if_present(pairs, "registry", registry, Path("wiki/wikis.json"))
    return pairs


def _add_if_present(pairs: list[_Pair], class_id: str, source: Path, dest_rel: Path) -> None:
    try:
        present = source.exists() or source.is_symlink()
    except OSError:
        present = source.is_symlink()
    if present:
        pairs.append(_Pair(class_id, source, dest_rel))


def _discover_attachments(personal_atts: Path, projects: Path) -> list[_Pair]:
    grouped: dict[str, list[Path]] = {}
    if personal_atts.exists():
        for leaf in _attachment_leaves(personal_atts):
            dest = Path("_personal/attachments") / leaf.relative_to(personal_atts)
            grouped.setdefault(_norm(dest), []).append(leaf)
    if projects.is_dir():
        for project in sorted(projects.iterdir(), key=lambda path: path.name):
            attachments = project / "attachments"
            if not attachments.exists():
                continue
            for leaf in _attachment_leaves(attachments):
                dest = Path("_personal/attachments") / project.name / leaf.relative_to(attachments)
                grouped.setdefault(_norm(dest), []).append(leaf)
    pairs: list[_Pair] = []
    for dest_key, sources in grouped.items():
        dest_rel = Path(dest_key)
        if len(sources) == 1:
            pairs.append(_Pair("attachments", sources[0], dest_rel))
            continue
        first, *rest = sources
        if all(_same_payload(first, other) for other in rest):
            pairs.append(_Pair("attachments", first, dest_rel))
            pairs.extend(_Pair("attachments", other, dest_rel, retire_only=True) for other in rest)
            continue
        raise FilesMigrateError(
            "collision",
            f"divergent attachment leaf collision at {dest_key}",
        )
    return pairs


def _attachment_leaves(root: Path) -> list[Path]:
    leaves: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in (*dirnames, *filenames):
            path = current / name
            st = os.lstat(path)
            reason = special_file_reason(st)
            if reason is not None:
                raise FilesMigrateError("special", f"legacy {reason} is not migratable: {path}")
        for name in filenames:
            leaves.append(current / name)
    return leaves


def _refuse_overlap(files_home: Path, sources: list[Path]) -> None:
    try:
        home = files_home.resolve()
    except OSError:
        home = files_home
    for source in sources:
        try:
            resolved = source.resolve()
        except OSError:
            resolved = source
        if home == resolved or home in resolved.parents or resolved in home.parents:
            raise FilesMigrateError(
                "overlap",
                "files_home overlaps a legacy personal, project-attachment, or wiki-topic source",
            )


def _parse_registry_if_present(pairs: list[_Pair], files_home: Path) -> dict[str, Any] | None:
    registry_pairs = [pair for pair in pairs if pair.class_id == "registry"]
    if not registry_pairs:
        return None
    source = registry_pairs[0].source
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FilesMigrateError("malformed", "malformed wiki registry") from exc
    return _transform_registry(raw, files_home=files_home)


def _transform_registry(raw: object, *, files_home: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FilesMigrateError("malformed", "malformed wiki registry")
    topics = raw.get("topics", {})
    projects = raw.get("projects", {})
    if not isinstance(topics, dict) or not isinstance(projects, dict):
        raise FilesMigrateError("malformed", "malformed wiki registry")
    topics_root = Path.home() / "wiki" / "topics"
    out_topics: dict[str, Any] = {}
    for name, entry in topics.items():
        if not isinstance(entry, dict) or "path" not in entry:
            raise FilesMigrateError("malformed", "malformed wiki registry")
        validate_topic_name(str(name))
        stored = Path(str(entry["path"]))
        expected = topics_root / str(name)
        dest = files_home / "wiki" / str(name)
        if stored.is_absolute():
            if not _same_path(stored, expected) and not _same_path(stored, dest):
                raise FilesMigrateError("malformed", "unknown registry topic path")
        elif stored.as_posix() not in {name, str(name)}:
            raise FilesMigrateError("malformed", "unknown registry topic path")
        out_topics[str(name)] = {"name": str(entry.get("name", name)), "path": str(name)}
    out_projects: dict[str, Any] = {}
    for project_id, entry in projects.items():
        if not isinstance(entry, dict):
            raise FilesMigrateError("malformed", "malformed wiki registry")
        if str(project_id) == PERSONAL_PROJECT_ID:
            out_projects[str(project_id)] = {
                "project_id": PERSONAL_PROJECT_ID,
                "project_root": str(files_home / "_personal"),
                "path": "personal",
            }
            continue
        out_projects[str(project_id)] = entry
    return {"topics": out_topics, "projects": out_projects}


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


def _prewalk(sources: list[Path]) -> list[_Held]:
    held: list[_Held] = []
    try:
        for source in sources:
            _prewalk_node(source, held)
    except Exception:
        _close_held(held)
        raise
    return held


def _prewalk_node(path: Path, held: list[_Held]) -> None:
    st = os.lstat(path)
    reason = special_file_reason(st)
    if reason is not None:
        raise FilesMigrateError("special", f"legacy {reason} is not migratable: {path}")
    directory = stat.S_ISDIR(st.st_mode)
    fd = os.open(path, _DIR_FLAGS if directory else _FILE_FLAGS)
    held.append(_Held(path, fd, (st.st_dev, st.st_ino)))
    if not directory:
        return
    with os.scandir(fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        _prewalk_node(path / name, held)


def _classify_destinations(files_home: Path, pairs: list[_Pair]) -> None:
    for pair in pairs:
        dest = files_home / pair.dest_rel
        if not dest.exists() or pair.retire_only:
            continue
        if pair.class_id == "registry":
            if _registry_dest_matches(pair.source, dest, files_home):
                pair.retire_only = True
                continue
            raise FilesMigrateError(
                "divergent",
                f"divergent both-present pair {pair.source} -> {pair.dest_rel}",
            )
        if _same_payload(pair.source, dest):
            pair.retire_only = True
            continue
        raise FilesMigrateError(
            "divergent",
            f"divergent both-present pair {pair.source} -> {pair.dest_rel}",
        )


def _assert_dest_graph(pairs: list[_Pair]) -> None:
    dests = [_norm(pair.dest_rel) for pair in pairs if not pair.retire_only]
    seen: dict[str, _Pair] = {}
    for pair in pairs:
        key = _norm(pair.dest_rel)
        previous = seen.get(key)
        if previous is None:
            seen[key] = pair
            continue
        if pair.retire_only or previous.retire_only:
            continue
        if pair.source == previous.source:
            continue
        raise FilesMigrateError("collision", f"destination collision at {key}")
    unique = sorted(set(dests))
    for index, left in enumerate(unique):
        for right in unique[index + 1 :]:
            if right.startswith(f"{left}/") or left.startswith(f"{right}/"):
                raise FilesMigrateError(
                    "prefix",
                    f"planned destinations overlap by prefix: {left} and {right}",
                )


def _inspect_existing_dest(files_home: Path, pairs: list[_Pair]) -> None:
    planned = {_norm(pair.dest_rel) for pair in pairs}
    if not files_home.is_dir():
        return
    for rel in _walk_dest_rels(files_home):
        if not _recognized_dest(rel, planned):
            raise FilesMigrateError(
                "unrecognized",
                f"unrecognized destination content: {rel}",
            )


def _walk_dest_rels(files_home: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(files_home, followlinks=False):
        current = Path(dirpath)
        for name in (*dirnames, *filenames):
            found.append((current / name).relative_to(files_home))
    return found


def _recognized_dest(rel: Path, planned: set[str]) -> bool:
    del planned
    parts = rel.parts
    if not parts:
        return True
    if parts[0] == "USER.md":
        return len(parts) == 1
    if parts[0] == "_personal":
        return True
    if parts[0] == "wiki":
        return len(parts) == 1 or parts[1] != "_gwiki"
    return False


def _assert_identities(held: list[_Held], *, allow_partial: bool) -> None:
    for node in held:
        try:
            current = os.lstat(node.path)
        except FileNotFoundError as exc:
            if allow_partial:
                raise FilesMigratePartialError(f"source identity changed: {node.path}") from exc
            raise FilesMigrateError("identity", f"source identity changed: {node.path}") from exc
        if (current.st_dev, current.st_ino) != node.identity:
            message = f"source identity changed: {node.path}"
            if allow_partial:
                raise FilesMigratePartialError(message)
            raise FilesMigrateError("identity", message)


def _maybe_swap(
    hooks: FilesMigrateHooks,
    class_id: str,
    published: list[str],
    held: list[_Held],
) -> None:
    previous = (
        _CLASS_ORDER[_CLASS_ORDER.index(class_id) - 1] if class_id != _CLASS_ORDER[0] else None
    )
    if hooks.swap_after_class is None or hooks.swap_after_class != previous:
        return
    if hooks.swap_fn is not None:
        hooks.swap_fn()
    _assert_identities(held, allow_partial=bool(published))


def _registry_dest_matches(source: Path, dest: Path, files_home: Path) -> bool:
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        actual = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return bool(actual == _transform_registry(raw, files_home=files_home))


def _publish_registry(pair: _Pair, files_home: Path) -> None:
    transformed = _parse_registry_if_present([pair], files_home)
    if transformed is None:
        return
    dest = files_home / pair.dest_rel
    if dest.exists():
        if _registry_dest_matches(pair.source, dest, files_home):
            _retire_source(pair.source)
            return
        raise FilesMigrateError(
            "divergent",
            f"divergent both-present pair {pair.source} -> {pair.dest_rel}",
        )
    if pair.dest_rel.parent.parts:
        ensure_files_home_descendant_dir(pair.dest_rel.parent)
    publish_files_home_descendant(
        pair.dest_rel,
        (json.dumps(transformed, indent=2) + "\n").encode("utf-8"),
    )
    _retire_source(pair.source)


def _publish_pair(pair: _Pair, hooks: FilesMigrateHooks) -> None:
    assert_held_files_home_identity()
    dest_rel = pair.dest_rel
    if dest_rel.parent.parts:
        ensure_files_home_descendant_dir(dest_rel.parent)
    try:
        if hooks.force_exdev:
            raise OSError(errno.EXDEV, "forced cross-device copy")
        _rename_into(pair.source, dest_rel)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_verify_delete(pair.source, dest_rel, hooks)


def _rename_into(source: Path, dest_rel: Path) -> None:
    dest_dir_fd, opened = _open_dest_parent(dest_rel)
    src_dir_fd = os.open(source.parent, _DIR_FLAGS)
    try:
        os.rename(
            source.name,
            dest_rel.name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dest_dir_fd,
        )
    finally:
        os.close(src_dir_fd)
        for fd in reversed(opened):
            os.close(fd)


def _open_dest_parent(dest_rel: Path) -> tuple[int, list[int]]:
    root_fd = files_home_root_fd()
    current = root_fd
    opened: list[int] = []
    for part in dest_rel.parent.parts:
        next_fd = os.open(part, _DIR_FLAGS, dir_fd=current)
        opened.append(next_fd)
        current = next_fd
    return current, opened


def _copy_verify_delete(source: Path, dest_rel: Path, hooks: FilesMigrateHooks) -> None:
    payload = _read_source_bytes(source)
    if hooks.fail_exdev_truncate:
        payload = payload[: max(0, len(payload) // 2)]
    try:
        if source.is_dir():
            _copy_tree(source, dest_rel, hooks)
        else:
            publish_files_home_descendant(dest_rel, payload)
            if hooks.fail_exdev_truncate or hooks.fail_exdev_verify:
                _cleanup_dest(dest_rel)
                raise FilesMigrateError("verify", "EXDEV copy failed verification")
            dest_bytes = (require_files_home() / dest_rel).read_bytes()
            if dest_bytes != _read_source_bytes(source):
                _cleanup_dest(dest_rel)
                raise FilesMigrateError("verify", "EXDEV copy failed verification")
        _retire_source(source)
    except FilesMigrateError:
        raise
    except OSError as exc:
        _cleanup_dest(dest_rel)
        raise FilesMigrateError("copy", f"EXDEV copy failed: {exc}") from exc


def _copy_tree(source: Path, dest_rel: Path, hooks: FilesMigrateHooks) -> None:
    ensure_files_home_descendant_dir(dest_rel)
    for dirpath, _dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        rel = current.relative_to(source)
        target = dest_rel / rel if rel.parts else dest_rel
        if rel.parts:
            ensure_files_home_descendant_dir(target)
        for name in filenames:
            child = current / name
            data = child.read_bytes()
            if hooks.fail_exdev_truncate:
                data = data[: max(0, len(data) // 2)]
            publish_files_home_descendant(target / name, data)
            if hooks.fail_exdev_verify or hooks.fail_exdev_truncate:
                _cleanup_dest(dest_rel)
                raise FilesMigrateError("verify", "EXDEV copy failed verification")


def _cleanup_dest(dest_rel: Path) -> None:
    dest = require_files_home() / dest_rel
    try:
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists() or dest.is_symlink():
            unlink_files_home_descendant(dest_rel)
    except (OSError, FileNotFoundError):
        return


def _read_source_bytes(source: Path) -> bytes:
    if source.is_dir():
        return b""
    return source.read_bytes()


def _same_payload(left: Path, right: Path) -> bool:
    left_st = os.lstat(left)
    right_st = os.lstat(right)
    if special_file_reason(left_st) or special_file_reason(right_st):
        return False
    if stat.S_ISDIR(left_st.st_mode) and stat.S_ISDIR(right_st.st_mode):
        return _tree_digest(left) == _tree_digest(right)
    if stat.S_ISREG(left_st.st_mode) and stat.S_ISREG(right_st.st_mode):
        if left_st.st_size != right_st.st_size:
            return False
        return _file_digest(left) == _file_digest(right)
    return False


def _file_digest(path: Path) -> bytes:
    return hashlib.sha256(path.read_bytes()).digest()


def _tree_digest(path: Path) -> tuple[object, ...]:
    entries: list[tuple[str, object]] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        st = os.lstat(child)
        if special_file_reason(st):
            entries.append((child.name, ("special", st.st_mode)))
        elif stat.S_ISDIR(st.st_mode):
            entries.append((child.name, _tree_digest(child)))
        else:
            entries.append((child.name, ("file", st.st_size, _file_digest(child))))
    return ("dir", tuple(entries))


def _retire_source(path: Path) -> None:
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _rewrite_scopes(files_home: Path) -> None:
    personal = files_home / "wiki" / "personal"
    if personal.is_dir():
        _write_scope(
            Path("wiki/personal"),
            identity=f"project:{PERSONAL_PROJECT_ID}",
            root=personal,
        )
    wiki = files_home / "wiki"
    if not wiki.is_dir():
        return
    for child in wiki.iterdir():
        if child.name in {"wikis.json", "personal", "_gwiki"} or not child.is_dir():
            continue
        _write_scope(Path("wiki") / child.name, identity=f"topic:{child.name}", root=child)


def _write_scope(dest_rel: Path, *, identity: str, root: Path) -> None:
    scope_rel = dest_rel / "_gwiki" / "scope.json"
    dest = require_files_home() / dest_rel
    scope_path = require_files_home() / scope_rel
    if not dest.is_dir():
        return
    if not scope_path.is_file() and not any(dest.iterdir()):
        return
    data: dict[str, Any] = {"identity": identity, "root": str(root)}
    if scope_path.is_file():
        try:
            loaded = json.loads(scope_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            data = dict(loaded)
            data["root"] = str(root)
            data.setdefault("identity", identity)
    publish_files_home_descendant(scope_rel, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def _seed_baseline(files_home: Path) -> list[str]:
    seeded: list[str] = []
    if not (files_home / "USER.md").exists():
        publish_files_home_descendant("USER.md", b"")
        seeded.append("USER.md")
    for child in ("notes", "reminders", "attachments"):
        rel = Path("_personal") / child
        if not (files_home / rel).exists():
            ensure_files_home_descendant_dir(rel)
            seeded.append(str(rel))
    ensure_personal_project_identity()
    if not (files_home / "_personal" / ".gobby" / "project.json").exists():
        seeded.append("_personal/.gobby")
    if not (files_home / "wiki").exists():
        ensure_files_home_descendant_dir("wiki")
        seeded.append("wiki")
    if not (files_home / "wiki" / "wikis.json").exists():
        publish_files_home_descendant(
            Path("wiki") / "wikis.json",
            (json.dumps({"topics": {}, "projects": {}}, indent=2) + "\n").encode("utf-8"),
        )
        seeded.append("wiki/wikis.json")
    return seeded


def _rewrite_locators(hooks: FilesMigrateHooks, files_home: Path) -> tuple[int, list[str]]:
    if hooks.before_locator_rewrite:
        raise FilesMigratePartialError("injected crash before locator rewrite")
    db = hooks.db
    if db is None:
        return 0, []
    from gobby.servers.chat_attachment_files import attachment_relative_locator
    from gobby.storage.chat_attachments import (
        list_attachment_records,
        rewrite_attachment_local_path,
    )
    from gobby.storage.hub.protocol import HubDatabase

    hub_db = cast(HubDatabase, db)
    rewritten = 0
    unrewritten: list[str] = []
    for record in list_attachment_records(hub_db):
        locator = attachment_relative_locator(record.project_id, record.id, record.filename)
        dest = files_home / locator
        if dest.is_file():
            if record.local_path != locator:
                rewrite_attachment_local_path(
                    hub_db,
                    attachment_id=record.id,
                    project_id=record.project_id,
                    local_path=locator,
                )
            rewritten += 1
            continue
        unrewritten.append(record.id)
    return rewritten, unrewritten


def _close_held(held: list[_Held]) -> None:
    for node in reversed(held):
        try:
            os.close(node.fd)
        except OSError:
            continue


def _norm(path: Path | str) -> str:
    return Path(path).as_posix().strip("/")
