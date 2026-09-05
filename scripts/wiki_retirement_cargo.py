"""Retire the exact source-installed Gobby Cargo package through Cargo itself."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from scripts.wiki_retirement_inventory import (
    CargoInstall,
    Inventory,
    RetirementError,
    Root,
    canonical,
    directory_fd,
    git,
    read_file,
    scan,
    sha,
)
from scripts.wiki_retirement_receipts import (
    Backup,
    Journal,
    artifact_bytes,
    private_directory,
    read_private,
    write_private,
)

PACKAGE = re.compile(r"^gobby-wiki ([0-9][A-Za-z0-9.+-]*) \(path\+(file://[^\n]+)\)$")


def metadata(home: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        json.loads(read_file(home / ".crates2.json")),
        tomllib.loads(read_file(home / ".crates.toml").decode()),
    )


def source_version(package: str, repository: Path, checkouts: list[Path]) -> str:
    match = PACKAGE.fullmatch(package)
    if match is None:
        raise RetirementError("Cargo gwiki package is not a source-installed Gobby package")
    url = urlsplit(match[2])
    source = Path(unquote(url.path))
    if url.netloc or not source.is_absolute() or ".." in source.parts:
        raise RetirementError("Cargo gwiki source is not an absolute local path")
    checkout = source.parent.parent
    if source != checkout / "crates/gwiki" or checkout not in checkouts:
        raise RetirementError("Cargo gwiki source is not in a recorded checkout")
    common_args = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    if git(checkout, *common_args) != git(repository, *common_args):
        raise RetirementError("Cargo gwiki source belongs to a different repository")
    return match[1]


def remaining_metadata(data: tuple[dict[str, Any], dict[str, Any]], package: str) -> bytes:
    # Copy only in memory for a semantic preservation comparison, never into backup.
    modern, legacy = json.loads(canonical(data))
    modern["installs"].pop(package, None)
    legacy["v1"].pop(package, None)
    return canonical([modern, legacy])


def discover_cargo(directory: Path, repository: Path, checkouts: list[Path]) -> CargoInstall | None:
    home = directory.parent
    if directory.name != "bin" or not os.path.lexists(home / ".crates2.json"):
        return None
    modern, legacy = metadata(home)
    owners = [key for key, value in modern["installs"].items() if "gwiki" in value["bins"]]
    if not owners:
        return None
    if len(owners) != 1:
        raise RetirementError("Cargo gwiki has multiple package owners")
    package = owners[0]
    source_version(package, repository, checkouts)
    selected = modern["installs"][package]
    if selected["bins"] != ["gwiki"] or legacy["v1"].get(package) != ["gwiki"]:
        raise RetirementError("Cargo gwiki metadata does not agree on the exact package binaries")
    if any(key != package and "gwiki" in value for key, value in legacy["v1"].items()):
        raise RetirementError("Cargo legacy registry has another gwiki owner")
    names = {
        name
        for key, value in modern["installs"].items()
        if key != package
        for name in value["bins"]
    }
    survivors = []
    for name in sorted(names):
        if Path(name).name != name or name in {".", ".."}:
            raise RetirementError("Cargo registry contains an out-of-scope binary name")
        entries = scan(directory / name)
        if len(entries) != 1 or entries[0].kind == "directory":
            raise RetirementError("Cargo survivor must be one file or symlink")
        survivors.append(entries[0].model_copy(update={"path": name}))
    return CargoInstall(
        home=str(home),
        package=package,
        metadata=selected,
        other_metadata_digest=sha(remaining_metadata((modern, legacy), package)),
        other_binaries=survivors,
    )


def verify_cargo(item: CargoInstall) -> bool:
    """Return whether both exact package entries remain; always verify survivors."""
    home = Path(item.home)
    modern, legacy = metadata(home)
    if sha(remaining_metadata((modern, legacy), item.package)) != item.other_metadata_digest:
        raise RetirementError("Unrelated Cargo package registrations changed")
    for expected in item.other_binaries:
        actual = scan(home / "bin" / expected.path)
        if len(actual) != 1 or actual[0].model_copy(update={"path": expected.path}) != expected:
            raise RetirementError("Unrelated Cargo binary changed")
    selected = modern["installs"].get(item.package)
    old = legacy["v1"].get(item.package)
    if selected is None and old is None:
        return False
    if selected != item.metadata or old != ["gwiki"]:
        raise RetirementError("Cargo package registration changed or is partially removed")
    return True


def cargo_writers() -> bool:
    output = subprocess.run(
        ["ps", "-axo", "comm=,args="], capture_output=True, text=True, check=True, timeout=10
    ).stdout
    return any(
        Path(fields[0]).name == "cargo" and re.search(r"\b(?:install|uninstall)\b", fields[1])
        for line in output.splitlines()
        if len(fields := line.split(maxsplit=1)) == 2
    )


def delete_cargo(item: CargoInstall, tree: Root, journal: Journal) -> None:
    binary = Path(item.home) / "bin/gwiki"
    file_key = f"file:{tree.path}:."
    present = verify_cargo(item)
    if not present:
        if journal.state(item.key) not in {"intent", "done"} or os.path.lexists(binary):
            raise RetirementError("Cargo removal has no matching intent or binary was recreated")
        journal.mark(file_key, "done")
        journal.mark(item.key, "done")
        return
    if journal.state(item.key) == "done" or scan(binary) != tree.entries:
        raise RetirementError("Cargo binary changed or was recreated")
    if cargo_writers():
        raise RetirementError("Cargo install/uninstall must drain before retirement")
    # The operator's announced quiet window fences installs. Cargo owns its own
    # metadata lock and binary removal; never emulate its private registry writer.
    journal.mark(item.key, "intent")
    journal.mark(file_key, "intent")
    match = PACKAGE.fullmatch(item.package)
    if match is None:
        raise RetirementError("Unexpected Cargo package")
    subprocess.run(
        [
            "cargo",
            "uninstall",
            "--root",
            item.home,
            "--package",
            f"gobby-wiki@{match[1]}",
            "--bin",
            "gwiki",
        ],
        cwd=item.home,
        check=True,
        capture_output=True,
        timeout=60,
    )
    if verify_cargo(item) or os.path.lexists(binary):
        raise RetirementError("Cargo uninstall did not remove the exact package and binary")
    journal.mark(file_key, "done")
    journal.mark(item.key, "done")


def restore_cargo(
    item: CargoInstall, tree: Root, backup: Backup, root: Path, destination: Path
) -> Root:
    private_directory(destination)
    private_directory(destination / "bin")
    expected = canonical({"package": item.package, "metadata": item.metadata})
    if artifact_bytes(root, backup.artifacts[item.key]) != expected:
        raise RetirementError("Cargo backup differs from inventoried package")
    payloads = {
        ".crates2.json": canonical({"installs": {item.package: item.metadata}}),
        ".crates.toml": ("[v1]\n" + json.dumps(item.package) + ' = ["gwiki"]\n').encode(),
        "bin/gwiki": artifact_bytes(root, backup.artifacts[f"file:{tree.path}:."]),
    }
    for name, data in payloads.items():
        path = destination / name
        if name == "bin/gwiki" and tree.entries[0].kind == "symlink":
            parent = directory_fd(path.parent)
            try:
                if os.path.lexists(path):
                    if os.readlink(path.name, dir_fd=parent).encode() != data:
                        raise RetirementError("Isolated Cargo restore symlink changed")
                else:
                    os.symlink(data.decode(), path.name, dir_fd=parent)
            finally:
                os.close(parent)
            continue
        if os.path.lexists(path):
            if read_file(path) != data:
                raise RetirementError("Isolated Cargo restore destination changed")
        else:
            write_private(path, data)
    binary = destination / "bin/gwiki"
    parent = directory_fd(binary.parent)
    try:
        if tree.entries[0].kind != "symlink":
            os.chmod(binary.name, tree.entries[0].mode, dir_fd=parent, follow_symlinks=False)
    finally:
        os.close(parent)
    return tree.model_copy(update={"path": str(binary), "entries": scan(binary)})


def rehearse_cargo(inventory: Inventory, backup: Backup, root: Path, journal: Journal) -> None:
    for index, item in enumerate(inventory.cargo_installs):
        key = f"rehearsed:{item.key}"
        if journal.state(key) == "done":
            continue
        original = next(
            tree for tree in inventory.roots if Path(tree.path) == Path(item.home) / "bin/gwiki"
        )
        destination = root / f"cargo-before-{index}"
        checkpoint = root / f"cargo-tree-{index}.json"
        if checkpoint.exists():
            saved = json.loads(read_private(checkpoint))
            tree = Root.model_validate(saved["tree"])
            copied = CargoInstall.model_validate(saved["install"])
            if (
                saved["inventory_digest"] != inventory.digest
                or saved["backup_digest"] != sha(canonical(backup.model_dump()))
                or Path(tree.path) != destination / "bin/gwiki"
                or copied.home != str(destination)
                or (copied.package, copied.metadata) != (item.package, item.metadata)
                or [(e.kind, e.mode, e.size, e.digest) for e in tree.entries]
                != [(e.kind, e.mode, e.size, e.digest) for e in original.entries]
            ):
                raise RetirementError("Cargo rehearsal checkpoint differs from its private binding")
        else:
            tree = restore_cargo(item, original, backup, root, destination)
            discovered = discover_cargo(
                destination / "bin",
                Path(inventory.repository),
                list(map(Path, inventory.checkouts)),
            )
            if discovered is None:
                raise RetirementError("Isolated Cargo restore has no package registration")
            copied = discovered
            write_private(
                checkpoint,
                canonical(
                    {
                        "inventory_digest": inventory.digest,
                        "backup_digest": sha(canonical(backup.model_dump())),
                        "tree": tree.model_dump(),
                        "install": copied.model_dump(),
                    }
                ),
            )
        delete_cargo(copied, tree, journal)
        restored = restore_cargo(item, original, backup, root, root / f"cargo-after-{index}")
        if restored.entries[0].digest != original.entries[0].digest:
            raise RetirementError("Restored Cargo binary differs")
        journal.mark(key, "done")
