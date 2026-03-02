"""Context injection and handoff workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These functions handle context injection, message injection, and handoff formatting.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.workflows.templates import TemplateRenderer

logger = logging.getLogger(__name__)


def inject_context(
    session_manager: Any,
    session_id: str,
    state: Any,
    template_engine: TemplateRenderer,
    source: str | list[str] | None = None,
    template: str | None = None,
    require: bool = False,
    skill_manager: Any | None = None,
    filter: str | None = None,
    session_task_manager: Any | None = None,
    memory_manager: Any | None = None,
    prompt_text: str | None = None,
    limit: int = 5,
    session_variable_manager: Any | None = None,
) -> dict[str, Any] | None:
    """Inject context from a source or multiple sources.

    Args:
        session_manager: The session manager instance
        session_id: Current session ID
        state: WorkflowState instance
        template_engine: Template engine for rendering
        source: Source type(s). Can be a string or list of strings.
                Supported: previous_session_summary, handoff, skills, task_context, memories, etc.
        template: Optional template for rendering
        require: If True, block session when no content found (default: False)
        skill_manager: HookSkillManager instance (required for source='skills')
        filter: Optional filter for skills source ('always_apply' to only include always-apply skills)
        session_task_manager: SessionTaskManager instance (required for source='task_context')
        memory_manager: MemoryManager instance (required for source='memories')
        prompt_text: User prompt text for memory recall (required for source='memories')
        limit: Max memories to retrieve (default: 5, used with source='memories')

    Returns:
        Dict with inject_context key, blocking decision, or None
    """
    # Validate required parameters
    if session_manager is None:
        logger.warning(f"inject_context: session_manager is None (session_id={session_id})")
        return None

    if state is None:
        logger.warning(f"inject_context: state is None (session_id={session_id})")
        return None

    if template_engine is None:
        logger.warning(f"inject_context: template_engine is None (session_id={session_id})")
        return None

    if not session_id:
        logger.warning("inject_context: session_id is empty or None")
        return None

    # Handle list of sources - recursively call for each source and combine
    if isinstance(source, list):
        combined_content: list[str] = []
        for single_source in source:
            result = inject_context(
                session_manager=session_manager,
                session_id=session_id,
                state=state,
                template_engine=template_engine,
                source=single_source,
                template=None,  # Don't render template for individual sources
                require=False,  # Don't block for individual sources
                skill_manager=skill_manager,
                filter=filter,
                session_task_manager=session_task_manager,
                memory_manager=memory_manager,
                prompt_text=prompt_text,
                limit=limit,
                session_variable_manager=session_variable_manager,
            )
            if result and result.get("inject_context"):
                combined_content.append(result["inject_context"])

        if combined_content:
            content = "\n\n".join(combined_content)
            if template:
                # Build source_contents mapping for individual source access
                source_contents: dict[str, str] = {}
                for i, single_source in enumerate(source):
                    if i < len(combined_content):
                        source_contents[single_source] = combined_content[i]
                render_context: dict[str, Any] = {
                    "session": session_manager.get(session_id),
                    "state": state,
                    "observations": session_variable_manager.get_variables(session_id).get(
                        "_observations", []
                    )
                    if session_variable_manager
                    else [],
                    "combined_content": content,
                    "source_contents": source_contents,
                }
                content = template_engine.render(template, render_context)
            state.context_injected = True
            return {"inject_context": content}

        # No content from any source - block if required
        if require:
            reason = f"Required handoff context not found (sources={source})"
            logger.warning(f"inject_context: {reason}")
            return {"decision": "block", "reason": reason}

        return None

    # Debug logging for troubleshooting
    logger.debug(
        f"inject_context called: source={source!r}, "
        f"template_present={template is not None}, "
        f"template_len={len(template) if template else 0}, "
        f"session_id={session_id}"
    )

    # Support template-only injection (no source lookup needed)
    condition_result = (not source) and bool(template)
    logger.debug(
        f"inject_context: not source={not source}, bool(template)={bool(template)}, "
        f"condition_result={condition_result}"
    )
    if not source and template:
        # Render static template directly
        logger.debug("inject_context: entering template-only path")
        render_context = {
            "session": session_manager.get(session_id),
            "state": state,
            "observations": session_variable_manager.get_variables(session_id).get(
                "_observations", []
            )
            if session_variable_manager
            else [],
        }
        rendered = template_engine.render(template, render_context)
        logger.debug(f"inject_context: rendered template, len={len(rendered) if rendered else 0}")
        if state:
            state.context_injected = True
        return {"inject_context": rendered}

    if not source:
        return None

    content = ""

    if source in ["previous_session_summary", "handoff"]:
        current_session = session_manager.get(session_id)
        if not current_session:
            logger.warning(f"Session {session_id} not found")
            return None

        if current_session.parent_session_id:
            parent = session_manager.get(current_session.parent_session_id)
            if parent:
                content = parent.summary_markdown
                # Failback: try reading from file if database summary is empty
                # This handles cases where daemon was unavailable during /clear
                if not content:
                    summary_dir = Path.home() / ".gobby" / "session_summaries"
                    if summary_dir.exists():
                        # Try {seq_num}-full.md first
                        seq_num = getattr(parent, "seq_num", None)
                        if seq_num:
                            candidate = summary_dir / f"{seq_num}-full.md"
                            if candidate.exists():
                                try:
                                    content = candidate.read_text()
                                    logger.info(
                                        f"Recovered summary from failback file for #{seq_num}"
                                    )
                                except Exception as e:
                                    logger.warning(f"Failed to read failback file {candidate}: {e}")

                        # Legacy fallback: session_*_{external_id}.md
                        if not content:
                            ext_id = getattr(parent, "external_id", None)
                            if ext_id:
                                for summary_file in summary_dir.glob(f"session_*_{ext_id}.md"):
                                    try:
                                        content = summary_file.read_text()
                                        logger.info(
                                            f"Recovered summary from legacy file for {ext_id}"
                                        )
                                        break
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to read failback file {summary_file}: {e}"
                                        )

    elif source == "observations":
        obs = (
            session_variable_manager.get_variables(session_id).get("_observations", [])
            if session_variable_manager
            else []
        )
        if obs:
            content = "## Observations\n" + json.dumps(obs, indent=2)

    elif source == "workflow_state":
        try:
            state_dict = state.model_dump()
        except AttributeError:
            state_dict = state.dict()
        content = "## Workflow State\n" + json.dumps(state_dict, indent=2, default=str)

    elif source == "compact_handoff":
        # Look at CURRENT session's compact_markdown (not parent)
        # On compact, the same session continues - compact_markdown was saved to this session
        # during pre_compact, so we read it from the current session itself.
        current_session = session_manager.get(session_id)
        logger.debug(
            f"compact_handoff lookup: session_id={session_id}, "
            f"compact_markdown exists: {bool(getattr(current_session, 'compact_markdown', None)) if current_session else False}"
        )
        if current_session and current_session.compact_markdown:
            content = current_session.compact_markdown
            logger.debug(
                f"Loaded compact_markdown ({len(content)} chars) from current session {session_id}"
            )

    elif source == "skills":
        # Inject skill context from skill_manager
        if skill_manager is None:
            logger.debug("inject_context: skills source requires skill_manager")
            return None

        skills = skill_manager.discover_core_skills()

        if session_variable_manager:
            active_names = session_variable_manager.get_variables(session_id).get(
                "_active_skill_names"
            )
            if active_names is not None:
                active_set = set(active_names)
                skills = [s for s in skills if getattr(s, "name", "") in active_set]

        # Apply filter if specified
        if filter == "always_apply":
            skills = [s for s in skills if s.is_always_apply()]
            if skills:
                content = _format_skills(skills)
                logger.debug(f"Formatted {len(skills)} skills for injection")
        else:
            if skills:
                content = _format_skills(skills)
                logger.debug(f"Formatted {len(skills)} skills for injection")

    elif source == "task_context":
        # Inject current task context from session_task_manager
        if session_task_manager is None:
            logger.debug("inject_context: task_context source requires session_task_manager")
            return None

        session_tasks = session_task_manager.get_session_tasks(session_id)

        # Filter for "worked_on" tasks (the active task)
        worked_on_tasks = [t for t in session_tasks if t.get("action") == "worked_on"]

        if worked_on_tasks:
            content = _format_task_context(worked_on_tasks)
            logger.debug(f"Formatted {len(worked_on_tasks)} active tasks for injection")

    elif source == "memories":
        # Inject relevant memories from memory_manager
        if memory_manager is None:
            logger.debug("inject_context: memories source requires memory_manager")
            return None

        if not memory_manager.config.enabled:
            logger.debug("inject_context: memory manager is disabled")
            return None

        # Get project_id from session
        project_id = None
        session = session_manager.get(session_id)
        if session:
            project_id = getattr(session, "project_id", None)

        try:
            memories = memory_manager.search_memories(
                query=prompt_text or "",
                project_id=project_id,
                limit=limit,
                search_mode="auto",
            )

            if memories:
                content = _format_memories(memories)
                logger.debug(f"Formatted {len(memories)} memories for injection")
        except Exception as e:
            logger.error(f"inject_context: memory recall failed: {e}")
            return None

    if content:
        if template:
            render_context = {
                "session": session_manager.get(session_id),
                "state": state,
                "observations": session_variable_manager.get_variables(session_id).get(
                    "_observations", []
                )
                if session_variable_manager
                else [],
            }

            if source in ["previous_session_summary", "handoff"]:
                render_context["summary"] = content
                render_context["handoff"] = {"notes": content}
            elif source == "observations":
                render_context["observations_text"] = content
            elif source == "workflow_state":
                render_context["workflow_state_text"] = content
            elif source == "compact_handoff":
                # Pass content to template (like /clear does with summary)
                render_context["handoff"] = content
            elif source == "skills":
                render_context["skills_list"] = content
            elif source == "task_context":
                render_context["task_context"] = content
            elif source == "memories":
                render_context["memories_list"] = content

            content = template_engine.render(template, render_context)

        state.context_injected = True
        return {"inject_context": content}

    # No content found - block if required
    if require:
        reason = f"Required handoff context not found (source={source})"
        logger.warning(f"inject_context: {reason}")
        return {"decision": "block", "reason": reason}

    return None


def inject_message(
    session_manager: Any,
    session_id: str,
    state: Any,
    template_engine: TemplateRenderer,
    content: str | None = None,
    **extra_kwargs: Any,
) -> dict[str, Any] | None:
    """Inject a message to the user/assistant, rendering it as a template.

    Args:
        session_manager: The session manager instance
        session_id: Current session ID
        state: WorkflowState instance
        template_engine: Template engine for rendering
        content: Template content to render
        **extra_kwargs: Additional context for rendering

    Returns:
        Dict with inject_message key, or None
    """
    if not content:
        return None

    render_context: dict[str, Any] = {
        "session": session_manager.get(session_id),
        "state": state,
        "step_action_count": state.step_action_count,
        "variables": state.variables or {},
    }
    render_context.update(extra_kwargs)

    rendered_content = template_engine.render(content, render_context)
    return {"inject_message": rendered_content}


def _format_memories(memories: list[Any]) -> str:
    """Format memory objects as markdown for injection.

    Args:
        memories: List of Memory objects

    Returns:
        Formatted markdown string with memory content
    """
    lines = ["## Relevant Memories"]
    for memory in memories:
        content = getattr(memory, "content", str(memory))
        memory_type = getattr(memory, "memory_type", None)

        if memory_type:
            lines.append(f"- [{memory_type}] {content}")
        else:
            lines.append(f"- {content}")

    return "\n".join(lines)


def _format_task_context(task_entries: list[dict[str, Any]]) -> str:
    """Format task entries as markdown for injection.

    Args:
        task_entries: List of dicts with 'task' key containing Task objects

    Returns:
        Formatted markdown string with task info
    """
    lines = ["## Active Task"]
    for entry in task_entries:
        task = entry.get("task")
        if task is None:
            continue

        seq_num = getattr(task, "seq_num", None)
        title = getattr(task, "title", "Untitled")
        status = getattr(task, "status", "unknown")
        description = getattr(task, "description", "")
        validation = getattr(task, "validation_criteria", "")

        # Format task reference
        ref = f"#{seq_num}" if seq_num else task.id[:8] if hasattr(task, "id") else "unknown"
        lines.append(f"**{ref}**: {title}")
        lines.append(f"Status: {status}")

        if description:
            lines.append(f"\n{description}")

        if validation:
            lines.append(f"\n**Validation Criteria**: {validation}")

    return "\n".join(lines)


def _format_skills(skills: list[Any]) -> str:
    """Format a list of ParsedSkill objects as markdown for injection.

    Respects each skill's ``injection_format`` field:
    - ``"summary"`` (default): ``- **name**: description`` under an
      ``## Available Skills`` heading.
    - ``"full"``: ``### name`` + description + full content body.
    - ``"content"``: raw content only, no wrapper.

    Args:
        skills: List of ParsedSkill objects

    Returns:
        Formatted markdown string with skill content
    """
    return _format_skills_with_formats(
        [(skill, getattr(skill, "injection_format", "summary")) for skill in skills]
    )


def _format_skills_with_formats(skills_with_formats: list[tuple[Any, str]]) -> str:
    """Format skills with pre-resolved injection formats.

    Like _format_skills() but uses the format resolved by SkillInjector
    instead of reading from the skill's injection_format field.

    Args:
        skills_with_formats: List of (ParsedSkill, resolved_format) tuples

    Returns:
        Formatted markdown string with skill content
    """
    summary_lines: list[str] = []
    expanded_sections: list[str] = []

    for skill, fmt in skills_with_formats:
        name = getattr(skill, "name", "unknown")
        description = getattr(skill, "description", "")
        content = getattr(skill, "content", "")

        if fmt == "full":
            section_lines = [f"### {name}"]
            if description:
                section_lines.append(description)
            if content:
                section_lines.append("")
                section_lines.append(content)
            expanded_sections.append("\n".join(section_lines))
        elif fmt == "content":
            if content:
                expanded_sections.append(content)
        else:
            # summary (default)
            if description:
                summary_lines.append(f"- **{name}**: {description}")
            else:
                summary_lines.append(f"- **{name}**")

    parts: list[str] = []
    if summary_lines:
        parts.append("## Available Skills\n" + "\n".join(summary_lines))
    if expanded_sections:
        parts.extend(expanded_sections)

    return "\n\n".join(parts)


def recommend_skills_for_task(
    task: dict[str, Any] | None,
    db: Any | None = None,
) -> list[str]:
    """Recommend relevant skills based on task category.

    Uses HookSkillManager to get skill recommendations based on the task's
    category field. Returns always-apply skills if no category is set.

    Args:
        task: Task dict with optional 'category' field, or None.
        db: Optional database instance for DB-backed skill loading.
            When provided, skills are read from the unified DB instead
            of falling back to filesystem discovery.

    Returns:
        List of recommended skill names for this task.
    """
    if task is None:
        return []

    try:
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager(db=db)
        category = task.get("category")
        return manager.recommend_skills(category=category)
    except Exception as e:
        logger.debug(f"Failed to recommend skills: {e}")
        return []


def format_handoff_as_markdown(ctx: Any, prompt_template: str | None = None) -> str:
    """Format HandoffContext as markdown for storage.

    Args:
        ctx: HandoffContext with extracted session data
        prompt_template: Optional custom template (unused, reserved for future)

    Returns:
        Formatted markdown string with all sections
    """
    _ = prompt_template  # Reserved for future template support
    sections: list[str] = []

    # Active task section
    if ctx.active_gobby_task:
        task = ctx.active_gobby_task
        sections.append(
            f"### Active Task\n"
            f"**{task.get('title', 'Untitled')}** ({task.get('id', 'unknown')})\n"
            f"Status: {task.get('status', 'unknown')}"
        )

    # Worktree context section
    if ctx.active_worktree:
        wt = ctx.active_worktree
        lines = ["### Worktree Context"]
        lines.append(f"- **Branch**: `{wt.get('branch_name', 'unknown')}`")
        lines.append(f"- **Path**: `{wt.get('worktree_path', 'unknown')}`")
        lines.append(f"- **Base**: `{wt.get('base_branch', 'main')}`")
        if wt.get("task_id"):
            lines.append(f"- **Task**: {wt.get('task_id')}")
        sections.append("\n".join(lines))

    # Git commits section
    if ctx.git_commits:
        lines = ["### Commits This Session"]
        for commit in ctx.git_commits:
            lines.append(f"- `{commit.get('hash', '')[:7]}` {commit.get('message', '')}")
        sections.append("\n".join(lines))

    # Git status section
    if ctx.git_status:
        sections.append(f"### Uncommitted Changes\n```\n{ctx.git_status}\n```")

    # Files modified section - only show files still dirty (not yet committed)
    if ctx.files_modified and ctx.git_status:
        # Filter to files that appear in git status (still uncommitted)
        # Normalize paths: files_modified may have absolute paths, git_status has relative
        cwd = Path.cwd()
        dirty_files = []
        for f in ctx.files_modified:
            # Try to make path relative to cwd for comparison
            try:
                rel_path = Path(f).relative_to(cwd)
                rel_str = str(rel_path)
            except ValueError:
                # Path not relative to cwd, use as-is
                rel_str = f
            # Check if relative path appears in git status
            if rel_str in ctx.git_status:
                dirty_files.append(rel_str)
        if dirty_files:
            lines = ["### Files Being Modified"]
            for f in dirty_files:
                lines.append(f"- {f}")
            sections.append("\n".join(lines))

    # Initial goal section - only if task is still active (not closed/completed)
    if ctx.initial_goal:
        task_status = None
        if ctx.active_gobby_task:
            task_status = ctx.active_gobby_task.get("status")
        # Only include if no task or task is still open/in_progress
        if task_status in (None, "open", "in_progress"):
            sections.append(f"### Original Goal\n{ctx.initial_goal}")

    # Recent activity section
    if ctx.recent_activity:
        lines = ["### Recent Activity"]
        for activity in ctx.recent_activity[-5:]:
            lines.append(f"- {activity}")
        sections.append("\n".join(lines))

    # Note: Active Skills section removed - redundant with _build_skill_injection_context()
    # which already handles skill restoration on session start

    return "\n\n".join(sections)


# --- ActionHandler-compatible wrappers ---
# These match the ActionHandler protocol: (context: Any, **kwargs) -> dict | None


async def handle_inject_context(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for inject_context."""
    from gobby.workflows.state_manager import SessionVariableManager

    # Get prompt_text from event_data if not explicitly passed
    prompt_text = kwargs.get("prompt_text")
    if prompt_text is None and context.event_data:
        prompt_text = context.event_data.get("prompt_text")

    session_variable_manager = SessionVariableManager(context.db) if context.db else None

    return await asyncio.to_thread(
        inject_context,
        session_manager=context.session_manager,
        session_id=context.session_id,
        state=context.state,
        template_engine=context.template_engine,
        source=kwargs.get("source"),
        template=kwargs.get("template"),
        require=kwargs.get("require", False),
        skill_manager=context.skill_manager,
        filter=kwargs.get("filter"),
        session_task_manager=context.session_task_manager,
        memory_manager=context.memory_manager,
        prompt_text=prompt_text,
        limit=kwargs.get("limit", 5),
        session_variable_manager=session_variable_manager,
    )


async def handle_inject_message(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for inject_message."""
    return await asyncio.to_thread(
        inject_message,
        session_manager=context.session_manager,
        session_id=context.session_id,
        state=context.state,
        template_engine=context.template_engine,
        content=kwargs.get("content"),
        **{k: v for k, v in kwargs.items() if k != "content"},
    )
