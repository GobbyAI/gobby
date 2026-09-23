"""Managed live-index bindings and recovery fencing for Ask runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from gobby.agents.code_index import (
    _CONFIG_PROBE_TIMEOUT,
    _SEARCH_SMOKE_TIMEOUT,
    _prepare_gcode_runtime,
    _run_gcode,
)
from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.storage import AskRunStorage
from gobby.code_index.eligibility import overlay_project_id_for_root
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV
from gobby.utils.machine_id import require_machine_id
from gobby.utils.native_bin import resolve_native_bin

if TYPE_CHECKING:
    from gobby.ask.contracts import AskRunRecord
    from gobby.storage.managed_credentials import ManagedCredentialManager

logger = logging.getLogger(__name__)


class SnapshotDriftError(RuntimeError):
    """An Ask binding or generation no longer matches its authority."""


class SnapshotCleanupError(RuntimeError):
    """Managed Ask authority could not be released."""


class _PublishedLifecycleCancellation(asyncio.CancelledError):
    """Cancellation arrived after the generation was published."""


async def _owned_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await asyncio.shield(task)
        raise


@dataclass(frozen=True)
class SnapshotIndexRuntime:
    executable: Path
    env: Mapping[str, str] = field(repr=False)
    managed_execution_id: str
    credential_generation: int
    argv_prefix: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedSnapshot:
    generation: int
    commit_oid: str
    binding: dict[str, Any]
    repository_root: Path
    source_root: Path
    runtime: SnapshotIndexRuntime
    manifest_pointer: dict[str, Any]


IndexPreparer = Callable[[Path, datetime], Awaitable[SnapshotIndexRuntime]]
IndexReleaser = Callable[[SnapshotIndexRuntime], None]


def _remaining_seconds(deadline_at: datetime) -> float:
    if deadline_at.tzinfo is None:
        raise ValueError("Ask snapshot deadline must be timezone-aware")
    remaining = (deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("Ask snapshot deadline exceeded")
    return remaining


class AskSnapshotManager:
    def __init__(
        self,
        *,
        run_storage: AskRunStorage,
        snapshot_executable: Path | None = None,
        index_preparer: IndexPreparer | None = None,
        index_releaser: IndexReleaser | None = None,
        credential_manager: ManagedCredentialManager | None = None,
    ) -> None:
        if index_preparer is None and credential_manager is None:
            raise ValueError("Ask requires managed index access")
        executable = snapshot_executable or resolve_native_bin("gcode")
        if executable is None:
            raise RuntimeError("gcode_not_installed")
        self.run_storage = run_storage
        self.snapshot_executable = Path(executable)
        self.index_preparer = index_preparer
        self.index_releaser = index_releaser
        self.credential_manager = credential_manager

    def prepare(
        self, *, run_id: str, repository_root: Path, artifacts: AskArtifactStore
    ) -> PreparedSnapshot:
        return self._run_sync(
            self.prepare_async(run_id=run_id, repository_root=repository_root, artifacts=artifacts)
        )

    async def prepare_async(
        self, *, run_id: str, repository_root: Path, artifacts: AskArtifactStore
    ) -> PreparedSnapshot:
        record = self._record_for_artifacts(run_id, artifacts)
        if record.generation is not None:
            raise ValueError("Ask run already has a current binding generation")
        return await self._bind(record, repository_root, artifacts, None)

    def recover(self, *, run_id: str, artifacts: AskArtifactStore) -> PreparedSnapshot:
        return self._run_sync(self.recover_async(run_id=run_id, artifacts=artifacts))

    async def recover_async(self, *, run_id: str, artifacts: AskArtifactStore) -> PreparedSnapshot:
        record = self._record_for_artifacts(run_id, artifacts)
        current = record.generation
        repository_root = Path(record.binding.repository_root)
        if current is None:
            return await self._bind(record, repository_root, artifacts, None)
        artifacts.verify_manifest()
        lifecycle = artifacts.read_body(current.lifecycle_artifact)
        if lifecycle.get("repository_root") != str(repository_root.resolve()):
            raise SnapshotDriftError("Ask binding repository changed")
        # Fence stale consumers with CAS before revoking the previous credential.
        published = False
        try:
            prepared = await self._bind(record, repository_root, artifacts, current.generation)
            published = True
            return prepared
        except _PublishedLifecycleCancellation:
            published = True
            raise
        finally:
            if published and self.credential_manager is not None:
                await _owned_thread(
                    self.credential_manager.revoke,
                    UUID(str(lifecycle["managed_execution_id"])),
                    generation=int(lifecycle["credential_generation"]),
                    reason="ask_binding_recovered",
                )

    async def _bind(
        self,
        record: AskRunRecord,
        repository_root: Path,
        artifacts: AskArtifactStore,
        previous: int | None,
    ) -> PreparedSnapshot:
        root = repository_root.resolve(strict=True)
        if root != Path(record.binding.repository_root).resolve(strict=True):
            raise SnapshotDriftError("Ask caller repository differs from recorded binding")
        if root.is_relative_to(
            artifacts.run_root.resolve()
        ) or artifacts.run_root.resolve().is_relative_to(root):
            raise ValueError("Ask repository and scratch roots must be disjoint")
        binding = {
            "project_id": overlay_project_id_for_root(root) or record.binding.project_id,
            "commit_oid": record.binding.commit_oid,
            "tree_oid": record.binding.tree_oid,
        }
        runtime = await self._prepare_index(record, root, artifacts)
        started = time.monotonic()
        generation = 1 if previous is None else previous + 1
        try:
            body = {
                "schema_version": 1,
                "generation": generation,
                "run_id": record.run_id,
                "project_id": record.binding.project_id,
                "binding": binding,
                "repository_root": str(root),
                "deadline_at": record.binding.deadline_at.isoformat().replace("+00:00", "Z"),
                "retrieval_mode": record.binding.retrieval_mode.value,
                "executable": str(runtime.executable.resolve()),
                "argv_prefix": list(runtime.argv_prefix),
                "managed_execution_id": runtime.managed_execution_id,
                "credential_generation": runtime.credential_generation,
            }
            pointer = await _owned_thread(
                artifacts.write_body,
                "binding-lifecycle",
                body,
                timeout_seconds=_remaining_seconds(record.binding.deadline_at),
            )
            await _owned_thread(artifacts.verify_manifest)
            publish = asyncio.create_task(
                asyncio.to_thread(
                    self.run_storage.publish_snapshot_generation,
                    record.run_id,
                    generation=generation,
                    lifecycle_artifact=pointer,
                    expected_previous_generation=previous,
                    deadline_at=record.binding.deadline_at,
                )
            )
            try:
                await asyncio.shield(publish)
            except asyncio.CancelledError:
                await asyncio.shield(publish)
                raise _PublishedLifecycleCancellation from None
        except _PublishedLifecycleCancellation:
            raise
        except BaseException:
            await _owned_thread(self._release_runtime, runtime, "preparation_failed")
            raise
        finally:
            logger.info(
                "Ask bind phase=publish run=%s duration_ms=%.1f",
                record.run_id,
                (time.monotonic() - started) * 1000,
            )
        return PreparedSnapshot(
            generation=generation,
            commit_oid=record.binding.commit_oid,
            binding=binding,
            repository_root=root,
            source_root=root,
            runtime=runtime,
            manifest_pointer=pointer,
        )

    async def _prepare_index(
        self, record: AskRunRecord, root: Path, artifacts: AskArtifactStore
    ) -> SnapshotIndexRuntime:
        deadline = record.binding.deadline_at
        if self.index_preparer is not None:
            async with asyncio.timeout(
                min(_remaining_seconds(deadline), _CONFIG_PROBE_TIMEOUT + _SEARCH_SMOKE_TIMEOUT)
            ):
                return await self.index_preparer(root, deadline)
        assert self.credential_manager is not None
        caller = self._caller_session_id(record.run_id)
        started = time.monotonic()
        issue = asyncio.create_task(
            asyncio.to_thread(self._issue_credential, caller, root, deadline)
        )
        try:
            issued = await asyncio.shield(issue)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                issued = await asyncio.shield(issue)
                await _owned_thread(
                    self.credential_manager.revoke,
                    issued.credential.managed_execution_id,
                    generation=issued.credential.credential_generation,
                    reason="ask_binding_cancelled",
                )
            raise
        finally:
            logger.info(
                "Ask bind phase=grant run=%s duration_ms=%.1f",
                record.run_id,
                (time.monotonic() - started) * 1000,
            )
        try:
            if (
                Path(issued.project_path).resolve() != root
                or str(issued.project_id) != record.binding.project_id
            ):
                raise SnapshotDriftError("managed Ask grant resolved another caller project")
            identity = {
                "GOBBY_AGENT_RUN_ID": str(issued.credential.managed_execution_id),
                "GOBBY_MACHINE_ID": require_machine_id(),
                "GOBBY_PROJECT_ID": str(issued.project_id),
                "GOBBY_SESSION_ID": str(caller),
            }
            result = await _owned_thread(
                _prepare_gcode_runtime,
                workspace=artifacts.run_root / "runtime",
                gcode_bin=self.snapshot_executable,
                credential=issued.credential,
                runtime_root=artifacts.run_root / "runtime-home",
                machine_id=identity["GOBBY_MACHINE_ID"],
                project_id=identity["GOBBY_PROJECT_ID"],
                session_id=identity["GOBBY_SESSION_ID"],
                principal_kind="tool_chat",
            )
            if not result.runtime_home:
                raise RuntimeError("managed Ask runtime home is missing")
            if not result.api_token:
                raise RuntimeError("managed Ask runtime capability is missing")
            env = {
                **identity,
                **result.env,
                "GOBBY_HOME": result.runtime_home,
                GOBBY_AGENT_API_TOKEN_ENV: result.api_token,
            }
            for phase, args, cap in (
                ("config", ["status", "--format", "json"], _CONFIG_PROBE_TIMEOUT),
                (
                    "search",
                    ["search-content", "__gobby_code_index_smoke__", "--limit", "1"],
                    _SEARCH_SMOKE_TIMEOUT,
                ),
            ):
                started = time.monotonic()
                try:
                    await _run_gcode(
                        [
                            str(self.snapshot_executable),
                            *args,
                            "--quiet",
                            "--allow-stale",
                            "--project",
                            str(root),
                        ],
                        timeout=min(_remaining_seconds(deadline), cap),
                        timeout_code=f"ask_bind_{phase}_timeout",
                        failure_code=f"ask_bind_{phase}_failed",
                        env=env,
                    )
                finally:
                    logger.info(
                        "Ask bind phase=%s run=%s duration_ms=%.1f",
                        phase,
                        record.run_id,
                        (time.monotonic() - started) * 1000,
                    )
        except BaseException:
            await _owned_thread(
                self.credential_manager.revoke,
                issued.credential.managed_execution_id,
                generation=issued.credential.credential_generation,
                reason="ask_binding_preparation_failed",
            )
            raise
        return SnapshotIndexRuntime(
            executable=self.snapshot_executable,
            env=env,
            managed_execution_id=str(issued.credential.managed_execution_id),
            credential_generation=issued.credential.credential_generation,
        )

    def _issue_credential(self, caller: UUID, root: Path, deadline: datetime) -> Any:
        assert self.credential_manager is not None
        timeout = min(_remaining_seconds(deadline), _CONFIG_PROBE_TIMEOUT)
        with database_operation_deadline(
            timeout_seconds=timeout, operation_timeout_seconds=timeout
        ):
            return self.credential_manager.issue_tool_request(
                session_id=caller, requested_project_path=str(root), expires_at=deadline
            )

    def release(self, snapshot: PreparedSnapshot, *, artifacts: AskArtifactStore) -> None:
        record = self._record_for_artifacts(artifacts.run_id, artifacts)
        current = record.generation
        if (
            current is None
            or current.generation != snapshot.generation
            or current.lifecycle_artifact != snapshot.manifest_pointer
        ):
            raise SnapshotDriftError("release requires the current binding generation")
        self._release_runtime(snapshot.runtime, "released")

    def _release_runtime(self, runtime: SnapshotIndexRuntime, reason: str) -> None:
        if self.index_releaser is not None:
            self.index_releaser(runtime)
        elif self.credential_manager is not None:
            self.credential_manager.revoke(
                UUID(runtime.managed_execution_id),
                generation=runtime.credential_generation,
                reason=f"ask_binding_{reason}",
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

    @staticmethod
    def _run_sync(coroutine: Coroutine[Any, Any, PreparedSnapshot]) -> PreparedSnapshot:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("use the async Ask snapshot API from an active event loop")
