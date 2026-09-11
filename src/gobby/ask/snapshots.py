"""Managed historical source snapshots for Ask runs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from gobby.agents.code_index import ensure_isolation_code_index
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.storage import AskRunStorage
from gobby.code_index.eligibility import code_index_id_for_root
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.native_bin import resolve_native_bin
from gobby.utils.project_context import ensure_project_json_for_isolation

if TYPE_CHECKING:
    from gobby.ask.contracts import AskRunRecord, SnapshotGeneration
    from gobby.storage.managed_credentials import ManagedCredentialManager


_CLEANUP_TIMEOUT_SECONDS = 10.0


class SnapshotDriftError(RuntimeError):
    """The retained checkout no longer matches its immutable manifest."""


class SnapshotCleanupError(RuntimeError):
    """Snapshot resources remain owned for a later recoverable cleanup."""


class _PublishedLifecycleCancellation(asyncio.CancelledError):
    """Cancellation delivered after a generation became authoritative."""


async def _owned_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await asyncio.shield(task)
        raise


@dataclass(frozen=True, slots=True)
class SnapshotIndexRuntime:
    executable: Path
    env: Mapping[str, str]
    managed_execution_id: str
    credential_generation: int
    argv_prefix: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    generation: int
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


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _remaining_seconds(deadline_at: datetime) -> float:
    if deadline_at.tzinfo is None:
        raise ValueError("Ask snapshot deadline must be timezone-aware")
    remaining = (deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("Ask snapshot deadline exceeded")
    return remaining


def _safe_process_env() -> dict[str, str]:
    env = {
        key: value for key in ("PATH", "SYSTEMROOT") if (value := os.environ.get(key)) is not None
    }
    env.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(
    repository_root: Path,
    *arguments: str,
    timeout: float,
    input_bytes: bytes | None = None,
) -> bytes:
    completed = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "--literal-pathspecs",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "diff.external=",
            "-C",
            str(repository_root),
            *arguments,
        ],
        input=input_bytes,
        check=True,
        capture_output=True,
        timeout=timeout,
        env=_safe_process_env(),
    )
    return completed.stdout


def _native_snapshot(
    executable: Path,
    repository_root: Path,
    *,
    project_id: str,
    commit_oid: str,
    action: str,
    timeout: float,
    target_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request: dict[str, Any] = {
        "schema_version": 1,
        "project_id": project_id,
        "commit_oid": commit_oid,
        "action": action,
    }
    if target_root is not None:
        request["target_root"] = str(target_root)
    completed = subprocess.run(
        [
            str(executable),
            "--quiet",
            "--format",
            "json",
            "--project",
            str(repository_root),
            "evidence",
            "--snapshot-json",
            _canonical_json(request),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_safe_process_env(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if action == "verify" and "inventory_mismatch" in detail:
            raise SnapshotDriftError(detail)
        raise RuntimeError(f"native Ask snapshot {action} failed: {detail}")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("native Ask snapshot returned invalid JSON") from error
    if not isinstance(response, dict) or response.get("schema_version") != 1:
        raise RuntimeError("native Ask snapshot returned an unsupported contract")
    binding = response.get("binding")
    inventory = response.get("inventory")
    if not isinstance(binding, dict) or not isinstance(inventory, dict):
        raise RuntimeError("native Ask snapshot omitted binding or inventory")
    if binding.get("project_id") != project_id or binding.get("commit_oid") != commit_oid:
        raise SnapshotDriftError("native Ask snapshot identity changed")
    if inventory.get("complete") is not True or (
        inventory.get("digest") != binding.get("inventory_digest")
    ):
        raise SnapshotDriftError("native Ask snapshot inventory is incomplete or mismatched")
    return binding, inventory


class AskSnapshotManager:
    """Create, verify, recover, and release one managed detached checkout."""

    def __init__(
        self,
        *,
        worktree_storage: LocalWorktreeManager,
        run_storage: AskRunStorage,
        snapshot_executable: Path | None = None,
        index_preparer: IndexPreparer | None = None,
        index_releaser: IndexReleaser | None = None,
        credential_manager: ManagedCredentialManager | None = None,
    ) -> None:
        if index_preparer is None and credential_manager is None:
            raise ValueError("Ask snapshots require an injected or managed index preparer")
        executable = snapshot_executable or resolve_native_bin("gcode")
        if executable is None:
            raise RuntimeError("gcode_not_installed")
        self.worktree_storage = worktree_storage
        self.run_storage = run_storage
        self.snapshot_executable = Path(executable)
        self.index_preparer = index_preparer
        self.index_releaser = index_releaser
        self.credential_manager = credential_manager

    def prepare(
        self,
        *,
        run_id: str,
        repository_root: Path,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        return self._run_sync(
            self.prepare_async(
                run_id=run_id,
                repository_root=repository_root,
                artifacts=artifacts,
            )
        )

    async def prepare_async(
        self,
        *,
        run_id: str,
        repository_root: Path,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        record = self._record_for_artifacts(run_id, artifacts)
        deadline_at = record.binding.deadline_at
        _remaining_seconds(deadline_at)
        if self.run_storage.get_snapshot_generation(run_id) is not None:
            raise ValueError("Ask run already has a current snapshot generation")
        repository_root = repository_root.resolve()
        snapshot_project_id = code_index_id_for_root(artifacts.run_root / "source")
        binding, inventory = await asyncio.to_thread(
            _native_snapshot,
            self.snapshot_executable,
            repository_root,
            project_id=snapshot_project_id,
            commit_oid=record.binding.commit_oid,
            action="inspect",
            timeout=_remaining_seconds(deadline_at),
        )
        if binding.get("tree_oid") != record.binding.tree_oid:
            raise SnapshotDriftError("native Ask snapshot tree differs from the stored run")
        identity_body = {
            "schema_version": 1,
            "run_id": run_id,
            "project_id": record.binding.project_id,
            "commit_oid": record.binding.commit_oid,
            "deadline_at": self._format_deadline(deadline_at),
            "retrieval_mode": record.binding.retrieval_mode.value,
            "binding": binding,
            "inventory": inventory,
        }
        _remaining_seconds(deadline_at)
        identity_pointer = await _owned_thread(
            artifacts.write_body,
            "snapshot",
            identity_body,
            timeout_seconds=_remaining_seconds(deadline_at),
        )
        _remaining_seconds(deadline_at)
        await _owned_thread(
            self.run_storage.attach_snapshot,
            run_id,
            inventory_digest=str(binding["inventory_digest"]),
            snapshot_artifact=identity_pointer,
            deadline_at=deadline_at,
        )
        return await self._create_generation(
            record=record,
            generation=1,
            expected_previous_generation=None,
            repository_root=repository_root,
            identity_pointer=identity_pointer,
            binding=binding,
            inventory=inventory,
            artifacts=artifacts,
        )

    def recover(
        self,
        *,
        run_id: str,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        return self._run_sync(self.recover_async(run_id=run_id, artifacts=artifacts))

    async def recover_async(
        self,
        *,
        run_id: str,
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        record = self._record_for_artifacts(run_id, artifacts)
        deadline_at = record.binding.deadline_at
        _remaining_seconds(deadline_at)
        await asyncio.to_thread(artifacts.verify_manifest)
        _remaining_seconds(deadline_at)
        current = self.run_storage.get_snapshot_generation(run_id)
        if current is None:
            raise SnapshotDriftError("Ask run has no persisted snapshot lifecycle")
        lifecycle = await asyncio.to_thread(artifacts.read_body, current.lifecycle_artifact)
        identity_pointer, identity = await self._validate_lifecycle(
            record, current, lifecycle, artifacts
        )
        repository_root = Path(self._required_text(lifecycle, "repository_root")).resolve()
        source_root = Path(self._required_text(lifecycle, "source_root"))
        if source_root != artifacts.run_root / "source":
            raise SnapshotDriftError("snapshot lifecycle source path escaped the Ask run")
        binding = self._required_mapping(identity, "binding")
        inventory = self._required_mapping(identity, "inventory")
        _remaining_seconds(deadline_at)
        inspected_binding, inspected_inventory = await asyncio.to_thread(
            _native_snapshot,
            self.snapshot_executable,
            repository_root,
            project_id=self._required_text(binding, "project_id"),
            commit_oid=record.binding.commit_oid,
            action="inspect",
            timeout=_remaining_seconds(deadline_at),
        )
        if inspected_binding != binding or inspected_inventory != inventory:
            raise SnapshotDriftError("snapshot identity no longer matches the pinned commit")

        created_worktree = False
        worktree_id = self._required_text(lifecycle, "worktree_id")
        if source_root.exists():
            current_worktree = self.worktree_storage.get_by_path(str(source_root))
            if current_worktree is None or current_worktree.id != worktree_id:
                raise SnapshotDriftError("snapshot checkout lost its current managed record")
            await asyncio.to_thread(
                _native_snapshot,
                self.snapshot_executable,
                repository_root,
                project_id=self._required_text(binding, "project_id"),
                commit_oid=record.binding.commit_oid,
                action="verify",
                target_root=source_root,
                timeout=_remaining_seconds(deadline_at),
            )
        else:
            stale = self.worktree_storage.get(worktree_id)
            if stale is not None:
                self.worktree_storage.delete(stale.id)
            await _owned_thread(
                _git,
                repository_root,
                "worktree",
                "prune",
                timeout=_remaining_seconds(deadline_at),
            )
            worktree_id = await self._create_worktree(
                record=record,
                repository_root=repository_root,
                source_root=source_root,
                deadline_at=deadline_at,
            )
            created_worktree = True
            try:
                await self._materialize(
                    record=record,
                    repository_root=repository_root,
                    source_root=source_root,
                    deadline_at=deadline_at,
                )
            except BaseException:
                await _owned_thread(
                    self._delete_worktree,
                    repository_root,
                    source_root,
                    worktree_id,
                )
                raise

        runtime: SnapshotIndexRuntime | None = None
        try:
            runtime = await self._prepare_index(
                record.run_id, source_root, deadline_at, record.binding.commit_oid
            )
            lifecycle_pointer = await self._publish_lifecycle(
                record=record,
                generation=current.generation + 1,
                expected_previous_generation=current.generation,
                repository_root=repository_root,
                source_root=source_root,
                worktree_id=worktree_id,
                runtime=runtime,
                identity_pointer=identity_pointer,
                binding=binding,
                artifacts=artifacts,
            )
        except _PublishedLifecycleCancellation:
            previous_runtime = self._runtime_from_lifecycle(lifecycle)
            await _owned_thread(self._release_runtime, previous_runtime, "recovered")
            raise
        except BaseException:
            if runtime is not None:
                await _owned_thread(self._release_runtime, runtime, "recovery_failed")
            if created_worktree:
                await _owned_thread(
                    self._delete_worktree,
                    repository_root,
                    source_root,
                    worktree_id,
                )
            raise
        previous_runtime = self._runtime_from_lifecycle(lifecycle)
        await _owned_thread(self._release_runtime, previous_runtime, "recovered")
        return PreparedSnapshot(
            generation=current.generation + 1,
            commit_oid=record.binding.commit_oid,
            binding=binding,
            inventory=inventory,
            repository_root=repository_root,
            source_root=source_root,
            worktree_id=worktree_id,
            runtime=runtime,
            manifest_pointer=lifecycle_pointer,
        )

    def release(self, snapshot: PreparedSnapshot, *, artifacts: AskArtifactStore) -> None:
        if artifacts.run_root / "source" != snapshot.source_root:
            raise ValueError("snapshot does not belong to this Ask artifact store")
        current = self.run_storage.get_snapshot_generation(artifacts.run_id)
        if (
            current is None
            or current.generation != snapshot.generation
            or (current.lifecycle_artifact != snapshot.manifest_pointer)
        ):
            raise SnapshotDriftError("release requires the current snapshot lifecycle generation")
        self._release_runtime(snapshot.runtime, "released")
        self._delete_worktree(
            snapshot.repository_root,
            snapshot.source_root,
            snapshot.worktree_id,
        )

    async def _create_generation(
        self,
        *,
        record: AskRunRecord,
        generation: int,
        expected_previous_generation: int | None,
        repository_root: Path,
        identity_pointer: dict[str, Any],
        binding: dict[str, Any],
        inventory: dict[str, Any],
        artifacts: AskArtifactStore,
    ) -> PreparedSnapshot:
        source_root = artifacts.run_root / "source"
        if source_root.exists():
            raise FileExistsError(f"Ask snapshot path already exists: {source_root}")
        worktree_id = await self._create_worktree(
            record=record,
            repository_root=repository_root,
            source_root=source_root,
            deadline_at=record.binding.deadline_at,
        )
        runtime: SnapshotIndexRuntime | None = None
        try:
            await self._materialize(
                record=record,
                repository_root=repository_root,
                source_root=source_root,
                deadline_at=record.binding.deadline_at,
            )
            runtime = await self._prepare_index(
                record.run_id,
                source_root,
                record.binding.deadline_at,
                record.binding.commit_oid,
            )
            lifecycle_pointer = await self._publish_lifecycle(
                record=record,
                generation=generation,
                expected_previous_generation=expected_previous_generation,
                repository_root=repository_root,
                source_root=source_root,
                worktree_id=worktree_id,
                runtime=runtime,
                identity_pointer=identity_pointer,
                binding=binding,
                artifacts=artifacts,
            )
        except _PublishedLifecycleCancellation:
            raise
        except BaseException:
            if runtime is not None:
                await _owned_thread(self._release_runtime, runtime, "preparation_failed")
            await _owned_thread(
                self._delete_worktree,
                repository_root,
                source_root,
                worktree_id,
            )
            raise
        return PreparedSnapshot(
            generation=generation,
            commit_oid=record.binding.commit_oid,
            binding=binding,
            inventory=inventory,
            repository_root=repository_root,
            source_root=source_root,
            worktree_id=worktree_id,
            runtime=runtime,
            manifest_pointer=lifecycle_pointer,
        )

    async def _create_worktree(
        self,
        *,
        record: AskRunRecord,
        repository_root: Path,
        source_root: Path,
        deadline_at: datetime,
    ) -> str:
        added = False
        try:
            add_task = asyncio.create_task(
                asyncio.to_thread(
                    _git,
                    repository_root,
                    "worktree",
                    "add",
                    "--detach",
                    "--no-checkout",
                    str(source_root),
                    record.binding.commit_oid,
                    timeout=_remaining_seconds(deadline_at),
                )
            )
            try:
                await asyncio.shield(add_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(add_task)
                except BaseException:
                    pass
                else:
                    await asyncio.shield(
                        asyncio.create_task(
                            asyncio.to_thread(
                                self._delete_worktree,
                                repository_root,
                                source_root,
                                None,
                            )
                        )
                    )
                raise
            added = True
            source_root.chmod(0o700)
            remaining = _remaining_seconds(deadline_at)
            with database_operation_deadline(
                timeout_seconds=remaining,
                operation_timeout_seconds=remaining,
            ):
                worktree = self.worktree_storage.create(
                    project_id=record.binding.project_id,
                    branch_name=None,
                    worktree_path=str(source_root),
                    base_branch=record.binding.commit_oid,
                    agent_session_id=str(self._caller_session_id(record.run_id)),
                    workspace_role="ask_snapshot",
                )
            return worktree.id
        except BaseException:
            if added:
                await _owned_thread(
                    self._delete_worktree,
                    repository_root,
                    source_root,
                    None,
                )
            raise

    async def _materialize(
        self,
        *,
        record: AskRunRecord,
        repository_root: Path,
        source_root: Path,
        deadline_at: datetime,
    ) -> None:
        await _owned_thread(
            _native_snapshot,
            self.snapshot_executable,
            repository_root,
            project_id=code_index_id_for_root(source_root),
            commit_oid=record.binding.commit_oid,
            action="materialize",
            target_root=source_root,
            timeout=_remaining_seconds(deadline_at),
        )
        _remaining_seconds(deadline_at)
        async with asyncio.timeout(_remaining_seconds(deadline_at)):
            await ensure_project_json_for_isolation(
                repository_root, source_root, snapshot_commit=record.binding.commit_oid
            )
        _remaining_seconds(deadline_at)

    async def _publish_lifecycle(
        self,
        *,
        record: AskRunRecord,
        generation: int,
        expected_previous_generation: int | None,
        repository_root: Path,
        source_root: Path,
        worktree_id: str,
        runtime: SnapshotIndexRuntime,
        identity_pointer: dict[str, Any],
        binding: Mapping[str, Any],
        artifacts: AskArtifactStore,
    ) -> dict[str, Any]:
        deadline_at = record.binding.deadline_at
        _remaining_seconds(deadline_at)
        body = {
            "schema_version": 1,
            "generation": generation,
            "run_id": record.run_id,
            "project_id": record.binding.project_id,
            "commit_oid": record.binding.commit_oid,
            "deadline_at": self._format_deadline(deadline_at),
            "retrieval_mode": record.binding.retrieval_mode.value,
            "inventory_digest": binding["inventory_digest"],
            "snapshot_artifact": identity_pointer,
            "repository_root": str(repository_root),
            "source_root": str(source_root),
            "worktree_id": worktree_id,
            "index_runtime": {
                "executable": str(runtime.executable),
                "argv_prefix": list(runtime.argv_prefix),
                "managed_execution_id": runtime.managed_execution_id,
                "credential_generation": runtime.credential_generation,
            },
        }
        pointer: dict[str, Any] = await _owned_thread(
            artifacts.write_body,
            "snapshot-lifecycle",
            body,
            timeout_seconds=_remaining_seconds(deadline_at),
        )
        _remaining_seconds(deadline_at)
        await _owned_thread(artifacts.verify_manifest)
        _remaining_seconds(deadline_at)
        publish_task = asyncio.create_task(
            asyncio.to_thread(
                self.run_storage.publish_snapshot_generation,
                record.run_id,
                generation=generation,
                lifecycle_artifact=pointer,
                expected_previous_generation=expected_previous_generation,
                deadline_at=deadline_at,
            )
        )
        try:
            await asyncio.shield(publish_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(publish_task)
            except BaseException:
                raise
            raise _PublishedLifecycleCancellation from None
        return pointer

    async def _validate_lifecycle(
        self,
        record: AskRunRecord,
        current: SnapshotGeneration,
        lifecycle: Mapping[str, Any],
        artifacts: AskArtifactStore,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if lifecycle.get("generation") != current.generation:
            raise SnapshotDriftError("snapshot lifecycle generation does not match pipeline state")
        expected = {
            "run_id": record.run_id,
            "project_id": record.binding.project_id,
            "commit_oid": record.binding.commit_oid,
            "deadline_at": self._format_deadline(record.binding.deadline_at),
            "retrieval_mode": record.binding.retrieval_mode.value,
            "inventory_digest": record.binding.inventory_digest,
        }
        for key, value in expected.items():
            if lifecycle.get(key) != value:
                raise SnapshotDriftError(f"snapshot lifecycle {key} differs from stored run")
        identity_pointer = self._required_mapping(lifecycle, "snapshot_artifact")
        if identity_pointer != record.binding.snapshot_artifact:
            raise SnapshotDriftError("snapshot lifecycle artifact identity changed")
        identity = await asyncio.to_thread(artifacts.read_body, identity_pointer)
        for key in ("run_id", "project_id", "commit_oid", "deadline_at", "retrieval_mode"):
            if identity.get(key) != expected[key]:
                raise SnapshotDriftError(f"snapshot identity {key} differs from stored run")
        binding = self._required_mapping(identity, "binding")
        inventory = self._required_mapping(identity, "inventory")
        if binding.get("inventory_digest") != record.binding.inventory_digest or (
            inventory.get("digest") != record.binding.inventory_digest
        ):
            raise SnapshotDriftError("snapshot identity inventory digest changed")
        return identity_pointer, identity

    async def _prepare_index(
        self,
        run_id: str,
        source_root: Path,
        deadline_at: datetime,
        commit_oid: str,
    ) -> SnapshotIndexRuntime:
        remaining = _remaining_seconds(deadline_at)
        if self.index_preparer is not None:
            async with asyncio.timeout(remaining):
                runtime = await self.index_preparer(source_root, deadline_at)
            _remaining_seconds(deadline_at)
            return runtime
        if self.credential_manager is None:
            raise RuntimeError("managed Ask snapshot index services are unavailable")
        caller_session_id = self._caller_session_id(run_id)
        issue_task = asyncio.create_task(
            asyncio.to_thread(
                self._issue_index_credential,
                caller_session_id,
                source_root,
                deadline_at,
                remaining,
            )
        )
        try:
            issued = await asyncio.shield(issue_task)
        except asyncio.CancelledError:
            try:
                issued = await asyncio.shield(issue_task)
            except BaseException:
                pass
            else:
                await _owned_thread(
                    self.credential_manager.revoke,
                    issued.credential.managed_execution_id,
                    generation=issued.credential.credential_generation,
                    reason="ask_snapshot_preparation_failed",
                )
            raise
        try:
            _remaining_seconds(deadline_at)
            if Path(issued.project_path).resolve() != source_root.resolve():
                raise RuntimeError("managed Ask grant resolved a different project path")
            identity_env = {
                "GOBBY_AGENT_RUN_ID": str(issued.credential.managed_execution_id),
                "GOBBY_MACHINE_ID": require_machine_id(),
                "GOBBY_PROJECT_ID": str(issued.project_id),
                "GOBBY_SESSION_ID": str(caller_session_id),
            }
            prepare_task = asyncio.create_task(
                ensure_isolation_code_index(
                    str(source_root),
                    timeout=_remaining_seconds(deadline_at),
                    gcode_bin=self.snapshot_executable,
                    credential=issued.credential,
                    principal_kind="tool_chat",
                    identity_env=identity_env,
                    snapshot_commit=commit_oid,
                )
            )
            try:
                result = await asyncio.shield(prepare_task)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(prepare_task)
                raise
            if not result.runtime_home:
                raise RuntimeError("managed Ask snapshot runtime home is missing")
            executable = self.snapshot_executable
            _remaining_seconds(deadline_at)
        except BaseException:
            await _owned_thread(
                self.credential_manager.revoke,
                issued.credential.managed_execution_id,
                generation=issued.credential.credential_generation,
                reason="ask_snapshot_preparation_failed",
            )
            raise
        return SnapshotIndexRuntime(
            executable=Path(executable),
            # Evidence invokes the native binary directly, bypassing the wrapper
            # that normally selects this grant's local machine identity.
            env={**identity_env, **result.env, "GOBBY_HOME": result.runtime_home},
            managed_execution_id=str(issued.credential.managed_execution_id),
            credential_generation=issued.credential.credential_generation,
        )

    def _issue_index_credential(
        self,
        caller_session_id: UUID,
        source_root: Path,
        deadline_at: datetime,
        timeout_seconds: float,
    ) -> Any:
        assert self.credential_manager is not None
        with database_operation_deadline(
            timeout_seconds=timeout_seconds,
            operation_timeout_seconds=timeout_seconds,
        ):
            return self.credential_manager.issue_tool_request(
                session_id=caller_session_id,
                requested_project_path=str(source_root),
                expires_at=deadline_at,
            )

    def _caller_session_id(self, run_id: str) -> UUID:
        caller = self.run_storage.execution_inputs(run_id).get("caller_session_id")
        if not isinstance(caller, str) or not caller:
            raise RuntimeError("Ask original caller session is not bound")
        try:
            return UUID(caller)
        except ValueError as error:
            raise RuntimeError("Ask original caller session is invalid") from error

    def _record_for_artifacts(self, run_id: str, artifacts: AskArtifactStore) -> AskRunRecord:
        record = self.run_storage.get(run_id)
        if record is None:
            raise ValueError(f"Ask run not found: {run_id}")
        if artifacts.run_id != run_id or artifacts.project_id != record.binding.project_id:
            raise ValueError("Ask artifact store does not belong to the stored run")
        return record

    def _delete_worktree(
        self,
        repository_root: Path,
        source_root: Path,
        worktree_id: str | None,
    ) -> None:
        cutoff = time.monotonic() + _CLEANUP_TIMEOUT_SECONDS

        def remaining() -> float:
            value = cutoff - time.monotonic()
            if value <= 0:
                raise SnapshotCleanupError(
                    f"snapshot cleanup deadline exceeded; ownership retained for {source_root}"
                )
            return value

        try:
            _git(
                repository_root,
                "worktree",
                "remove",
                "--force",
                str(source_root),
                timeout=remaining(),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            try:
                _git(
                    repository_root,
                    "worktree",
                    "prune",
                    timeout=remaining(),
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise SnapshotCleanupError(
                    f"snapshot Git cleanup failed; ownership retained for {source_root}"
                ) from error
        if source_root.exists():
            raise SnapshotCleanupError(
                f"snapshot checkout still exists; ownership retained for {source_root}"
            )
        if worktree_id is not None:
            timeout_seconds = remaining()
            with database_operation_deadline(
                timeout_seconds=timeout_seconds,
                operation_timeout_seconds=timeout_seconds,
            ):
                worktree = self.worktree_storage.get(worktree_id)
                if worktree is not None:
                    remaining()
                    self.worktree_storage.delete(worktree_id)

    def _release_runtime(self, runtime: SnapshotIndexRuntime, reason: str) -> None:
        if self.index_releaser is not None:
            self.index_releaser(runtime)
        elif self.credential_manager is not None:
            self.credential_manager.revoke(
                UUID(runtime.managed_execution_id),
                generation=runtime.credential_generation,
                reason=f"ask_snapshot_{reason}",
            )

    @staticmethod
    def _runtime_from_lifecycle(body: Mapping[str, Any]) -> SnapshotIndexRuntime:
        runtime = body.get("index_runtime")
        if not isinstance(runtime, Mapping):
            raise SnapshotDriftError("snapshot lifecycle has invalid runtime identity")
        executable = runtime.get("executable")
        argv_prefix = runtime.get("argv_prefix")
        managed_execution_id = runtime.get("managed_execution_id")
        generation = runtime.get("credential_generation")
        if (
            not isinstance(executable, str)
            or not isinstance(argv_prefix, list)
            or not all(isinstance(value, str) for value in argv_prefix)
            or not isinstance(managed_execution_id, str)
            or not isinstance(generation, int)
        ):
            raise SnapshotDriftError("snapshot lifecycle has invalid runtime identity")
        return SnapshotIndexRuntime(
            executable=Path(executable),
            env={},
            managed_execution_id=managed_execution_id,
            credential_generation=generation,
            argv_prefix=tuple(argv_prefix),
        )

    @staticmethod
    def _required_text(body: Mapping[str, Any], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise SnapshotDriftError(f"snapshot lifecycle is missing {key}")
        return value

    @staticmethod
    def _required_mapping(body: Mapping[str, Any], key: str) -> dict[str, Any]:
        value = body.get(key)
        if not isinstance(value, Mapping):
            raise SnapshotDriftError(f"snapshot lifecycle is missing {key}")
        return dict(value)

    @staticmethod
    def _format_deadline(deadline_at: datetime) -> str:
        return deadline_at.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _run_sync(coroutine: Coroutine[Any, Any, PreparedSnapshot]) -> PreparedSnapshot:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("use the async Ask snapshot API from an active event loop")
