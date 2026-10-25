"""A landing world: isolated hub rows, a throwaway Git main checkout and the landing seats."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.code_index.context import CodeIndexContext
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.land_commit import LandingResult, land_candidate
from gobby.tasks.step_records import TASK_STEPS
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.review_approvals import (
    approve_review,
    grant_review_writes,
    grant_step_writes,
    reject_review,
)

DEV_MANAGER = "fixture-dev-manager"
ORCHESTRATOR = "orchestrator"
SECURITY_REVIEWER = "security-reviewer"


def bind_task_manager(
    db: HubDatabase, *, manager: str, parent: str, project_id: str, task: Task
) -> None:
    """Run ``manager`` as a Dev Manager whose active agent run is bound to ``task``."""
    grant_step_writes(db, manager, project_id, DEV_MANAGER, ("closing",))
    LocalAgentRunManager(db).create(
        parent_session_id=parent,
        provider="claude",
        prompt="Land the reviewed task.",
        agent_name=DEV_MANAGER,
        child_session_id=manager,
        task_id=task.id,
    )


def grant_orchestrator(db: HubDatabase, session_id: str, project_id: str) -> None:
    """Run a registered root session as the project's Orchestrator, which writes every step."""
    grant_step_writes(db, session_id, project_id, ORCHESTRATOR, TASK_STEPS)


@dataclass
class LandingCase:
    db: HubDatabase
    repo: Path
    project_id: str
    creator: str
    claimant: str
    reviewer: str
    delegator: str
    base: str
    managers: dict[str, str] = field(default_factory=dict)
    orchestrator_session: str | None = None

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                **(env or {}),
            },
        )
        return result.stdout.strip()

    def candidate(self, branch: str, files: dict[str, str], base: str | None = None) -> str:
        self.git("switch", "-c", branch, base or self.base)
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", "--all")
        self.git("commit", "-m", "test candidate")
        sha = self.git("rev-parse", "HEAD")
        self.git("switch", "trunk")
        return sha

    def session(self, title: str, *, parent: str | None = None) -> str:
        """Register a local session in the project; ``parent`` None makes it a root."""
        session_id = str(uuid4())
        # The next free number, so sessions registered through SessionManager never collide.
        self.db.execute(
            """
            INSERT INTO sessions (id, external_id, machine_id, source, project_id, title,
                                  status, agent_depth, seq_num, parent_session_id)
            SELECT %s, %s, %s, 'test', %s, %s, 'active', %s,
                   COALESCE(MAX(seq_num), 699) + 1, %s
            FROM sessions WHERE project_id = %s
            """,
            (
                session_id,
                f"ext-{session_id}",
                require_machine_id(),
                self.project_id,
                title,
                0 if parent is None else 1,
                parent,
                self.project_id,
            ),
        )
        return session_id

    def task(self, title: str = "Reviewed candidate", labels: list[str] | None = None) -> Task:
        task = LocalTaskManager(self.db).create_task(
            self.project_id,
            title,
            created_in_session_id=self.creator,
            claimed_by_session_id=self.claimant,
            labels=labels,
            validation_criteria="Land the reviewed SHA.",
        )
        self.db.execute(
            "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s", (self.delegator, task.id)
        )
        return LocalTaskManager(self.db).get_task(task.id)

    def link(self, task: Task, sha: str) -> None:
        self.db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([sha]), task.id))

    def approve(self, task: Task, sha: str, author: str | None = None) -> None:
        reviewer = author or self.reviewer
        grant_review_writes(self.db, reviewer, self.project_id)
        approve_review(self.db, task_id=task.id, reviewer_session_id=reviewer, commit_sha=sha)

    def reject(self, task: Task, sha: str) -> None:
        grant_review_writes(self.db, self.reviewer, self.project_id)
        reject_review(self.db, task_id=task.id, reviewer_session_id=self.reviewer, commit_sha=sha)

    def security_seat(self) -> str:
        """A session running as the installed security-reviewer definition, with no agent run."""
        session = self.session("security-reviewer", parent=self.creator)
        grant_step_writes(self.db, session, self.project_id, SECURITY_REVIEWER, ("review",))
        return session

    def security_reviewer(self) -> str:
        """A running Security Reviewer seat: its definition plus a running agent run."""
        session = self.security_seat()
        runs = LocalAgentRunManager(self.db)
        run = runs.create(
            parent_session_id=self.creator,
            provider="claude",
            prompt="Review the candidate for security.",
            agent_name=SECURITY_REVIEWER,
            child_session_id=session,
        )
        runs.start(run.id)
        return session

    def approve_as(self, task: Task, sha: str, session: str) -> None:
        """Record an approval from a session that already runs its own definition."""
        approve_review(self.db, task_id=task.id, reviewer_session_id=session, commit_sha=sha)

    def reviewed(self, sha: str, title: str = "Reviewed candidate") -> Task:
        task = self.task(title)
        self.link(task, sha)
        self.approve(task, sha)
        return task

    def direct(self, files: dict[str, str]) -> str:
        """Commit only ``files`` onto trunk, as a direct commit beside staged work does."""
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", "--", *files)
        self.git("commit", "-m", "direct commit", "--", *files)
        return self.git("rev-parse", "HEAD")

    def manager_for(self, task: Task, session: str | None = None) -> str:
        """Bind a Dev Manager run to ``task``; the default manager is made once per task."""
        if session is None and task.id in self.managers:
            return self.managers[task.id]
        manager = session or self.session("dev-manager", parent=self.creator)
        bind_task_manager(
            self.db, manager=manager, parent=self.creator, project_id=self.project_id, task=task
        )
        if session is None:
            self.managers[task.id] = manager
        return manager

    def orchestrator(self) -> str:
        """The project's root Orchestrator session, which writes every task step."""
        if self.orchestrator_session is None:
            self.orchestrator_session = self.session("orchestrator")
            grant_orchestrator(self.db, self.orchestrator_session, self.project_id)
        return self.orchestrator_session

    def lend_hub(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
        """Serve this hub to code that opens the runtime hub; record each open's options."""
        opened: list[dict[str, object]] = []

        @contextmanager
        def lent(*_args: object, **options: object) -> Iterator[HubDatabase]:
            opened.append(options)
            yield self.db

        monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", lent)
        return opened

    async def land(
        self,
        task: Task,
        sha: str,
        caller: str | None = None,
        *,
        code_index: CodeIndexContext | None = None,
    ) -> LandingResult:
        session = caller or self.manager_for(task)
        with session_context_for_test(session):
            return await land_candidate(
                self.db,
                task=task,
                caller_session_id=session,
                commit_sha=sha,
                code_index=code_index,
            )


def make_landing_case(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> LandingCase:
    repo = tmp_path / "main"
    repo.mkdir()
    fixture = LandingCase(temp_db, repo, sample_project["id"], "", "", "", "", "")
    fixture.creator, fixture.claimant, fixture.reviewer, fixture.delegator = (
        fixture.session(role) for role in ("creator", "claimant", "reviewer", "delegator")
    )
    fixture.git("init", "--initial-branch=trunk")
    fixture.git("config", "user.name", "Landing test")
    fixture.git("config", "user.email", "landing@example.invalid")
    fixture.git("config", "core.hooksPath", str(tmp_path / "empty-hooks"))
    (repo / "README.md").write_text("base\n")
    fixture.git("add", "README.md")
    fixture.git("commit", "-m", "initial")
    fixture.base = fixture.git("rev-parse", "HEAD")
    LocalProjectCheckoutManager(temp_db).rebind(require_machine_id(), fixture.project_id, str(repo))
    return fixture
