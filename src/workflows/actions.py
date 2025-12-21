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
    llm_service: Any | None = None
    transcript_processor: Any | None = None
    config: Any | None = None


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
        llm_service: Any | None = None,
        transcript_processor: Any | None = None,
        config: Any | None = None,
    ):
        self.db = db
        self.session_manager = session_manager
        self.template_engine = template_engine
        self.llm_service = llm_service
        self.transcript_processor = transcript_processor
        self.config = config
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
        Generate a handoff record by summarizing the session and saving to sessions.summary_markdown.
        """
        # We need LLM service and transcript processor
        if not context.llm_service or not context.transcript_processor:
            logger.warning("generate_handoff: Missing LLM service or transcript processor")
            return {"error": "Missing services"}

        current_session = context.session_manager.get(context.session_id)
        if not current_session:
            return {"error": "Session not found"}

        # Get transcript path (from context or session?)
        # Session object usually has the transcript path if registered.
        # But we might need to get it from the event if this is triggered dynamically?
        # Actually Event usually has transcript_path.
        # But ActionContext doesn't have Event directly unless we pass it.
        # We assume `current_session.jsonl_path` is valid if we registered it.
        transcript_path = getattr(current_session, "jsonl_path", None)
        if not transcript_path:
            # Try to get it from kwargs if passed (e.g. from event data)
            # But arguments to action come from YAML.
            logger.warning(f"generate_handoff: No transcript path for session {context.session_id}")
            return {"error": "No transcript path"}

        # Use SummaryGenerator Logic (but reimplemented here or call it?)
        # THe plan says: "Use context.llm_service to generate summary"
        # We should emulate SummaryGenerator._generate_recursive or similar.
        # Or, since we want to migrate, maybe we should just instantiate SummaryGenerator here?
        # No, that defeats the purpose of decoupling.
        # Let's use the template provided in YAML and the LLM service directly.

        template = kwargs.get("template")
        if not template:
            # Fallback to a default prompt if none in YAML
            template = (
                "Summarize this session, focusing on what was accomplished, "
                "key decisions, and what is left to do.\n\n"
                "Transcript:\n{transcript_summary}"
            )

        # 1. Process Transcript
        try:
            import json
            from pathlib import Path

            # Read JSONL transcript
            transcript_file = Path(transcript_path)
            if not transcript_file.exists():
                logger.warning(f"Transcript file not found: {transcript_path}")
                return {"error": "Transcript not found"}

            turns = []
            with open(transcript_file) as f:
                for line in f:
                    if line.strip():
                        turns.append(json.loads(line))

            # Get turns since last /clear (up to 50 turns)
            recent_turns = context.transcript_processor.extract_turns_since_clear(turns, max_turns=50)

            # Format turns for LLM
            transcript_summary = self._format_turns_for_llm(recent_turns)
        except Exception as e:
            logger.error(f"Failed to process transcript: {e}")
            return {"error": str(e)}

        # 2. Gather Context Variables for Template
        render_context = {
            "transcript_summary": transcript_summary,
            "session": current_session,
            "state": context.state,
            "last_messages": "",  # TODO: extract from turns
            "git_status": "",  # TODO: run git status
            "file_changes": "",  # TODO: run git diff
        }

        # 3. Render Prompt
        prompt = context.template_engine.render(template, render_context)

        # 4. Call LLM
        try:
            # Get provider from LLM service and call generate_summary
            llm_context = {
                "turns": recent_turns,
                "transcript_summary": transcript_summary,
                "session": current_session,
            }
            provider = context.llm_service.get_default_provider()
            summary_content = await provider.generate_summary(
                context=llm_context,
                prompt_template=template,
            )
        except Exception as e:
            logger.error(f"LLM generation failed: {e}")
            return {"error": f"LLM error: {e}"}

        # 5. Save to Production Location (sessions table)
        context.session_manager.update_summary(context.session_id, summary_markdown=summary_content)

        # 6. Mark Session Status
        context.session_manager.update_status(context.session_id, "handoff_ready")

        logger.info(f"Generated handoff summary for session {context.session_id}")
        return {"handoff_created": True, "summary_length": len(summary_content)}

    def _format_turns_for_llm(self, turns: list[dict]) -> str:
        """
        Format transcript turns for LLM analysis.

        Args:
            turns: List of transcript turn dicts

        Returns:
            Formatted string with turn summaries
        """
        formatted = []
        for i, turn in enumerate(turns):
            message = turn.get("message", {})
            role = message.get("role", "unknown")
            content = message.get("content", "")

            # Assistant messages have content as array of blocks
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "thinking":
                            text_parts.append(f"[Thinking: {block.get('thinking', '')}]")
                        elif block.get("type") == "tool_use":
                            text_parts.append(f"[Tool: {block.get('name', 'unknown')}]")
                content = " ".join(text_parts)

            formatted.append(f"[Turn {i + 1} - {role}]: {content}")

        return "\n\n".join(formatted)
