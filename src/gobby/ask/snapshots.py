"""Managed historical source snapshots for Ask runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import UUID

from gobby.agents.code_index import ensure_isolation_code_index
from gobby.ask.artifacts import AskArtifactStore
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.native_bin import resolve_native_bin
from gobby.utils.project_context import ensure_project_json_for_isolation
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import DestructiveBoundary
from gobby.worktrees.git.manager import WorktreeGitManager

if TYPE_CHECKING:
    from gobby.storage.managed_credentials import ManagedCredentialManager


_MAX_EVIDENCE_FILE_BYTES = 10 * 1024 * 1024
_OID_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cjs": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".php": "php",
    ".dart": "dart",
    ".cs": "csharp",
    ".m": "objc",
    ".mm": "objc",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".hh": "cpp",
    ".ex": "elixir",
    ".exs": "elixir",
    ".rb": "ruby",
    ".rake": "ruby",
    ".gemspec": "ruby",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sc": "scala",
    ".lua": "lua",
    ".swift": "swift",
    ".sh": "bash",
    ".bash": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonc": "json",
}
_GENERATED_SNAPSHOT_PATHS = {".gobby/isolation.json", ".gobby/bin/gcode"}


class SnapshotDriftError(RuntimeError):
    """The retained checkout no longer matches its immutable manifest."""


@dataclass(frozen=True, slots=True)
class SnapshotIndexRuntime:
    executable: Path
    env: Mapping[str, str]
    managed_execution_id: str
    credential_generation: int


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    commit_oid: str
    binding: dict[str, Any]
    inventory: dict[str, Any]
    repository_root: Path
    source_root: Path
    worktree_id: str
    runtime: SnapshotIndexRuntime
    manifest_pointer: dict[str, Any]


IndexPreparer = Callable[[Path, datetime], Awaitable[SnapshotIndexRuntime]]
IndexReleaser = Callable[[SnapshotIndexRuntime], None]


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _git(
    repository_root: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        input=input_bytes,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return completed.stdout


def _git_text(repository_root: Path, *arguments: str) -> str:
    return _git(repository_root, *arguments).decode().strip()


def _safe_repo_path(raw_path: bytes) -> tuple[str, str | None]:
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        return "".join(f"\\x{byte:02x}" for byte in raw_path), "unsafe_path"
    parsed = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or parsed.is_absolute()
        or any(part in {".", "..", ""} for part in parsed.parts)
    ):
        return path, "unsafe_path"
    return path, None


def _language(path: str, content: bytes | None, all_paths: set[str]) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix != ".h":
        return _LANGUAGES.get(suffix)
    stem = str(PurePosixPath(path).with_suffix(""))
    if any(f"{stem}{candidate}" in all_paths for candidate in (".m", ".mm")):
        return "objc"
    if any(f"{stem}{candidate}" in all_paths for candidate in (".cpp", ".cc", ".cxx")):
        return "cpp"
    source = (content or b"").decode("utf-8", errors="ignore")
    if re.search(r"@(interface|protocol|class|property|end)\b", source):
        return "objc"
    if re.search(r"\b(namespace|template|class|constexpr|decltype)\b|::", source):
        return "cpp"
    return "c"


def _load_snapshot(
    repository_root: Path,
    project_id: str,
    expected_commit_oid: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _OID_PATTERN.fullmatch(expected_commit_oid):
        raise ValueError("Ask snapshot requires a full hexadecimal commit OID")
    commit_oid = _git_text(
        repository_root,
        "rev-parse",
        "--verify",
        f"{expected_commit_oid}^{{commit}}",
    )
    if commit_oid != expected_commit_oid:
        raise SnapshotDriftError("snapshot recovery would change the pinned commit")
    tree_oid = _git_text(repository_root, "rev-parse", "--verify", f"{commit_oid}^{{tree}}")
    raw_records = [
        record
        for record in _git(repository_root, "ls-tree", "-rlz", "--full-tree", tree_oid).split(b"\0")
        if record
    ]
    all_paths = {_safe_repo_path(record.split(b"\t", 1)[1])[0] for record in raw_records}
    entries: list[dict[str, Any]] = []
    for record in raw_records:
        header, raw_path = record.split(b"\t", 1)
        mode, object_type, object_oid, raw_size = header.decode().split()
        path, exclusion = _safe_repo_path(raw_path)
        size_bytes = int(raw_size) if raw_size.isdigit() else None
        if mode == "100644" and object_type == "blob":
            kind, blob_oid = "file", object_oid
        elif mode == "100755" and object_type == "blob":
            kind, blob_oid = "executable", object_oid
        elif mode == "120000" and object_type == "blob":
            kind, blob_oid, exclusion = "symlink", object_oid, exclusion or "symlink"
        elif mode == "160000" and object_type == "commit":
            kind, blob_oid, exclusion = "gitlink", None, exclusion or "gitlink"
        else:
            kind, blob_oid, exclusion = "unsupported", None, exclusion or "unsupported_object"
        if size_bytes is not None and size_bytes > _MAX_EVIDENCE_FILE_BYTES:
            exclusion = "oversized"
        content: bytes | None = None
        content_hash: str | None = None
        if exclusion is None and blob_oid is not None:
            content = _git(repository_root, "cat-file", "blob", blob_oid)
            if b"\0" in content:
                exclusion = "binary"
            else:
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    exclusion = "unsupported_encoding"
                else:
                    content_hash = hashlib.sha256(content).hexdigest()
        entries.append(
            {
                "path": path,
                "mode": mode,
                "kind": kind,
                "object_oid": object_oid,
                "blob_oid": blob_oid,
                "size_bytes": size_bytes,
                "content_hash": content_hash,
                "language": _language(path, content, all_paths),
                "exclusion": exclusion,
            }
        )
    entries.sort(key=lambda entry: entry["path"])
    inventory_digest = _canonical_hash(entries)
    inventory = {
        "schema_version": 1,
        "complete": True,
        "digest": inventory_digest,
        "entries": entries,
    }
    parent_oids = _git_text(repository_root, "show", "-s", "--format=%P", commit_oid).split()
    if parent_oids:
        comparison_parent_oid, comparison_kind = parent_oids[0], "first_parent"
    else:
        comparison_parent_oid = (
            _git(
                repository_root,
                "hash-object",
                "-t",
                "tree",
                "--stdin",
                input_bytes=b"",
            )
            .decode()
            .strip()
        )
        comparison_kind = "empty_tree"
    changed_paths = _load_changed_paths(repository_root, comparison_parent_oid, commit_oid)
    binding = {
        "project_id": project_id,
        "commit_oid": commit_oid,
        "tree_oid": tree_oid,
        "inventory_digest": inventory_digest,
        "commit": {
            "parent_oids": parent_oids,
            "comparison_parent_oid": comparison_parent_oid,
            "comparison_kind": comparison_kind,
            "changed_paths_digest": _canonical_hash(changed_paths),
            "changed_paths": changed_paths,
        },
    }
    return binding, inventory


def _load_changed_paths(
    repository_root: Path, base_oid: str, commit_oid: str
) -> list[dict[str, Any]]:
    fields = _git(
        repository_root,
        "diff-tree",
        "--raw",
        "-r",
        "-z",
        "--no-abbrev",
        "--no-commit-id",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        base_oid,
        commit_oid,
    ).split(b"\0")
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(fields) and fields[index]:
        header = fields[index].decode()
        index += 1
        old_mode, new_mode, old_oid, new_oid, status_token = header.removeprefix(":").split()
        status = {
            "A": "added",
            "C": "copied",
            "D": "deleted",
            "M": "modified",
            "R": "renamed",
            "T": "type_changed",
        }.get(status_token[0])
        if status is None or index >= len(fields):
            raise RuntimeError(f"unsupported git change record: {header}")
        first_path, first_exclusion = _safe_repo_path(fields[index])
        index += 1
        if status == "added":
            old_path, new_path = None, first_path
            old_exclusion, new_exclusion = None, first_exclusion
        elif status == "deleted":
            old_path, new_path = first_path, None
            old_exclusion, new_exclusion = first_exclusion, None
        elif status in {"renamed", "copied"}:
            if index >= len(fields):
                raise RuntimeError(f"rename/copy record lacks destination: {header}")
            second_path, second_exclusion = _safe_repo_path(fields[index])
            index += 1
            old_path, new_path = first_path, second_path
            old_exclusion, new_exclusion = first_exclusion, second_exclusion
        else:
            old_path = new_path = first_path
            old_exclusion = new_exclusion = first_exclusion
        result.append(
            {
                "status": status,
                "similarity": int(status_token[1:]) if status_token[1:] else None,
                "old_path": old_path,
                "new_path": new_path,
                "old_exclusion": old_exclusion,
                "new_exclusion": new_exclusion,
                "old_mode": old_mode if old_mode != "000000" else None,
                "new_mode": new_mode if new_mode != "000000" else None,
                "old_blob_oid": (
                    old_oid if old_mode.startswith("100") or old_mode == "120000" else None
                ),
                "new_blob_oid": (
                    new_oid if new_mode.startswith("100") or new_mode == "120000" else None
                ),
            }
        )
    status_order = {
        name: order
        for order, name in enumerate(
            ("added", "copied", "deleted", "modified", "renamed", "type_changed")
        )
    }

    def option(value: object) -> tuple[bool, object]:
        return value is not None, value or ""

    result.sort(
        key=lambda item: (
            status_order[item["status"]],
            option(item["similarity"]),
            option(item["old_path"]),
            option(item["new_path"]),
            option(item["old_exclusion"]),
            option(item["new_exclusion"]),
            option(item["old_mode"]),
            option(item["new_mode"]),
            option(item["old_blob_oid"]),
            option(item["new_blob_oid"]),
        )
    )
    return result


class AskSnapshotManager:
    """Create, verify, recover, and release one managed detached checkout."""

    def __init__(
        self,
        *,
        worktree_storage: LocalWorktreeManager,
        index_preparer: IndexPreparer | None = None,
        index_releaser: IndexReleaser | None = None,
        credential_manager: ManagedCredentialManager | None = None,
        session_id: UUID | None = None,
    ) -> None:
        if index_preparer is None and (credential_manager is None or session_id is None):
            raise ValueError("Ask snapshots require an injected or managed index preparer")
        self.worktree_storage = worktree_storage
        self.index_preparer = index_preparer
        self.index_releaser = index_releaser
        self.credential_manager = credential_manager
        self.session_id = session_id

    def prepare(
        self,
        *,
        run_id: str,
        project_id: str,
        repository_root: Path,
        commit_oid: str,
        deadline_at: datetime,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        return self._run_sync(
            self.prepare_async(
                run_id=run_id,
                project_id=project_id,
                repository_root=repository_root,
                commit_oid=commit_oid,
                deadline_at=deadline_at,
                artifacts=artifacts,
            )
        )

    async def prepare_async(
        self,
        *,
        run_id: str,
        project_id: str,
        repository_root: Path,
        commit_oid: str,
        deadline_at: datetime,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        if deadline_at.tzinfo is None or deadline_at <= datetime.now(UTC):
            raise ValueError("Ask snapshot deadline has already expired")
        repository_root = repository_root.resolve()
        binding, inventory = await asyncio.to_thread(
            _load_snapshot,
            repository_root,
            project_id,
            commit_oid,
        )
        source_root = artifacts.run_root / "source"
        if source_root.exists():
            raise FileExistsError(f"Ask snapshot path already exists: {source_root}")
        await asyncio.to_thread(
            _git,
            repository_root,
            "worktree",
            "add",
            "--detach",
            str(source_root),
            commit_oid,
        )
        source_root.chmod(0o700)
        worktree = self.worktree_storage.create(
            project_id=project_id,
            branch_name=None,
            worktree_path=str(source_root),
            base_branch=commit_oid,
            agent_session_id=str(self.session_id) if self.session_id is not None else None,
            workspace_role="ask_snapshot",
        )
        try:
            await asyncio.to_thread(ensure_project_json_for_isolation, repository_root, source_root)
            runtime = await self._prepare_index(source_root, deadline_at)
            body = {
                "schema_version": 1,
                "run_id": run_id,
                "project_id": project_id,
                "repository_root": str(repository_root),
                "source_root": str(source_root),
                "worktree_id": worktree.id,
                "commit_oid": commit_oid,
                "binding": binding,
                "inventory": inventory,
                "index_runtime": {
                    "executable": str(runtime.executable),
                    "managed_execution_id": runtime.managed_execution_id,
                    "credential_generation": runtime.credential_generation,
                },
            }
            pointer = artifacts.write_body("snapshot", body)
        except BaseException:
            await asyncio.to_thread(self._delete_worktree, repository_root, worktree.id)
            raise
        return PreparedSnapshot(
            commit_oid=commit_oid,
            binding=binding,
            inventory=inventory,
            repository_root=repository_root,
            source_root=source_root,
            worktree_id=worktree.id,
            runtime=runtime,
            manifest_pointer=pointer,
        )

    def recover(
        self,
        manifest_pointer: dict[str, Any],
        *,
        deadline_at: datetime,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        return self._run_sync(
            self.recover_async(
                manifest_pointer,
                deadline_at=deadline_at,
                artifacts=artifacts,
            )
        )

    async def recover_async(
        self,
        manifest_pointer: dict[str, Any],
        *,
        deadline_at: datetime,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        body = await asyncio.to_thread(artifacts.read_body, manifest_pointer)
        repository_root = Path(self._required_text(body, "repository_root")).resolve()
        source_root = Path(self._required_text(body, "source_root"))
        project_id = self._required_text(body, "project_id")
        commit_oid = self._required_text(body, "commit_oid")
        if source_root != artifacts.run_root / "source":
            raise SnapshotDriftError("snapshot manifest source path escaped the Ask run")
        binding, inventory = await asyncio.to_thread(
            _load_snapshot,
            repository_root,
            project_id,
            commit_oid,
        )
        if binding != body.get("binding") or inventory != body.get("inventory"):
            raise SnapshotDriftError("snapshot manifest no longer matches the pinned commit")

        manifest_worktree_id = self._required_text(body, "worktree_id")
        current_worktree = self.worktree_storage.get_by_path(str(source_root))
        if source_root.exists():
            if current_worktree is None:
                raise SnapshotDriftError("snapshot checkout lost its managed worktree record")
            if current_worktree.project_id != project_id:
                raise SnapshotDriftError("snapshot worktree project scope changed")
            worktree_id = current_worktree.id
            await asyncio.to_thread(
                self._verify_checkout,
                source_root,
                commit_oid,
                binding["tree_oid"],
            )
        else:
            await asyncio.to_thread(_git, repository_root, "worktree", "prune")
            stale = current_worktree or self.worktree_storage.get(manifest_worktree_id)
            if stale is not None:
                self.worktree_storage.delete(stale.id)
            await asyncio.to_thread(
                _git,
                repository_root,
                "worktree",
                "add",
                "--detach",
                str(source_root),
                commit_oid,
            )
            source_root.chmod(0o700)
            replacement = self.worktree_storage.create(
                project_id=project_id,
                branch_name=None,
                worktree_path=str(source_root),
                base_branch=commit_oid,
                agent_session_id=str(self.session_id) if self.session_id is not None else None,
                workspace_role="ask_snapshot",
            )
            worktree_id = replacement.id
            await asyncio.to_thread(ensure_project_json_for_isolation, repository_root, source_root)
        self._revoke_manifest_runtime(body)
        runtime = await self._prepare_index(source_root, deadline_at)
        return PreparedSnapshot(
            commit_oid=commit_oid,
            binding=binding,
            inventory=inventory,
            repository_root=repository_root,
            source_root=source_root,
            worktree_id=worktree_id,
            runtime=runtime,
            manifest_pointer=manifest_pointer,
        )

    def release(self, snapshot: PreparedSnapshot, *, artifacts: AskArtifactStore) -> None:
        """Revoke the grant and release the managed checkout, retaining evidence bodies."""
        if artifacts.run_root / "source" != snapshot.source_root:
            raise ValueError("snapshot does not belong to this Ask artifact store")
        if self.index_releaser is not None:
            self.index_releaser(snapshot.runtime)
        elif self.credential_manager is not None:
            self.credential_manager.revoke(
                UUID(snapshot.runtime.managed_execution_id),
                generation=snapshot.runtime.credential_generation,
                reason="ask_snapshot_released",
            )
        self._delete_worktree(snapshot.repository_root, snapshot.worktree_id)

    async def _prepare_index(
        self, source_root: Path, deadline_at: datetime
    ) -> SnapshotIndexRuntime:
        if self.index_preparer is not None:
            return await self.index_preparer(source_root, deadline_at)
        if self.credential_manager is None or self.session_id is None:
            raise RuntimeError("managed Ask snapshot index services are unavailable")
        issued = await asyncio.to_thread(
            self.credential_manager.issue_tool_request,
            session_id=self.session_id,
            requested_project_path=str(source_root),
            expires_at=deadline_at,
        )
        try:
            if Path(issued.project_path).resolve() != source_root.resolve():
                raise RuntimeError("managed Ask grant resolved a different project path")
            remaining = (deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise TimeoutError("Ask snapshot deadline exceeded during preparation")
            identity_env = {
                "GOBBY_MACHINE_ID": require_machine_id(),
                "GOBBY_PROJECT_ID": str(issued.project_id),
                "GOBBY_SESSION_ID": str(self.session_id),
            }
            result = await ensure_isolation_code_index(
                str(source_root),
                timeout=remaining,
                credential=issued.credential,
                identity_env=identity_env,
            )
            executable = result.wrapper_path or resolve_native_bin("gcode")
            if executable is None:
                raise RuntimeError("gcode_not_installed")
        except BaseException:
            await asyncio.to_thread(
                self.credential_manager.revoke,
                issued.credential.managed_execution_id,
                generation=issued.credential.credential_generation,
                reason="ask_snapshot_preparation_failed",
            )
            raise
        return SnapshotIndexRuntime(
            executable=Path(executable),
            env={**identity_env, **result.env},
            managed_execution_id=str(issued.credential.managed_execution_id),
            credential_generation=issued.credential.credential_generation,
        )

    def _delete_worktree(self, repository_root: Path, worktree_id: str) -> None:
        result = delete_worktree_transaction(
            DestructiveBoundary(),
            request=WorktreeDeletionRequest(
                worktree_id=worktree_id,
                surface=DeletionSurface.MCP,
                force=True,
            ),
            worktree_storage=self.worktree_storage,
            resolve_git_manager=lambda _worktree: WorktreeGitManager(repository_root),
            task_manager=None,
        )
        if not result.success:
            raise RuntimeError(result.error or "failed to release Ask snapshot worktree")

    def _revoke_manifest_runtime(self, body: Mapping[str, Any]) -> None:
        if self.credential_manager is None:
            return
        runtime = body.get("index_runtime")
        if not isinstance(runtime, Mapping):
            return
        managed_execution_id = runtime.get("managed_execution_id")
        generation = runtime.get("credential_generation")
        if not isinstance(managed_execution_id, str) or not isinstance(generation, int):
            raise SnapshotDriftError("snapshot manifest has invalid managed grant identity")
        self.credential_manager.revoke(
            UUID(managed_execution_id),
            generation=generation,
            reason="ask_snapshot_recovered",
        )

    @staticmethod
    def _verify_checkout(source_root: Path, commit_oid: str, tree_oid: str) -> None:
        if _git_text(source_root, "rev-parse", "HEAD") != commit_oid:
            raise SnapshotDriftError("working tree drift: snapshot HEAD changed")
        if _git_text(source_root, "rev-parse", "HEAD^{tree}") != tree_oid:
            raise SnapshotDriftError("working tree drift: snapshot tree changed")
        status = _git_text(source_root, "status", "--porcelain=v1", "--untracked-files=all")
        dirty: list[str] = []
        for line in status.splitlines():
            path = line[3:].split(" -> ")[-1]
            if line.startswith("?? ") and (
                path in _GENERATED_SNAPSHOT_PATHS or path.startswith(".gobby/bin/")
            ):
                continue
            dirty.append(line)
        if dirty:
            raise SnapshotDriftError("working tree drift: " + ", ".join(dirty))

    @staticmethod
    def _required_text(body: Mapping[str, Any], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise SnapshotDriftError(f"snapshot manifest is missing {key}")
        return value

    @staticmethod
    def _run_sync(coroutine: Coroutine[Any, Any, PreparedSnapshot]) -> PreparedSnapshot:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("use the async Ask snapshot API from an active event loop")
