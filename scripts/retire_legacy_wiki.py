"""Retire the archived wiki using an explicit inventory and private recovery set.

Run with ``uv run python -m scripts.retire_legacy_wiki`` (inventory is the default).
Connections: GOBBY_RETIRE_DATABASE_URL (defaults to operator bootstrap),
GOBBY_RETIRE_QDRANT_URL, GOBBY_RETIRE_QDRANT_API_KEY, GOBBY_RETIRE_FALKOR_URL.
Rehearsal uses the same names with GOBBY_RETIRE_ISOLATED_ prefix. PostgreSQL must
be a separate prepared baseline-425 database with a canonical maintenance epoch
and verified hub backup; ``restore`` stages the scoped data before that backup.
The general maintenance backup is a separate schema-runner prerequisite. This
procedure's recovery artifacts contain only the exact inventoried wiki targets.

Runtime proof is JSON matching RuntimeProof: inventory digest, installed main
checkout/commit, gcode/gdaemon/ghook path+SHA256 pairs, and six named check evidence
files. Each check file is owner-only JSON containing check, inventory_digest,
command (argv), exit_code=0, passed=true, and nonempty output. Required checks:
http_missing_routes, mcp_absent, no_injection, no_writers, no_regeneration,
installed_startup. Each check also records created_at and runtime_digest, the
SHA256 of canonical JSON containing installed_repository, installed_commit and
the binaries mapping. Check timestamps precede proof creation by at most30minutes.
This records reviewed executable evidence; apply additionally
rechecks installed hashes, commit, current writers and all remaining targets.

Operational sequence (all paths come from the inventory output):
  inventory
  backup --inventory <private>/inventory.json
  restore --inventory <private>/inventory.json
  rehearse --inventory <private>/inventory.json --defer-schema
  # Operator runs canonical hub-maintenance schema-apply against isolated425.
  rehearse --inventory <private>/inventory.json
  # Install removal in MAIN; prove it using isolated426 runtime state while live
  # stays stopped425. Place proof/evidence in the private inventory directory.
  apply --inventory <private>/inventory.json --runtime-proof <private>/proof.json --defer-schema
  # MAIN operator: uv run gobby hub-maintenance run schema-apply
  retry --inventory <private>/inventory.json --runtime-proof <private>/proof.json

Deferred receipts have complete=false and deferred_schema=true. Completion requires
schema exactly426 and all five wiki tables absent. Future migrations are refused.
Rehearsal PG tools additionally require GOBBY_RETIRE_ISOLATED_POSTGRES_CONTAINER;
its local published port and gobby.retirement.role=rehearsal label are verified.
Recovery restore destinations stay private and isolated; restoring the live old
runtime additionally requires the recorded Git baseline and canonical hub recovery.
Inventoried Cargo installs additionally require --cargo-quiet-window on apply/retry:
announce and fence Cargo installs before using Cargo's own exact package uninstall.
The scoped Cargo backup contains only the selected package entry and binary;
unrelated registry records and binaries are compared by fingerprints.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.wiki_retirement_cargo import (
    delete_cargo,
    discover_cargo,
    rehearse_cargo,
    restore_cargo,
    source_version,
    verify_cargo,
)
from scripts.wiki_retirement_code_index import CodeIndexStorage
from scripts.wiki_retirement_inventory import (
    Inventory,
    RetirementError,
    Root,
    StoreTarget,
    baseline,
    canonical,
    checkout_identities,
    discover_roots,
    git,
    native_directories,
    now,
    read_file,
    reject_new_roots,
    sha,
    validate_inventory,
    writer_processes,
)
from scripts.wiki_retirement_receipts import (
    Backup,
    Journal,
    Receipt,
    RehearsalTrees,
    RuntimeProof,
    artifact_bytes,
    backup_files,
    delete_files,
    operation_lock,
    prepare_directory,
    put_artifact,
    read_private,
    restore_files,
    runtime_digest,
    verify_remaining,
    write_private,
)
from scripts.wiki_retirement_storage import Storage, configured_stores

CHECKS = {
    "http_missing_routes",
    "mcp_absent",
    "no_injection",
    "no_writers",
    "no_regeneration",
    "installed_startup",
}


def code_index_manager(stores: Storage) -> CodeIndexStorage:
    manager = getattr(stores, "code_index", None)
    if not isinstance(manager, CodeIndexStorage):
        raise RetirementError("Exact code-index storage is required by this inventory")
    return manager


def inventory(
    repository: Path, gobby_home: Path, files_home: Path, stores: Storage | None, errors: list[str]
) -> Inventory:
    checkouts = {repository}
    identities = {}
    targets = []
    if stores is not None:
        try:
            identities = stores.identities()
            checkouts.update(getattr(stores, "checkouts", lambda: [])())
            targets = stores.inventory()
        except Exception as exc:
            errors.append(
                f"Datastore inventory incomplete: {exc}"
                if isinstance(exc, RetirementError)
                else f"Datastore inventory incomplete: {type(exc).__name__}"
            )
    # Git's recorded worktrees include historical wiki-branch checkouts. Only
    # marked vault children of each checkout can become filesystem targets.
    for checkout in list(checkouts):
        try:
            for line in git(checkout, "worktree", "list", "--porcelain").splitlines():
                if line.startswith("worktree "):
                    candidate = Path(line.removeprefix("worktree "))
                    if candidate.is_dir():
                        checkouts.add(candidate)
        except subprocess.CalledProcessError:
            if os.path.lexists(checkout / ".git"):
                errors.append(f"Cannot inspect recorded checkout: {checkout}")
    roots = []
    cargo_installs = []
    try:
        roots = discover_roots(repository, gobby_home, files_home, sorted(checkouts))
        for tree in roots:
            if Path(tree.owner_path).name == ".crates2.json":
                item = discover_cargo(Path(tree.path).parent, repository, sorted(checkouts))
                if item is None:
                    raise RetirementError("Cargo ownership disappeared during inventory")
                cargo_installs.append(item)
    except (OSError, ValueError, RetirementError) as exc:
        errors.append(f"Filesystem inventory incomplete: {exc}")
    value = Inventory(
        created_at=now(),
        repository=str(repository),
        baseline=baseline(repository),
        uid=os.getuid(),
        gobby_home=str(gobby_home),
        files_home=str(files_home),
        checkouts=sorted(map(str, checkouts)),
        checkout_projects=checkout_identities(sorted(checkouts)),
        native_dirs=list(map(str, native_directories(gobby_home))),
        cargo_installs=cargo_installs,
        roots=roots,
        stores=targets,
        backend_identities=identities,
        writers=writer_processes(),
        errors=errors,
    )
    manager = getattr(stores, "code_index", None)
    if manager is not None:
        try:
            targets = manager.inventory(
                roots,
                sorted(checkouts),
                gobby_home,
                Path(__file__).resolve().parents[1],
                value.baseline,
                repository,
            )
            value = value.model_copy(update={"code_indexes": targets})
        except Exception as exc:
            errors.append(
                f"Code-index inventory incomplete: {exc}"
                if isinstance(exc, RetirementError)
                else f"Code-index inventory incomplete: {type(exc).__name__}"
            )
            value = value.model_copy(update={"errors": errors})
    return value


def load_inventory(path: Path) -> Inventory:
    value = Inventory.model_validate_json(read_private(path))
    validate_inventory(value)
    expected = prepare_directory(value) / "inventory.json"
    if path != expected:
        raise RetirementError(
            f"Inventory must remain in its private retirement directory: {expected}"
        )
    return value


def check_connections(inventory: Inventory, stores: Storage) -> None:
    if stores.identities() != inventory.backend_identities:
        raise RetirementError("Backend identity differs from inventory")


def create_backup(inventory: Inventory, root: Path, stores: Storage) -> Backup:
    check_connections(inventory, stores)
    path = root / "backup.json"
    if path.exists():
        previous = Backup.model_validate_json(read_private(path))
        if previous.inventory_digest != inventory.digest or previous.baseline != inventory.baseline:
            raise RetirementError("Backup belongs to another inventory")
        if previous.complete:
            validate_backup(inventory, root, previous)
            return previous
    value = Backup(
        inventory_digest=inventory.digest,
        baseline=inventory.baseline,
        created_at=now(),
        artifacts={},
        complete=False,
    )
    write_private(path, value.model_dump_json(indent=2).encode())
    try:
        artifacts = backup_files(inventory, root)
        for item in inventory.cargo_installs:
            if not verify_cargo(item):
                raise RetirementError("Cargo package disappeared before backup")
            artifacts[item.key] = put_artifact(
                root, item.key, canonical({"package": item.package, "metadata": item.metadata})
            )
        value = value.model_copy(update={"artifacts": artifacts})
        write_private(path, value.model_dump_json(indent=2).encode())
        for target in inventory.stores:
            data = stores.backup(target)
            artifacts[target.key] = put_artifact(root, target.key, data)
            value = value.model_copy(update={"artifacts": dict(artifacts)})
            write_private(path, value.model_dump_json(indent=2).encode())
        for code_target in inventory.code_indexes:
            data = code_index_manager(stores).backup(code_target)
            artifacts[code_target.key] = put_artifact(root, code_target.key, data)
            value = value.model_copy(update={"artifacts": dict(artifacts)})
            write_private(path, value.model_dump_json(indent=2).encode())
        value = value.model_copy(update={"complete": True})
        write_private(path, value.model_dump_json(indent=2).encode())
        return value
    except BaseException as exc:
        write_private(
            root / "backup-failure.json",
            canonical(
                {
                    "inventory_digest": inventory.digest,
                    "at": now(),
                    "error": type(exc).__name__,
                    "completed_artifacts": sorted(value.artifacts),
                    "complete": False,
                }
            ),
        )
        raise


def validate_backup(inventory: Inventory, root: Path, backup: Backup) -> None:
    if not backup.complete or backup.inventory_digest != inventory.digest:
        raise RetirementError("A complete inventory-bound backup is required")
    if backup.baseline != inventory.baseline:
        raise RetirementError("Backup baseline differs")
    expected = {target.key for target in inventory.stores}
    expected.update(target.key for target in inventory.code_indexes)
    expected.update(item.key for item in inventory.cargo_installs)
    for tree in inventory.roots:
        expected.update(
            f"file:{tree.path}:{entry.path}" for entry in tree.entries if entry.kind != "directory"
        )
    if set(backup.artifacts) != expected:
        raise RetirementError("Backup coverage differs from inventory")
    for artifact in backup.artifacts.values():
        artifact_bytes(root, artifact)


def verify_runtime(inventory: Inventory, proof: RuntimeProof, root: Path) -> None:
    if proof.inventory_digest != inventory.digest:
        raise RetirementError("Runtime proof belongs to another inventory")
    created = datetime.fromisoformat(proof.created_at)
    if created.tzinfo is None or not timedelta(0) <= datetime.now(UTC) - created <= timedelta(
        hours=12
    ):
        raise RetirementError("Runtime proof must be from the last 12 hours")
    repository = Path(proof.installed_repository)
    if git(repository, "rev-parse", "HEAD") != proof.installed_commit:
        raise RetirementError("Installed checkout changed since runtime proof")
    common = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    actual = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-dir"))
    if common != actual:
        raise RetirementError("Runtime proof must identify the main checkout")
    git(repository, "merge-base", "--is-ancestor", inventory.baseline, proof.installed_commit)
    if set(proof.binaries) != {"gcode", "gdaemon", "ghook"}:
        raise RetirementError("Runtime proof must identify the three surviving native set members")
    native_dir = Path(
        os.environ.get("GOBBY_NATIVE_BIN_DIR", str(Path(inventory.gobby_home) / "bin"))
    )
    if not native_dir.is_absolute() or str(native_dir) not in inventory.native_dirs:
        raise RetirementError("Runtime native directory was not part of the inventory")
    for name, evidence in proof.binaries.items():
        path = Path(evidence.path)
        if path != native_dir / name:
            raise RetirementError("Runtime binary is outside the installed native set")
        if sha(read_file(path)) != evidence.digest:
            raise RetirementError(f"Installed binary changed: {name}")
    if set(proof.checks) != CHECKS:
        raise RetirementError("Runtime proof lacks required executable checks")
    runtime_hash = runtime_digest(proof)
    for name, evidence in proof.checks.items():
        path = Path(evidence.path)
        if path.parent != root:
            raise RetirementError("Runtime evidence must stay in the private inventory directory")
        data = read_private(path)
        if sha(data) != evidence.digest:
            raise RetirementError(f"Runtime evidence changed: {name}")
        check = json.loads(data)
        if (
            check.get("check"),
            check.get("inventory_digest"),
            check.get("exit_code"),
            check.get("passed"),
        ) != (name, inventory.digest, 0, True):
            raise RetirementError(f"Runtime check did not pass for this inventory: {name}")
        command = check.get("command")
        checked_at = datetime.fromisoformat(check.get("created_at", ""))
        if (
            check.get("runtime_digest") != runtime_hash
            or checked_at.tzinfo is None
            or not (timedelta(0) <= created - checked_at <= timedelta(minutes=30))
        ):
            raise RetirementError(
                f"Runtime check does not bind the installed runtime and time: {name}"
            )
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) for arg in command)
            or not isinstance(check.get("output"), str)
            or not check["output"]
        ):
            raise RetirementError(f"Runtime check lacks command/output evidence: {name}")
    if writer_processes():
        raise RetirementError("Wiki writer processes are still running")


def check_remaining_stores(inventory: Inventory, stores: Storage, journal: Journal) -> None:
    actual = {target.key: target for target in stores.inventory()}
    expected = {target.key: target for target in inventory.stores}
    if actual.keys() - expected.keys():
        raise RetirementError("New datastore artifacts require a new inventory")
    for key, target in expected.items():
        data = stores.capture(target)
        state = journal.state(key)
        if data is None:
            if state not in {"intent", "done"} and target.digest != sha(b"{}"):
                raise RetirementError(f"Unrecorded missing datastore target: {key}")
        elif state == "done" or sha(data) != target.digest:
            raise RetirementError(f"Recreated or changed datastore target: {key}")


def verify_ownership(inventory: Inventory, journal: Journal) -> None:
    source_entries = {
        str(Path(tree.path) / entry.path): f"file:{tree.path}:{entry.path}"
        for tree in inventory.roots
        for entry in tree.entries
    }
    for tree in inventory.roots:
        cargo = next(
            (
                item
                for item in inventory.cargo_installs
                if Path(tree.path) == Path(item.home) / "bin/gwiki"
            ),
            None,
        )
        if cargo is not None:
            source_version(
                cargo.package, Path(inventory.repository), list(map(Path, inventory.checkouts))
            )
            present = verify_cargo(cargo)
            if not present and journal.state(cargo.key) not in {"intent", "done"}:
                raise RetirementError("Cargo ownership disappeared without recorded intent")
            continue
        path = Path(tree.owner_path)
        try:
            current = sha(read_file(path))
        except FileNotFoundError:
            key = source_entries.get(str(path))
            if key is not None and journal.state(key) in {"done", "intent"}:
                continue
            raise RetirementError(f"Ownership evidence disappeared: {path}") from None
        if current != tree.owner_digest:
            raise RetirementError(f"Ownership evidence changed: {path}")


def apply(
    inventory: Inventory,
    backup: Backup,
    root: Path,
    stores: Storage,
    proof: RuntimeProof,
    *,
    defer_schema: bool = False,
    cargo_quiet_window: bool = False,
) -> Receipt:
    if inventory.cargo_installs and not cargo_quiet_window:
        raise RetirementError("Cargo removal requires the announced --cargo-quiet-window")
    validate_inventory(inventory)
    validate_backup(inventory, root, backup)
    verify_runtime(inventory, proof, root)
    check_connections(inventory, stores)
    rehearsal = Receipt.model_validate_json(read_private(root / "rehearsal.json"))
    expected_backup = sha(canonical(backup.model_dump(mode="json")))
    if (rehearsal.inventory_digest, rehearsal.backup_digest, rehearsal.complete) != (
        inventory.digest,
        expected_backup,
        True,
    ):
        raise RetirementError("Deletion and restoration rehearsal must complete before live apply")
    journal = Journal(root / "apply.json", inventory, backup, "apply")
    try:
        reject_new_roots(inventory)
        verify_ownership(inventory, journal)
        check_remaining_stores(inventory, stores, journal)
        for tree in inventory.roots:
            verify_remaining(tree, journal)
        # Registrations first, projections next, files next, guarded schema last.
        ordered = sorted(inventory.stores, key=lambda item: item.kind == "postgres")
        for target in ordered:
            if target.kind == "postgres":
                continue
            delete_store(target, stores, journal)
        for item in inventory.cargo_installs:
            tree = next(
                tree for tree in inventory.roots if Path(tree.path) == Path(item.home) / "bin/gwiki"
            )
            delete_cargo(item, tree, journal)
        delete_files(inventory, journal)
        for target in ordered:
            if target.kind == "postgres":
                if defer_schema and journal.state(target.key) != "done":
                    stores.require_schema_pending()
                    journal.mark(target.key, "intent")
                    journal.defer_schema()
                    return journal.receipt
                delete_store(target, stores, journal)
        for code_target in inventory.code_indexes:
            stores.require_schema_retired()
            code_index_manager(stores).delete(
                code_target,
                artifact_bytes(root, backup.artifacts[code_target.key]),
                inventory.digest,
                root,
                journal,
            )
        reject_new_roots(inventory)
        verify_ownership(inventory, journal)
        check_remaining_stores(inventory, stores, journal)
        for tree in inventory.roots:
            verify_remaining(tree, journal)
        journal.finish()
        return journal.receipt
    except BaseException as exc:
        journal.failed(
            f"{type(exc).__name__}: {exc}"
            if isinstance(exc, RetirementError)
            else type(exc).__name__
        )
        raise


def delete_store(target: StoreTarget, stores: Storage, journal: Journal) -> None:
    if journal.state(target.key) == "done":
        return
    data = stores.capture(target)
    if data is None and journal.state(target.key) == "intent":
        if target.kind == "postgres":
            stores.require_schema_retired()
        journal.mark(target.key, "done")
        return
    if data is not None and sha(data) != target.digest:
        raise RetirementError(f"Datastore changed immediately before delete: {target.key}")
    if target.kind == "postgres":
        stores.require_schema_pending()
    journal.mark(target.key, "intent")
    stores.delete(target)
    if stores.capture(target) is not None:
        raise RetirementError(f"Datastore deletion did not complete: {target.key}")
    if target.kind == "postgres":
        stores.require_schema_retired()
    journal.mark(target.key, "done")


def restore(
    inventory: Inventory,
    backup: Backup,
    root: Path,
    source: Storage,
    isolated: Storage,
    destination: Path,
) -> list[Root]:
    validate_backup(inventory, root, backup)
    source_ids, isolated_ids = source.identities(), isolated.identities()
    if any(source_ids[key] == isolated_ids[key] for key in source_ids):
        raise RetirementError("Every rehearsal datastore must be isolated from the source")
    destination = destination.absolute()
    if not destination.is_relative_to(root):
        raise RetirementError("Rehearsal files must remain under the private inventory directory")
    trees = restore_files(inventory, backup, root, destination)
    for index, item in enumerate(inventory.cargo_installs):
        tree = next(
            tree for tree in inventory.roots if Path(tree.path) == Path(item.home) / "bin/gwiki"
        )
        restore_cargo(item, tree, backup, root, root / f"cargo-staged-{index}")
    for target in inventory.stores:
        isolated.restore(target, artifact_bytes(root, backup.artifacts[target.key]))
        data = isolated.capture(target)
        if (data is None and target.digest != sha(b"{}")) or (
            data is not None and sha(data) != target.digest
        ):
            raise RetirementError(f"Restored datastore differs: {target.key}")
    for code_target in inventory.code_indexes:
        manager = code_index_manager(isolated)
        staged = manager.staged_root(code_target, root)
        manager.restore(
            code_target, artifact_bytes(root, backup.artifacts[code_target.key]), staged
        )
    return list(trees)


def rehearse(
    inventory: Inventory,
    backup: Backup,
    root: Path,
    source: Storage,
    isolated: Storage,
    *,
    defer_schema: bool = False,
) -> Receipt:
    validate_backup(inventory, root, backup)
    source_ids, isolated_ids = source.identities(), isolated.identities()
    if any(source_ids[key] == isolated_ids[key] for key in source_ids):
        raise RetirementError("Every rehearsal datastore must be isolated from the source")
    journal = Journal(root / "rehearsal.json", inventory, backup, "rehearsal")
    if journal.receipt.complete:
        return journal.receipt
    first, second = root / "rehearsal-before", root / "rehearsal-after"
    trees_path = root / "rehearsal-trees.json"
    try:
        if trees_path.exists():
            checkpoint = RehearsalTrees.model_validate_json(read_private(trees_path))
            if (checkpoint.inventory_digest, checkpoint.backup_digest) != (
                inventory.digest,
                sha(canonical(backup.model_dump(mode="json"))),
            ):
                raise RetirementError(
                    "Rehearsal tree checkpoint belongs to another inventory or backup"
                )
            trees = checkpoint.roots
        else:
            trees = restore_files(inventory, backup, root, first)
            checkpoint = RehearsalTrees(
                inventory_digest=inventory.digest,
                backup_digest=sha(canonical(backup.model_dump(mode="json"))),
                roots=trees,
            )
            write_private(trees_path, checkpoint.model_dump_json().encode())
        if len(trees) != len(inventory.roots):
            raise RetirementError("Rehearsal tree checkpoint coverage differs")
        for index, (tree, original) in enumerate(zip(trees, inventory.roots, strict=True)):
            if (
                Path(tree.path) != first / f"root-{index}"
                or tree.kind != original.kind
                or (tree.owner_path, tree.owner_digest)
                != (original.owner_path, original.owner_digest)
            ):
                raise RetirementError(
                    "Rehearsal tree checkpoint is outside its exact private destination"
                )
            shape = [(e.path, e.kind, e.mode, e.digest, e.size) for e in tree.entries]
            expected = [(e.path, e.kind, e.mode, e.digest, e.size) for e in original.entries]
            if shape != expected:
                raise RetirementError("Rehearsal tree checkpoint differs from inventoried content")
        copied = inventory.model_copy(update={"roots": trees, "backend_identities": isolated_ids})
        for target in sorted(inventory.stores, key=lambda item: item.kind == "postgres"):
            if journal.state(f"restored:{target.key}") == "done":
                continue
            if target.kind == "postgres":
                continue
            delete_store(target, isolated, journal)
        delete_files(copied, journal)
        rehearse_cargo(inventory, backup, root, journal)
        for target in inventory.stores:
            if target.kind != "postgres" or journal.state(f"restored:{target.key}") == "done":
                continue
            if defer_schema and journal.state(target.key) != "done":
                isolated.require_schema_pending()
                journal.mark(target.key, "intent")
                journal.defer_schema()
                return journal.receipt
            delete_store(target, isolated, journal)
        for code_target in inventory.code_indexes:
            if journal.state(f"restored:{code_target.key}") != "done":
                isolated.require_schema_retired()
                code_index_manager(isolated).delete(
                    code_target,
                    artifact_bytes(root, backup.artifacts[code_target.key]),
                    inventory.digest,
                    root,
                    journal,
                )
        for target in inventory.stores:
            if journal.state(f"restored:{target.key}") != "done":
                isolated.restore(target, artifact_bytes(root, backup.artifacts[target.key]))
                data = isolated.capture(target)
                if data is None or sha(data) != target.digest:
                    raise RetirementError(f"Post-deletion restore differs: {target.key}")
                journal.mark(f"restored:{target.key}", "done")
        for code_target in inventory.code_indexes:
            if journal.state(f"restored:{code_target.key}") != "done":
                manager = code_index_manager(isolated)
                staged = manager.staged_root(code_target, root)
                manager.restore(
                    code_target, artifact_bytes(root, backup.artifacts[code_target.key]), staged
                )
                journal.mark(f"restored:{code_target.key}", "done")
        if journal.state("restored:files") != "done":
            restore_files(inventory, backup, root, second)
            journal.mark("restored:files", "done")
        journal.finish()
        return journal.receipt
    except BaseException as exc:
        journal.failed(
            f"{type(exc).__name__}: {exc}"
            if isinstance(exc, RetirementError)
            else type(exc).__name__
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "operation",
        nargs="?",
        default="inventory",
        choices=("inventory", "backup", "restore", "rehearse", "apply", "retry"),
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--gobby-home", type=Path, default=Path.home() / ".gobby")
    parser.add_argument("--files-home", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--runtime-proof", type=Path)
    parser.add_argument(
        "--cargo-quiet-window",
        action="store_true",
        help="Confirm the announced quiet window also fences Cargo install/uninstall",
    )
    parser.add_argument(
        "--defer-schema",
        action="store_true",
        help="Record exact426 intent and return incomplete for canonical hub-maintenance orchestration",
    )
    options = parser.parse_args(argv)
    stores = None
    isolated = None
    try:
        errors = []
        try:
            stores = configured_stores()
        except Exception as exc:
            if options.operation != "inventory":
                raise
            errors.append(str(exc) if isinstance(exc, RetirementError) else type(exc).__name__)
        if options.operation == "inventory":
            from gobby.config.bootstrap import load_bootstrap

            files_home = options.files_home
            if files_home is None:
                configured_home = load_bootstrap().files_home
                if configured_home is None:
                    raise RetirementError("The local files owner home is required")
                files_home = Path(configured_home)
            value = inventory(
                options.repository.absolute(),
                options.gobby_home.absolute(),
                files_home.absolute(),
                stores,
                errors,
            )
            root = prepare_directory(value)
            write_private(root / "inventory.json", value.model_dump_json(indent=2).encode())
            print(
                json.dumps(
                    {
                        "inventory": str(root / "inventory.json"),
                        "digest": value.digest,
                        "roots": len(value.roots),
                        "targets": len(value.stores),
                        "writers": len(value.writers),
                        "errors": value.errors,
                    }
                )
            )
            return 1 if value.errors else 0
        if options.inventory is None or stores is None:
            raise RetirementError("An explicit --inventory is required")
        value = load_inventory(options.inventory.absolute())
        root = prepare_directory(value)
        with operation_lock(root):
            if options.operation == "backup":
                create_backup(value, root, stores)
                print(json.dumps({"backup": str(root / "backup.json"), "complete": True}))
                return 0
            backup = Backup.model_validate_json(read_private(root / "backup.json"))
            if options.operation in {"restore", "rehearse"}:
                isolated = configured_stores(isolated=True)
                if options.operation == "restore":
                    restore(value, backup, root, stores, isolated, root / "staged-restore")
                    print(
                        json.dumps(
                            {
                                "restored": True,
                                "next": "verify isolated maintenance backup, then rehearse",
                            }
                        )
                    )
                    return 0
                result = rehearse(
                    value, backup, root, stores, isolated, defer_schema=options.defer_schema
                )
            else:
                if options.runtime_proof is None:
                    raise RetirementError("Apply/retry require --runtime-proof")
                proof = RuntimeProof.model_validate_json(
                    read_private(options.runtime_proof.absolute())
                )
                result = apply(
                    value,
                    backup,
                    root,
                    stores,
                    proof,
                    defer_schema=options.defer_schema,
                    cargo_quiet_window=options.cargo_quiet_window,
                )
            print(result.model_dump_json())
            return 0
    except (RetirementError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Retirement refused: {exc}", file=sys.stderr)
        return 1
    finally:
        if isolated is not None:
            isolated.close()
        if stores is not None:
            stores.close()


if __name__ == "__main__":
    raise SystemExit(main())
