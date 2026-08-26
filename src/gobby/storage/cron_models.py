"""Cron job and run dataclasses for the scheduler system."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from gobby.utils.datetime import normalize_datetime_model
from gobby.utils.machine_id import require_machine_id

logger = logging.getLogger(__name__)

CronRunStatus = Literal[
    "pending", "running", "completed", "failed", "skipped", "dispatched", "interrupted"
]
CronRunChildType = Literal["agent_run", "pipeline_execution"]


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=(
        "run_at",
        "next_run_at",
        "last_run_at",
    ),
)
@dataclass
class CronJob:
    """A scheduled recurring job."""

    id: str
    project_id: str
    name: str
    schedule_type: Literal["cron", "interval", "once"]
    action_type: Literal["agent_spawn", "pipeline", "shell", "handler", "dispatcher"]
    action_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    display_name: str | None = None
    description: str | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = None
    run_at: datetime | None = None
    timezone: str = "UTC"
    enabled: bool = True
    is_system: bool = False
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    consecutive_failures: int = 0

    @property
    def restart_protected(self) -> bool:
        """Whether an active run of this job holds the daemon restart lease.

        Declared in the job definition's ``action_config`` so bundled system
        jobs (memory dream) carry it without a schema column; ``gobby stop``
        and ``gobby restart`` refuse while such a run is active.
        """
        return bool(self.action_config.get("restart_protected", False))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CronJob:
        """Convert database row to CronJob object."""
        keys = set(row.keys())
        action_config_raw = row["action_config"]
        try:
            action_config = json.loads(action_config_raw) if action_config_raw else {}
        except json.JSONDecodeError:
            logger.warning("Bad JSON in action_config for job %s: %r", row["id"], action_config_raw)
            action_config = {}

        return cls(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            schedule_type=row["schedule_type"],
            action_type=row["action_type"],
            action_config=action_config,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            display_name=row["display_name"] if "display_name" in keys else None,
            description=row["description"] if "description" in keys else None,
            cron_expr=row["cron_expr"] if "cron_expr" in keys else None,
            interval_seconds=row["interval_seconds"] if "interval_seconds" in keys else None,
            run_at=row["run_at"] if "run_at" in keys else None,
            timezone=row["timezone"] if "timezone" in keys and row["timezone"] else "UTC",
            enabled=bool(row["enabled"]) if "enabled" in keys else True,
            is_system=bool(row["is_system"]) if "is_system" in keys else False,
            next_run_at=row["next_run_at"] if "next_run_at" in keys else None,
            last_run_at=row["last_run_at"] if "last_run_at" in keys else None,
            last_status=row["last_status"] if "last_status" in keys else None,
            consecutive_failures=(
                row["consecutive_failures"] if "consecutive_failures" in keys else 0
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert CronJob to full dictionary."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "schedule_type": self.schedule_type,
            "cron_expr": self.cron_expr,
            "interval_seconds": self.interval_seconds,
            "run_at": self.run_at,
            "timezone": self.timezone,
            "action_type": self.action_type,
            "action_config": self.action_config,
            "enabled": self.enabled,
            "is_system": self.is_system,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "consecutive_failures": self.consecutive_failures,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_brief(self) -> dict[str, Any]:
        """Convert CronJob to brief format for list operations."""
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "schedule_type": self.schedule_type,
            "cron_expr": self.cron_expr,
            "interval_seconds": self.interval_seconds,
            "action_type": self.action_type,
            "enabled": self.enabled,
            "is_system": self.is_system,
            "next_run_at": self.next_run_at,
            "last_status": self.last_status,
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass
class CronRunChild:
    """Child work launched by a cron run."""

    type: CronRunChildType
    id: str
    status: str | None
    terminal: bool
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert child projection to dictionary."""
        return {
            "type": self.type,
            "id": self.id,
            "status": self.status,
            "terminal": self.terminal,
            "missing": self.missing,
        }


@normalize_datetime_model(
    required=(
        "triggered_at",
        "created_at",
    ),
    optional=(
        "started_at",
        "completed_at",
    ),
)
@dataclass
class CronRun:
    """A single execution of a cron job."""

    id: str
    cron_job_id: str
    machine_id: str = field(default_factory=require_machine_id, kw_only=True)
    triggered_at: datetime
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: CronRunStatus = "pending"
    output: str | None = None
    error: str | None = None
    agent_run_id: str | None = None
    pipeline_execution_id: str | None = None
    scheduler_owner: str | None = None
    child: CronRunChild | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CronRun:
        """Convert database row to CronRun object."""
        keys = set(row.keys())
        return cls(
            id=row["id"],
            cron_job_id=row["cron_job_id"],
            machine_id=str(row["machine_id"]),
            triggered_at=row["triggered_at"],
            created_at=row["created_at"],
            started_at=row["started_at"] if "started_at" in keys else None,
            completed_at=row["completed_at"] if "completed_at" in keys else None,
            status=row["status"] if "status" in keys and row["status"] else "pending",
            output=row["output"] if "output" in keys else None,
            error=row["error"] if "error" in keys else None,
            agent_run_id=row["agent_run_id"] if "agent_run_id" in keys else None,
            pipeline_execution_id=(
                row["pipeline_execution_id"] if "pipeline_execution_id" in keys else None
            ),
            scheduler_owner=row["scheduler_owner"] if "scheduler_owner" in keys else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert CronRun to dictionary."""
        return {
            "id": self.id,
            "cron_job_id": self.cron_job_id,
            "machine_id": self.machine_id,
            "triggered_at": self.triggered_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "agent_run_id": self.agent_run_id,
            "pipeline_execution_id": self.pipeline_execution_id,
            "scheduler_owner": self.scheduler_owner,
            "child": self.child.to_dict() if self.child else None,
            "created_at": self.created_at,
        }

    def to_brief(self) -> dict[str, Any]:
        """Slim representation for list operations."""
        return {
            "id": self.id,
            "cron_job_id": self.cron_job_id,
            "machine_id": self.machine_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "agent_run_id": self.agent_run_id,
            "pipeline_execution_id": self.pipeline_execution_id,
            "scheduler_owner": self.scheduler_owner,
            "child": self.child.to_dict() if self.child else None,
        }
