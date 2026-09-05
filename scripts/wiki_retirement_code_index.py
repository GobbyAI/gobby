"""Exact retained code-index inventory and scoped PostgreSQL recovery."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import sql

from gobby.cli.hub_backup._integrity import refuse_symlink_traversal
from gobby.identity import hash_password
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.users import LocalUserManager
from gobby.utils.native_bin import local_native_bin_path
from scripts.wiki_retirement_inventory import (
    CODE_TOMBSTONE_HASH,
    CODE_TOMBSTONE_LANGUAGE,
    CodeIndexFile,
    CodeIndexTarget,
    CodeIndexVersion,
    RetirementError,
    Root,
    canonical,
    git,
    read_file,
    sha,
    validate_code_version,
)
from scripts.wiki_retirement_receipts import Journal, private_directory, read_private, write_private

TABLE_PATHS = {
    "code_indexed_files": "file_path",
    "code_symbols": "file_path",
    "code_content_chunks": "file_path",
    "code_imports": "source_file",
    "code_calls": "file_path",
    "code_inheritance": "file_path",
    "code_indexed_file_states": "file_path",
}
PARENT_TABLES = ("code_indexed_projects", "code_indexed_project_states")
SEQUENCES = ("code_imports", "code_calls", "code_inheritance")


class ProjectionStorage(Protocol):
    def discover_paths(self, project_id: str) -> set[str]: ...
    def capture(self, project_id: str, files: list[dict[str, Any]]) -> dict[str, Any]: ...
    def restore(
        self, project_id: str, files: list[dict[str, Any]], snapshot: dict[str, Any]
    ) -> None: ...


def normalized_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise RetirementError(f"Noncanonical indexed path: {value}")
    return path


def source_deletions(source: Path, baseline: str) -> tuple[str, dict[str, str]]:
    revision = git(source, "rev-parse", "HEAD")
    git(source, "merge-base", "--is-ancestor", baseline, revision)
    paths = git(source, "diff", "--name-only", "--diff-filter=D", baseline, revision).splitlines()
    archived = {}
    for line in git(source, "ls-tree", "-r", baseline).splitlines():
        metadata, path = line.split("\t", 1)
        archived[path] = metadata.split()[2]
    return revision, {path: archived[path] for path in paths}


def project_lock_key(project_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(b"gcode:index:" + project_id.encode()).digest()[:8], "big", signed=True
    )


def ordered_code_targets(targets: list[CodeIndexTarget]) -> list[CodeIndexTarget]:
    """Remove parent selectors before overlay indexing can remove its tombstones."""

    def key(target: CodeIndexTarget) -> tuple[bool, str]:
        root = Path(target.root_path)
        common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        directory = git(root, "rev-parse", "--path-format=absolute", "--git-dir")
        return directory != common, target.project_id

    return sorted(targets, key=key)


class CodeIndexStorage:
    def __init__(
        self,
        database_url: str,
        schema: str,
        pg: psycopg.Connection[Any],
        projections: ProjectionStorage,
        *,
        isolated: bool,
        qdrant_url: str,
        falkor_url: str,
    ) -> None:
        self.database_url = database_url
        self.schema = schema
        self.pg = pg
        self.projections = projections
        self.isolated = isolated
        self.qdrant_url = qdrant_url
        self.falkor_url = falkor_url

    def _table(self, name: str) -> sql.Composed:
        if name not in {*TABLE_PATHS, *PARENT_TABLES}:
            raise RetirementError("Unowned code-index table")
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def _rows(self, target: CodeIndexTarget) -> dict[str, list[dict[str, Any]]]:
        paths = [file.file_path for file in target.files]
        result = {}
        for table, column in TABLE_PATHS.items():
            rows = self.pg.execute(
                sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE project_id=%s AND {}=ANY(%s)").format(
                    self._table(table), sql.Identifier(column)
                ),
                (target.project_id, paths),
            )
            result[table] = sorted([row[0] for row in rows], key=canonical)
        self.pg.rollback()
        foreign = [
            row
            for row in result["code_indexed_file_states"]
            if row["machine_id"] != target.machine_id
        ]
        if foreign:
            raise RetirementError(
                f"Other-machine selectors retain wiki content: {target.project_id}"
            )
        return result

    def _parents(self, target: CodeIndexTarget) -> dict[str, list[dict[str, Any]]]:
        result = {}
        for table in PARENT_TABLES:
            column = "id" if table == "code_indexed_projects" else "project_id"
            query = sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE {}=%s").format(
                self._table(table), sql.Identifier(column)
            )
            args = [target.project_id]
            if table == "code_indexed_project_states":
                query += sql.SQL(" AND machine_id=%s")
                args.append(target.machine_id)
            result[table] = [row[0] for row in self.pg.execute(query, args)]
        self.pg.rollback()
        return result

    def _identity_rows(self, target: CodeIndexTarget) -> dict[str, list[dict[str, Any]]]:
        """Read only native deletion identities, never source bodies or projection payloads."""
        paths = [file.file_path for file in target.files]
        result = {}
        for table, columns in (
            ("code_indexed_files", ("id", "file_path", "content_hash")),
            ("code_symbols", ("id", "file_path", "file_content_hash")),
        ):
            fields = sql.SQL(",").join(
                sql.SQL("{},{}").format(sql.Literal(name), sql.Identifier(name)) for name in columns
            )
            rows = self.pg.execute(
                sql.SQL(
                    "SELECT jsonb_build_object({}) FROM {} WHERE project_id=%s AND file_path=ANY(%s)"
                ).format(fields, self._table(table)),
                (target.project_id, paths),
            )
            result[table] = sorted([row[0] for row in rows], key=canonical)
        foreign = self.pg.execute(
            sql.SQL(
                "SELECT 1 FROM {} WHERE project_id=%s AND file_path=ANY(%s) AND machine_id<>%s LIMIT 1"
            ).format(self._table("code_indexed_file_states")),
            (target.project_id, paths, target.machine_id),
        ).fetchone()
        self.pg.rollback()
        if foreign:
            raise RetirementError("Other-machine selectors retain wiki content")
        return result

    @staticmethod
    def _bind_versions(
        target: CodeIndexTarget, rows: dict[str, list[dict[str, Any]]]
    ) -> CodeIndexTarget:
        file_rows: dict[str, list[dict[str, Any]]] = {}
        symbol_rows: dict[tuple[str, str], list[str]] = {}
        for row in rows["code_indexed_files"]:
            file_rows.setdefault(row["file_path"], []).append(row)
        for row in rows["code_symbols"]:
            symbol_rows.setdefault((row["file_path"], row["file_content_hash"]), []).append(
                row["id"]
            )
        files = []
        for file in target.files:
            versions = []
            for row in file_rows.get(file.file_path, []):
                symbols = sorted(symbol_rows.get((file.file_path, row["content_hash"]), []))
                validate_code_version(
                    target.project_id, file.file_path, row["id"], row["content_hash"], symbols
                )
                versions.append(
                    CodeIndexVersion(
                        id=row["id"], content_hash=row["content_hash"], symbol_ids=symbols
                    )
                )
            files.append(
                file.model_copy(update={"versions": sorted(versions, key=lambda item: item.id)})
            )
        return target.model_copy(update={"files": files})

    @staticmethod
    def identity_digest(target: CodeIndexTarget) -> str:
        return sha(
            canonical(
                {
                    "project_id": target.project_id,
                    "machine_id": target.machine_id,
                    "root_path": target.root_path,
                    "files": target.native_files(),
                }
            )
        )

    def capture(self, target: CodeIndexTarget) -> dict[str, Any]:
        return {
            "sql": self._rows(target),
            "parents": self._parents(target),
            "projections": self.projections.capture(target.project_id, target.native_files()),
        }

    @staticmethod
    def digest(snapshot: dict[str, Any]) -> str:
        from scripts.wiki_retirement_code_projections import fingerprint

        return sha(
            canonical({"sql": snapshot["sql"], "projections": fingerprint(snapshot["projections"])})
        )

    @staticmethod
    def assert_remaining(current: dict[str, Any], expected: dict[str, Any]) -> None:
        from scripts.wiki_retirement_code_projections import assert_remaining

        for table in TABLE_PATHS:
            approved = {canonical(row) for row in expected["sql"][table]}
            if table == "code_indexed_files":
                approved.update(
                    canonical({**row, "graph_synced": False, "vectors_synced": False})
                    for row in expected["sql"][table]
                )
            if any(canonical(row) not in approved for row in current["sql"][table]):
                raise RetirementError(f"Code-index rows changed outside bounded cleanup: {table}")
        assert_remaining(current["projections"], expected["projections"])

    @staticmethod
    def is_absent(snapshot: dict[str, Any]) -> bool:
        from scripts.wiki_retirement_code_projections import is_absent

        return not any(snapshot["sql"].values()) and is_absent(snapshot["projections"])

    def backup(self, target: CodeIndexTarget) -> bytes:
        snapshot = self.capture(target)
        self.validate_snapshot(target, snapshot)
        if self.digest(snapshot) != target.digest:
            raise RetirementError(f"Code index changed since inventory: {target.project_id}")
        return canonical(snapshot)

    def native_backends(self) -> dict[str, Any]:
        connection = psycopg.conninfo.conninfo_to_dict(self.database_url)
        host, database = connection.get("host"), connection.get("dbname")
        if (
            not isinstance(host, str)
            or not isinstance(database, str)
            or not host
            or not database
            or "," in host
            or host.startswith("/")
        ):
            raise RetirementError("Native retirement requires one explicit PostgreSQL TCP target")
        qdrant = urlsplit(self.qdrant_url)
        falkor = urlsplit(self.falkor_url)
        if (
            qdrant.username
            or qdrant.password
            or qdrant.query
            or qdrant.fragment
            or not qdrant.hostname
        ):
            raise RetirementError(
                "Qdrant retirement endpoint must exclude credentials and query data"
            )
        if not falkor.hostname:
            raise RetirementError("FalkorDB retirement endpoint lacks a host")
        return {
            "postgres_host": host,
            "postgres_port": int(connection.get("port") or "5432"),
            "postgres_database": database,
            "qdrant_url": self.qdrant_url,
            "falkor_host": falkor.hostname,
            "falkor_port": falkor.port or 6379,
            "falkor_graph": "gobby_code",
        }

    def staged_root(self, target: CodeIndexTarget, recovery: Path) -> Path:
        if not self.isolated:
            raise RetirementError("Staged code-index checkouts require isolated state")
        parent = recovery / "code-index-rehearsal"
        private_directory(parent)
        root = parent / target.project_id
        private_directory(root)
        marker = root / ".gobby" / "project.json"
        expected = canonical({"id": target.project_id, "name": f"retirement-{target.project_id}"})
        if marker.exists():
            if read_private(marker) != expected:
                raise RetirementError("Staged code-index checkout identity changed")
        else:
            if any(root.iterdir()):
                raise RetirementError("Staged checkout destination is not empty")
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, timeout=30)
            private_directory(root / ".gobby")
            write_private(marker, expected)
        if (root / ".gobby" / "isolation.json").exists():
            raise RetirementError(
                "Staged code-index checkout must retain its explicit project UUID"
            )
        return root

    @staticmethod
    def _command(command: list[str], cwd: Path, receipt: Path) -> None:
        try:
            result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=900, check=False)
            exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code, stdout, stderr = None, exc.stdout or b"", exc.stderr or b""
        stdout_path = receipt.with_suffix(".stdout.bin")
        stderr_path = receipt.with_suffix(".stderr.bin")
        write_private(stdout_path, stdout)
        write_private(stderr_path, stderr)
        write_private(
            receipt,
            canonical(
                {
                    "command": command,
                    "cwd": str(cwd),
                    "exit_code": exit_code,
                    "stdout_digest": sha(stdout),
                    "stderr_digest": sha(stderr),
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                }
            ),
        )
        if exit_code is None:
            raise RetirementError(f"Exact code-index command timed out; receipt {receipt}")
        if exit_code:
            raise RetirementError(
                f"Exact code-index command failed with exit {exit_code}; receipt {receipt}"
            )

    def delete(
        self,
        target: CodeIndexTarget,
        data: bytes,
        inventory_digest: str,
        recovery: Path,
        journal: Journal,
    ) -> None:
        expected = json.loads(data)
        self.validate_snapshot(target, expected)
        if self.digest(expected) != target.digest:
            raise RetirementError("Code-index deletion backup differs from inventory")
        current = self.capture(target)
        state = journal.state(target.key)
        if state == "done":
            if not self.is_absent(current):
                raise RetirementError("Retired code-index content was recreated")
            return
        if state is None and self.digest(current) != target.digest:
            raise RetirementError("Code index changed before deletion")
        self.assert_remaining(current, expected)
        self._delete_native(target, inventory_digest, recovery, journal, expected)

    def delete_direct(
        self,
        target: CodeIndexTarget,
        inventory_digest: str,
        receipt_root: Path,
        journal: Journal,
    ) -> None:
        if self.identity_digest(target) != target.digest:
            raise RetirementError("Direct code-index identity manifest differs from inventory")
        current = self._bind_versions(target, self._identity_rows(target))
        if journal.state(target.key) is None and self.identity_digest(current) != target.digest:
            raise RetirementError("Code-index identities changed before direct deletion")
        for actual, expected in zip(current.files, target.files, strict=True):
            if any(version not in expected.versions for version in actual.versions):
                raise RetirementError("Unlisted code-index identities appeared during direct retry")
        self._delete_native(target, inventory_digest, receipt_root, journal, None)

    def _delete_native(
        self,
        target: CodeIndexTarget,
        inventory_digest: str,
        recovery: Path,
        journal: Journal,
        expected: dict[str, Any] | None,
    ) -> None:
        root = self.staged_root(target, recovery) if self.isolated else Path(target.root_path)
        refuse_symlink_traversal(root, label="Code-index checkout")
        native = local_native_bin_path("gcode")
        if not native.is_file():
            raise RetirementError("The exact managed gcode binary is unavailable")
        directory = recovery / (
            "code-index-rehearsal-receipts" if self.isolated else "code-index-apply-receipts"
        )
        private_directory(directory)
        manifest_path = directory / f"{target.project_id}.manifest.json"
        manifest = canonical(
            {
                "version": 1,
                "source_inventory_digest": inventory_digest,
                "project_id": target.project_id,
                "machine_id": target.machine_id,
                "root_path": str(root),
                "files": target.native_files(),
                "backends": self.native_backends(),
            }
        )
        if manifest_path.exists() and read_private(manifest_path) != manifest:
            raise RetirementError("Native code-index manifest changed during retry")
        write_private(manifest_path, manifest)
        paths = [file.file_path for file in target.files]
        for path in paths:
            refuse_symlink_traversal(root / path, label="Indexed legacy path")
            if os.path.lexists(root / path):
                raise RetirementError(
                    "Indexed legacy file remains present before selector retirement"
                )
        journal.mark(target.key, "intent")
        for index in range(0, len(paths), 256):
            batch = paths[index : index + 256]
            self._command(
                [str(native), "--project", str(root), "index", "--files", *batch],
                root,
                directory / f"{target.project_id}.selectors-{index}.json",
            )
        if expected is not None:
            self.assert_remaining(self.capture(target), expected)
        command = [
            str(native),
            "--project",
            str(root),
            "retire-files",
            "--manifest",
            str(manifest_path),
        ]
        self._command(command, root, directory / f"{target.project_id}.preflight.json")
        native_receipt = directory / f"{target.project_id}.native.json"
        self._command(
            [*command, "--apply", "--receipt", str(native_receipt)],
            root,
            directory / f"{target.project_id}.apply-command.json",
        )
        if expected is not None:
            if not self.is_absent(self.capture(target)):
                raise RetirementError("Exact code-index retirement left selected artifacts")
        else:
            receipt = json.loads(read_private(native_receipt))
            if (
                receipt.get("complete") is not True
                or receipt.get("manifest_digest") != sha(manifest)
                or receipt.get("source_inventory_digest") != inventory_digest
            ):
                raise RetirementError("Native code-index receipt does not prove exact completion")
        journal.mark(target.key, "done")

    def restore(self, target: CodeIndexTarget, data: bytes, root: Path) -> None:
        snapshot = json.loads(data)
        self.validate_snapshot(target, snapshot)
        if self.digest(snapshot) != target.digest:
            raise RetirementError("Code-index recovery differs from inventory")
        self.restore_sql(target, snapshot, root)
        self.projections.restore(target.project_id, target.native_files(), snapshot["projections"])
        if self.digest(self.capture(target)) != target.digest:
            raise RetirementError("Restored code index differs from inventoried content")

    @staticmethod
    def validate_snapshot(target: CodeIndexTarget, snapshot: dict[str, Any]) -> None:
        if set(snapshot["sql"]) != set(TABLE_PATHS) or set(snapshot["parents"]) != set(
            PARENT_TABLES
        ):
            raise RetirementError("Code-index recovery table coverage differs")
        versions = {
            (file.file_path, version.content_hash): version
            for file in target.files
            for version in file.versions
        }
        for (path, content_hash), selected_version in versions.items():
            validate_code_version(
                target.project_id,
                path,
                selected_version.id,
                content_hash,
                selected_version.symbol_ids,
            )
        for table, rows in snapshot["sql"].items():
            for row in rows:
                path = row[TABLE_PATHS[table]]
                content_hash = row[
                    "file_content_hash" if table == "code_symbols" else "content_hash"
                ]
                version = versions.get((path, content_hash))
                if row["project_id"] != target.project_id or version is None:
                    raise RetirementError("Code-index recovery contains an unowned content version")
                if content_hash == CODE_TOMBSTONE_HASH:
                    if table == "code_indexed_files":
                        if (row["language"], row["symbol_count"], row["byte_size"]) != (
                            CODE_TOMBSTONE_LANGUAGE,
                            0,
                            0,
                        ):
                            raise RetirementError(
                                "Code-index tombstone row is not canonical and empty"
                            )
                    elif table != "code_indexed_file_states":
                        raise RetirementError(
                            "Code-index tombstone contains forbidden content facts"
                        )
                if table == "code_indexed_files" and row["id"] != version.id:
                    raise RetirementError("Code-index recovery version ID differs")
                if table == "code_symbols" and row["id"] not in version.symbol_ids:
                    raise RetirementError("Code-index recovery symbol ID differs")
                if table == "code_indexed_file_states" and row["machine_id"] != target.machine_id:
                    raise RetirementError("Code-index recovery contains another machine's selector")
        for row in snapshot["parents"]["code_indexed_projects"]:
            if row["id"] != target.project_id:
                raise RetirementError("Code-index recovery project differs")
        for row in snapshot["parents"]["code_indexed_project_states"]:
            if (row["project_id"], row["machine_id"], row["root_path"]) != (
                target.project_id,
                target.machine_id,
                target.root_path,
            ):
                raise RetirementError("Code-index recovery checkout differs")

    def inventory(
        self,
        roots: list[Root],
        checkouts: list[Path],
        gobby_home: Path,
        source: Path,
        baseline: str,
        repository: Path,
        *,
        direct: bool = False,
    ) -> list[CodeIndexTarget]:
        machine_id = str(UUID(read_file(gobby_home / "machine_id").decode().strip()))
        revision, removed = source_deletions(source, baseline)
        common = git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common != git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise RetirementError(
                "Removal source does not share the archived repository's Git identity"
            )
        states = self.pg.execute(
            sql.SQL(
                "SELECT project_id::text, machine_id::text, root_path FROM {} "
                "WHERE machine_id=%s ORDER BY project_id"
            ).format(self._table("code_indexed_project_states")),
            (machine_id,),
        ).fetchall()
        self.pg.rollback()
        targets = []
        for project_id, selected_machine, root_text in states:
            root = Path(root_text)
            if root not in checkouts:
                continue
            if not root.is_dir():
                raise RetirementError(f"Recorded code-index checkout is unavailable: {root}")
            refuse_symlink_traversal(root, label="Code-index inventory checkout")
            same_git = (
                git(root, "rev-parse", "--path-format=absolute", "--git-common-dir") == common
            )
            paths: set[str] = set()
            for table, column in TABLE_PATHS.items():
                paths.update(
                    row[0]
                    for row in self.pg.execute(
                        sql.SQL("SELECT DISTINCT {} FROM {} WHERE project_id=%s").format(
                            sql.Identifier(column), self._table(table)
                        ),
                        (project_id,),
                    )
                )
            self.pg.rollback()
            paths.update(self.projections.discover_paths(project_id))
            files = []
            for file_path in sorted(paths):
                relative = normalized_path(file_path)
                owners = [tree for tree in roots if (root / relative).is_relative_to(tree.path)]
                if owners:
                    files.append(
                        CodeIndexFile(file_path=file_path, versions=[], owner_path=owners[0].path)
                    )
                elif same_git and file_path in removed:
                    files.append(
                        CodeIndexFile(
                            file_path=file_path,
                            versions=[],
                            owner_path=str(source),
                            removed_revision=revision,
                            archived_blob=removed[file_path],
                        )
                    )
            if not files:
                continue
            target = CodeIndexTarget(
                project_id=project_id,
                machine_id=selected_machine,
                root_path=root_text,
                files=files,
                digest="",
            )
            rows = self._identity_rows(target) if direct else self._rows(target)
            target = self._bind_versions(target, rows)
            if direct:
                if self._identity_rows(target) != rows:
                    raise RetirementError("Code-index identities changed during inventory")
                targets.append(target.model_copy(update={"digest": self.identity_digest(target)}))
                continue
            snapshot = self.capture(target)
            self.validate_snapshot(target, snapshot)
            if snapshot["sql"] != rows:
                raise RetirementError("Code-index content changed during inventory")
            targets.append(target.model_copy(update={"digest": self.digest(snapshot)}))
        return ordered_code_targets(targets)

    def restore_sql(self, target: CodeIndexTarget, snapshot: dict[str, Any], root: Path) -> None:
        if not self.isolated:
            raise RetirementError("Code-index restoration requires isolated state")
        db = PostgresHubDatabase(self.database_url)
        try:
            with db.transaction() as tx:
                tx.execute(
                    sql.SQL("SET LOCAL search_path TO {}, pg_catalog")
                    .format(sql.Identifier(self.schema))
                    .as_string(self.pg)
                )
                # Match the native per-project transaction lock during recovery.
                tx.execute("SET LOCAL lock_timeout='5s'")
                tx.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (project_lock_key(target.project_id),)
                )
                self._machine(db, target.machine_id)
                for table in PARENT_TABLES:
                    for original in snapshot["parents"][table]:
                        row = dict(original)
                        if table == "code_indexed_project_states":
                            row["root_path"] = str(root)
                        self._insert_if_absent(db, table, row, parent=True)
                for table in TABLE_PATHS:
                    for row in snapshot["sql"][table]:
                        self._insert_if_absent(db, table, row)
                for table in SEQUENCES:
                    tx.execute(
                        sql.SQL(
                            "SELECT setval(pg_get_serial_sequence(%s,'id'), "
                            "GREATEST(COALESCE((SELECT MAX(id) FROM {}),1), "
                            "nextval(pg_get_serial_sequence(%s,'id'))))"
                        )
                        .format(self._table(table))
                        .as_string(self.pg),
                        (table, table),
                    )
        finally:
            db.close()

    def _insert_if_absent(
        self,
        db: PostgresHubDatabase,
        table: str,
        row: dict[str, Any],
        *,
        parent: bool = False,
    ) -> None:
        if table in {"code_indexed_file_states", "code_indexed_project_states"}:
            columns = ["machine_id", "project_id"]
            if table == "code_indexed_file_states":
                columns.append("file_path")
        else:
            columns = ["id"]
        predicate = sql.SQL(" AND ").join(
            sql.SQL("{}=%s").format(sql.Identifier(key)) for key in columns
        )
        existing = db.fetchone(
            sql.SQL("SELECT to_jsonb(t) AS snapshot FROM {} t WHERE {}")
            .format(self._table(table), predicate)
            .as_string(self.pg),
            tuple(row[key] for key in columns),
        )
        if existing is not None:
            current = json.loads(existing["snapshot"])
            if parent:
                if (
                    table == "code_indexed_project_states"
                    and current["root_path"] != row["root_path"]
                ):
                    raise RetirementError("Recovery project already belongs to another checkout")
            elif current != row:
                raise RetirementError(f"Code-index restore would overwrite changed row: {table}")
            return
        db.execute(
            sql.SQL(
                "INSERT INTO {} OVERRIDING SYSTEM VALUE SELECT * "
                "FROM jsonb_populate_record(NULL::{},%s::jsonb)"
            )
            .format(self._table(table), self._table(table))
            .as_string(self.pg),
            (canonical(row).decode(),),
        )

    @staticmethod
    def _machine(db: PostgresHubDatabase, machine_id: str) -> None:
        machines = LocalMachineManager(db)
        if machines.get(machine_id) is not None:
            return
        users = LocalUserManager(db)
        user_id = str(uuid5(NAMESPACE_URL, f"gobby:wiki-retirement:machine-owner:{machine_id}"))
        if users.get(user_id) is None:
            users.create(
                user_id=user_id,
                name="Wiki retirement rehearsal",
                email=f"wiki-retirement-{machine_id}@example.invalid",
                password_hash=hash_password(secrets.token_urlsafe(32)),
            )
        machines.upsert_seen(machine_id, user_id, label="Wiki retirement rehearsal")
