"""External validator for objective task validation.

Provides a separate validation path using either:
1. A fresh LLM context (direct API calls) - mode: "llm"
2. An in-process agent instance with tools - mode: "agent"
3. A spawned headless agent process - mode: "spawn"

All modes ensure the validator has no prior knowledge of the implementation.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.prompts import PromptLoader
from gobby.storage.database import DatabaseProtocol
from gobby.tasks.commits import (
    extract_mentioned_files,
    extract_mentioned_symbols,
    summarize_diff_for_validation,
)
from gobby.tasks.issue_extraction import parse_issues_from_response
from gobby.tasks.validation_models import Issue
from gobby.utils.json_helpers import extract_json_object

if TYPE_CHECKING:
    from typing import Protocol

    from gobby.agents.runner import AgentRunner

    class AgentSpawner(Protocol):
        """Protocol for agent spawning interface (gobby-agents)."""

        async def spawn_agent(self, **kwargs: Any) -> dict[str, Any]:
            """Spawn a new agent process."""
            ...

        async def get_agent_result(self, agent_id: str, **kwargs: Any) -> dict[str, Any]:
            """Get the result of a completed agent run."""
            ...


logger = logging.getLogger(__name__)

# Default system prompt for external validators


# Module-level loader (initialized lazily)
_loader: PromptLoader | None = None


def _get_loader(db: DatabaseProtocol | None = None) -> PromptLoader:
    """Get or create the module-level PromptLoader."""
    global _loader
    if _loader is None:
        _loader = PromptLoader(db=db)

    return _loader


@dataclass
class ExternalValidationResult:
    """Result from external validation.

    Used by QA loop to determine next action:
    - status="valid": Task can be marked complete
    - status="invalid": Task should be retried with issues as feedback
    - status="error": Validation failed (timeout, crash, etc.) - may retry or escalate
    - status="skipped": Validation was skipped (disabled in config)

    Attributes:
        status: Validation status - "valid", "invalid", "error", "skipped", or "pending"
        summary: Human-readable summary of validation result
        issues: List of structured issues found (actionable feedback for implementation agent)
        error: Error message if status is "error"
    """

    status: str
    summary: str
    issues: list[Issue] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        """Whether validation passed (status is 'valid')."""
        return self.status == "valid"

    def format_issues_for_feedback(self) -> str:
        """Format issues as actionable feedback for implementation agent.

        Returns a formatted string suitable for including in a prompt to the
        implementation agent, describing what needs to be fixed.
        """
        if not self.issues:
            return ""

        lines = ["## Validation Issues\n"]
        for i, issue in enumerate(self.issues, 1):
            lines.append(f"### Issue {i}: {issue.title}")
            if hasattr(issue, "severity"):
                lines.append(f"**Severity:** {issue.severity}")
            if hasattr(issue, "issue_type"):
                lines.append(f"**Type:** {issue.issue_type}")
            if hasattr(issue, "location") and issue.location:
                lines.append(f"**Location:** {issue.location}")
            if hasattr(issue, "details") and issue.details:
                lines.append(f"\n{issue.details}")
            if hasattr(issue, "suggested_fix") and issue.suggested_fix:
                lines.append(f"\n**Suggested Fix:** {issue.suggested_fix}")
            lines.append("")

        return "\n".join(lines)


async def run_external_validation(
    config: TaskValidationConfig,
    llm_service: LLMService | None,
    task: dict[str, Any],
    changes_context: str,
    force_external: bool = False,
    agent_runner: "AgentRunner | None" = None,
    agent_spawner: "AgentSpawner | None" = None,
) -> ExternalValidationResult:
    """Run external validation with a fresh LLM context or agent.

    Creates a completely fresh validation context without any prior conversation,
    ensuring the validator is objective and has no knowledge of the implementation
    process.

    Three modes are supported:
    - "llm": Direct LLM API calls (default, backwards compatible)
    - "agent": In-process agent instance with tools for validation
    - "spawn": Spawned headless agent process via gobby-agents

    Args:
        config: Validation configuration
        llm_service: LLM service for making requests (used in llm mode)
        task: Task dictionary with id, title, description, validation_criteria
        changes_context: Code changes to validate (typically a git diff)
        force_external: If True, run external validation even if config.use_external_validator is False
        agent_runner: Agent runner for in-process validation (required for agent mode)
        agent_spawner: Agent spawner interface for headless agents (required for spawn mode)

    Returns:
        ExternalValidationResult with status, summary, and any issues found
    """
    # Check if external validation should be skipped
    if not force_external and not config.use_external_validator:
        return ExternalValidationResult(
            status="skipped",
            summary="External validation skipped (disabled in config)",
            issues=[],
        )

    # Dispatch based on mode
    mode = getattr(config, "external_validator_mode", "llm")

    if mode == "spawn":
        return await _run_spawn_validation(
            config=config,
            task=task,
            changes_context=changes_context,
            agent_spawner=agent_spawner,
        )
    elif mode == "agent":
        return await _run_agent_validation(
            config=config,
            task=task,
            changes_context=changes_context,
            agent_runner=agent_runner,
        )
    else:
        if llm_service is None:
            return ExternalValidationResult(
                status="error",
                summary="External validation requires llm_service for 'llm' mode",
                issues=[],
            )
        return await _run_llm_validation(
            config=config,
            llm_service=llm_service,
            task=task,
            changes_context=changes_context,
        )


async def _run_llm_validation(
    config: TaskValidationConfig,
    llm_service: LLMService,
    task: dict[str, Any],
    changes_context: str,
) -> ExternalValidationResult:
    """Run validation using direct LLM API calls.

    Args:
        config: Validation configuration
        llm_service: LLM service for making requests
        task: Task dictionary
        changes_context: Code changes to validate

    Returns:
        ExternalValidationResult
    """
    # Determine which model to use
    model = config.external_validator_model or config.model

    # Build the validation prompt
    prompt = _build_external_validation_prompt(task, changes_context)

    # Render system prompt
    system_prompt = _get_loader().render("external_validation/system", {})

    try:
        provider = llm_service.get_provider(config.provider)
        response = await provider.generate_text(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model,
        )

        return _parse_external_validation_response(response)

    except Exception as e:
        logger.error(f"External validation failed: {e}")
        return ExternalValidationResult(
            status="error",
            summary=f"External validation failed: {str(e)}",
            issues=[],
            error=str(e),
        )


async def _run_agent_validation(
    config: TaskValidationConfig,
    task: dict[str, Any],
    changes_context: str,
    agent_runner: "AgentRunner | None" = None,
) -> ExternalValidationResult:
    """Run validation by spawning an agent instance.

    Spawns a headless agent that can use tools to validate the implementation.
    This provides more thorough validation as the agent can read files,
    run commands, etc.

    Args:
        config: Validation configuration
        task: Task dictionary
        changes_context: Code changes to validate
        agent_runner: Agent runner for spawning agents

    Returns:
        ExternalValidationResult
    """
    if not agent_runner:
        logger.warning("Agent validation requested but no agent runner available")
        return ExternalValidationResult(
            status="error",
            summary="Agent validation not available (no agent runner)",
            issues=[],
            error="Agent runner required for agent mode",
        )

    try:
        from gobby.agents.runner_models import AgentConfig

        # Build prompt for validation agent
        prompt = _build_agent_validation_prompt(task, changes_context)

        # Create agent config for in-process execution
        agent_config = AgentConfig(
            prompt=prompt,
            mode="in_process",  # Run in-process for direct result access
            max_turns=20,
            timeout=120.0,
            source="external_validator",
            model=config.external_validator_model or config.model,
            provider=config.provider,
        )

        # Run the agent directly
        result = await agent_runner.run(agent_config)

        # Parse the agent's output
        if result.status == "error":
            return ExternalValidationResult(
                status="error",
                summary=f"Validation agent failed: {result.error or 'Unknown error'}",
                issues=[],
                error=result.error,
            )

        # Parse the agent's response for validation verdict
        return _parse_external_validation_response(result.output or "")

    except Exception as e:
        logger.error(f"Agent validation failed: {e}")
        return ExternalValidationResult(
            status="error",
            summary=f"Agent validation failed: {str(e)}",
            issues=[],
            error=str(e),
        )


async def _run_spawn_validation(
    config: TaskValidationConfig,
    task: dict[str, Any],
    changes_context: str,
    agent_spawner: "AgentSpawner | None" = None,
) -> ExternalValidationResult:
    """Run validation by spawning a separate headless agent process.

    Spawns a completely separate agent process via gobby-agents.spawn_agent.
    This ensures the validator has no shared state with the implementation agent
    and runs in a fresh context.

    Args:
        config: Validation configuration
        task: Task dictionary
        changes_context: Code changes to validate
        agent_spawner: Agent spawner interface (gobby-agents)

    Returns:
        ExternalValidationResult
    """
    if not agent_spawner:
        logger.warning("Spawn validation requested but no agent spawner available")
        return ExternalValidationResult(
            status="error",
            summary="Spawn validation not available (no agent spawner)",
            issues=[],
            error="Agent spawner required for spawn mode",
        )

    try:
        # Build validation prompt with objective instructions
        prompt = _build_spawn_validation_prompt(task, changes_context)

        # Determine model to use
        model = config.external_validator_model or config.model

        # Spawn an autonomous agent with no parent context
        spawn_result = await agent_spawner.spawn_agent(
            prompt=prompt,
            mode="autonomous",
            model=model,
            provider=config.provider,
            max_turns=5,  # Validation should be quick
            timeout=120.0,
            # Critical: no parent session context to ensure fresh context
            parent_session_id=None,
            session_context=None,
        )

        if not spawn_result.get("success"):
            error_msg = spawn_result.get("error", "Failed to spawn validation agent")
            logger.error(f"Failed to spawn validation agent: {error_msg}")
            return ExternalValidationResult(
                status="error",
                summary=f"Failed to spawn validation agent: {error_msg}",
                issues=[],
                error=error_msg,
            )

        agent_id = spawn_result.get("agent_id")
        if not agent_id:
            return ExternalValidationResult(
                status="error",
                summary="Spawn succeeded but no agent_id returned",
                issues=[],
                error="No agent_id in spawn result",
            )

        # Poll for agent completion
        result = await agent_spawner.get_agent_result(agent_id)

        if not result.get("success"):
            status = result.get("status", "error")
            error_msg = result.get("error", "Agent execution failed")

            if status == "timeout":
                return ExternalValidationResult(
                    status="error",
                    summary=f"Validation agent timed out: {error_msg}",
                    issues=[],
                    error=error_msg,
                )

            return ExternalValidationResult(
                status="error",
                summary=f"Validation agent failed: {error_msg}",
                issues=[],
                error=error_msg,
            )

        # Parse the agent's output
        output = result.get("output", "")
        return _parse_external_validation_response(output)

    except Exception as e:
        logger.error(f"Spawn validation failed: {e}")
        return ExternalValidationResult(
            status="error",
            summary=f"Spawn validation failed: {str(e)}",
            issues=[],
            error=str(e),
        )


def _build_spawn_validation_prompt(
    task: dict[str, Any],
    changes_context: str,
) -> str:
    """Build the validation prompt for spawn mode.

    Creates a prompt that instructs the spawned agent to be objective
    and adversarial in its validation.

    Args:
        task: Task dictionary
        changes_context: Code changes to validate

    Returns:
        Formatted prompt string
    """
    task_id = task.get("id", "unknown")
    task_title = task.get("title", "Unknown Task")
    task_description = task.get("description", "")
    validation_criteria = task.get("validation_criteria", "")
    category = task.get("category", "")

    # Extract files mentioned in the task for prioritization
    priority_files = extract_mentioned_files(task)

    # Summarize diff with priority files for better context
    summarized_changes = summarize_diff_for_validation(
        changes_context, priority_files=priority_files if priority_files else None
    )

    # Build criteria section
    if validation_criteria:
        criteria_section = f"Acceptance Criteria:\n{validation_criteria}"
    elif task_description:
        criteria_section = f"Task Description:\n{task_description}"
    else:
        criteria_section = "No specific criteria provided. Evaluate for general correctness."

    # Build category section
    category_section = ""
    if category:
        category_section = f"\n\n## Task Category\n{category}"

    # Build priority files section
    priority_section = ""
    if priority_files:
        priority_section = (
            f"\n\n**Prioritized files based on task description:** {', '.join(priority_files)}"
        )

    # Extract symbols mentioned in the task for verification
    mentioned_symbols = extract_mentioned_symbols(task)
    symbol_section = ""
    if mentioned_symbols:
        symbol_section = f"\n\n**Key symbols to verify in the changes:** {', '.join(mentioned_symbols)}\nVerify these specific functions/classes are present and correctly implemented."

    prompt = f"""You are an OBJECTIVE and ADVERSARIAL QA validator.

## Critical Instructions
- You have NO prior context about this task or its implementation
- Do NOT assume the implementation is correct
- Verify each criterion INDEPENDENTLY
- Be CRITICAL - look for what's missing or broken
- Your role is to find problems, not to approve

## Task Being Validated
ID: {task_id}
Title: {task_title}

{criteria_section}{category_section}{priority_section}{symbol_section}

## Code Changes to Validate
{summarized_changes}

## Validation Process
1. Review each acceptance criterion one by one
2. Check if the code changes actually satisfy each criterion
3. Look for edge cases, missing error handling, security issues
4. Verify tests exist and cover the requirements
5. Be thorough and skeptical

## Required Output
After your analysis, provide your verdict as a JSON object:

```json
{{
  "status": "valid" | "invalid",
  "summary": "Brief assessment explaining your verdict",
  "issues": [
    {{
      "type": "acceptance_gap|test_failure|lint_error|type_error|security",
      "severity": "blocker|major|minor",
      "title": "Brief description of the issue",
      "location": "file:line (if applicable)",
      "details": "Full explanation of the problem",
      "suggested_fix": "How to resolve (if known)"
    }}
  ]
}}
```

If ALL criteria are FULLY met with no issues, return status "valid".
If there are ANY problems or gaps, return status "invalid" with detailed issues.

Begin your validation now. Be critical and thorough.
"""

    return prompt


def _build_agent_validation_prompt(
    task: dict[str, Any],
    changes_context: str,
) -> str:
    """Build the validation prompt for agent mode.

    The agent prompt is more comprehensive as the agent can use tools.

    Args:
        task: Task dictionary
        changes_context: Code changes to validate

    Returns:
        Formatted prompt string
    """
    task_title = task.get("title", "Unknown Task")
    task_description = task.get("description", "")
    validation_criteria = task.get("validation_criteria", "")

    # Extract files mentioned in the task for prioritization
    priority_files = extract_mentioned_files(task)

    # Summarize diff with priority files for better context
    summarized_changes = summarize_diff_for_validation(
        changes_context, priority_files=priority_files if priority_files else None
    )

    # Build criteria section
    if validation_criteria:
        criteria_section = f"Acceptance Criteria:\n{validation_criteria}"
    elif task_description:
        criteria_section = f"Task Description:\n{task_description}"
    else:
        criteria_section = "No specific criteria provided. Evaluate for general correctness."

    # Build priority files section
    priority_section = ""
    if priority_files:
        priority_section = (
            f"\n\n**Prioritized files based on task description:** {', '.join(priority_files)}"
        )

    # Extract symbols mentioned in the task for verification
    mentioned_symbols = extract_mentioned_symbols(task)
    symbol_section = ""
    if mentioned_symbols:
        symbol_section = f"\n\n**Key symbols to verify in the changes:** {', '.join(mentioned_symbols)}\nVerify these specific functions/classes are present and correctly implemented."

    prompt = f"""You are an objective QA validator. You have NO prior context about this task.

## Your Role
Validate whether the code changes satisfy the acceptance criteria. You have access to tools to:
- Read files to verify implementation details
- Run tests if needed
- Check for common issues

## Task Being Validated
Title: {task_title}

{criteria_section}{priority_section}{symbol_section}

## Code Changes to Validate
{summarized_changes}

## Instructions
1. Review the changes against the acceptance criteria
2. Use tools if needed to verify specific requirements
3. Check for correctness, completeness, and potential issues
4. Be objective and thorough

## Required Output
After your analysis, provide your verdict as a JSON object:

```json
{{
  "status": "valid" | "invalid",
  "summary": "Brief assessment of the changes",
  "issues": [
    {{
      "type": "acceptance_gap|test_failure|lint_error|type_error|security",
      "severity": "blocker|major|minor",
      "title": "Brief description",
      "location": "file:line (if applicable)",
      "details": "Full explanation",
      "suggested_fix": "How to resolve (if applicable)"
    }}
  ]
}}
```

If all criteria are met, return status "valid" with an empty issues array.
If there are problems, return status "invalid" with detailed issues.

Begin your validation now.
"""

    return prompt


def _build_external_validation_prompt(
    task: dict[str, Any],
    changes_context: str,
) -> str:
    """Build the external validation prompt.

    Args:
        task: Task dictionary
        changes_context: Code changes to validate

    Returns:
        Formatted prompt string
    """
    task_title = task.get("title", "Unknown Task")
    task_description = task.get("description", "")
    validation_criteria = task.get("validation_criteria", "")

    # Extract files mentioned in the task for prioritization
    priority_files = extract_mentioned_files(task)

    # Summarize diff with priority files for better context
    summarized_changes = summarize_diff_for_validation(
        changes_context, priority_files=priority_files if priority_files else None
    )

    # Build criteria section
    if validation_criteria:
        criteria_section = f"Acceptance Criteria:\n{validation_criteria}"
    elif task_description:
        criteria_section = f"Task Description:\n{task_description}"
    else:
        criteria_section = "No specific criteria provided. Evaluate for general correctness."

    # Build priority files section
    priority_section = ""
    if priority_files:
        priority_section = (
            f"\n\n**Prioritized files based on task description:** {', '.join(priority_files)}"
        )

    # Extract symbols mentioned in the task for verification
    mentioned_symbols = extract_mentioned_symbols(task)
    symbol_section = ""
    if mentioned_symbols:
        symbol_section = f"\n\n**Key symbols to verify in the changes:** {', '.join(mentioned_symbols)}\nVerify these specific functions/classes are present and correctly implemented."

    prompt = f"""You are reviewing code changes for the following task.

## Task
Title: {task_title}

{criteria_section}{priority_section}{symbol_section}

## Code Changes to Validate
{summarized_changes}

## Instructions
1. Review each change against the acceptance criteria
2. Check for correctness, completeness, and potential issues
3. Be objective - you have no prior context about this implementation

## Output Format
Return your assessment as a JSON object:

```json
{{
  "status": "valid" | "invalid",
  "summary": "Brief assessment of the changes",
  "issues": [
    {{
      "type": "acceptance_gap|test_failure|lint_error|type_error|security",
      "severity": "blocker|major|minor",
      "title": "Brief description",
      "location": "file:line (if applicable)",
      "details": "Full explanation",
      "suggested_fix": "How to resolve (if applicable)"
    }}
  ]
}}
```

If all criteria are met, return status "valid" with an empty issues array.
If there are problems, return status "invalid" with detailed issues.
"""

    return prompt


def _parse_external_validation_response(response: str) -> ExternalValidationResult:
    """Parse the external validation response.

    Args:
        response: Raw LLM response

    Returns:
        ExternalValidationResult
    """
    if not response or not response.strip():
        return ExternalValidationResult(
            status="error",
            summary="Empty response from validator",
            issues=[],
            error="Empty response",
        )

    # Extract JSON from response using shared utility
    data = extract_json_object(response)
    if data is None:
        logger.warning("Failed to parse external validation response")
        return ExternalValidationResult(
            status="error",
            summary="Failed to parse validator response",
            issues=[],
            error="No valid JSON found in response",
        )

    # Extract fields
    status = data.get("status", "pending")
    summary = data.get("summary", "")

    # Parse issues using the issue extraction module
    # Reconstruct the response with issues for parsing
    issues_response = json.dumps({"issues": data.get("issues", [])})
    issues = parse_issues_from_response(issues_response)

    return ExternalValidationResult(
        status=status,
        summary=summary,
        issues=issues,
    )
