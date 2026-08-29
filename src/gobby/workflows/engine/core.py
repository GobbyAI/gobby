"""Core rule engine with single-pass evaluation loop.

Rules are stateless event handlers: event comes in, conditions match, effect fires.
Effect types: block, set_variable, inject_context, mcp_call, observe,
rewrite_input, load_skill.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.metrics_events import MetricsEventStore

from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.runtime_models import ConfigSnapshot
from gobby.config.values import ConfigRuntimeReader
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.normalization import normalize_tool_fields
from gobby.skills.materialization import (
    SkillScriptMaterializer,
    get_skill_script_materializer,
)
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.revisions import get_definitions_revision
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import (
    CompletionSubscriberManager,
    PipelineSubscriberStorageError,
)
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.telemetry.tracing import create_span
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    RuleDefinitionBody,
    RuleTriggerEvent,
)
from gobby.workflows.engine._offload import offload
from gobby.workflows.engine.blocked_tool_recovery import (
    clear_blocked_tool_recovery_state,
    format_consecutive_tool_block_reason,
    is_blocked_tool_recovery_remediation,
)
from gobby.workflows.engine.effects import EffectsMixin
from gobby.workflows.engine.enforcement import EnforcementMixin
from gobby.workflows.engine.evaluation import EvaluationContext, EvaluationMixin
from gobby.workflows.engine.event_utils import (
    _COMPACT_TURN_END_BYPASS_PENDING,
    _block_tool_name,
    _clear_edit_write_state,
    _event_value,
    _get_tool_identity,
    _is_manual_compact_event,
    _is_pipeline_direct_mcp_event,
    _is_turn_end_event,
    _is_turn_start_event,
    _is_write_like_event_data,
    _project_id_from_event,
    _resolve_rule_events,
)
from gobby.workflows.engine.proxy_hooks import ProxyHooksMixin
from gobby.workflows.engine.templating import TemplatingMixin
from gobby.workflows.selectors import rule_matches_agent
from gobby.workflows.session_feedback_survey import inject_survey_active
from gobby.workflows.step_instances import AgentStepInstanceManager

logger = logging.getLogger(__name__)

_DEFAULT_RULE_CONFIG_SNAPSHOT = ConfigSnapshot(
    revision=0,
    desired=DaemonConfig(),
    active=DaemonConfig(),
    row_revisions={},
    pending_restart_keys=frozenset(),
    failed_live_keys={},
    desired_values={
        "rules.enforcement_enabled": True,
        "rules.aggregate_blocks": True,
    },
    active_values={
        "rules.enforcement_enabled": True,
        "rules.aggregate_blocks": True,
    },
)

__all__ = [
    "RuleEngine",
    "_COMPACT_TURN_END_BYPASS_PENDING",
    "_block_tool_name",
    "_clear_edit_write_state",
    "_event_value",
    "_get_tool_identity",
    "_is_manual_compact_event",
    "_is_pipeline_direct_mcp_event",
    "_is_turn_end_event",
    "_is_turn_start_event",
    "_is_write_like_event_data",
    "_project_id_from_event",
    "_resolve_rule_events",
]


class RuleEngine(
    EvaluationMixin,
    EffectsMixin,
    ProxyHooksMixin,
    TemplatingMixin,
    EnforcementMixin,
):
    """Single-pass rule evaluation engine.

    Loads rules from rule_definitions via RuleDefinitionManager,
    applies session overrides, evaluates in priority order.
    """

    def __init__(
        self,
        db: HubDatabase,
        skill_manager: Any | None = None,
        metrics_event_store: "MetricsEventStore | None" = None,
        mcp_dispatcher: Any | None = None,
        runner: Any | None = None,
        completion_registry: Any | None = None,
        task_manager: Any | None = None,
        config_runtime: ConfigRuntimeReader | None = None,
        skill_script_materializer: SkillScriptMaterializer | None = None,
    ):
        self.db = db
        self.rule_manager = RuleDefinitionManager(db)
        self.agent_manager = AgentDefinitionManager(db)
        self.instance_manager = AgentStepInstanceManager(db)
        self.workflow_audit = WorkflowAuditManager(db)
        self._skill_manager = skill_manager
        self._event_store = metrics_event_store
        self._mcp_dispatcher = mcp_dispatcher
        self._runner = runner
        self._completion_registry = completion_registry
        self._task_manager = task_manager
        self._config_runtime = config_runtime
        self.skill_script_materializer = skill_script_materializer or get_skill_script_materializer(
            db
        )
        self._agent_def_cache_revision = get_definitions_revision("agents")
        self._agent_def_cache: dict[tuple[str, str | None], AgentDefinitionBody | None] = {}
        self._background_run_commands: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def prewarm_skill_scripts(self, *, project_id: str | None) -> None:
        """Prepare skill scripts referenced by enabled rules."""
        try:
            rows = await offload(
                self.rule_manager.list_all,
                project_id=project_id,
                enabled=True,
            )
        except Exception:
            logger.warning(
                "Workflow skill prewarm could not load enabled rules for project %s",
                project_id,
                exc_info=True,
            )
            return

        skills: set[str] = set()
        for row in rows:
            try:
                body = RuleDefinitionBody.model_validate(row.definition_json)
            except Exception:
                logger.warning(
                    "Workflow skill prewarm skipped invalid rule %s for project %s",
                    row.name,
                    project_id,
                    exc_info=True,
                )
                continue
            skills.update(
                effect.skill
                for effect in body.resolved_effects
                if effect.type == "run_command"
                and effect.skill is not None
                and effect.script is not None
            )
        ordered_skills = sorted(skills)
        results = await asyncio.gather(
            *(
                self.skill_script_materializer.resolve(skill, project_id=project_id)
                for skill in ordered_skills
            ),
            return_exceptions=True,
        )
        for skill, result in zip(ordered_skills, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Workflow skill prewarm failed for skill %s in project %s",
                    skill,
                    project_id,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def evaluate(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        """Evaluate all matching rules for an event.

        Args:
            event: The hook event to evaluate.
            session_id: Current session ID.
            variables: Session variables dict (mutated in-place by set_variable).
            eval_context: Additional eval context (LazyBool thunks, etc).
            blocking_deadline: Shared aggregate deadline for inline blocking effects.

        Returns:
            HookResponse with merged results from all matching rules.
        """
        with create_span(
            "rules.evaluate",
            attributes={"event_type": str(event.event_type), "session_id": session_id},
        ) as span:
            try:
                if isinstance(event.data, dict):
                    if event.cwd and "cwd" not in event.data:
                        event.data["cwd"] = event.cwd
                    project_path = event.metadata.get("project_path")
                    if (
                        isinstance(project_path, str)
                        and project_path
                        and "project_path" not in event.data
                    ):
                        event.data["project_path"] = project_path
                    normalize_tool_fields(event.data)

                raw_event_value = _event_value(event.event_type)
                resolved_rule_events = _resolve_rule_events(event.event_type)
                if RuleTriggerEvent.TURN_START in resolved_rule_events:
                    if variables.get(_COMPACT_TURN_END_BYPASS_PENDING):
                        variables[_COMPACT_TURN_END_BYPASS_PENDING] = False
                elif RuleTriggerEvent.TURN_END in resolved_rule_events and variables.get(
                    _COMPACT_TURN_END_BYPASS_PENDING, False
                ):
                    variables[_COMPACT_TURN_END_BYPASS_PENDING] = False
                    resolved_rule_events = [
                        trigger
                        for trigger in resolved_rule_events
                        if trigger != RuleTriggerEvent.TURN_END
                    ]

                if _is_manual_compact_event(event):
                    variables[_COMPACT_TURN_END_BYPASS_PENDING] = True

                if not resolved_rule_events:
                    return HookResponse(decision="allow")

                project_from_vars = variables.get("project")
                if not (isinstance(project_from_vars, dict) and project_from_vars.get("path")):
                    variables["project"] = await offload(
                        self._resolve_project_info,
                        event,
                        project_from_vars,
                    )

                is_before_tool = raw_event_value == HookEventType.BEFORE_TOOL.value
                is_after_tool = raw_event_value == HookEventType.AFTER_TOOL.value
                is_turn_start = RuleTriggerEvent.TURN_START in resolved_rule_events
                is_turn_end = RuleTriggerEvent.TURN_END in resolved_rule_events

                config_snapshot = (
                    self._config_runtime.snapshot
                    if self._config_runtime is not None
                    else _DEFAULT_RULE_CONFIG_SNAPSHOT
                )
                config_values = config_snapshot.active_values
                if config_values.get("rules.enforcement_enabled", True) is False:
                    return HookResponse(decision="allow")
                inject_survey_active(variables, config_values)
                aggregate_blocks = config_values.get("rules.aggregate_blocks", True) is not False

                if eval_context is None:
                    eval_context = {}
                eval_context.setdefault("foreign_dirty_edit_conflict", "")
                eval_context.setdefault("foreign_staged_commit_conflict", "")
                eval_context.setdefault("found_work_shirk", False)
                eval_context.setdefault("found_work_shirk_confirmed", False)
                eval_context.setdefault("terminal_validation_failure", False)
                eval_context.setdefault("terminal_validation_failure_commands", [])
                eval_context.setdefault("_blocking_deadline", blocking_deadline)

                active_agent_wait = False
                if is_turn_end:
                    try:
                        active_agent_wait = await offload(
                            CompletionSubscriberManager(self.db).has_active_agent_wait,
                            session_id,
                        )
                    except PipelineSubscriberStorageError as exc:
                        logger.warning(
                            "Failed to determine active agent wait for session %s: %s",
                            session_id,
                            exc,
                        )
                eval_context["_has_active_agent_wait"] = active_agent_wait

                # Collect mcp_call effects from hardcoded rules and DB rules.
                # Initialized early so hardcoded turn-start rules can append.
                mcp_calls: list[dict[str, Any]] = []
                evaluation = EvaluationContext(
                    event=event,
                    session_id=session_id,
                    variables=variables,
                    eval_context=eval_context,
                    is_before_tool=is_before_tool,
                    block_tool_name=_block_tool_name(event),
                    mcp_calls=mcp_calls,
                )

                # Auto-track consecutive retries after a blocked BEFORE_TOOL.
                # _last_blocked_tool is only set by pre-execution gate/enforcement blocks.
                # tool_block_pending is reserved for real tool execution failures.
                if is_before_tool and variables.get("_last_blocked_tool"):
                    if _is_pipeline_direct_mcp_event(event):
                        # Synthetic pipeline MCP events clear block state so the next real user tool starts from 0.
                        variables["consecutive_tool_blocks"] = 0
                        clear_blocked_tool_recovery_state(variables)
                    else:
                        tool_name = _get_tool_identity(event.data)
                        last_blocked = variables.get("_last_blocked_tool", "")
                        if tool_name == last_blocked:
                            if is_blocked_tool_recovery_remediation(variables, event.data):
                                variables["consecutive_tool_blocks"] = 0
                                clear_blocked_tool_recovery_state(variables)
                            else:
                                count = variables.get("consecutive_tool_blocks", 0) + 1
                                variables["consecutive_tool_blocks"] = count
                                max_attempts = int(
                                    variables.get("max_consecutive_blocked_tool_attempts", 5)
                                )
                                total_attempts = count + 1
                                if total_attempts >= max_attempts:
                                    resp = HookResponse(
                                        decision="block",
                                        reason=format_consecutive_tool_block_reason(
                                            tool_name=tool_name,
                                            total_attempts=total_attempts,
                                            variables=variables,
                                        ),
                                    )
                                    return await self._finalize_block_response(
                                        resp,
                                        evaluation,
                                        span,
                                        source="rule",
                                        rule_name="consecutive-tool-block",
                                    )
                        else:
                            # Different tool — reset counter, let it through to rule evaluation
                            variables["consecutive_tool_blocks"] = 0
                # Track edit/write attempts — set pending on pre-tool
                if is_before_tool:
                    if _is_write_like_event_data(event.data):
                        variables["edit_write_pending"] = True

                elif is_turn_start:
                    variables["consecutive_tool_blocks"] = 0
                    clear_blocked_tool_recovery_state(variables)
                    variables["tool_block_pending"] = False
                    variables["stop_attempts"] = 0
                    variables["_block_reasons_shown"] = []

                    # [auto-discover-servers] — hardcoded, always-on
                    # Seed progressive discovery on first prompt so agents
                    # don't need to call list_mcp_servers() manually.
                    if not variables.get("servers_listed"):
                        mcp_calls.append(
                            {
                                "server": "_proxy",
                                "tool": "list_mcp_servers",
                                "arguments": {"name_filter": "gobby-*"},
                                "inject_result": True,
                            }
                        )
                        variables["servers_listed"] = True

                # Auto-increment ordinary turn-end attempts; active agent waits consume none.
                if is_turn_end:
                    if not active_agent_wait:
                        variables["stop_attempts"] = variables.get("stop_attempts", 0) + 1
                    logger.debug(
                        "TURN_END gate diagnostics",
                        extra={
                            "session_id": session_id,
                            "raw_event": raw_event_value,
                            "auto_task_ref": variables.get("auto_task_ref"),
                            "stop_attempts": variables["stop_attempts"],
                            "task_claimed": variables.get("task_claimed"),
                            "claimed_tasks": variables.get("claimed_tasks"),
                            "edit_write_pending": variables.get("edit_write_pending"),
                            "tool_block_pending": variables.get("tool_block_pending"),
                            "active_agent_wait": active_agent_wait,
                        },
                    )

                # 1. Load enabled rules for this event, sorted by priority
                rules = await offload(
                    self._load_rules,
                    resolved_rule_events,
                    project_id=_project_id_from_event(event),
                )

                # 2. Filter by agent_scope
                agent_type = variables.get("_agent_type")
                rules = await offload(self._filter_by_agent_scope, rules, agent_type)

                # 3. Filter by audience
                rules = await offload(self._filter_by_audience, rules, variables)

                # 4. Filter by active rules (selector-based)
                rules = await offload(
                    self._filter_by_active_rules,
                    rules,
                    variables,
                    project_id=_project_id_from_event(event),
                )

                if span.is_recording():
                    span.set_attribute("rule_count", len(rules))
                    span.set_attribute(
                        "rules.resolved_events",
                        [rule_event.value for rule_event in resolved_rule_events],
                    )

                # 4b. Agent-level tool enforcement (broadest scope, preempts everything)
                if is_before_tool:
                    agent_block = await offload(
                        self._check_agent_tool_enforcement,
                        event,
                        session_id,
                        variables,
                    )
                    if agent_block is not None:
                        variables["_last_blocked_tool"] = _get_tool_identity(event.data)
                        if _is_write_like_event_data(event.data):
                            _clear_edit_write_state(variables)
                        return await self._finalize_block_response(
                            agent_block,
                            evaluation,
                            span,
                            source="step-enforcement",
                            rule_name="agent-tool-enforcement",
                            fallback_reason=(
                                "Rule enforced by Gobby: [agent-tool-enforcement]\n"
                                "Agent-level tool enforcement blocked this tool for the "
                                "current session."
                            ),
                            warn_detail="agent-level enforcement returned an empty reason",
                        )

                # 4c. Step-level tool enforcement (preempts declarative rules)
                if is_before_tool:
                    step_block = await offload(
                        self._check_step_tool_enforcement,
                        event,
                        session_id,
                        variables,
                    )
                    if step_block is not None:
                        variables["_last_blocked_tool"] = _get_tool_identity(event.data)
                        # Blocked edit/write never executed — nothing to recover
                        if _is_write_like_event_data(event.data):
                            _clear_edit_write_state(variables)
                        return await self._finalize_block_response(
                            step_block,
                            evaluation,
                            span,
                            source="step-enforcement",
                            rule_name="step-tool-enforcement",
                            fallback_reason=(
                                "Rule enforced by Gobby: [step-tool-enforcement]\n"
                                "Current workflow step blocked this tool for the "
                                "active session."
                            ),
                            warn_detail="step enforcement returned an empty reason",
                        )

                # 4c. Step workflow transition processing (after successful MCP tool calls)
                _step_transition_msg: str | None = None
                if is_after_tool:
                    _step_transition_msg = await self._process_step_after_tool(
                        event, session_id, variables
                    )
                    if _step_transition_msg:
                        evaluation.context_parts.append(_step_transition_msg)

                # Deferred overrides — these used to early-return, but that skipped rule
                # evaluation entirely, preventing background mcp_call effects
                # from being collected. Now we record the override and let the loop run.
                override_decision: str | None = None
                override_reason: str | None = None

                # Force-allow stop (catastrophic failure bypass — self-clearing)
                if is_turn_end and variables.get("force_allow_stop"):
                    variables["force_allow_stop"] = False
                    if variables.get("task_claimed"):
                        logger.warning(
                            "force_allow_stop suppressed - task_claimed=True, deferring to require-task-close rule (session %s)",
                            session_id,
                        )
                    else:
                        override_decision = "allow"

                # Auto-block stop when a tool just failed (self-clearing)
                elif is_turn_end and variables.get("tool_block_pending"):
                    variables["tool_block_pending"] = False
                    override_decision = "block"
                    override_reason = (
                        "Rule enforced by Gobby: [tool-failure-recovery]\n"
                        "A tool just failed. Read the error and recover — do not stop."
                    )

                # Block stop when edit/write is pending (failed or in-flight)
                elif is_turn_end and variables.get("edit_write_pending"):
                    edit_stop_blocks = variables.get("edit_write_stop_blocks", 0)
                    if edit_stop_blocks < 3:  # Circuit breaker
                        variables["edit_write_stop_blocks"] = edit_stop_blocks + 1
                        override_decision = "block"
                        override_reason = (
                            "Rule enforced by Gobby: [edit-write-recovery]\n"
                            "Your last file mutation attempt failed. "
                            "Read the error and retry — do not stop."
                        )
                    else:
                        # Circuit breaker tripped — clear and allow stop
                        _clear_edit_write_state(variables)

                if not rules:
                    # Auto-manage tool_block_pending on after_tool execution results.
                    if is_after_tool:
                        self._manage_after_tool_recovery_state(event, variables)
                    # Honour hardcoded override decisions (e.g. tool_block_pending stop gate)
                    # even when no declarative rules are installed for this event.
                    resp = self._assemble_response(
                        evaluation,
                        override_decision=override_decision,
                        override_reason=override_reason,
                        block_gates=[],
                        include_rule_outputs=False,
                    )
                    return await self._finalize_block_response(resp, evaluation, span)

                # Auto-manage tool_block_pending on after_tool before rule eval.
                if is_after_tool:
                    self._manage_after_tool_recovery_state(event, variables)

                # 5. Evaluate rules in priority order
                block_gates = await self._run_rule_loop(
                    rules,
                    evaluation,
                    aggregate_blocks=aggregate_blocks,
                )

                # Proxy transformations are deferred until every original-input
                # denial has passed. Apply prior declarative rewrites first so
                # each trusted handler receives the latest accumulated command.
                if is_before_tool and not block_gates and evaluation.proxy_hooks:
                    rewrite_meta = variables.get("_rewrite_input")
                    input_was_rewritten = False
                    if isinstance(rewrite_meta, dict):
                        updates = rewrite_meta.get("input_updates")
                        tool_input = event.data.get("tool_input")
                        if isinstance(updates, dict) and isinstance(tool_input, dict):
                            tool_input.update(updates)
                            input_was_rewritten = bool(updates)

                    proxy_changed = await self._run_proxy_hooks(
                        evaluation.proxy_hooks,
                        event,
                        blocking_deadline=blocking_deadline,
                    )
                    if proxy_changed:
                        tool_input = event.data.get("tool_input")
                        command = (
                            tool_input.get("command") if isinstance(tool_input, dict) else None
                        )
                        if isinstance(command, str):
                            rewrite_meta = variables.setdefault("_rewrite_input", {})
                            current_updates = rewrite_meta.get("input_updates")
                            if not isinstance(current_updates, dict):
                                current_updates = {}
                            rewrite_meta["input_updates"] = {
                                **current_updates,
                                "command": command,
                            }

                    if input_was_rewritten or proxy_changed:
                        agent_block = await offload(
                            self._check_agent_tool_enforcement,
                            event,
                            session_id,
                            variables,
                        )
                        if agent_block is not None:
                            variables["_last_blocked_tool"] = _get_tool_identity(event.data)
                            if _is_write_like_event_data(event.data):
                                _clear_edit_write_state(variables)
                            return await self._finalize_block_response(
                                agent_block,
                                evaluation,
                                span,
                                source="step-enforcement",
                                rule_name="agent-tool-enforcement",
                            )

                        step_block = await offload(
                            self._check_step_tool_enforcement,
                            event,
                            session_id,
                            variables,
                        )
                        if step_block is not None:
                            variables["_last_blocked_tool"] = _get_tool_identity(event.data)
                            if _is_write_like_event_data(event.data):
                                _clear_edit_write_state(variables)
                            return await self._finalize_block_response(
                                step_block,
                                evaluation,
                                span,
                                source="step-enforcement",
                                rule_name="step-tool-enforcement",
                            )

                        block_gates = await self._run_rule_loop(
                            rules,
                            evaluation,
                            aggregate_blocks=aggregate_blocks,
                            block_effects_only=True,
                        )

                # 6. Build response — overrides take precedence over rule-evaluated decisions,
                # but the rule loop always runs so mcp_calls are always collected.
                resp = self._assemble_response(
                    evaluation,
                    override_decision=override_decision,
                    override_reason=override_reason,
                    block_gates=block_gates,
                )

                if span.is_recording():
                    span.set_attribute(
                        "rules.evaluated",
                        [row.name for row, _ in rules],
                    )
                    if mcp_calls:
                        span.set_attribute(
                            "rules.mcp_calls",
                            [f"{c.get('server')}/{c.get('tool')}" for c in mcp_calls],
                        )
                return await self._finalize_block_response(
                    resp,
                    evaluation,
                    span,
                    block_gates=block_gates,
                )
            except Exception as e:
                if span.is_recording():
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                raise

    def _load_rules(
        self,
        rule_events: list[RuleTriggerEvent],
        *,
        project_id: str | None = None,
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Load enabled rules matching any trigger event, sorted by priority.

        Global rows always apply; project-owned rows apply only to events from
        that project.
        """
        ordered: list[tuple[int, RuleDefinitionRow, RuleDefinitionBody]] = []
        seen_rows: set[str] = set()

        for trigger_index, rule_event in enumerate(rule_events):
            rows = self.rule_manager.list_by_event(
                event=rule_event.value,
                project_id=project_id,
                enabled=True,
            )
            for row in rows:
                if row.id in seen_rows:
                    continue
                try:
                    payload = row.definition_json
                    body = (
                        RuleDefinitionBody.model_validate(payload)
                        if isinstance(payload, dict)
                        else RuleDefinitionBody.model_validate_json(payload)
                    )
                    ordered.append((trigger_index, row, body))
                    seen_rows.add(row.id)
                except Exception as e:
                    logger.warning("Failed to parse rule %s: %s", row.name, e)

        ordered.sort(key=lambda item: (item[1].priority, item[0], item[1].name))
        return [(row, body) for _, row, body in ordered]

    def _filter_by_agent_scope(
        self,
        rules: list[tuple[RuleDefinitionRow, RuleDefinitionBody]],
        agent_type: str | None,
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Filter rules by agent_scope.

        - Rules with no agent_scope (None) are global — always included.
        - Rules with agent_scope require _agent_type to be in the list.
        - If no _agent_type is set, only global rules are included.
        """
        return [
            (row, body)
            for row, body in rules
            if body.agent_scope is None
            or (
                agent_type is not None
                and ("*" in body.agent_scope or agent_type in body.agent_scope)
            )
        ]

    def _filter_by_audience(
        self,
        rules: list[tuple[RuleDefinitionRow, RuleDefinitionBody]],
        variables: dict[str, Any],
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Filter rules by broad runtime audience."""
        return [(row, body) for row, body in rules if self._audience_matches(body, variables)]

    def _audience_matches(
        self,
        body: RuleDefinitionBody,
        variables: dict[str, Any],
    ) -> bool:
        audience = body.audience
        if audience is None or audience == "all":
            return True

        explicit = variables.get("_audience")
        agent_type = variables.get("_agent_type")
        is_spawned = bool(variables.get("is_spawned_agent"))

        if audience == explicit or audience == agent_type:
            return True
        if audience == "autonomous":
            return is_spawned or agent_type == "autonomous"
        if audience == "interactive":
            return not is_spawned and agent_type in (None, "default", "interactive")
        return False

    def _filter_by_active_rules(
        self,
        rules: list[tuple[RuleDefinitionRow, RuleDefinitionBody]],
        variables: dict[str, Any],
        *,
        project_id: str | None = None,
    ) -> list[tuple[RuleDefinitionRow, RuleDefinitionBody]]:
        """Filter rules using the current agent definition, falling back to session metadata."""
        agent = self._load_active_agent_definition(
            variables.get("_agent_type"),
            project_id=project_id,
        )
        if agent is not None:
            return [(row, body) for row, body in rules if rule_matches_agent(agent, row)]

        active_names = variables.get("_active_rule_names")
        if active_names is None:
            return rules  # no filter — current behavior preserved
        active_set = set(active_names)
        return [(row, body) for row, body in rules if row.name in active_set]

    def _load_active_agent_definition(
        self,
        agent_type: Any,
        *,
        project_id: str | None = None,
    ) -> AgentDefinitionBody | None:
        if not isinstance(agent_type, str) or not agent_type:
            return None

        revision = get_definitions_revision("agents")
        if revision != self._agent_def_cache_revision:
            self._agent_def_cache.clear()
            self._agent_def_cache_revision = revision

        cache_key = (agent_type, project_id)
        if cache_key in self._agent_def_cache:
            return self._agent_def_cache[cache_key]

        agent: AgentDefinitionBody | None = None
        row = self.agent_manager.get_by_name(agent_type, project_id=project_id)
        if row is not None:
            try:
                data = row.definition_json
                if isinstance(data, str):
                    data = json.loads(data)
                if isinstance(data, dict):
                    payload = dict(data)
                    payload.setdefault("name", row.name)
                    agent = AgentDefinitionBody.model_validate(payload)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("Failed to decode active agent definition %s: %s", agent_type, exc)
            except ValidationError as exc:
                logger.debug("Failed to validate active agent definition %s: %s", agent_type, exc)

        self._agent_def_cache[cache_key] = agent
        return agent
