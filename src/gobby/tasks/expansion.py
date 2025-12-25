"""
Task expansion module.

Handles breaking down high-level tasks into smaller, actionable subtasks
using LLM providers.
"""

import logging
from typing import Any

from gobby.config.app import TaskExpansionConfig
from gobby.llm import LLMService

logger = logging.getLogger(__name__)


class TaskExpander:
    """Expands tasks into subtasks using LLM."""

    def __init__(self, config: TaskExpansionConfig, llm_service: LLMService):
        self.config = config
        self.llm_service = llm_service

    async def expand_task(
        self,
        task_id: str,
        title: str,
        description: str | None = None,
        context: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Expand a task into subtasks.

        Args:
            task_id: ID of the task to expand
            title: Task title
            description: Task description
            context: Additional context for expansion

        Returns:
            List of subtask dictionaries with title and description
        """
        if not self.config.enabled:
            logger.info("Task expansion disabled, skipping")
            return []

        logger.info(f"Expanding task {task_id}: {title}")

        # Construct prompt
        prompt = self.config.prompt or (
            "Break down the following task into 3-5 smaller, actionable subtasks.\n"
            "Return ONLY a valid JSON list of objects with 'title' and 'description' keys.\n\n"
            f"Task: {title}\n"
            f"Description: {description or 'None'}\n"
            f"Context: {context or 'None'}"
        )

        try:
            # Call LLM
            provider = self.llm_service.get_provider(self.config.provider)
            response_content = await provider.generate_text(
                prompt=prompt,
                system_prompt="You are a technical project manager. Break down tasks effectively.",
                model=self.config.model,
            )

            # Parse response (expected to be JSON list)
            # In a real implementation, we'd need robust JSON parsing here
            # For now, we assume the LLM follows instructions or we use a structured output parser
            # This is a placeholder for the actual parsing logic
            import json

            content = response_content.strip()
            # Handle potential markdown code blocks
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content.rsplit("\n", 1)[0]
                if content.startswith("json"):
                    content = content[4:].strip()

            subtasks = json.loads(content)

            if not isinstance(subtasks, list):
                logger.warning(f"LLM returned non-list for task expansion: {type(subtasks)}")
                return []

            return subtasks

        except Exception as e:
            logger.error(f"Failed to expand task {task_id}: {e}")
            return []
