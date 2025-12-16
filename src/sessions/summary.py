"""
Summary Generator for session summaries and title synthesis (local-first).

Handles:
- Session summary generation from JSONL transcripts using LLM synthesis
- Session title synthesis from user prompts
- Storage in database and markdown files
"""

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from gobby.llm.base import LLMProvider
from gobby.llm.claude import ClaudeLLMProvider
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService
    from gobby.llm.service import LLMService
    from gobby.storage.sessions import LocalSessionManager
from gobby.storage.session_tasks import SessionTaskManager

# Backward-compatible alias
TranscriptProcessor = ClaudeTranscriptParser


class SummaryGenerator:
    """
    Generates session summaries using LLM synthesis (local-first).

    Handles:
    - Comprehensive session summary generation from JSONL transcripts
    - Session title synthesis from first user prompt
    - Summary storage in database and markdown files
    - Git status and file change tracking
    - TodoWrite list extraction and inclusion
    """

    def __init__(
        self,
        session_storage: "LocalSessionManager",
        transcript_processor: ClaudeTranscriptParser,
        summary_file_path: str = "~/.gobby/session_summaries",
        logger_instance: logging.Logger | None = None,
        llm_service: "LLMService | None" = None,
        config: "DaemonConfig | None" = None,
    ) -> None:
        """
        Initialize SummaryGenerator.

        Args:
            session_storage: LocalSessionManager for SQLite operations
            transcript_processor: Processor for JSONL transcript parsing
            summary_file_path: Directory path for session summary files
            logger_instance: Optional logger instance
            llm_service: Optional LLMService for multi-provider support
            config: Optional DaemonConfig instance for feature configuration
        """
        self._storage = session_storage
        self._transcript_processor = transcript_processor
        self._summary_file_path = summary_file_path
        self.logger = logger_instance or logging.getLogger(__name__)
        self._llm_service = llm_service
        self._config = config

        # Initialize LLM provider from llm_service or create default
        self.llm_provider: LLMProvider | None = None

        if llm_service:
            try:
                self.llm_provider = llm_service.get_default_provider()
                self.logger.debug(
                    f"Using '{self.llm_provider.provider_name}' provider for SummaryGenerator"
                )
            except ValueError as e:
                self.logger.warning(f"LLMService has no providers: {e}")

        if not self.llm_provider:
            # Fallback to ClaudeLLMProvider
            try:
                from gobby.config.app import load_config

                config = config or load_config()
                self._config = config
                self.llm_provider = ClaudeLLMProvider(config)
                self.logger.debug("Initialized default ClaudeLLMProvider for SummaryGenerator")
            except Exception as e:
                self.logger.error(f"Failed to initialize default LLM provider: {e}")

    def _get_provider_for_feature(
        self, feature_name: str
    ) -> tuple["LLMProvider | None", str | None]:
        """
        Get LLM provider and prompt for a specific feature.

        Args:
            feature_name: Feature name (e.g., "session_summary", "title_synthesis")

        Returns:
            Tuple of (provider, prompt) where prompt is from feature config.
            Returns (None, None) if feature is disabled.
        """
        if not self._config:
            return self.llm_provider, None

        # Try to get feature-specific config
        try:
            if feature_name == "session_summary":
                feature_config = self._config.session_summary
            elif feature_name == "title_synthesis":
                feature_config = self._config.title_synthesis
            else:
                return self.llm_provider, None

            if not feature_config:
                return self.llm_provider, None

            # Check if feature is enabled
            if not getattr(feature_config, "enabled", True):
                self.logger.debug(f"Feature '{feature_name}' is disabled in config")
                return None, None

            # Get provider from LLMService if available
            provider_name = getattr(feature_config, "provider", None)
            prompt = getattr(feature_config, "prompt", None)

            if self._llm_service and provider_name:
                try:
                    provider = self._llm_service.get_provider(provider_name)
                    self.logger.debug(f"Using provider '{provider_name}' for {feature_name}")
                    return provider, prompt
                except ValueError as e:
                    self.logger.warning(
                        f"Provider '{provider_name}' not available for {feature_name}: {e}"
                    )

            return self.llm_provider, prompt

        except Exception as e:
            self.logger.warning(f"Failed to get feature config for {feature_name}: {e}")
            return self.llm_provider, None

    def generate_session_summary(
        self, session_id: str, input_data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Generate comprehensive LLM-powered session summary from JSONL transcript.

        Args:
            session_id: Internal database UUID (sessions.id), not cli_key
            input_data: Session end input data containing cli_key and transcript_path

        Returns:
            Dict with status and summary metadata
        """
        external_id = None
        try:
            # Extract external_id from input_data
            external_id = input_data.get("session_id")
            if not external_id:
                self.logger.error(f"No external_id in input_data for session_id={session_id}")
                return {"status": "no_external_id", "session_id": session_id}

            # Source is hardcoded since all hook calls are from Claude Code
            session_source = "Claude Code"

            # Get transcript path
            transcript_path = input_data.get("transcript_path")
            if not transcript_path:
                self.logger.warning(f"No transcript path found for session {external_id}")
                return {"status": "no_transcript", "external_id": external_id}

            # Read JSONL transcript
            transcript_file = Path(transcript_path)
            if not transcript_file.exists():
                self.logger.warning(f"Transcript file not found: {transcript_path}")
                return {"status": "transcript_not_found", "path": transcript_path}

            # Parse JSONL and extract last 50 turns
            turns = []
            with open(transcript_file) as f:
                for line in f:
                    if line.strip():
                        turns.append(json.loads(line))

            # Get turns since last /clear (up to 50 turns)
            last_turns = self._transcript_processor.extract_turns_since_clear(turns, max_turns=50)

            # Get last two user<>agent message pairs
            last_messages = self._transcript_processor.extract_last_messages(
                last_turns, num_pairs=2
            )

            # Extract last TodoWrite tool call
            todowrite_list = self._extract_last_todowrite(last_turns)

            # Get tasks associated with this session
            session_tasks_str = None
            if session_id:
                try:
                    task_manager = SessionTaskManager(self._storage.db)
                    tasks = task_manager.get_session_tasks(session_id)
                    if tasks:
                        task_lines = []
                        for item in tasks:
                            task = item["task"]
                            action = item["action"]
                            icon = "[x]" if task.status == "closed" else "[ ]"
                            task_lines.append(
                                f"- {icon} **{task.title}** ({task.status}) - *{action}*"
                            )
                        session_tasks_str = "\n".join(task_lines)
                except Exception as e:
                    self.logger.warning(f"Failed to fetch session tasks: {e}")

            # Get git status and file changes
            git_status = self._get_git_status()
            file_changes = self._get_file_changes()

            # Generate summary using LLM
            summary_markdown = self._generate_summary_with_llm(
                last_turns=last_turns,
                last_messages=last_messages,
                git_status=git_status,
                file_changes=file_changes,
                external_id=external_id,
                session_id=session_id,
                session_source=session_source,
                todowrite_list=todowrite_list,
                session_tasks_str=session_tasks_str,
            )

            # Store summary in multiple locations
            file_result = None
            db_result = None

            if session_id:
                # ALWAYS write to file (for file-based restore)
                file_result = self.write_summary_to_file(session_id, summary_markdown)

                # ALWAYS update database (store both path and markdown content)
                db_result = self.update_summary_in_database(
                    session_id,
                    summary_path=file_result,
                    summary_markdown=summary_markdown,
                )
            else:
                self.logger.warning(
                    f"Cannot store summary: no sessions.id for external_id={external_id}"
                )

            return {
                "status": "success",
                "external_id": external_id,
                "file_written": file_result,
                "db_updated": db_result,
                "summary_length": len(summary_markdown),
            }

        except Exception as e:
            self.logger.error(f"Failed to create session summary: {e}", exc_info=True)
            return {"status": "error", "error": str(e), "external_id": external_id}

    def synthesize_title(
        self,
        session_id: str,
        external_id: str,
        user_prompt: str,
        source: str = "claude",
        machine_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Synthesize session title and update in database if title is null.

        Args:
            session_id: Internal database UUID (sessions.id)
            external_id: External session identifier
            user_prompt: User's prompt text
            source: Session source (claude, codex, gemini)
            machine_id: Machine identifier

        Returns:
            Dict with status and title
        """
        try:
            # Fetch current session to check if title is null
            session = self._storage.get(session_id)
            if not session:
                self.logger.error(f"Session {session_id} not found")
                return {"status": "error", "title": None}

            # If title already exists, skip synthesis
            if session.title:
                self.logger.debug(f"Session {session_id} already has title: '{session.title}'")
                return {"status": "already_set", "title": session.title}

            # Get feature-specific provider and prompt
            provider, config_prompt = self._get_provider_for_feature("title_synthesis")

            if not provider:
                self.logger.warning("LLM provider not available - skipping title synthesis")
                return {"status": "llm_unavailable", "title": None}

            self.logger.debug(f"Synthesizing title for session {session_id}")

            # Use config prompt template if available
            if config_prompt:
                prompt_template = config_prompt
            else:
                prompt_template = """Generate a concise 3-6 word title summarizing the user's intent.

User's first message: {user_prompt}

Requirements:
- Use title case
- Be specific and descriptive
- 3-6 words maximum
- Focus on the task/goal

Examples:
- "Fix JWT Authentication Bug"
- "Implement User Dashboard"
- "Debug Database Connection Issue"
- "Refactor Payment Processing"

Respond with ONLY the title, no explanation."""

            synthesized_title: str | None = None
            try:

                async def _run_title() -> str | None:
                    result: str | None = await provider.synthesize_title(
                        user_prompt=user_prompt,
                        prompt_template=prompt_template,
                    )
                    return result

                synthesized_title = anyio.run(_run_title)
            except Exception as e:
                self.logger.error(f"Failed to synthesize title with LLM provider: {e}")
                return {"status": "synthesis_failed", "title": None}

            if not synthesized_title:
                self.logger.warning(f"Title synthesis failed for session {session_id}")
                return {"status": "synthesis_failed", "title": None}

            self.logger.debug(f"Synthesized session title: '{synthesized_title}'")

            # Update title in database
            updated_session = self._storage.update_title(session_id, synthesized_title)
            if updated_session:
                return {"status": "success", "title": synthesized_title}
            else:
                self.logger.error(f"Failed to update title in database for session {session_id}")
                return {"status": "update_failed", "title": synthesized_title}

        except Exception as e:
            self.logger.exception(f"Failed to synthesize/update title: {e}")
            return {"status": "error", "title": None}

    def write_summary_to_file(self, session_id: str, summary: str) -> str | None:
        """
        Write session summary to markdown file.

        Args:
            session_id: Internal database UUID (sessions.id)
            summary: Markdown summary content

        Returns:
            Path to written file, or None on failure
        """
        try:
            # Create summary directory from config
            summary_dir = Path(self._summary_file_path).expanduser()
            summary_dir.mkdir(parents=True, exist_ok=True)

            # Write markdown file with Unix timestamp for chronological sorting
            timestamp = int(time.time())
            summary_file = summary_dir / f"session_{timestamp}_{session_id}.md"
            summary_file.write_text(summary, encoding="utf-8")

            self.logger.debug(f"Session summary written to: {summary_file}")
            return str(summary_file)

        except Exception as e:
            self.logger.exception(f"Failed to write summary file: {e}")
            return None

    def update_summary_in_database(
        self,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> bool:
        """
        Update session summary in database.

        Args:
            session_id: Internal database UUID (sessions.id)
            summary_path: Path to summary file
            summary_markdown: Summary markdown content

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            session = self._storage.update_summary(
                session_id,
                summary_path=summary_path,
                summary_markdown=summary_markdown,
            )
            if session:
                self.logger.debug(f"Session summary updated in database: {session_id}")
                return True
            else:
                self.logger.error("Failed to update summary in database: session not found")
                return False

        except Exception as e:
            self.logger.exception(f"Failed to update summary in database: {e}")
            return False

    def _generate_summary_with_llm(
        self,
        last_turns: list[dict],
        last_messages: list[dict],
        git_status: str,
        file_changes: str,
        external_id: str,
        session_id: str | None,
        session_source: str | None,
        todowrite_list: str | None = None,
        session_tasks_str: str | None = None,
    ) -> str:
        """
        Generate session summary using LLM provider.

        Args:
            last_turns: List of recent transcript turns
            last_messages: List of last user<>agent message pairs
            git_status: Git status output
            file_changes: Formatted file changes
            external_id: Claude Code session key
            session_id: Internal database UUID
            session_source: Session source (e.g., "Claude Code")
            todowrite_list: Optional TodoWrite list markdown
            session_tasks_str: Optional formatted session tasks list

        Returns:
            Formatted markdown summary
        """
        # Get feature-specific provider and prompt
        provider, prompt = self._get_provider_for_feature("session_summary")

        if not provider:
            return "Session summary unavailable (LLM provider not initialized)"

        # Prepare context
        transcript_summary = self._format_turns_for_llm(last_turns)

        context = {
            "transcript_summary": transcript_summary,
            "last_messages": last_messages,
            "git_status": git_status,
            "file_changes": file_changes,
            "todowrite_list": todowrite_list,
            "session_tasks": session_tasks_str,
            "external_id": external_id,
            "session_id": session_id,
            "session_source": session_source,
        }

        try:

            async def _run_gen() -> str:
                if prompt:
                    result: str = await provider.generate_summary(context, prompt_template=prompt)
                    return result
                result = await provider.generate_summary(context)
                return result

            llm_summary: str = anyio.run(_run_gen)

            if not llm_summary:
                raise RuntimeError("LLM summary generation failed - no summary produced")

            # Build header
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

            if session_id and session_source:
                header = f"# Session Summary\nSession ID:     {session_id}\n{session_source} ID: {external_id}\nGenerated:      {timestamp}\n\n"
            elif session_id:
                header = f"# Session Summary\nSession ID:     {session_id}\nClaude Code ID: {external_id}\nGenerated:      {timestamp}\n\n"
            else:
                header = f"# Session Summary\nClaude Code ID: {external_id}\nGenerated:      {timestamp}\n\n"

            final_summary = header + llm_summary

            # Insert TodoWrite list if it exists
            if todowrite_list:
                todo_section_marker = "## Claude's Todo List"
                if todo_section_marker in final_summary:
                    parts = final_summary.split(todo_section_marker)
                    if len(parts) == 2:
                        next_section_idx = parts[1].find("\n##")
                        if next_section_idx != -1:
                            after_next = parts[1][next_section_idx:]
                            final_summary = (
                                f"{parts[0]}{todo_section_marker}\n{todowrite_list}\n{after_next}"
                            )
                        else:
                            final_summary = f"{parts[0]}{todo_section_marker}\n{todowrite_list}"
                else:
                    # Fallback: insert before Next Steps
                    if "## Next Steps" in final_summary:
                        parts = final_summary.split("## Next Steps", 1)
                        final_summary = f"{parts[0]}\n## Claude's Todo List\n{todowrite_list}\n\n## Next Steps{parts[1]}"
                    else:
                        final_summary = (
                            f"{final_summary}\n\n## Claude's Todo List\n{todowrite_list}"
                        )

            # Insert Session Tasks if available
            if session_tasks_str:
                final_summary += f"\n\n## Tasks Activity\n{session_tasks_str}"

            return final_summary

        except Exception as e:
            self.logger.error(f"LLM summary generation failed: {e}", exc_info=True)
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

            if session_id and session_source:
                error_header = f"# Session Summary\nSession ID:     {session_id}\n{session_source} ID: {external_id}\nGenerated:      {timestamp}\n\n"
            elif session_id:
                error_header = f"# Session Summary\nSession ID:     {session_id}\nClaude Code ID: {external_id}\nGenerated:      {timestamp}\n\n"
            else:
                error_header = f"# Session Summary\nClaude Code ID: {external_id}\nGenerated:      {timestamp}\n\n"

            error_summary = error_header + f"Error generating summary: {str(e)}"

            if todowrite_list:
                error_summary = f"{error_summary}\n\n## Claude's Todo List\n{todowrite_list}"

            return error_summary

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

    def _extract_last_todowrite(self, turns: list[dict]) -> str | None:
        """
        Extract the last TodoWrite tool call's todos list from transcript.

        Args:
            turns: List of transcript turns

        Returns:
            Formatted markdown string with todo list, or None if not found
        """
        # Scan turns in reverse to find most recent TodoWrite
        for turn in reversed(turns):
            message = turn.get("message", {})
            content = message.get("content", [])

            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") == "TodoWrite":
                            tool_input = block.get("input", {})
                            todos = tool_input.get("todos", [])

                            if not todos:
                                return None

                            # Format as markdown checklist
                            lines = []
                            for todo in todos:
                                content_text = todo.get("content", "")
                                status = todo.get("status", "pending")

                                # Map status to checkbox style
                                if status == "completed":
                                    checkbox = "[x]"
                                elif status == "in_progress":
                                    checkbox = "[>]"
                                else:
                                    checkbox = "[ ]"

                                lines.append(f"- {checkbox} {content_text} ({status})")

                            return "\n".join(lines)

        return None

    def _get_git_status(self) -> str:
        """
        Get git status for current directory.

        Returns:
            Git status output or error message
        """
        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return "Not a git repository or git not available"

    def _get_file_changes(self) -> str:
        """
        Get detailed file changes from git.

        Returns:
            Formatted file changes or error message
        """
        try:
            # Get changed files with status
            diff_result = subprocess.run(
                ["git", "diff", "HEAD", "--name-status"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Get untracked files
            untracked_result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Combine results
            changes = []
            if diff_result.stdout.strip():
                changes.append("Modified/Deleted:")
                changes.append(diff_result.stdout.strip())

            if untracked_result.stdout.strip():
                changes.append("\nUntracked:")
                changes.append(untracked_result.stdout.strip())

            return "\n".join(changes) if changes else "No changes"

        except Exception:
            return "Unable to determine file changes"
