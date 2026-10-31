"""Record and announce an implementation link that landed before its test step.

A Developer's await gate keeps it from linking production work until the Test Writer's
test step is approved for the current epoch. When a link gets through anyway, the link
still succeeds, but the task keeps durable evidence (who linked what, and which gate did
not hold), and the linking seat's agent-run parent and the team's report_to are told at
once. Nothing is refused and nothing is recovered silently.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

import psycopg

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.tasks import Task
from gobby.storage.team_records import ADMISSION_STEP
from gobby.tasks.step_records import approved_task_step, list_task_step_records

INCIDENT_AUTHOR_TYPE = "link_incident"

logger = logging.getLogger(__name__)


def has_approved_test_step(db: HubDatabase, task: Task) -> bool:
    """True when the task's test step is approved for its current epoch and evidence.

    This is the same projection the close gate reads, so the alert and the gate agree.
    """
    return approved_task_step(task, list_task_step_records(db, task.id), "test") is not None


def _team_for_task(db: HubDatabase, task_id: str) -> str | None:
    """The team admitted for the task that has not been torn down, or None.

    Liveness in ``live_teams`` also needs an active worktree, which a team admitted without a
    tree lacks. The alert needs only the admission record, so this reads that record directly.
    """
    row = db.fetchone(
        """
        SELECT output_json #>> '{team,team_id}' AS team_id
        FROM step_executions
        WHERE step_id = %s
          AND output_json #>> '{team,team_id}' = execution_id::text
          AND output_json #>> '{team,task_id}' = %s
          AND output_json #>> '{teardown,outcome}' IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (ADMISSION_STEP, task_id),
    )
    return str(row["team_id"]) if row else None


def _team_report_to(db: HubDatabase, team_id: str) -> str | None:
    """The report_to ref a team was launched with, from its pipeline execution inputs."""
    execution = LocalPipelineExecutionManager(db, None).get_execution(team_id)
    if execution is None or not execution.inputs_json:
        return None
    report_to = json.loads(execution.inputs_json).get("report_to")
    return report_to if isinstance(report_to, str) and report_to.strip() else None


def record_out_of_order_link(
    db: HubDatabase,
    task: Task,
    *,
    commit_sha: str,
    session_id: str | None,
) -> None:
    """Write the durable incident row, then alert the parent and the team's report_to.

    Without a caller session there is no agent run to resolve and no sender for an alert,
    so the row still records the link and states that the alerts were skipped.
    """
    run = LocalAgentRunManager(db).get_by_session(session_id) if session_id else None
    seat = run.agent_name if run else None
    parent = run.parent_session_id if run else None
    team_id = _team_for_task(db, task.id)
    report_to = _team_report_to(db, team_id) if team_id else None
    recipients = [ref for ref in dict.fromkeys([parent, report_to]) if ref]
    delivered: list[str] = []
    failed: list[str] = []
    if session_id is not None and recipients:
        content = (
            f"EVENT=OUT_OF_ORDER_LINK TASK={task.id} SESSION={session_id} "
            f"SEAT={seat or 'unknown'} COMMIT={commit_sha} GATE=test "
            f"NOTE=implementation linked before the test step was approved for epoch "
            f"{task.review_epoch}; the task records it. Check the test step before the candidate."
        )
        messages = InterSessionMessageManager(db)
        for recipient in recipients:
            try:
                messages.create_message(
                    from_session=session_id,
                    to_session=recipient,
                    content=content,
                    metadata_json=json.dumps({"task_id": task.id, "commit_sha": commit_sha}),
                )
            except psycopg.IntegrityError:
                # An unresolvable ref fails the session foreign key; the link already stands,
                # so the durable row names the recipient that was not reached.
                logger.warning(
                    "out-of-order link alert for task %s not delivered to %s",
                    task.id,
                    recipient,
                    exc_info=True,
                )
                failed.append(recipient)
            else:
                delivered.append(recipient)
    skipped = None if session_id is not None and recipients else "no caller session or recipient"
    body = json.dumps(
        {
            "event": "out_of_order_link",
            "failed_gate": "test",
            "task_id": task.id,
            "session_id": session_id,
            "seat": seat,
            "commit_sha": commit_sha,
            "review_epoch": task.review_epoch,
            "reason": "implementation linked before the test step was approved for this epoch",
            "alerted": delivered,
            "alert_failed": failed,
            "alert_skipped": skipped,
        },
        sort_keys=True,
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(uuid4()), task.id, session_id or "unknown", INCIDENT_AUTHOR_TYPE, body),
        )
