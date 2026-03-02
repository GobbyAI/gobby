"""Session action tools - thin wrappers around workflow actions.

Exposes internal workflow actions as MCP tools:
- capture_baseline_dirty_files: Capture pre-session dirty files
- synthesize_title_from_prompt: Generate session title from user prompt
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from gobby.workflows.git_utils import get_dirty_files

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import LocalSessionManager

logger = logging.getLogger(__name__)

# Emoji ranges for sanitization
_EMOJI_PATTERN = re.compile(
    "[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff\U00002702-\U000027b0\U0000fe00-\U0000fe0f"
    "\U0000200d\U000024c2-\U0001f251\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff]+"
)


def _resolve_provider(llm_service: Any, config: Any) -> tuple[Any, str | None]:
    """Resolve LLM provider and model from config, falling back to defaults."""
    try:
        provider, model, _ = llm_service.get_provider_for_feature(config)
    except (ValueError, KeyError, AttributeError):
        provider = llm_service.get_default_provider()
        model = None
    return provider, model


def _sanitize_title(raw: str) -> str:
    """Strip markdown, emoji, normalize whitespace from LLM title."""
    title = raw.strip().strip('"').strip("'").split("\n")[0]
    title = re.sub(r"[#*_~`\[\]()]", "", title)
    title = _EMOJI_PATTERN.sub("", title)
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 80:
        title = title[:77] + "..."
    return title or "Untitled Session"


def register_action_tools(
    registry: InternalToolRegistry,
    session_manager: LocalSessionManager,
    llm_service: Any | None = None,
    transcript_processor: Any | None = None,
    config: Any | None = None,
    db: Any | None = None,
    worktree_manager: Any | None = None,
) -> None:
    """Register session action tools on the registry.

    Args:
        registry: The session tool registry
        session_manager: LocalSessionManager for session lookups
        llm_service: LLM service for handoff generation
        transcript_processor: Transcript processor for handoff generation
        config: DaemonConfig for settings
        db: Database for dependency injection
        worktree_manager: Worktree manager for context enrichment
    """

    @registry.tool(
        name="capture_baseline_dirty_files",
        description="Capture current dirty files as baseline for session-aware commit detection.",
    )
    async def capture_baseline_dirty_files_tool(
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Capture current dirty files as a baseline.

        Args:
            project_path: Path to the project directory for git status check
        """
        try:
            dirty_files = get_dirty_files(project_path)
            return {
                "success": True,
                "file_count": len(dirty_files),
                "files": sorted(dirty_files),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="synthesize_title_from_prompt",
        description=(
            "Generate a session title from the user's prompt text. "
            "Fires on before_agent to provide immediate title feedback. "
            "Skips slash commands, very short prompts, and sessions that already have a title."
        ),
    )
    async def synthesize_title_from_prompt_tool(
        session_id: str,
        prompt_text: str = "",
    ) -> dict[str, Any]:
        """Synthesize a session title from the user's prompt.

        Args:
            session_id: Session ID (auto-injected by hook manager)
            prompt_text: The user's prompt text (auto-injected by hook manager)
        """
        # --- Guard: skip if prompt is too short or a slash command ---
        text = (prompt_text or "").strip()
        if len(text) < 10:
            return {"success": True, "skipped": True, "reason": "prompt_too_short"}
        if text.startswith("/"):
            return {"success": True, "skipped": True, "reason": "slash_command"}

        # --- Guard: skip if session already has a non-default title ---
        try:
            session = session_manager.get(session_id)
            if session is None:
                return {"success": False, "error": f"Session not found: {session_id}"}

            existing_title = getattr(session, "title", None)
            if existing_title and existing_title not in (
                "Untitled Session",
                "New Session",
            ):
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "title_already_set",
                    "title": existing_title,
                }
        except Exception as e:
            logger.warning("Failed to check session title", exc_info=True)
            return {"success": False, "error": str(e)}

        # --- Resolve LLM provider ---
        if llm_service is None:
            return {"success": False, "error": "LLM service not available"}

        try:
            from gobby.config.sessions import SessionTitleConfig

            title_config = getattr(config, "session_title", None) if config else None
            config_obj = title_config or SessionTitleConfig()
            provider, model = _resolve_provider(llm_service, config_obj)

            # --- Load prompt template ---
            system_prompt: str | None = None
            try:
                from gobby.prompts.loader import PromptLoader

                loader = PromptLoader(db=db)
                rendered = loader.render(
                    "sessions/title_from_prompt",
                    {"prompt_text": text},
                )
                # Use the rendered template as the user message, system prompt
                # stays generic
                llm_prompt = rendered
                system_prompt = (
                    "You generate short titles for chat sessions. "
                    "Output ONLY 3-5 words. No quotes, no explanation, no punctuation."
                )
            except FileNotFoundError:
                # Fallback: use inline prompt
                llm_prompt = text[:500]
                system_prompt = (
                    "Given a user's first message to an AI coding assistant, "
                    "generate a 3-5 word title that captures the intent. "
                    "Output ONLY the title. No quotes, no punctuation."
                )

            # --- Call LLM ---
            llm_timeout = getattr(config_obj, "timeout", 30)
            raw_title = await asyncio.wait_for(
                provider.generate_text(
                    llm_prompt,
                    system_prompt=system_prompt,
                    model=model,
                    max_tokens=30,
                ),
                timeout=llm_timeout,
            )
            title = _sanitize_title(raw_title)

            # --- Update session ---
            session_manager.update_title(session_id, title)

            # --- Rename tmux window ---
            try:
                from gobby.workflows.summary_actions import _rename_tmux_window

                # Refresh session to get terminal_context
                updated_session = session_manager.get(session_id)
                if updated_session and len(title) < 80:
                    await _rename_tmux_window(updated_session, title)
            except Exception as e:
                logger.debug("tmux rename skipped: %s", e)

            return {"success": True, "title": title}

        except TimeoutError:
            logger.warning("Title synthesis timed out for session %s", session_id)
            return {"success": False, "error": "LLM call timed out"}
        except Exception as e:
            logger.error(
                "Title synthesis failed for session %s: %s",
                session_id,
                e,
                exc_info=True,
            )
            return {"success": False, "error": str(e)}
