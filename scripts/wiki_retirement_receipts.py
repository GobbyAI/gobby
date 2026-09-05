"""Private recovery artifacts and durable per-item retirement receipts."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field

from scripts.wiki_retirement_inventory import (
    Entry,
    Inventory,
    Record,
    RetirementError,
    Root,
    _entry,
    canonical,
    directory_fd,
    now,
    read_file,
    retirement_directory,
    scan,
    sha,
)


class Artifact(Record):
    path: str
    digest: str
    size: int


class Backup(Record):
    inventory_digest: str
    baseline: str
    created_at: str
    artifacts: dict[str, Artifact]
    complete: bool


class Receipt(Record):
    inventory_digest: str
    backup_digest: str
    operation: str
    updated_at: str
    items: dict[str, Literal["intent", "done"]] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    complete: bool = False
    deferred_schema: bool = False


class RehearsalTrees(Record):
    inventory_digest: str
    backup_digest: str
    roots: list[Root]


class Evidence(Record):
    path: str
    digest: str


class RuntimeProof(Record):
    inventory_digest: str
    installed_repository: str
    installed_commit: str
    binaries: dict[str, Evidence]
    checks: dict[str, Evidence]
    created_at: str


def runtime_digest(proof: RuntimeProof) -> str:
    """Bind executable check artifacts to one installed checkout and binary set."""
    return sha(
        canonical(
            {
                "installed_repository": proof.installed_repository,
                "installed_commit": proof.installed_commit,
                "binaries": {key: value.model_dump() for key, value in proof.binaries.items()},
            }
        )
    )


def private_directory(path: Path) -> None:
    """Create only the final component; verify all ancestors without following links."""
    parent = directory_fd(path.parent)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise RetirementError(
                    f"Recovery directory must be owned by operator and 0700: {path}"
                )
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def prepare_directory(inventory: Inventory) -> Path:
    root = retirement_directory(inventory)
    private_directory(root.parent)
    private_directory(root)
    return root


def write_private(path: Path, data: bytes) -> None:
    parent = directory_fd(path.parent)
    temporary = f".{path.name}.{os.getpid()}.tmp"
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def read_private(path: Path) -> bytes:
    data = read_file(path)
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise RetirementError(f"Recovery file must be 0600: {path}")
    return data


@contextmanager
def operation_lock(root: Path) -> Iterator[None]:
    parent = directory_fd(root)
    try:
        fd = os.open(
            "operation.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RetirementError("Another retirement operation owns this inventory") from exc
            yield
        finally:
            os.close(fd)
    finally:
        os.close(parent)


class Journal:
    def __init__(self, path: Path, inventory: Inventory, backup: Backup, operation: str) -> None:
        self.path = path
        backup_digest = sha(canonical(backup.model_dump(mode="json")))
        if path.exists():
            self.receipt = Receipt.model_validate_json(read_private(path))
            if (
                self.receipt.inventory_digest,
                self.receipt.backup_digest,
                self.receipt.operation,
            ) != (inventory.digest, backup_digest, operation):
                raise RetirementError("Receipt belongs to another inventory, backup or operation")
        else:
            self.receipt = Receipt(
                inventory_digest=inventory.digest,
                backup_digest=backup_digest,
                operation=operation,
                updated_at=now(),
            )
            self.save()

    def save(self) -> None:
        write_private(self.path, self.receipt.model_dump_json(indent=2).encode())

    def state(self, key: str) -> str | None:
        return self.receipt.items.get(key)

    def mark(self, key: str, state: Literal["intent", "done"]) -> None:
        self.receipt = self.receipt.model_copy(
            update={
                "items": {**self.receipt.items, key: state},
                "updated_at": now(),
                "complete": False,
            }
        )
        self.save()

    def failed(self, error: str) -> None:
        self.receipt = self.receipt.model_copy(
            update={
                "errors": [*self.receipt.errors, error],
                "updated_at": now(),
                "complete": False,
            }
        )
        self.save()

    def defer_schema(self) -> None:
        self.receipt = self.receipt.model_copy(
            update={"deferred_schema": True, "complete": False, "updated_at": now()}
        )
        self.save()

    def finish(self) -> None:
        self.receipt = self.receipt.model_copy(
            update={"complete": True, "deferred_schema": False, "updated_at": now()}
        )
        self.save()


def entry_key(root: Root, entry: Entry) -> str:
    return f"file:{root.path}:{entry.path}"


def put_artifact(root: Path, key: str, data: bytes) -> Artifact:
    name = f"artifact-{sha(key.encode())}.bin"
    write_private(root / name, data)
    return Artifact(path=name, digest=sha(data), size=len(data))


def artifact_bytes(root: Path, artifact: Artifact) -> bytes:
    if Path(artifact.path).name != artifact.path or not artifact.path.startswith("artifact-"):
        raise RetirementError("Invalid recovery artifact path")
    data = read_private(root / artifact.path)
    if sha(data) != artifact.digest or len(data) != artifact.size:
        raise RetirementError(f"Recovery artifact changed: {artifact.path}")
    return data


def backup_files(inventory: Inventory, destination: Path) -> dict[str, Artifact]:
    artifacts = {}
    for root in inventory.roots:
        if sha(read_file(Path(root.owner_path))) != root.owner_digest:
            raise RetirementError(f"Ownership evidence changed: {root.owner_path}")
        if scan(Path(root.path)) != root.entries:
            raise RetirementError(f"Tree changed since inventory: {root.path}")
        for entry in root.entries:
            path = Path(root.path) / entry.path
            if entry.kind == "directory":
                continue
            if entry.kind == "symlink":
                parent = directory_fd(path.parent)
                try:
                    data = os.readlink(path.name, dir_fd=parent).encode()
                finally:
                    os.close(parent)
            else:
                data = read_file(path)
            if sha(data) != entry.digest:
                raise RetirementError(f"File changed during backup: {path}")
            artifacts[entry_key(root, entry)] = put_artifact(
                destination, entry_key(root, entry), data
            )
        if scan(Path(root.path)) != root.entries:
            raise RetirementError(f"Tree changed during backup: {root.path}")
    return artifacts


def verify_remaining(root: Root, journal: Journal) -> None:
    try:
        current = {entry.path: entry for entry in scan(Path(root.path))}
    except FileNotFoundError:
        current = {}
    expected = {entry.path: entry for entry in root.entries}
    if current.keys() - expected.keys():
        raise RetirementError(f"New files require a new inventory: {root.path}")
    for name, entry in expected.items():
        state = journal.state(entry_key(root, entry))
        actual = current.get(name)
        if actual is None:
            if state not in {"done", "intent"}:
                raise RetirementError(f"Unrecorded missing entry: {root.path}/{name}")
        elif state == "done" or actual != entry:
            raise RetirementError(f"Recreated or changed entry: {root.path}/{name}")


def delete_files(inventory: Inventory, journal: Journal) -> None:
    # Rename atomically into the private recovery directory before inspecting and
    # unlinking. A competing replacement is retained, never deleted on stale evidence.
    for root in inventory.roots:
        verify_remaining(root, journal)
    quarantine = directory_fd(journal.path.parent)
    try:
        for root in inventory.roots:
            path = Path(root.path)
            for entry in reversed(root.entries):
                key = entry_key(root, entry)
                if journal.state(key) == "done":
                    continue
                target = path if entry.path == "." else path / entry.path
                claimed = "quarantine-" + sha(key.encode())
                try:
                    actual = _entry(quarantine, claimed, entry.path)
                except FileNotFoundError:
                    actual = None
                if actual is None:
                    parent = directory_fd(target.parent)
                    try:
                        try:
                            current = _entry(parent, target.name, entry.path)
                        except FileNotFoundError:
                            if journal.state(key) != "intent":
                                raise
                            journal.mark(key, "done")
                            continue
                        if current != entry:
                            raise RetirementError(f"Entry changed before quarantine: {target}")
                        journal.mark(key, "intent")
                        os.rename(target.name, claimed, src_dir_fd=parent, dst_dir_fd=quarantine)
                        os.fsync(parent)
                        os.fsync(quarantine)
                    finally:
                        os.close(parent)
                    actual = _entry(quarantine, claimed, entry.path)
                if actual != entry:
                    # A racing regular file/symlink can be put back using an exclusive
                    # hard link. If a new source entry exists, preserve both versions.
                    if actual.kind != "directory":
                        parent = directory_fd(target.parent)
                        try:
                            try:
                                os.link(
                                    claimed,
                                    target.name,
                                    src_dir_fd=quarantine,
                                    dst_dir_fd=parent,
                                    follow_symlinks=False,
                                )
                            except FileExistsError:
                                pass
                            else:
                                os.unlink(claimed, dir_fd=quarantine)
                                os.fsync(parent)
                        finally:
                            os.close(parent)
                    raise RetirementError(
                        f"Changed entry preserved; inspect {target} and {journal.path.parent / claimed}"
                    )
                if entry.kind == "directory":
                    os.rmdir(claimed, dir_fd=quarantine)
                else:
                    os.unlink(claimed, dir_fd=quarantine)
                os.fsync(quarantine)
                journal.mark(key, "done")
    finally:
        os.close(quarantine)


def restore_files(
    inventory: Inventory, backup: Backup, backup_root: Path, destination: Path
) -> list[Root]:
    """Restore into an empty isolated directory; original paths are never opened."""
    private_directory(destination)
    expected_roots = {f"root-{index}" for index in range(len(inventory.roots))}
    if {path.name for path in destination.iterdir()} - expected_roots:
        raise RetirementError("Restore destination contains unrecorded entries")
    restored = []
    for index, root in enumerate(inventory.roots):
        target_root = destination / f"root-{index}"
        if os.path.lexists(target_root):
            expected_entries = {entry.path: entry for entry in root.entries}
            for current in scan(target_root):
                expected = expected_entries.get(current.path)
                if expected is None or (current.kind, current.digest, current.size) != (
                    expected.kind,
                    expected.digest,
                    expected.size,
                ):
                    raise RetirementError("Isolated restore destination contains changed entries")
        for entry in root.entries:
            target = target_root if entry.path == "." else target_root / entry.path
            if os.path.lexists(target):
                continue
            parent = directory_fd(target.parent)
            try:
                if entry.kind == "directory":
                    os.mkdir(target.name, 0o700, dir_fd=parent)
                else:
                    data = artifact_bytes(backup_root, backup.artifacts[entry_key(root, entry)])
                    if sha(data) != entry.digest:
                        raise RetirementError("Backup does not match inventoried entry")
                    if entry.kind == "symlink":
                        os.symlink(data.decode(), target.name, dir_fd=parent)
                    else:
                        fd = os.open(
                            target.name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=parent,
                        )
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(data)
                            handle.flush()
                            os.fsync(handle.fileno())
            finally:
                os.close(parent)
        for entry in reversed(root.entries):
            if entry.kind == "symlink":
                continue
            target = target_root if entry.path == "." else target_root / entry.path
            parent = directory_fd(target.parent)
            try:
                os.chmod(target.name, entry.mode, dir_fd=parent, follow_symlinks=False)
            finally:
                os.close(parent)
        entries = scan(target_root)
        original_content = [(e.path, e.kind, e.size, e.digest, e.mode) for e in root.entries]
        restored_content = [(e.path, e.kind, e.size, e.digest, e.mode) for e in entries]
        if original_content != restored_content:
            raise RetirementError("Restored tree differs from inventory")
        restored.append(root.model_copy(update={"path": str(target_root), "entries": entries}))
    return restored
