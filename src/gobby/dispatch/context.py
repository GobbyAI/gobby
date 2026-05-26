"""Context and candidate reload helpers for dispatcher heartbeat evaluation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

from gobby.dispatch import rules as dispatch_rules
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._stage_hydration import hydrate_task_stage_state
from gobby.storage.tasks._stage_registry import StageRegistryEntry, StageRegistryManager
from gobby.storage.tasks._stage_types import StageState
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager, WorkflowDefinitionRow
from gobby.workflows.definitions import AgentDefinitionBody


def reload_candidate(
    task_id: str,
    *,
    db: HubDatabase | None = None,
    project_id: str | None = None,
) -> Task | None:
    if db is None:
        return None
    where_clause, params = _candidate_lookup_clause(task_id, project_id)
    if where_clause is None:
        return None
    rows = db.fetchall(
        f"""
        SELECT
            t.*,
            s.task_id AS stage_task_id,
            s.stage_name AS stage_name,
            s.position AS stage_position,
            s.state AS stage_state,
            s.review_policy AS stage_review_policy,
            s.reviewer_agent AS stage_reviewer_agent,
            s.entered_at AS stage_entered_at,
            s.entered_by_session_id AS stage_entered_by_session_id,
            s.completed_at AS stage_completed_at,
            s.completed_by_session_id AS stage_completed_by_session_id,
            s.completed_commit_sha AS stage_completed_commit_sha,
            s.work_attempt_count AS stage_work_attempt_count,
            s.review_round_count AS stage_review_round_count,
            s.max_work_attempts AS stage_max_work_attempts,
            s.max_review_rounds AS stage_max_review_rounds,
            s.artifact_refs AS stage_artifact_refs,
            s.notes AS stage_notes,
            s.updated_at AS stage_updated_at
        FROM tasks t
        LEFT JOIN task_stage_states s ON s.task_id = t.id
        WHERE {where_clause}
        ORDER BY s.position, s.stage_name
        """,  # nosec B608 # where_clause is selected from fixed templates.
        tuple(params),
    )
    if not rows:
        return None
    task = Task.from_row(rows[0])
    task.stages = tuple(_stage_from_joined_row(row) for row in rows if row["stage_task_id"])
    hydrate_task_blocking_state(db, [task])
    return task


def build_context(
    db: HubDatabase,
    task: Task,
    *,
    services: object | None = None,
) -> object:
    artifacts = TaskArtifactManager(db).get_artifacts(task.id)
    children = _children(db, task.id)
    build_config = getattr(services, "config", None) if services is not None else None
    stage_registry = _stage_registry(db)
    agent_definitions = _agent_definitions(db, project_id=task.project_id)
    return SimpleNamespace(
        agent_definitions=agent_definitions,
        agents=agent_definitions,
        artifacts=artifacts,
        children=children,
        build_config=build_config,
        current_stage=dispatch_rules.current_stage(task),
        db=db,
        failure_context=_latest_failure_context(db, task.id),
        project_id=task.project_id,
        services=services,
        stage_registry=stage_registry,
        task=task,
    )


def _candidate_lookup_clause(
    task_id: str,
    project_id: str | None,
) -> tuple[str | None, list[object]]:
    if task_id.startswith("#") or task_id.isdigit():
        if project_id is None:
            return None, []
        try:
            seq_num = int(task_id[1:] if task_id.startswith("#") else task_id)
        except ValueError:
            return None, []
        return "t.project_id = ? AND t.seq_num = ?", [project_id, seq_num]

    if "." in task_id and all(part.isdigit() for part in task_id.split(".")):
        if project_id is None:
            return None, []
        return "t.project_id = ? AND t.path_cache = ?", [project_id, task_id]

    params: list[object] = [task_id]
    clause = "t.id = ?"
    if project_id is not None:
        clause += " AND t.project_id = ?"
        params.append(project_id)
    return clause, params


def _stage_from_joined_row(row: Any) -> StageState:
    return StageState(
        task_id=row["stage_task_id"],
        stage_name=row["stage_name"],
        position=int(row["stage_position"]),
        state=row["stage_state"],
        review_policy=row["stage_review_policy"],
        reviewer_agent=row["stage_reviewer_agent"],
        entered_at=row["stage_entered_at"],
        entered_by_session_id=row["stage_entered_by_session_id"],
        completed_at=row["stage_completed_at"],
        completed_by_session_id=row["stage_completed_by_session_id"],
        completed_commit_sha=row["stage_completed_commit_sha"],
        work_attempt_count=int(row["stage_work_attempt_count"]),
        review_round_count=int(row["stage_review_round_count"]),
        max_work_attempts=row["stage_max_work_attempts"],
        max_review_rounds=row["stage_max_review_rounds"],
        artifact_refs=_artifact_refs(row["stage_artifact_refs"]),
        notes=row["stage_notes"],
        updated_at=row["stage_updated_at"],
    )


def _artifact_refs(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        return None
    return {str(key): str(item) for key, item in decoded.items()}


def _latest_failure_context(db: HubDatabase, task_id: str) -> str | None:
    row = db.fetchone(
        """
        SELECT body
          FROM task_comments
         WHERE task_id = ?
           AND author_type = 'system'
           AND (
               body LIKE '## Holistic QA Failure%'
               OR body LIKE '## Holistic QA Follow-Up%'
           )
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (task_id,),
    )
    if row is None:
        return None
    body = row["body"]
    return body if isinstance(body, str) and body else None


def _children(db: HubDatabase, task_id: str) -> list[Task]:
    rows = db.fetchall("SELECT * FROM tasks WHERE parent_task_id = ?", (task_id,))
    children = [Task.from_row(row) for row in rows]
    hydrate_task_stage_state(db, children)
    hydrate_task_blocking_state(db, children)
    return children


def _stage_registry(db: HubDatabase) -> dict[str, StageRegistryEntry]:
    return {entry.name: entry for entry in StageRegistryManager(db).list_all()}


def _agent_definitions(
    db: HubDatabase,
    *,
    project_id: str | None,
) -> dict[str, SimpleNamespace]:
    manager = LocalWorkflowDefinitionManager(db)
    if project_id is None:
        rows = [
            row
            for row in manager.list_all(workflow_type="agent", include_deleted=False)
            if row.project_id is None
        ]
    else:
        rows = manager.list_all(
            project_id=project_id,
            workflow_type="agent",
            include_deleted=False,
        )
    definitions: dict[str, SimpleNamespace] = {}
    for row in sorted(rows, key=_agent_definition_precedence):
        definitions[row.name] = _agent_definition_view(row)
    return definitions


def _agent_definition_precedence(row: WorkflowDefinitionRow) -> tuple[int, str]:
    return (0 if row.project_id is None else 1, row.name)


def _agent_definition_view(row: WorkflowDefinitionRow) -> SimpleNamespace:
    try:
        body = AgentDefinitionBody.model_validate_json(row.definition_json)
    except ValueError as exc:
        return SimpleNamespace(
            name=row.name,
            enabled=False,
            row_enabled=row.enabled,
            parse_error=str(exc),
            source=row.source,
            project_id=row.project_id,
        )

    spawn_capable = "spawn" in body.surfaces
    enabled = bool(row.enabled and body.enabled and spawn_capable and not body.deprecated)
    return SimpleNamespace(
        name=row.name,
        enabled=enabled,
        row_enabled=row.enabled,
        body_enabled=body.enabled,
        deprecated=body.deprecated,
        surfaces=tuple(body.surfaces),
        spawn_capable=spawn_capable,
        source=row.source,
        project_id=row.project_id,
        definition=body,
    )


def _field(
    obj: object | None,
    name: str,
    default: object | None = None,
) -> object | None:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return cast(Mapping[str, object | None], obj).get(name, default)
    return getattr(obj, name, default)
