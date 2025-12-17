import logging
from dataclasses import dataclass
from typing import Any, Protocol

from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import LocalSessionManager
from gobby.workflows.definitions import WorkflowState
from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)


@dataclass
class ActionContext:
    """Context passed to action handlers."""

    session_id: str
    state: WorkflowState
    db: LocalDatabase
    session_manager: LocalSessionManager
    template_engine: TemplateEngine
    # Future: services registry


class ActionHandler(Protocol):
    """Protocol for action handlers."""

    async def __call__(self, context: ActionContext, **kwargs) -> dict[str, Any] | None: ...


class ActionExecutor:
    """Registry and executor for workflow actions."""

    def __init__(
        self,
        db: LocalDatabase,
        session_manager: LocalSessionManager,
        template_engine: TemplateEngine,
    ):
        self.db = db
        self.session_manager = session_manager
        self.template_engine = template_engine
        self._handlers: dict[str, ActionHandler] = {}
        self._register_defaults()

    def register(self, name: str, handler: ActionHandler) -> None:
        """Register an action handler."""
        self._handlers[name] = handler

    def _register_defaults(self) -> None:
        """Register built-in actions."""
        self.register("inject_context", self._handle_inject_context)
        self.register("inject_message", self._handle_inject_message)
        self.register("capture_artifact", self._handle_capture_artifact)
        self.register("generate_handoff", self._handle_generate_handoff)
        # TODO: Add switch_mode, etc.

    async def execute(
        self, action_type: str, context: ActionContext, **kwargs
    ) -> dict[str, Any] | None:
        """Execute an action."""
        handler = self._handlers.get(action_type)
        if not handler:
            logger.warning(f"Unknown action type: {action_type}")
            return None

        try:
            return await handler(context, **kwargs)
        except Exception as e:
            logger.error(f"Error executing action {action_type}: {e}", exc_info=True)
            return {"error": str(e)}

    # --- Action Implementations ---

    async def _handle_inject_context(
        self, context: ActionContext, source: str, **kwargs
    ) -> dict[str, Any] | None:
        """
        Inject context from a source.
        Returns: {"inject_context": "content..."}
        """
        content = ""

        if source == "previous_session_summary":
            # 1. Find current session to get external/machine/project info to find parent
            current_session = context.session_manager.get(context.session_id)
            if not current_session:
                logger.warning(f"Session {context.session_id} not found")
                return None

            # Find parent manually if not linked
            # For now, just check if parent_session_id is set
            if current_session.parent_session_id:
                parent = context.session_manager.get(current_session.parent_session_id)
                if parent and parent.summary_markdown:
                    content = parent.summary_markdown
            else:
                # Try to find recent session? Move usage of find_parent to "find_parent_session" action?
                # WORKFLOWS.md says: source="previous_session_summary"
                pass

        elif source == "handoff":
            # Query workflow_handoffs table
            # We need to find the specific handoff consumed by this session or ready for it
            # For MVP, let's look for handoff where consumed_by_session = this session
            row = context.db.fetchone(
                """
                 SELECT * FROM workflow_handoffs 
                 WHERE consumed_by_session = ?
                 """,
                (context.session_id,),
            )
            if row:
                # TODO: Format handoff data
                content = f"Handoff Notes: {row['notes']}\n"
                if row["pending_tasks"]:
                    content += f"Pending Tasks: {row['pending_tasks']}\n"
            else:
                # Maybe look for unconsumed handoff?
                # Ideally, 'restore_from_handoff' action handles the claiming.
                # 'inject_context' just reads.
                pass

        if content:
            # Render content if template is used (in future).
            # Current logic just sets it.
            # But wait, inject_context usually pulls FROM a source.
            # If 'template' arg is provided, we might wrap the content in it?
            # WORKFLOWS.md says: source="previous_session_summary", template="..."
            template = kwargs.get("template")
            if template:
                # We need to construct a context for the template
                # that contains the 'source' data.
                # e.g. source="handoff" -> context={"handoff": ...}
                render_context = {
                    "session": context.session_manager.get(context.session_id),
                    "state": context.state,
                    "artifacts": context.state.artifacts,
                }

                # Add source data to context
                if source == "previous_session_summary":
                    render_context["summary"] = content
                elif source == "handoff":
                    # We need parsed handoff data here
                    # For now just passing raw content string might be limiting if template expects struct
                    # But content creation above was just a string.
                    # Let's improve the "handoff" fetching above to return dict first.
                    pass

                # Render
                content = context.template_engine.render(template, render_context)

            context.state.context_injected = True
            return {"inject_context": content}

        return None

    async def _handle_inject_message(
        self, context: ActionContext, content: str, **kwargs
    ) -> dict[str, Any] | None:
        """
        Inject a message to the user/assistant, rendering it as a template.
        """
        render_context = {
            "session": context.session_manager.get(context.session_id),
            "state": context.state,
            "artifacts": context.state.artifacts,
            "phase_action_count": context.state.phase_action_count,
            "variables": context.state.variables or {},
        }

        # Add any extra kwargs as context?
        render_context.update(kwargs)

        rendered_content = context.template_engine.render(content, render_context)

        # We return it as 'inject_message' which the hook handler should display or inject
        # The hook system currently expects 'inject_context' for prompt augmentation,
        # or we might need a new response field for 'message' (Ephemeral message?)
        # WORKFLOWS.md calls it "inject_message".
        # Ideally this shows up to the user or is injected into conversation history.

        return {"inject_message": rendered_content}

    async def _handle_capture_artifact(
        self, context: ActionContext, pattern: str, **kwargs
    ) -> dict[str, Any] | None:
        """
        Capture an artifact (file) and store its path/content in state.
        """
        import glob
        import os

        # Security check: Ensure pattern is relative and within allowed paths?
        # For now, assume agent has access to CWD.

        # We need the CWD of the session.
        # Session object has no CWD anymore (removed in migration 6),
        # but the agent runs in project root usually.
        # Let's assume absolute paths or relative to project root.

        # Name to store as (from 'as' arg, but kwargs uses 'as' which is reserved... passed as 'as_' or similar?)
        # Let's assume the YAML parser maps 'as' to something else or we get it from kwargs.
        save_as = kwargs.get("as")

        matches = glob.glob(pattern, recursive=True)
        if not matches:
            return None

        # Just grab the first match for now if multiple, or list?
        # If 'as' is provided, we map a single file.

        filepath = os.path.abspath(matches[0])

        if save_as:
            context.state.artifacts[save_as] = filepath

        return {"captured": filepath}

    async def _handle_generate_handoff(
        self, context: ActionContext, **kwargs
    ) -> dict[str, Any] | None:
        """
        Generate a handoff record.
        """
        include = kwargs.get("include", [])

        current_session = context.session_manager.get(context.session_id)
        if not current_session:
            return None

        # Extract data
        artifacts = context.state.artifacts if "artifacts" in include else {}
        pending_tasks = []  # TODO: Query task system for open tasks

        # Notes? Maybe from an argument?
        notes = "Auto-generated handoff"

        # Create record
        context.db.execute(
            """
            INSERT INTO workflow_handoffs (
                project_id, workflow_name, from_session_id, 
                phase, artifacts, pending_tasks, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_session.project_id,
                context.state.workflow_name,
                context.session_id,
                context.state.phase,
                str(artifacts),  # JSON serialization needed
                str(pending_tasks),  # JSON serialization needed
                notes,
            ),
        )

        # Mark session as handoff ready
        context.session_manager.update_status(context.session_id, "handoff_ready")

        return {"handoff_created": True}
