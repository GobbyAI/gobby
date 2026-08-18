"""Bounded LLM criteria review for the task-close checklist."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.prompts import PromptLoader
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.close_verdict import CloseVerdict, parse_close_verdict
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.generation_schemas import TASK_CLOSE_VALIDATION_SCHEMA
from gobby.tasks.validation_evidence import ValidationEvidenceTooLarge, build_close_diff_evidence

logger = logging.getLogger(__name__)

# Closure reasons that require no repository change: the criteria review judges
# the disposition justification instead of literal criterion satisfaction.
NO_WORK_CLOSE_REASONS: frozenset[str] = frozenset(
    {"duplicate", "already_implemented", "wont_fix", "obsolete", "out_of_repo"}
)


class ValidationPromptTooLarge(ValueError):
    """The full criteria and complete manifest cannot fit in the prompt contract."""


class TaskValidator:
    """Run one bounded criteria-vs-work coherence review."""

    def __init__(
        self,
        config: TaskValidationConfig,
        llm_service: LLMService,
        db: HubDatabase,
    ) -> None:
        self.config = config
        self.llm_service = llm_service
        self._loader = PromptLoader(db=db)

    async def validate_task(
        self,
        *,
        task_id: str,
        title: str,
        changes_summary: str,
        validation_criteria: str,
        diff_text: str | None,
        checklist_facts: Mapping[str, object],
        closure_reason: str = "completed",
    ) -> CloseVerdict:
        """Review all criteria once against a bounded work summary and linked diff."""
        if not self.config.enabled:
            raise RuntimeError("Task-close criteria review is disabled.")

        criteria = split_validation_criteria(validation_criteria)
        if not criteria:
            raise ValueError("Task-close criteria review requires explicit validation criteria.")

        try:
            diff_evidence = build_close_diff_evidence(
                diff_text,
                criteria=validation_criteria,
            )
        except ValidationEvidenceTooLarge as exc:
            raise ValidationPromptTooLarge(str(exc)) from exc
        criteria_text = "\n".join(
            f"{index}. {criterion}" for index, criterion in enumerate(criteria, start=1)
        )
        facts_text = json.dumps(checklist_facts, sort_keys=True, separators=(",", ":"), default=str)
        prompt = self._loader.render(
            self.config.prompt_path or "validation/validate",
            {
                "title": title,
                "closure_reason": closure_reason.strip() or "completed",
                "criteria_text": criteria_text,
                "changes_summary": changes_summary.strip(),
                "diff_evidence": diff_evidence.text,
                "checklist_facts": facts_text,
            },
        )
        prompt_chars = len(prompt)
        prompt_limit = self.config.close_review_prompt_max_chars
        if prompt_chars > prompt_limit:
            raise ValidationPromptTooLarge(
                f"Task-close criteria-review prompt is {prompt_chars} characters, exceeding the "
                f"configured limit of {prompt_limit} characters at "
                "gobby-tasks.validation.close_review_prompt_max_chars. Split the task into "
                "smaller tasks and preserve every validation criterion."
            )

        logger.debug(
            "Running bounded close criteria review for task %s "
            "(prompt_chars=%d manifest_files=%d excerpt_chars=%d)",
            task_id,
            len(prompt),
            diff_evidence.manifest_count,
            diff_evidence.excerpt_chars,
        )
        payload = await self.llm_service.call_json_feature(
            self.config,
            prompt,
            system_prompt=self.config.system_prompt,
            json_schema=TASK_CLOSE_VALIDATION_SCHEMA,
            caller="tasks.close_checklist",
        )
        return parse_close_verdict(payload, criteria)


__all__ = [
    "TaskValidator",
    "ValidationPromptTooLarge",
]
