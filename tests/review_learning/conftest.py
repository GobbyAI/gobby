from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class FakeMemory:
    id: str
    content: str
    memory_type: str = "pattern"
    project_id: str | None = "project"
    source_session_id: str | None = None
    tags: list[str] | None = None


@dataclass
class FakeTask:
    id: str
    title: str
    description: str
    labels: list[str]
    category: str
    validation_criteria: str
    seq_num: int | None = None
    closed_at: str | None = None


class FakeDB:
    def __init__(self, session_id: str | None = None, project_id: str = "session-project"):
        self.session_id = session_id
        self.project_id = project_id

    def fetchone(self, sql: str, params: tuple[Any, ...]) -> dict[str, str] | None:
        if "SELECT id FROM sessions WHERE id" in sql and self.session_id:
            return {"id": self.session_id}
        if "SELECT project_id FROM sessions WHERE id" in sql and self.session_id:
            return {"project_id": self.project_id}
        return None

    def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, str]]:
        return []


class FakeMemoryManager:
    def __init__(self, db: FakeDB | None = None):
        self.db = db or FakeDB()
        self.memories: list[FakeMemory] = []
        self.search_results: list[FakeMemory] = []
        self.search_queries: list[dict[str, Any]] = []
        self.raise_on_search = False

    async def create_memory(
        self,
        content: str,
        memory_type: str = "pattern",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> FakeMemory:
        memory = FakeMemory(
            id=f"mem-{len(self.memories) + 1}",
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            source_session_id=source_session_id,
            tags=tags or [],
        )
        self.memories.append(memory)
        return memory

    def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> list[FakeMemory]:
        memories = self.memories
        if project_id is not None:
            memories = [memory for memory in memories if memory.project_id == project_id]
        if memory_type is not None:
            memories = [memory for memory in memories if memory.memory_type == memory_type]
        if tags_all:
            memories = [
                memory for memory in memories if set(tags_all).issubset(set(memory.tags or []))
            ]
        return memories[offset : offset + limit]

    async def search_memories(
        self,
        query: str | None = None,
        project_id: str | None = None,
        limit: int = 10,
        tags_all: list[str] | None = None,
        **kwargs: Any,
    ) -> list[FakeMemory]:
        self.search_queries.append(
            {"query": query, "project_id": project_id, "tags_all": tags_all, "limit": limit}
        )
        if self.raise_on_search:
            raise RuntimeError("search failed")
        results = self.search_results or self.memories
        if tags_all:
            results = [
                memory for memory in results if set(tags_all).issubset(set(memory.tags or []))
            ]
        return results[:limit]


class FakeTaskManager:
    def __init__(self):
        self.tasks: list[FakeTask] = []
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def list_tasks(
        self,
        project_id: str | None = None,
        closed: bool | None = None,
        label: str | None = None,
        limit: int = 50,
        **kwargs: Any,
    ) -> list[FakeTask]:
        tasks = self.tasks
        if label is not None:
            tasks = [task for task in tasks if label in task.labels]
        if closed is False:
            tasks = [task for task in tasks if task.closed_at is None]
        return tasks[:limit]

    def create_task(self, **kwargs: Any) -> FakeTask:
        self.created.append(kwargs)
        task = FakeTask(
            id=f"task-{len(self.tasks) + 1}",
            seq_num=len(self.tasks) + 1,
            title=str(kwargs["title"]),
            description=str(kwargs["description"]),
            labels=list(kwargs["labels"]),
            category=str(kwargs["category"]),
            validation_criteria=str(kwargs["validation_criteria"]),
        )
        self.tasks.append(task)
        return task

    def update_task(self, task_id: str, **kwargs: Any) -> FakeTask:
        self.updated.append({"task_id": task_id, **kwargs})
        task = next(task for task in self.tasks if task.id == task_id)
        for key, value in kwargs.items():
            if hasattr(task, key) and value is not None:
                setattr(task, key, value)
        return task


@pytest.fixture
def fake_memory_manager() -> FakeMemoryManager:
    return FakeMemoryManager()


@pytest.fixture
def fake_task_manager() -> FakeTaskManager:
    return FakeTaskManager()
