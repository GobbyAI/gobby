"""Exact SQL recovery and native-command coordination for retired code artifacts."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import uuid4, uuid5

import click
import pytest

from gobby.storage.hub.postgres import PostgresHubDatabase
from scripts import retire_legacy_wiki as cli
from scripts import wiki_retirement_inventory as inv
from scripts.wiki_retirement_code_index import (
    CodeIndexStorage,
    ordered_code_targets,
    project_lock_key,
)
from scripts.wiki_retirement_receipts import (
    Backup,
    Journal,
    private_directory,
    put_artifact,
    write_private,
)
from scripts.wiki_retirement_storage import Datastores, Storage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("GOBBY_RETIRE_TEST_SERVICES") != "1",
        reason="Requires explicitly enabled dedicated retirement datastore fixtures",
    ),
]


@pytest.fixture
def indexed(
    postgres_db: PostgresHubDatabase,
    tmp_path: Path,
) -> Iterator[tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path]]:
    project = str(uuid4())
    mock_machine_id = str(uuid4())
    recovery = tmp_path / "recovery"
    private_directory(recovery)
    parent = recovery / "code-index-rehearsal"
    private_directory(parent)
    root = parent / project
    private_directory(root)
    private_directory(root / ".gobby")
    write_private(
        root / ".gobby/project.json",
        inv.canonical({"id": project, "name": f"retirement-{project}"}),
    )
    inv.git(root, "init", "--quiet")
    inv.git(root, "config", "user.email", "test@example.invalid")
    inv.git(root, "config", "user.name", "Retirement test")
    (root / "wiki").mkdir()
    (root / "wiki/page.md").write_text("legacy wiki")
    (root / "removed.py").write_text("def old_wiki(): pass\n")
    (root / "keep.py").write_text("def preserved(): pass\n")
    inv.git(root, "add", ".")
    inv.git(root, "commit", "--quiet", "-m", "archive")
    inv.git(root, "tag", "-a", "legacy-wiki-before-retirement-21771", "-m", "archive")
    inv.git(root, "rm", "removed.py")
    inv.git(root, "commit", "--quiet", "-m", "remove legacy source")
    home = tmp_path / "home"
    home.mkdir()
    (home / "machine_id").write_text(mock_machine_id)
    CodeIndexStorage._machine(postgres_db, mock_machine_id)
    postgres_db.execute("INSERT INTO code_indexed_projects(id) VALUES (%s)", (project,))
    postgres_db.execute(
        "INSERT INTO code_indexed_project_states(machine_id,project_id,root_path) VALUES (%s,%s,%s)",
        (mock_machine_id, project, str(root)),
    )
    for index, path in enumerate(("wiki/page.md", "removed.py", "keep.py")):
        content_hash = str(index + 1) * 64
        symbol = str(uuid4())
        postgres_db.execute(
            "INSERT INTO code_indexed_files(id,project_id,file_path,language,content_hash,symbol_count) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (str(uuid4()), project, path, "python", content_hash, 1),
        )
        postgres_db.execute(
            "INSERT INTO code_symbols(id,project_id,file_path,name,qualified_name,kind,language,"
            "byte_start,byte_end,line_start,line_end,file_content_hash,content_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                symbol,
                project,
                path,
                "symbol",
                "symbol",
                "function",
                "python",
                0,
                4,
                1,
                1,
                content_hash,
                "a" * 64,
            ),
        )
        postgres_db.execute(
            "INSERT INTO code_content_chunks(id,project_id,file_path,content_hash,chunk_index,"
            "line_start,line_end,content) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (str(uuid4()), project, path, content_hash, 0, 1, 1, f"content for {path}"),
        )
        postgres_db.execute(
            "INSERT INTO code_imports(project_id,source_file,content_hash,target_module) VALUES (%s,%s,%s,%s)",
            (project, path, content_hash, "shared.module"),
        )
        postgres_db.execute(
            "INSERT INTO code_calls(project_id,caller_symbol_id,callee_name,file_path,content_hash) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project, symbol, "shared_call", path, content_hash),
        )
        postgres_db.execute(
            "INSERT INTO code_inheritance(project_id,source_symbol_id,source_name,target_name,"
            "heritage_kind,file_path,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (project, symbol, "symbol", "SharedBase", "INHERITS", path, content_hash),
        )
        postgres_db.execute(
            "INSERT INTO code_indexed_file_states(machine_id,project_id,file_path,content_hash) VALUES (%s,%s,%s,%s)",
            (mock_machine_id, project, path, content_hash),
        )
    schema = postgres_db.fetchone("SELECT current_schema() AS name")
    assert schema is not None
    stores = Datastores(
        postgres_db.conninfo,
        "http://127.0.0.1:6338",
        "redis://127.0.0.1:16389",
        schema=schema["name"],
        isolated=True,
    )
    try:
        manager = stores.code_index
        vault = inv.Root(
            path=str(root / "wiki"),
            kind="checkout_vault",
            owner_path=str(root / "wiki/_gwiki/scope.json"),
            owner_digest="fixture",
            entries=inv.scan(root / "wiki"),
        )
        targets = manager.inventory([vault], [root], home, root, inv.baseline(root), root)
        assert len(targets) == 1
        yield manager, targets[0], root, recovery
    finally:
        stores.close()


def test_code_inventory_binds_exact_vault_and_committed_deletion(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
) -> None:
    manager, target, root, _ = indexed
    assert [file.file_path for file in target.files] == ["removed.py", "wiki/page.md"]
    removed, wiki = target.files
    assert removed.removed_revision == inv.git(root, "rev-parse", "HEAD")
    assert removed.archived_blob == inv.git(root, "rev-parse", f"{inv.baseline(root)}:removed.py")
    assert wiki.owner_path == str(root / "wiki")
    data = json.loads(manager.backup(target))
    assert {row["file_path"] for row in data["sql"]["code_indexed_files"]} == {
        "removed.py",
        "wiki/page.md",
    }
    assert len(data["sql"]["code_symbols"]) == 2
    assert len(data["sql"]["code_imports"]) == 2
    assert len(data["sql"]["code_calls"]) == 2
    assert len(data["sql"]["code_inheritance"]) == 2


def test_code_sql_restoration_is_exact_and_preserves_unrelated_rows(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
) -> None:
    manager, target, root, _ = indexed
    backup = manager.backup(target)
    survivor = postgres_db.fetchone("SELECT * FROM code_indexed_files WHERE file_path='keep.py'")
    postgres_db.execute(
        "DELETE FROM code_indexed_file_states WHERE file_path=ANY(%s)",
        (["wiki/page.md", "removed.py"],),
    )
    postgres_db.execute(
        "DELETE FROM code_indexed_files WHERE file_path=ANY(%s)", (["wiki/page.md", "removed.py"],)
    )
    assert manager.is_absent(manager.capture(target))
    manager.restore(target, backup, root)
    assert manager.digest(manager.capture(target)) == target.digest
    manager.restore(target, backup, root)
    assert manager.digest(manager.capture(target)) == target.digest
    assert (
        postgres_db.fetchone("SELECT * FROM code_indexed_files WHERE file_path='keep.py'")
        == survivor
    )
    assert (root / "keep.py").read_text() == "def preserved(): pass\n"


def test_code_foreign_machine_reference_is_refused_before_backup(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
) -> None:
    manager, target, root, _ = indexed
    other = str(uuid4())
    CodeIndexStorage._machine(postgres_db, other)
    postgres_db.execute(
        "INSERT INTO code_indexed_project_states(machine_id,project_id,root_path) VALUES (%s,%s,%s)",
        (other, target.project_id, str(root)),
    )
    postgres_db.execute(
        "INSERT INTO code_indexed_file_states(machine_id,project_id,file_path,content_hash) VALUES (%s,%s,%s,%s)",
        (other, target.project_id, "wiki/page.md", "1" * 64),
    )
    with pytest.raises(inv.RetirementError, match="Other-machine selectors"):
        manager.backup(target)
    assert postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM code_indexed_file_states WHERE machine_id=%s", (other,)
    ) == {"count": 1}


def test_code_recovery_refuses_cross_project_rows_and_changed_content(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
) -> None:
    manager, target, root, _ = indexed
    data = json.loads(manager.backup(target))
    data["sql"]["code_indexed_files"][0]["project_id"] = str(uuid4())
    with pytest.raises(inv.RetirementError, match="unowned content version"):
        manager.restore(target, inv.canonical(data), root)
    postgres_db.execute(
        "UPDATE code_content_chunks SET content='changed' WHERE file_path='wiki/page.md'"
    )
    with pytest.raises(inv.RetirementError, match="changed since inventory"):
        manager.backup(target)


def test_code_retry_admits_only_approved_remaining_rows(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
) -> None:
    manager, target, _, _ = indexed
    expected = manager.capture(target)
    postgres_db.execute("DELETE FROM code_indexed_file_states WHERE file_path='wiki/page.md'")
    manager.assert_remaining(manager.capture(target), expected)
    postgres_db.execute(
        "UPDATE code_content_chunks SET content='changed' WHERE file_path='wiki/page.md'"
    )
    with pytest.raises(inv.RetirementError, match="changed outside bounded cleanup"):
        manager.assert_remaining(manager.capture(target), expected)


def make_journal(target: inv.CodeIndexTarget, root: Path, recovery: Path) -> Journal:
    inventory = inv.Inventory(
        created_at=inv.now(),
        repository=str(root),
        baseline=inv.baseline(root),
        uid=root.stat().st_uid,
        gobby_home=str(recovery),
        files_home=str(recovery),
        checkouts=[str(root)],
        roots=[],
        stores=[],
        backend_identities={},
        writers=[],
        code_indexes=[target],
    )
    backup = Backup(
        inventory_digest=inventory.digest,
        baseline=inventory.baseline,
        created_at=inv.now(),
        artifacts={},
        complete=True,
    )
    return Journal(recovery / "journal.json", inventory, backup, "rehearsal")


def test_native_coordination_binds_manifest_and_retries_without_unrelated_cleanup(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, target, root, recovery = indexed
    data = manager.backup(target)
    (root / "wiki/page.md").unlink()
    journal = make_journal(target, root, recovery)
    binary = recovery / "gcode"
    binary.write_bytes(b"fixture boundary executable")
    monkeypatch.setattr(
        "scripts.wiki_retirement_code_index.local_native_bin_path", lambda name: binary
    )
    commands: list[list[str]] = []

    def command(argv: list[str], cwd: Path, receipt: Path) -> None:
        assert cwd == root
        assert receipt.is_relative_to(recovery)
        commands.append(argv)
        if "index" in argv:
            assert argv[argv.index("--files") + 1 :] == ["removed.py", "wiki/page.md"]
            postgres_db.execute(
                "DELETE FROM code_indexed_file_states WHERE project_id=%s AND file_path=ANY(%s)",
                (target.project_id, ["removed.py", "wiki/page.md"]),
            )
        elif "--apply" in argv:
            postgres_db.execute(
                "DELETE FROM code_indexed_files WHERE project_id=%s AND file_path=ANY(%s)",
                (target.project_id, ["removed.py", "wiki/page.md"]),
            )

    monkeypatch.setattr(manager, "_command", command)
    manager.delete(target, data, journal.receipt.inventory_digest, recovery, journal)
    assert journal.state(target.key) == "done"
    manifest_path = (
        recovery / "code-index-rehearsal-receipts" / f"{target.project_id}.manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["source_inventory_digest"] == journal.receipt.inventory_digest
    assert manifest["root_path"] == str(root)
    assert manifest["backends"] == manager.native_backends()
    assert manifest["files"] == target.native_files()
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert len(commands) == 3
    manager.delete(target, data, journal.receipt.inventory_digest, recovery, journal)
    assert len(commands) == 3
    assert postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM code_indexed_files WHERE file_path='keep.py'"
    ) == {"count": 1}


def test_native_coordination_refuses_present_file_before_selector_changes(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, target, root, recovery = indexed
    data = manager.backup(target)
    journal = make_journal(target, root, recovery)
    monkeypatch.setattr(
        "scripts.wiki_retirement_code_index.local_native_bin_path", lambda name: root / "keep.py"
    )
    with pytest.raises(inv.RetirementError, match="remains present"):
        manager.delete(target, data, journal.receipt.inventory_digest, recovery, journal)
    assert postgres_db.fetchone("SELECT COUNT(*) AS count FROM code_indexed_file_states") == {
        "count": 3
    }


def test_project_lock_key_matches_native_signed_sha256_prefix() -> None:
    assert project_lock_key("00000000-0000-0000-0000-000000000000") == 2245844412862068868


@pytest.fixture
def tombstone(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
) -> tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path]:
    manager, target, root, recovery = indexed
    file = target.files[0]
    version = inv.CodeIndexVersion(
        id=str(
            uuid5(
                inv.CODE_INDEX_NAMESPACE,
                f"{target.project_id}:{file.file_path}:{inv.CODE_TOMBSTONE_HASH}",
            )
        ),
        content_hash=inv.CODE_TOMBSTONE_HASH,
        symbol_ids=[],
    )
    postgres_db.execute(
        "INSERT INTO code_indexed_files(id,project_id,file_path,language,content_hash,symbol_count,byte_size) "
        "VALUES (%s,%s,%s,%s,%s,0,0)",
        (
            version.id,
            target.project_id,
            file.file_path,
            inv.CODE_TOMBSTONE_LANGUAGE,
            version.content_hash,
        ),
    )
    postgres_db.execute(
        "UPDATE code_indexed_file_states SET content_hash=%s WHERE project_id=%s AND file_path=%s",
        (version.content_hash, target.project_id, file.file_path),
    )
    versions = sorted([*file.versions, version], key=lambda item: item.id)
    target = target.model_copy(
        update={"files": [file.model_copy(update={"versions": versions}), *target.files[1:]]}
    )
    target = target.model_copy(update={"digest": manager.digest(manager.capture(target))})
    return manager, target, root, recovery


def test_canonical_empty_tombstone_backup_and_restore_preserve_real_versions(
    tombstone: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
) -> None:
    manager, target, root, _ = tombstone
    data = manager.backup(target)
    assert len(json.loads(data)["sql"]["code_indexed_files"]) == 3
    postgres_db.execute(
        "DELETE FROM code_indexed_file_states WHERE file_path=ANY(%s)",
        (["removed.py", "wiki/page.md"],),
    )
    postgres_db.execute(
        "DELETE FROM code_indexed_files WHERE file_path=ANY(%s)", (["removed.py", "wiki/page.md"],)
    )
    assert manager.is_absent(manager.capture(target))
    manager.restore(target, data, root)
    assert manager.digest(manager.capture(target)) == target.digest
    assert postgres_db.fetchone(
        "SELECT content_hash FROM code_indexed_file_states WHERE file_path='removed.py'"
    ) == {"content_hash": inv.CODE_TOMBSTONE_HASH}
    assert postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM code_indexed_files WHERE file_path='removed.py'"
    ) == {"count": 2}
    assert postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM code_indexed_files WHERE file_path='keep.py'"
    ) == {"count": 1}


@pytest.mark.parametrize("mutation", ["language", "symbol_count", "byte_size", "content_fact"])
def test_tombstone_metadata_or_content_facts_refuse_backup(
    tombstone: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
    mutation: str,
) -> None:
    manager, target, _, _ = tombstone
    if mutation == "content_fact":
        postgres_db.execute(
            "INSERT INTO code_content_chunks(id,project_id,file_path,content_hash,chunk_index,line_start,line_end,content) VALUES (%s,%s,%s,%s,0,1,1,'forbidden')",
            (str(uuid4()), target.project_id, "removed.py", inv.CODE_TOMBSTONE_HASH),
        )
    elif mutation == "language":
        postgres_db.execute(
            "UPDATE code_indexed_files SET language='python' WHERE content_hash=%s",
            (inv.CODE_TOMBSTONE_HASH,),
        )
    elif mutation == "symbol_count":
        postgres_db.execute(
            "UPDATE code_indexed_files SET symbol_count=1 WHERE content_hash=%s",
            (inv.CODE_TOMBSTONE_HASH,),
        )
    else:
        postgres_db.execute(
            "UPDATE code_indexed_files SET byte_size=1 WHERE content_hash=%s",
            (inv.CODE_TOMBSTONE_HASH,),
        )
    with pytest.raises(inv.RetirementError, match="tombstone"):
        manager.backup(target)
    assert postgres_db.fetchone("SELECT COUNT(*) AS count FROM code_indexed_file_states") == {
        "count": 3
    }


def test_parent_checkout_cleanup_precedes_lexically_earlier_overlay(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
) -> None:
    _, target, root, recovery = indexed
    overlay = recovery / "linked-overlay"
    inv.git(root, "worktree", "add", "--quiet", "--detach", str(overlay))
    parent = target.model_copy(update={"project_id": "d45545c5-ded5-4335-b115-0245752edacf"})
    child = target.model_copy(
        update={"project_id": "bdebec6e-2d60-57da-bd1a-b9ef55f15936", "root_path": str(overlay)}
    )
    assert child.project_id < parent.project_id
    assert ordered_code_targets([child, parent]) == [parent, child]


def test_native_interruption_keeps_durable_intent_and_retries_exact_remaining_rows(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, target, root, recovery = indexed
    data = manager.backup(target)
    (root / "wiki/page.md").unlink()
    journal = make_journal(target, root, recovery)
    monkeypatch.setattr(
        "scripts.wiki_retirement_code_index.local_native_bin_path", lambda _: root / "keep.py"
    )
    interrupted = False
    commands: list[list[str]] = []

    def command(argv: list[str], cwd: Path, receipt: Path) -> None:
        nonlocal interrupted
        commands.append(argv)
        assert cwd == root and receipt.parent.is_relative_to(recovery)
        if "index" in argv:
            postgres_db.execute(
                "DELETE FROM code_indexed_file_states WHERE file_path=ANY(%s)",
                (["removed.py", "wiki/page.md"],),
            )
        elif "--apply" in argv:
            if not interrupted:
                interrupted = True
                postgres_db.execute("DELETE FROM code_indexed_files WHERE file_path='removed.py'")
                raise InterruptedError("native deletion interrupted")
            postgres_db.execute("DELETE FROM code_indexed_files WHERE file_path='wiki/page.md'")

    monkeypatch.setattr(manager, "_command", command)
    with pytest.raises(InterruptedError, match="native deletion interrupted"):
        manager.delete(target, data, journal.receipt.inventory_digest, recovery, journal)
    assert json.loads(journal.path.read_text())["items"][target.key] == "intent"
    assert postgres_db.fetchone(
        "SELECT COUNT(*) AS count FROM code_indexed_files WHERE file_path='wiki/page.md'"
    ) == {"count": 1}
    manager.delete(target, data, journal.receipt.inventory_digest, recovery, journal)
    assert journal.state(target.key) == "done"
    assert len(commands) == 6
    assert postgres_db.fetchone("SELECT file_path FROM code_indexed_files") == {
        "file_path": "keep.py"
    }


def test_native_coordination_refuses_symlink_ancestors_before_commands(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, target, root, recovery = indexed
    data = manager.backup(target)
    (root / "wiki/page.md").unlink()
    (root / "wiki").rmdir()
    (root / "wiki").symlink_to(recovery, target_is_directory=True)
    journal = make_journal(target, root, recovery)
    monkeypatch.setattr(
        "scripts.wiki_retirement_code_index.local_native_bin_path", lambda _: root / "keep.py"
    )
    with pytest.raises(click.ClickException, match="symlink"):
        manager.delete(target, data, journal.receipt.inventory_digest, recovery, journal)
    assert journal.state(target.key) is None
    assert postgres_db.fetchone("SELECT COUNT(*) AS count FROM code_indexed_file_states") == {
        "count": 3
    }
    assert (root / "keep.py").read_text() == "def preserved(): pass\n"


def test_native_failure_receipt_records_exit_and_sanitized_output(tmp_path: Path) -> None:
    receipt = tmp_path / "failure.json"
    with pytest.raises(inv.RetirementError, match="exit 7"):
        CodeIndexStorage._command(
            ["/bin/sh", "-c", "printf diagnostic >&2; exit 7"], tmp_path, receipt
        )
    result = json.loads(receipt.read_text())
    assert result["exit_code"] == 7
    assert result["stderr_digest"] == inv.sha(b"diagnostic")
    assert "stderr" not in result
    assert receipt.stat().st_mode & 0o777 == 0o600
    diagnostic = Path(result["stderr_path"])
    assert diagnostic.read_bytes() == b"diagnostic"
    assert diagnostic.stat().st_mode & 0o777 == 0o600


def test_native_timeout_keeps_private_partial_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == ["gcode", "retire-files"] and kwargs["timeout"] == 900
        raise subprocess.TimeoutExpired(
            ["gcode", "retire-files"], 900, output=b"partial", stderr=b"private detail"
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    receipt = tmp_path / "timeout.json"
    with pytest.raises(inv.RetirementError, match="timed out") as error:
        CodeIndexStorage._command(["gcode", "retire-files"], tmp_path, receipt)
    result = json.loads(receipt.read_text())
    assert result["exit_code"] is None
    assert Path(result["stdout_path"]).read_bytes() == b"partial"
    assert Path(result["stderr_path"]).read_bytes() == b"private detail"
    assert "private detail" not in str(error.value)


def test_inventory_without_code_indexes_retains_historical_digest(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
) -> None:
    _, target, root, recovery = indexed
    journal = make_journal(target, root, recovery)
    assert journal.receipt.inventory_digest
    legacy = inv.Inventory(
        created_at=inv.now(),
        repository=str(root),
        baseline=inv.baseline(root),
        uid=root.stat().st_uid,
        gobby_home=str(recovery),
        files_home=str(recovery),
        checkouts=[str(root)],
        roots=[],
        stores=[],
        backend_identities={},
        writers=[],
    )
    historical = legacy.model_dump(mode="json")
    assert "code_indexes" not in historical
    assert inv.Inventory.model_validate(historical).digest == inv.sha(inv.canonical(historical))


@pytest.mark.parametrize("field,value", [("removed_revision", "HEAD"), ("archived_blob", "abcd")])
def test_inventory_rejects_unpinned_archived_git_identity(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    field: str,
    value: str,
) -> None:
    _, target, root, recovery = indexed
    vault = inv.Root(
        path=str(root / "wiki"),
        kind="checkout_vault",
        owner_path=str(root / "wiki/_gwiki/scope.json"),
        owner_digest="fixture",
        entries=inv.scan(root / "wiki"),
    )
    inventory = inv.Inventory(
        created_at=inv.now(),
        repository=str(root),
        baseline=inv.baseline(root),
        uid=root.stat().st_uid,
        gobby_home=str(recovery),
        files_home=str(recovery),
        checkouts=[str(root)],
        roots=[vault],
        stores=[],
        backend_identities={},
        writers=[],
        code_indexes=[target],
    )
    inv.validate_code_indexes(inventory)
    changed = target.files[0].model_copy(update={field: value})
    substituted = target.model_copy(update={"files": [changed, *target.files[1:]]})
    with pytest.raises(inv.RetirementError, match="full immutable Git object IDs"):
        inv.validate_code_indexes(inventory.model_copy(update={"code_indexes": [substituted]}))


def test_rehearsal_requires_retired_schema_before_code_cleanup_and_restores_exactly(
    indexed: tuple[CodeIndexStorage, inv.CodeIndexTarget, Path, Path],
    postgres_db: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, target, root, recovery = indexed
    data = manager.backup(target)
    value = inv.Inventory(
        created_at=inv.now(),
        repository=str(root),
        baseline=inv.baseline(root),
        uid=root.stat().st_uid,
        gobby_home=str(recovery),
        files_home=str(recovery),
        checkouts=[str(root)],
        roots=[],
        stores=[],
        backend_identities={},
        writers=[],
        code_indexes=[target],
    )
    backup = Backup(
        inventory_digest=value.digest,
        baseline=value.baseline,
        created_at=inv.now(),
        artifacts={target.key: put_artifact(recovery, target.key, data)},
        complete=True,
    )
    events: list[str] = []

    class Admission:
        ready = False

        def __init__(self, identity: str) -> None:
            self.identity = identity

        def identities(self) -> dict[str, str]:
            return {"postgres": self.identity}

        def require_schema_retired(self) -> None:
            if not self.ready:
                raise inv.RetirementError("Canonical schema426 is not verified")
            events.append("schema_verified")

    isolated = Admission("isolated")
    source = Admission("source")
    # The inventory has no ordinary StoreTargets; these facades supply only the
    # admission boundary, while code SQL and projections use the real fixtures.
    source_api, isolated_api = cast(Storage, source), cast(Storage, isolated)
    monkeypatch.setattr(cli, "code_index_manager", lambda _: manager)
    with pytest.raises(inv.RetirementError, match="schema426"):
        cli.rehearse(value, backup, recovery, source_api, isolated_api)
    receipt = json.loads((recovery / "rehearsal.json").read_text())
    assert not receipt["complete"] and "schema426" in receipt["errors"][0]
    assert manager.digest(manager.capture(target)) == target.digest
    (root / "wiki/page.md").unlink()
    isolated.ready = True
    monkeypatch.setattr(
        "scripts.wiki_retirement_code_index.local_native_bin_path", lambda _: root / "keep.py"
    )

    def command(argv: list[str], cwd: Path, receipt: Path) -> None:
        assert events[0] == "schema_verified" and cwd == root
        assert receipt.is_relative_to(recovery)
        if "index" in argv:
            events.append("selectors")
            postgres_db.execute(
                "DELETE FROM code_indexed_file_states WHERE file_path=ANY(%s)",
                (["removed.py", "wiki/page.md"],),
            )
        elif "--apply" in argv:
            events.append("content")
            postgres_db.execute(
                "DELETE FROM code_indexed_files WHERE file_path=ANY(%s)",
                (["removed.py", "wiki/page.md"],),
            )

    monkeypatch.setattr(manager, "_command", command)
    result = cli.rehearse(value, backup, recovery, source_api, isolated_api)
    assert result.complete and result.items[f"restored:{target.key}"] == "done"
    assert events == ["schema_verified", "selectors", "content"]
    assert manager.digest(manager.capture(target)) == target.digest
    assert cli.rehearse(value, backup, recovery, source_api, isolated_api) == result
    assert postgres_db.fetchone("SELECT COUNT(*) AS count FROM code_indexed_files") == {"count": 3}
