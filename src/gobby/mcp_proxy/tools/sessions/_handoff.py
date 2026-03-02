"""Handoff tools for session management.

This module contains MCP tools for setting and retrieving handoff context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import LocalSessionManager


def register_handoff_tools(
    registry: InternalToolRegistry,
    session_manager: LocalSessionManager,
    llm_service: Any | None = None,
    transcript_processor: Any | None = None,
    inter_session_message_manager: InterSessionMessageManager | None = None,
) -> None:
    """
    Register handoff tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: LocalSessionManager instance for session operations
        llm_service: LLM service for generating full summaries (optional)
        transcript_processor: Transcript processor for parsing transcripts (optional)
        inter_session_message_manager: For sending P2P messages between sessions (optional)
    """
    from gobby.utils.project_context import get_project_context

    def _resolve_session_id(ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        return session_manager.resolve_session_reference(ref, project_id)

    def _send_to_peer(from_session_id: str, to_session_ref: str, content: str) -> dict[str, Any]:
        """Send handoff content to a peer session via P2P message."""
        if inter_session_message_manager is None:
            return {"success": False, "error": "Inter-session message manager not available"}

        try:
            resolved_to = _resolve_session_id(to_session_ref)
            to_session_obj = session_manager.get(resolved_to)
            if not to_session_obj:
                return {"success": False, "error": f"Target session {to_session_ref} not found"}

            # Validate same project
            from_session_obj = session_manager.get(from_session_id)
            if from_session_obj and to_session_obj:
                from_proj = getattr(from_session_obj, "project_id", None)
                to_proj = getattr(to_session_obj, "project_id", None)
                if from_proj and to_proj and from_proj != to_proj:
                    return {"success": False, "error": "Sessions belong to different projects"}

            msg = inter_session_message_manager.create_message(
                from_session=from_session_id,
                to_session=resolved_to,
                content=content,
                message_type="handoff",
            )
            return {"success": True, "message_id": msg.id, "to_session": resolved_to}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="set_handoff_context",
        description=(
            "Set handoff context for a session. Two modes:\n"
            "1. Agent-authored (fast): Pass `content` directly — writes to summary_markdown, "
            "sets handoff_ready.\n"
            "2. Automated fallback: Omit `content` — uses TranscriptAnalyzer and/or LLM.\n"
            "Optionally sends context to a peer session via `to_session`.\n\n"
            "Args:\n"
            "    session_id: (REQUIRED) Your session ID. Accepts #N, N, UUID, or prefix."
        ),
    )
    async def set_handoff_context(
        session_id: str,
        content: str | None = None,
        to_session: str | None = None,
        notes: str | None = None,
        compact: bool = False,
        full: bool = False,
        write_file: bool = False,
        output_path: str = ".gobby/session_summaries/",
        set_handoff_ready: bool = True,
    ) -> dict[str, Any]:
        """
        Set handoff context for a session.

        Args:
            session_id: Session reference - supports #N, N (seq_num), UUID, or prefix (REQUIRED)
            content: Agent-authored handoff content (fast path, skips transcript analysis)
            to_session: Target session to send handoff context to via P2P message
            notes: Additional notes to include in handoff
            compact: Generate compact summary only (TranscriptAnalyzer)
            full: Generate full LLM summary only
            write_file: Also write to file (default: False). DB is always written.
            output_path: Directory for file output (default: .gobby/session_summaries/)
            set_handoff_ready: Set session status to handoff_ready (default: True)

        Returns:
            Success status, markdown lengths, and context summary
        """
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        # Resolve session reference
        try:
            resolved_id = _resolve_session_id(session_id)
            session = session_manager.get(resolved_id)
        except ValueError as e:
            return {"success": False, "error": str(e), "session_id": session_id}

        if not session:
            return {"success": False, "error": "No session found", "session_id": session_id}

        # --- Agent-authored fast path ---
        if content is not None:
            session_manager.update_summary(session.id, summary_markdown=content)

            if set_handoff_ready:
                session_manager.update_status(session.id, "handoff_ready")

            result: dict[str, Any] = {
                "success": True,
                "session_id": session.id,
                "mode": "agent_authored",
                "summary_length": len(content),
            }

            if to_session:
                result["send_result"] = _send_to_peer(session.id, to_session, content)

            return result

        # --- Automated fallback ---
        import json
        import subprocess  # nosec B404 - subprocess needed for git commands
        from pathlib import Path

        from gobby.sessions.analyzer import TranscriptAnalyzer
        from gobby.workflows.context_actions import format_handoff_as_markdown
        from gobby.workflows.git_utils import get_file_changes, get_git_diff_summary
        from gobby.workflows.summary_actions import (
            _format_structured_context,
            format_turns_for_llm,
        )

        # Get transcript path
        transcript_path = session.jsonl_path
        if not transcript_path:
            return {
                "success": False,
                "error": "No transcript path for session",
                "session_id": session.id,
            }

        path = Path(transcript_path)
        if not path.exists():
            return {
                "success": False,
                "error": "Transcript file not found",
                "path": transcript_path,
            }

        # Read and parse transcript
        turns = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    turns.append(json.loads(line))

        # Analyze transcript
        analyzer = TranscriptAnalyzer()
        handoff_ctx = analyzer.extract_handoff_context(turns)

        # Enrich with real-time git status
        if not handoff_ctx.git_status:
            try:
                result_proc = subprocess.run(  # nosec B603 B607 - hardcoded git command
                    ["git", "status", "--short"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=path.parent,
                )
                handoff_ctx.git_status = (
                    result_proc.stdout.strip() if result_proc.returncode == 0 else ""
                )
            except Exception as e:
                import logging

                logging.getLogger(__name__).debug("Git status is optional, failed: %s", e)

        # Get recent git commits
        try:
            result_proc = subprocess.run(  # nosec B603 B607 - hardcoded git command
                ["git", "log", "--oneline", "-10", "--format=%H|%s"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=path.parent,
            )
            if result_proc.returncode == 0:
                commits = []
                for line in result_proc.stdout.strip().split("\n"):
                    if "|" in line:
                        hash_val, message = line.split("|", 1)
                        commits.append({"hash": hash_val, "message": message})
                if commits:
                    handoff_ctx.git_commits = commits
        except Exception as e:
            import logging

            logging.getLogger(__name__).debug("Git log is optional, failed: %s", e)

        # Determine what to generate (neither flag = both)
        generate_compact = compact or not full
        generate_full = full or not compact

        # Generate content
        compact_markdown = None
        full_markdown = None
        full_error = None

        if generate_compact:
            compact_markdown = format_handoff_as_markdown(handoff_ctx)

        if generate_full:
            try:
                # Use injected llm_service, fallback to ClaudeLLMProvider
                provider = None
                if llm_service:
                    provider = llm_service.get_default_provider()
                if not provider:
                    from gobby.config.app import load_config
                    from gobby.llm.claude import ClaudeLLMProvider

                    config = load_config()
                    provider = ClaudeLLMProvider(config)

                # Use injected transcript_processor, fallback to ClaudeTranscriptParser
                parser = transcript_processor
                if not parser:
                    from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

                    parser = ClaudeTranscriptParser()

                # Get prompt template
                prompt_template = None
                try:
                    from gobby.prompts.loader import PromptLoader

                    loader = PromptLoader(db=session_manager.db)
                    prompt_obj = loader.load("handoff/session_end")
                    prompt_template = prompt_obj.content
                except FileNotFoundError:
                    pass

                if not prompt_template:
                    raise ValueError("No prompt template found for handoff/session_end")

                # Prepare context for LLM
                last_turns = parser.extract_turns_since_clear(turns, max_turns=50)
                last_messages = parser.extract_last_messages(turns, num_pairs=2)
                last_messages_str = format_turns_for_llm(last_messages) if last_messages else ""

                # Gather real git data (matches summary_actions.generate_summary)
                file_changes = get_file_changes()
                git_diff_summary = get_git_diff_summary()
                structured_context = _format_structured_context(handoff_ctx)

                context = {
                    "transcript_summary": format_turns_for_llm(last_turns),
                    "last_messages": last_messages_str,
                    "git_status": handoff_ctx.git_status or "",
                    "file_changes": file_changes,
                    "git_diff_summary": git_diff_summary,
                    "structured_context": structured_context,
                    "external_id": session.id[:12],
                    "session_id": session.id,
                    "session_source": session.source,
                }

                full_markdown = await provider.generate_summary(
                    context, prompt_template=prompt_template
                )

            except Exception as e:
                full_error = str(e)
                if full and not compact:
                    return {
                        "success": False,
                        "error": f"Failed to generate full summary: {e}",
                        "session_id": session.id,
                    }

        # Always save to database
        if compact_markdown:
            session_manager.update_compact_markdown(session.id, compact_markdown)
        if full_markdown:
            session_manager.update_summary(session.id, summary_markdown=full_markdown)

        # Set handoff_ready status
        if set_handoff_ready:
            session_manager.update_status(session.id, "handoff_ready")

        # Save to file if requested
        files_written: list[str] = []
        if write_file:
            from gobby.workflows.summary_actions import _write_summary_file

            if full_markdown:
                full_path: str | None = await _write_summary_file(
                    session.id, full_markdown, output_path, session_manager, mode="full"
                )
                if full_path:
                    files_written.append(full_path)

            if compact_markdown:
                compact_path: str | None = await _write_summary_file(
                    session.id, compact_markdown, output_path, session_manager, mode="compact"
                )
                if compact_path:
                    files_written.append(compact_path)

        result_dict: dict[str, Any] = {
            "success": True,
            "session_id": session.id,
            "mode": "automated",
            "compact_length": len(compact_markdown) if compact_markdown else 0,
            "full_length": len(full_markdown) if full_markdown else 0,
            "full_error": full_error,
            "files_written": files_written,
            "context_summary": {
                "has_active_task": bool(handoff_ctx.active_gobby_task),
                "files_modified_count": len(handoff_ctx.files_modified),
                "git_commits_count": len(handoff_ctx.git_commits),
                "has_initial_goal": bool(handoff_ctx.initial_goal),
            },
        }

        # Send to peer if requested
        if to_session:
            send_content = full_markdown or compact_markdown or ""
            if send_content:
                result_dict["send_result"] = _send_to_peer(session.id, to_session, send_content)

        return result_dict

    @registry.tool(
        name="get_handoff_context",
        description=(
            "Get handoff context from a session. Finds sessions by ID, project/source, "
            "or most recent handoff_ready.\n"
            "Accepts #N, N, UUID, or prefix for session_id and link_child_session_id."
        ),
    )
    def get_handoff_context(
        session_id: str | None = None,
        project_id: str | None = None,
        source: str | None = None,
        link_child_session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Retrieve handoff context from a session.

        Args:
            session_id: Session reference - supports #N, N (seq_num), UUID, or prefix (optional)
            project_id: Project ID to find parent session in (optional)
            source: Filter by CLI source - claude, gemini, codex, cursor, windsurf, copilot (optional)
            link_child_session_id: Session to link as child - supports #N, N, UUID, or prefix (optional)

        Returns:
            Handoff context markdown and session metadata
        """
        from gobby.utils.machine_id import get_machine_id

        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        parent_session = None

        # Option 1: Direct session_id lookup with resolution
        if session_id:
            try:
                resolved_id = _resolve_session_id(session_id)
                parent_session = session_manager.get(resolved_id)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        # Option 2: Find parent by project_id and source
        if not parent_session and project_id:
            machine_id = get_machine_id()
            if machine_id:
                parent_session = session_manager.find_parent(
                    machine_id=machine_id,
                    project_id=project_id,
                    source=source,
                    status="handoff_ready",
                )

        # Option 3: Find most recent handoff_ready session
        if not parent_session:
            sessions = session_manager.list(status="handoff_ready", limit=1)
            parent_session = sessions[0] if sessions else None

        if not parent_session:
            return {
                "success": False,
                "found": False,
                "message": "No handoff-ready session found",
                "filters": {
                    "session_id": session_id,
                    "project_id": project_id,
                    "source": source,
                },
            }

        # Get handoff context (prefer summary_markdown, fall back to compact_markdown)
        context = parent_session.summary_markdown or parent_session.compact_markdown

        if not context:
            return {
                "success": False,
                "found": True,
                "session_id": parent_session.id,
                "has_context": False,
                "message": "Session found but has no handoff context",
            }

        # Optionally link child session (resolve if using #N format)
        resolved_child_id = None
        if link_child_session_id:
            try:
                resolved_child_id = _resolve_session_id(link_child_session_id)
                session_manager.update_parent_session_id(resolved_child_id, parent_session.id)
            except ValueError as e:
                return {
                    "success": False,
                    "found": True,
                    "session_id": parent_session.id,
                    "has_context": True,
                    "error": f"Failed to resolve child session '{link_child_session_id}': {e}",
                    "context": context,
                }

        return {
            "success": True,
            "found": True,
            "session_id": parent_session.id,
            "has_context": True,
            "context": context,
            "context_type": (
                "summary_markdown" if parent_session.summary_markdown else "compact_markdown"
            ),
            "parent_title": parent_session.title,
            "parent_status": parent_session.status,
            "linked_child": resolved_child_id or link_child_session_id,
        }
