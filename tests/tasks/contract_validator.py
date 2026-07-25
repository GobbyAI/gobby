"""Compatibility helpers for validator tests focused on non-verdict behavior."""

from __future__ import annotations

from typing import Any, cast

from gobby.llm import LLMService
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.validation import TaskValidator

_DEFAULT_CRITERION = "The requested task behavior is observable."
_DEFAULT_EVIDENCE_ID = "test-evidence-1"


class _ContractResponseLLM:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.criteria = _DEFAULT_CRITERION

    async def call_json_feature(self, *args: Any, **kwargs: Any) -> Any:
        response = await self.delegate.call_json_feature(*args, **kwargs)
        if not isinstance(response, dict) or "criterion_results" in response:
            return response
        status = response.get("status")
        if status not in {"valid", "invalid"}:
            return response
        criterion_status = "satisfied" if status == "valid" else "gap"
        evidence_ids = [_DEFAULT_EVIDENCE_ID] if status == "valid" else []
        explanation = str(response.get("feedback") or "Criterion result supplied by test fixture.")
        return {
            **response,
            "criterion_results": [
                {
                    "criterion": criterion,
                    "status": criterion_status,
                    "evidence_ids": evidence_ids,
                    "explanation": explanation,
                }
                for criterion in split_validation_criteria(self.criteria)
            ],
        }


class ContractTaskValidator(TaskValidator):
    """Supply explicit criteria/evidence to legacy tests of unrelated behavior."""

    def __init__(self, config: Any, llm_service: Any, **kwargs: Any) -> None:
        self._contract_llm = _ContractResponseLLM(llm_service)
        super().__init__(config, cast(LLMService, self._contract_llm), **kwargs)

    async def validate_task(self, *args: Any, **kwargs: Any) -> Any:
        criteria = kwargs.get("validation_criteria") or _DEFAULT_CRITERION
        kwargs["validation_criteria"] = criteria
        kwargs.setdefault("admissible_evidence_ids", (_DEFAULT_EVIDENCE_ID,))
        self._contract_llm.criteria = criteria
        return await super().validate_task(*args, **kwargs)
