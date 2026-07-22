"""In-process drain fence for project-scoped derived-state writers."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any


class ProjectWriteRejected(RuntimeError):
    """Raised when a project-scoped write cannot be admitted."""


class ProjectWriteDrainTimeout(TimeoutError):
    """Raised when purge cannot drain admitted writers before its bound."""


class ProjectWriteFence:
    """Coordinate ordinary writers with exclusive project purge barriers."""

    def __init__(self, project_lookup: Callable[[str], Any | None]) -> None:
        self._project_lookup = project_lookup
        self._condition = asyncio.Condition()
        self._writers: Counter[str] = Counter()
        self._global_writers = 0
        self._exclusive: set[str] = set()
        self._task_writers: dict[asyncio.Task[Any], Counter[str]] = {}
        self._task_global_writers: Counter[asyncio.Task[Any]] = Counter()

    @asynccontextmanager
    async def writer(self, project_id: str) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Project writer admission requires an asyncio task")

        admitted_by_global = False
        async with self._condition:
            task_writers = self._task_writers.get(task)
            admitted_by_global = self._task_global_writers[task] > 0
            if admitted_by_global:
                pass
            elif task_writers is not None and task_writers[project_id] > 0:
                task_writers[project_id] += 1
            else:
                if project_id in self._exclusive:
                    raise ProjectWriteRejected(f"Project {project_id} is being purged")
                project = self._project_lookup(project_id)
                if project is None or project.deleted_at is not None:
                    raise ProjectWriteRejected(f"Project {project_id} is absent or deleted")
                if task_writers is None:
                    task_writers = Counter()
                    self._task_writers[task] = task_writers
                self._writers[project_id] += 1
                task_writers[project_id] = 1
        try:
            yield
        finally:
            if not admitted_by_global:
                async with self._condition:
                    task_writers = self._task_writers[task]
                    task_writers[project_id] -= 1
                    if task_writers[project_id] == 0:
                        del task_writers[project_id]
                        self._writers[project_id] -= 1
                        if self._writers[project_id] == 0:
                            del self._writers[project_id]
                        self._condition.notify_all()
                    if not task_writers:
                        del self._task_writers[task]

    @asynccontextmanager
    async def global_writer(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Global writer admission requires an asyncio task")

        async with self._condition:
            if self._task_global_writers[task] > 0:
                self._task_global_writers[task] += 1
            else:
                if self._exclusive:
                    raise ProjectWriteRejected("A project purge is in progress")
                self._global_writers += 1
                self._task_global_writers[task] = 1
        try:
            yield
        finally:
            async with self._condition:
                self._task_global_writers[task] -= 1
                if self._task_global_writers[task] == 0:
                    del self._task_global_writers[task]
                    self._global_writers -= 1
                    self._condition.notify_all()

    @asynccontextmanager
    async def exclusive(self, project_id: str, *, timeout: float) -> AsyncIterator[None]:
        async with self._condition:
            if project_id in self._exclusive:
                raise ProjectWriteRejected(f"Project {project_id} already has an exclusive owner")
            self._exclusive.add(project_id)
            self._condition.notify_all()
            try:
                await asyncio.wait_for(self._wait_until_drained(project_id), timeout=timeout)
            except TimeoutError as exc:
                self._exclusive.remove(project_id)
                self._condition.notify_all()
                raise ProjectWriteDrainTimeout(
                    f"Timed out draining derived writers for project {project_id}"
                ) from exc
        try:
            yield
        finally:
            async with self._condition:
                self._exclusive.remove(project_id)
                self._condition.notify_all()

    async def _wait_until_drained(self, project_id: str) -> None:
        while self._writers.get(project_id, 0) or self._global_writers:
            await self._condition.wait()
