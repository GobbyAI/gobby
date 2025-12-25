"""
Task validation module.

Handles validating task completion against acceptance criteria
using LLM providers.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from gobby.config.app import TaskValidationConfig
from gobby.llm import LLMService

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of task validation."""

    status: Literal["valid", "invalid", "pending"]
    feedback: str | None = None


class TaskValidator:
    """Validates task completion using LLM."""

    def __init__(self, config: TaskValidationConfig, llm_service: LLMService):
        self.config = config
        self.llm_service = llm_service

    async def validate_task(
        self,
        task_id: str,
        title: str,
        original_instruction: str | None,
        changes_summary: str,
    ) -> ValidationResult:
        """
        Validate task completion.

        Args:
            task_id: Task ID
            title: Task title
            original_instruction: Original user instruction/request
            changes_summary: Summary of changes made (files, diffs, etc.)

        Returns:
            ValidationResult with status and feedback
        """
        if not self.config.enabled:
            return ValidationResult(status="pending", feedback="Validation disabled")

        if not original_instruction:
            logger.warning(f"Cannot validate task {task_id}: missing original instruction")
            return ValidationResult(status="pending", feedback="Missing original instruction")

        logger.info(f"Validating task {task_id}: {title}")

        prompt = self.config.prompt or (
            "Validate if the following changes satisfy the original instruction.\n"
            "Return ONLY a valid JSON object with 'status' ('valid' or 'invalid') "
            "and 'feedback' (string explanation).\n\n"
            f"Original Instruction: {original_instruction}\n"
            f"Task: {title}\n"
            f"Changes:\n{changes_summary}"
        )

        try:
            provider = self.llm_service.get_provider(self.config.provider)
            response_content = await provider.generate_text(
                prompt=prompt,
                system_prompt="You are a QA engineer. Validate work strictly against requirements.",
                model=self.config.model,
            )

            import json

            content = response_content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content.rsplit("\n", 1)[0]
                if content.startswith("json"):
                    content = content[4:].strip()

            result_data = json.loads(content)

            return ValidationResult(
                status=result_data.get("status", "pending"), feedback=result_data.get("feedback")
            )

        except Exception as e:
            logger.error(f"Failed to validate task {task_id}: {e}")
            return ValidationResult(status="pending", feedback=f"Validation failed: {str(e)}")
