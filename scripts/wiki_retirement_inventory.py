"""Exact, owner-checked inventory for the one-time legacy wiki retirement."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

ARCHIVE_TAG = "legacy-wiki-before-retirement-21771"
CODE_TOMBSTONE_HASH = "__gcode_tombstone__"
CODE_TOMBSTONE_LANGUAGE = "__gcode_deleted__"
CODE_INDEX_NAMESPACE = UUID("c0de1de0-0000-4000-8000-000000000000")
PG_TABLES = ("gwiki_documents", "gwiki_chunks", "gwiki_links", "gwiki_sources", "gwiki_ingestions")
GRAPH = "gobby_wiki"
COLLECTION = re.compile(r"^gwiki_(?:project|topic)_[^/\\\x00-\x1f:]+$")
VAULT_NAME = re.compile(r"^(?:wiki|gobby-wiki(?:-\d{3})?)$")
WIKI_NAME = re.compile(r"(?:^|[-_:])(?:gwiki|wiki|codewiki)(?:$|[-_:])")
MIRROR_NAMES = {
    ".DS_Store",
    ".obsidian",
    "_gwiki",
    "_index.md",
    "_meta",
    "ai-readme.md",
    "code",
    "inbox",
    "knowledge",
    "log.md",
    "meta",
    "outputs",
    "raw",
    "recaps",
    "exports",
    "sessions",
}


class RetirementError(RuntimeError):
    """A bounded retirement precondition failed."""


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Entry(Record):
    path: str
    kind: Literal["file", "directory", "symlink"]
    device: int
    inode: int
    uid: int
    mode: int
    size: int
    digest: str


class Root(Record):
    path: str
    kind: Literal["hub_wiki", "checkout_vault", "mirror_entry", "binary"]
    owner_path: str
    owner_digest: str
    entries: list[Entry]


class StoreTarget(Record):
    kind: Literal["postgres", "qdrant", "falkor", "rule", "variable", "job", "config", "discovery"]
    name: str
    digest: str
    count: int
    identity: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"


class CargoInstall(Record):
    home: str
    package: str
    metadata: dict[str, Any]
    other_metadata_digest: str
    other_binaries: list[Entry]

    @property
    def key(self) -> str:
        return f"cargo:{self.home}:{self.package}"


class Process(Record):
    pid: int
    started: str
    command: str


class CodeIndexVersion(Record):
    id: str
    content_hash: str
    symbol_ids: list[str]


class CodeIndexFile(Record):
    file_path: str
    versions: list[CodeIndexVersion]
    owner_path: str
    removed_revision: str | None = None
    archived_blob: str | None = None

    def native(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "versions": [version.model_dump() for version in self.versions],
        }


class CodeIndexTarget(Record):
    project_id: str
    machine_id: str
    root_path: str
    files: list[CodeIndexFile]
    digest: str

    @property
    def key(self) -> str:
        return f"code_index:{self.project_id}"

    def native_files(self) -> list[dict[str, Any]]:
        return [file.native() for file in self.files]


def validate_code_version(
    project_id: str, file_path: str, version_id: str, content_hash: str, symbol_ids: list[str]
) -> None:
    """Admit current SHA256 content or the exact empty overlay deletion sentinel."""
    if str(UUID(version_id)) != version_id:
        raise RetirementError("Invalid code-index version identity")
    if content_hash == CODE_TOMBSTONE_HASH:
        expected = str(uuid5(CODE_INDEX_NAMESPACE, f"{project_id}:{file_path}:{content_hash}"))
        if version_id != expected or symbol_ids:
            raise RetirementError("Code-index tombstone identity or empty symbol contract differs")
    elif not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise RetirementError("Invalid code-index content hash")


class Inventory(Record):
    version: Literal[1] = 1
    mode: Literal["recovery", "direct"] = Field(
        default="recovery", exclude_if=lambda value: value == "recovery"
    )
    created_at: str
    repository: str
    archive_tag: Literal["legacy-wiki-before-retirement-21771"] = (
        "legacy-wiki-before-retirement-21771"
    )
    baseline: str
    uid: int
    gobby_home: str
    files_home: str
    checkouts: list[str]
    checkout_projects: dict[str, str] = Field(default_factory=dict)
    native_dirs: list[str] = Field(default_factory=list)
    cargo_installs: list[CargoInstall] = Field(default_factory=list)
    roots: list[Root]
    stores: list[StoreTarget]
    backend_identities: dict[str, str]
    writers: list[Process]
    errors: list[str] = Field(default_factory=list)
    # Empty omission preserves the identity of already archived v1 inventories.
    code_indexes: list[CodeIndexTarget] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )

    @property
    def digest(self) -> str:
        return sha(canonical(self.model_dump(mode="json")))


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise RetirementError(f"Expected an absolute normalized path: {path}")
    return path


def directory_fd(path: Path) -> int:
    """Open every path component without following symlinks, including parents."""
    absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(path: Path) -> bytes:
    parent = directory_fd(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RetirementError(f"Expected an operator-owned regular file: {path}")
            return handle.read()
    finally:
        os.close(parent)


def _entry(parent: int, name: str, relative: str) -> Entry:
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if info.st_uid != os.getuid():
        raise RetirementError(f"Unowned entry: {relative}")
    if stat.S_ISDIR(info.st_mode):
        kind, digest, size = "directory", "", 0
    elif stat.S_ISLNK(info.st_mode):
        data = os.readlink(name, dir_fd=parent).encode()
        kind, digest, size = "symlink", sha(data), len(data)
    elif stat.S_ISREG(info.st_mode):
        digest_builder = hashlib.sha256()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise RetirementError(f"Entry changed while reading: {relative}")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest_builder.update(chunk)
            after = os.fstat(handle.fileno())
            if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise RetirementError(f"Entry changed while reading: {relative}")
        kind, digest, size = "file", digest_builder.hexdigest(), info.st_size
    else:
        raise RetirementError(f"Unsupported special file: {relative}")
    return Entry(
        path=relative,
        kind=kind,
        device=info.st_dev,
        inode=info.st_ino,
        uid=info.st_uid,
        mode=stat.S_IMODE(info.st_mode),
        size=size,
        digest=digest,
    )


def scan(path: Path) -> list[Entry]:
    """Describe an exact tree, retaining symlink entries without traversing targets."""
    parent = directory_fd(path.parent)
    result: list[Entry] = []

    def visit(fd: int, name: str, relative: str) -> None:
        entry = _entry(fd, name, relative)
        result.append(entry)
        if entry.kind == "directory":
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (entry.device, entry.inode):
                    raise RetirementError(f"Directory changed while scanning: {path / relative}")
                for part in sorted(os.listdir(child)):
                    visit(child, part, part if relative == "." else f"{relative}/{part}")
            finally:
                os.close(child)

    try:
        visit(parent, path.name, ".")
    finally:
        os.close(parent)
    return result


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def baseline(repository: Path) -> str:
    if git(repository, "cat-file", "-t", ARCHIVE_TAG) != "tag":
        raise RetirementError("Retirement requires the annotated archive tag")
    return git(repository, "rev-parse", f"{ARCHIVE_TAG}^{{commit}}")


def writer_processes() -> list[Process]:
    import psutil

    result = []
    for process in psutil.process_iter(["pid", "create_time", "cmdline", "uids"]):
        try:
            if process.uids().real != os.getuid():
                continue
            args = process.cmdline()
            if any(Path(arg).name == "gwiki" for arg in args) or any(
                re.search(r"gobby[./]wiki(?:[./]|$)", arg) for arg in args
            ):
                result.append(
                    Process(
                        pid=process.pid, started=str(process.create_time()), command=" ".join(args)
                    )
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(result, key=lambda item: item.pid)


def native_directories(gobby_home: Path) -> list[Path]:
    candidates = {gobby_home / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")}
    candidates.add(Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))) / "bin")
    configured = os.environ.get("GOBBY_NATIVE_BIN_DIR")
    if configured:
        candidates.add(absolute(Path(configured)))
    candidates.update(Path(value) for value in os.get_exec_path() if Path(value).is_absolute())
    return sorted(candidates)


def checkout_identities(checkouts: list[Path]) -> dict[str, str]:
    identities = {}
    repositories = {}
    for checkout in checkouts:
        try:
            data = json.loads(read_file(checkout / ".gobby" / "project.json"))
            project_id = str(UUID(data["id"]))
            identities[str(checkout)] = project_id
            try:
                common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
            except subprocess.CalledProcessError:
                continue
            repositories[common] = project_id
        except FileNotFoundError:
            continue
    for checkout in checkouts:
        if str(checkout) in identities:
            continue
        try:
            common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        except subprocess.CalledProcessError:
            continue
        if common in repositories:
            identities[str(checkout)] = repositories[common]
    return identities


def scope_marker(marker: Path, expected_id: str, expected_root: Path | None = None) -> bytes:
    data = read_file(marker)
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("identity") != f"project:{expected_id}":
        raise RetirementError(f"Wiki scope identity does not match recorded checkout: {marker}")
    root = value.get("root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise RetirementError(f"Wiki scope has no absolute vault root: {marker}")
    if expected_root is not None and root != str(expected_root):
        raise RetirementError(f"Wiki scope points to a different vault: {marker}")
    return data


def discover_roots(
    repository: Path, gobby_home: Path, files_home: Path, checkouts: list[Path]
) -> list[Root]:
    """Admit owned hub storage, marked vaults and recorded wiki-branch mirror entries."""
    roots = []
    bootstrap = gobby_home / "bootstrap.yaml"
    hub = files_home / "wiki"
    if os.path.lexists(hub):
        roots.append(
            Root(
                path=str(hub),
                kind="hub_wiki",
                owner_path=str(bootstrap),
                owner_digest=sha(read_file(bootstrap)),
                entries=scan(hub),
            )
        )
    identities = checkout_identities(checkouts)
    for checkout in sorted(set(checkouts + [repository])):
        fd = directory_fd(checkout)
        try:
            names = os.listdir(fd)
        finally:
            os.close(fd)
        mirror_marker = checkout / "_gwiki" / "scope.json"
        if os.path.lexists(mirror_marker):
            project_id = identities.get(str(checkout))
            if not project_id or git(checkout, "branch", "--show-current") != "wiki":
                raise RetirementError(f"Unowned legacy mirror: {checkout}")
            marker_bytes = scope_marker(mirror_marker, project_id)
            source = Path(json.loads(marker_bytes)["root"])
            if source.parent not in checkouts or source == checkout:
                raise RetirementError(f"Mirror source is not a recorded vault: {checkout}")
            scope_marker(source / "_gwiki" / "scope.json", project_id, source)
            if git(source.parent, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(
                checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ):
                raise RetirementError(
                    f"Mirrored vault belongs to a different repository: {checkout}"
                )
            # These are names emitted by the archived vault layout and historical
            # wiki worktree mirror. The repository root and Git metadata survive.
            owned_names = MIRROR_NAMES
            unknown = set(names) - owned_names - {".git", ".gobby"}
            if unknown:
                raise RetirementError(
                    f"Unclassified legacy mirror entries: {checkout}: {sorted(unknown)}"
                )
            for name in sorted(set(names) & owned_names):
                roots.append(
                    Root(
                        path=str(checkout / name),
                        kind="mirror_entry",
                        owner_path=str(mirror_marker),
                        owner_digest=sha(marker_bytes),
                        entries=scan(checkout / name),
                    )
                )
            continue
        for name in sorted(names):
            if not VAULT_NAME.fullmatch(name):
                continue
            vault = checkout / name
            if vault == hub or vault.is_relative_to(hub):
                continue
            marker = vault / "_gwiki" / "scope.json"
            if not os.path.lexists(marker):
                continue
            project_id = identities.get(str(checkout))
            if not project_id:
                raise RetirementError(f"Vault has no recorded project identity: {vault}")
            marker_bytes = scope_marker(marker, project_id, vault)
            roots.append(
                Root(
                    path=str(vault),
                    kind="checkout_vault",
                    owner_path=str(marker),
                    owner_digest=sha(marker_bytes),
                    entries=scan(vault),
                )
            )
    for directory in native_directories(gobby_home):
        binary = directory / "gwiki"
        sidecar = directory / ".gwiki-install.json"
        stamp = directory / ".gwiki-version"
        present = [path for path in (binary, sidecar, stamp) if os.path.lexists(path)]
        if not present:
            if os.path.lexists(directory.parent / ".crates2.json"):
                from scripts.wiki_retirement_cargo import discover_cargo

                if discover_cargo(directory, repository, checkouts) is not None:
                    raise RetirementError(
                        "Cargo gwiki registration has an unrecorded missing binary"
                    )
            continue
        owner = bootstrap if directory == gobby_home / "bin" else sidecar
        if directory != gobby_home / "bin":
            if not sidecar.is_file() or sidecar.is_symlink():
                from scripts.wiki_retirement_cargo import discover_cargo

                if discover_cargo(directory, repository, checkouts) is None:
                    raise RetirementError(
                        f"Additional installed gwiki needs ownership evidence: {binary}"
                    )
                owner = directory.parent / ".crates2.json"
            else:
                metadata = json.loads(read_file(sidecar))
                if metadata.get("install_method") not in {
                    "workspace",
                    "release",
                    "cargo",
                    "source",
                }:
                    raise RetirementError(f"Unrecognized gwiki installation provenance: {sidecar}")
        for path in present:
            roots.append(
                Root(
                    path=str(path),
                    kind="binary",
                    owner_path=str(owner),
                    owner_digest=sha(read_file(owner)),
                    entries=scan(path),
                )
            )
    return roots


def validate_inventory(inventory: Inventory) -> None:
    if inventory.errors:
        raise RetirementError("Inventory is incomplete: " + "; ".join(inventory.errors))
    if inventory.uid != os.getuid():
        raise RetirementError("Inventory belongs to another operator")
    if baseline(Path(inventory.repository)) != inventory.baseline:
        raise RetirementError("Archive baseline changed")
    paths = [absolute(Path(root.path)) for root in inventory.roots]
    if len(paths) != len(set(paths)):
        raise RetirementError("Duplicate inventory roots")
    hub = Path(inventory.files_home) / "wiki"
    for root, path in zip(inventory.roots, paths, strict=True):
        if any(path != other and path.is_relative_to(other) for other in paths):
            raise RetirementError("Overlapping inventory roots")
        if root.kind == "hub_wiki":
            admitted = path == hub
            owner = Path(inventory.gobby_home) / "bootstrap.yaml"
        elif root.kind == "binary":
            admitted = path.parent in native_directories(
                Path(inventory.gobby_home)
            ) and path.name in {"gwiki", ".gwiki-version", ".gwiki-install.json"}
            owner = (
                Path(inventory.gobby_home) / "bootstrap.yaml"
                if path.parent == Path(inventory.gobby_home) / "bin"
                else path.parent / ".gwiki-install.json"
            )
            if any(Path(item.home) / "bin/gwiki" == path for item in inventory.cargo_installs):
                owner = path.parent.parent / ".crates2.json"
        elif root.kind == "mirror_entry":
            admitted = str(path.parent) in inventory.checkouts and path.name in MIRROR_NAMES
            owner = path.parent / "_gwiki" / "scope.json"
            if git(path.parent, "branch", "--show-current") != "wiki":
                raise RetirementError("Mirrored vault checkout changed branch")
        else:
            admitted = str(path.parent) in inventory.checkouts and bool(
                VAULT_NAME.fullmatch(path.name)
            )
            owner = path / "_gwiki" / "scope.json"
        if not admitted or Path(root.owner_path) != owner:
            raise RetirementError(f"Out-of-scope root or ownership evidence: {path}")
        seen = set()
        for entry in root.entries:
            relative = Path(entry.path)
            if relative.is_absolute() or ".." in relative.parts or entry.path in seen:
                raise RetirementError(f"Invalid inventory entry: {entry.path}")
            if entry.uid != inventory.uid:
                raise RetirementError("Entry owner differs from inventory")
            seen.add(entry.path)
        if "." not in seen:
            raise RetirementError("Inventory root entry missing")
    for target in inventory.stores:
        valid = (
            (target.kind == "postgres" and target.name == "wiki_schema")
            or (target.kind == "qdrant" and COLLECTION.fullmatch(target.name))
            or (target.kind == "falkor" and target.name == GRAPH)
            or (target.kind == "config" and target.name == "wiki_configuration")
            or (target.kind == "discovery" and target.name == "gobby-wiki")
            or (target.kind in {"rule", "variable", "job"} and bool(target.name))
        )
        if not valid:
            raise RetirementError(f"Unowned datastore target: {target.key}")
    if len({target.key for target in inventory.stores}) != len(inventory.stores):
        raise RetirementError("Duplicate datastore targets")
    if len({item.home for item in inventory.cargo_installs}) != len(inventory.cargo_installs):
        raise RetirementError("Duplicate Cargo installations")
    for item in inventory.cargo_installs:
        if not any(
            tree.kind == "binary" and Path(tree.path) == Path(item.home) / "bin/gwiki"
            for tree in inventory.roots
        ):
            raise RetirementError("Cargo registration has no exact inventoried binary")
    validate_code_indexes(inventory)


def validate_code_indexes(inventory: Inventory) -> None:
    seen = set()
    vaults = {tree.path for tree in inventory.roots}
    deleted_by_revision: dict[str, set[str]] = {}
    archived_blobs: dict[str, str] = {}
    repository = Path(inventory.repository)
    common: str | None = None
    if any(file.removed_revision for target in inventory.code_indexes for file in target.files):
        common = git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        for line in git(repository, "ls-tree", "-r", inventory.baseline).splitlines():
            metadata, path = line.split("\t", 1)
            archived_blobs[path] = metadata.split()[2]
    for target in inventory.code_indexes:
        if target.project_id in seen or not target.files:
            raise RetirementError("Duplicate or empty code-index target")
        seen.add(target.project_id)
        if any(str(UUID(value)) != value for value in (target.project_id, target.machine_id)):
            raise RetirementError("Noncanonical code-index identity")
        if target.root_path not in inventory.checkouts:
            raise RetirementError("Code-index root is not a recorded checkout")
        if not re.fullmatch(r"[0-9a-f]{64}", target.digest):
            raise RetirementError("Invalid code-index snapshot digest")
        if (
            any(file.removed_revision for file in target.files)
            and git(
                Path(target.root_path), "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            != common
        ):
            raise RetirementError("Archived source path belongs to another repository")
        paths = set()
        versions = set()
        symbols = set()
        for file in target.files:
            relative = Path(file.file_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or str(relative) != file.file_path
                or file.file_path == "."
            ):
                raise RetirementError("Invalid code-index relative path")
            if file.file_path in paths:
                raise RetirementError("Duplicate code-index file")
            paths.add(file.file_path)
            if file.removed_revision is None:
                if file.owner_path not in vaults or not (
                    Path(target.root_path) / relative
                ).is_relative_to(file.owner_path):
                    raise RetirementError("Code-index path lacks inventoried vault ownership")
            else:
                if not re.fullmatch(r"[0-9a-f]{40}", file.removed_revision) or not re.fullmatch(
                    r"[0-9a-f]{40}", file.archived_blob or ""
                ):
                    raise RetirementError(
                        "Archived deletion requires full immutable Git object IDs"
                    )
                if file.removed_revision not in deleted_by_revision:
                    git(
                        repository,
                        "merge-base",
                        "--is-ancestor",
                        inventory.baseline,
                        file.removed_revision,
                    )
                    deleted_by_revision[file.removed_revision] = set(
                        git(
                            repository,
                            "diff",
                            "--name-only",
                            "--diff-filter=D",
                            inventory.baseline,
                            file.removed_revision,
                        ).splitlines()
                    )
                if (
                    archived_blobs.get(file.file_path) != file.archived_blob
                    or file.file_path not in deleted_by_revision[file.removed_revision]
                ):
                    raise RetirementError("Code-index path lacks exact archived deletion ownership")
            for version in file.versions:
                if str(UUID(version.id)) != version.id or version.id in versions:
                    raise RetirementError("Duplicate or invalid code-index version")
                versions.add(version.id)
                validate_code_version(
                    target.project_id,
                    file.file_path,
                    version.id,
                    version.content_hash,
                    version.symbol_ids,
                )
                for symbol in version.symbol_ids:
                    if str(UUID(symbol)) != symbol or symbol in symbols:
                        raise RetirementError("Duplicate or invalid code-index symbol")
                    symbols.add(symbol)


def retirement_directory(inventory: Inventory) -> Path:
    path = Path(inventory.gobby_home) / "retirement" / inventory.digest
    for scope in [*map(Path, inventory.checkouts), Path(inventory.files_home)]:
        if path.is_relative_to(scope) or scope.is_relative_to(path):
            raise RetirementError("Recovery directory overlaps indexing or active files storage")
    return path


def reject_new_roots(inventory: Inventory) -> None:
    """Cheap discovery rerun after deletion; never reads through an absent scope marker."""
    expected = {Path(root.path) for root in inventory.roots}
    candidates = {Path(inventory.files_home) / "wiki"}
    for directory in native_directories(Path(inventory.gobby_home)):
        candidates.update(
            directory / name for name in ("gwiki", ".gwiki-version", ".gwiki-install.json")
        )
    mirrored = {Path(root.path).parent for root in inventory.roots if root.kind == "mirror_entry"}
    for checkout in map(Path, inventory.checkouts):
        fd = directory_fd(checkout)
        try:
            names = os.listdir(fd)
        finally:
            os.close(fd)
        if checkout in mirrored:
            candidates.update(checkout / name for name in names if name not in {".git", ".gobby"})
        else:
            for name in names:
                if not VAULT_NAME.fullmatch(name):
                    continue
                path = checkout / name
                if path.is_symlink() or os.path.lexists(path / "_gwiki" / "scope.json"):
                    candidates.add(path)
    new = [str(path) for path in candidates - expected if os.path.lexists(path)]
    if new:
        raise RetirementError(f"New wiki roots require a new inventory: {sorted(new)}")
