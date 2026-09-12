"""Provider fallback rotation for agent spawning.

When an agent fails due to a provider-side issue (rate limits, outages),
this module selects the next untried provider from a comma-separated
fallback list. Used by the lifecycle monitor during task recovery and
by the spawn factory before dispatching.
"""

from __future__ import annotations

import logging
import warnings
from typing import TYPE_CHECKING

from gobby.agents.stall_classifier import StallClassifier
from gobby.config.feature_base import DEFAULT_PROFILE_CANDIDATES, parse_feature_candidate

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager

logger = logging.getLogger(__name__)


def rotated_model_for_provider(*, target_provider: str, source_model: str) -> str | None:
    """Return the model ``target_provider`` uses at ``source_model``'s tier.

    Rotating a spawn onto another provider cannot carry the agent's model
    across — ``gpt-5.6-sol`` means nothing to the claude CLI — but dropping it
    is worse: the CLI then falls back to whatever default it has configured,
    which is a model nobody chose and may carry its own spend cap. The feature
    profiles in ``DEFAULT_PROFILE_CANDIDATES`` already pair each provider's
    models by capability tier, so they answer the substitution directly.

    Returns ``None`` when ``source_model`` sits in no profile or the target
    provider has no candidate at that tier; callers must fail rather than
    spawn with the model unset.
    """
    normalized = source_model.strip().lower()
    for candidates in DEFAULT_PROFILE_CANDIDATES.values():
        parsed = [parse_feature_candidate(candidate) for candidate in candidates]
        if not any(model.lower() == normalized for _, model in parsed):
            continue
        for provider, model in parsed:
            if provider == target_provider:
                return model
        return None
    return None


def parse_provider_list(provider_string: str | None) -> list[str]:
    """Parse a comma-separated provider string into a list.

    .. deprecated::
        Use ``fallback_agent`` on agent definitions instead of comma-separated
        provider strings. Kept for backward compatibility.

    Args:
        provider_string: e.g. "qwen,claude" or "claude"

    Returns:
        List of provider names, stripped and lowercased.
        Empty list if input is None or empty.
    """
    if not provider_string:
        return []
    if "," in provider_string:
        warnings.warn(
            "Comma-separated provider strings are deprecated; "
            "use fallback_agent on agent definitions instead",
            DeprecationWarning,
            stacklevel=2,
        )
    return [p.strip().lower() for p in provider_string.split(",") if p.strip()]


def get_failed_providers_for_task(
    task_id: str,
    agent_run_manager: LocalAgentRunManager,
    *,
    classifier: StallClassifier,
) -> list[str]:
    """Get providers that failed with provider-side errors for a task.

    Queries agent_runs for error/timeout runs on this task and checks
    whether the error matches provider error patterns.

    Args:
        task_id: Task ID to check.
        agent_run_manager: Agent run storage manager.
        classifier: Registry-backed classifier root.

    Returns:
        List of provider names that failed due to provider errors.
    """
    rows = agent_run_manager.db.fetchall(
        """
        SELECT provider, error FROM agent_runs
        WHERE task_id = %s AND status IN ('error', 'timeout')
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (task_id,),
    )

    failed: list[str] = []
    for row in rows:
        provider = row["provider"]
        error = row["error"]
        if provider and classifier.for_provider(provider).is_provider_error(error):
            if provider.lower() not in failed:
                failed.append(provider.lower())

    return failed


def select_next_provider(
    task_id: str,
    provider_list: list[str],
    failed_provider: str | None = None,
    is_provider_error: bool = False,
    *,
    agent_run_manager: LocalAgentRunManager | None = None,
    classifier: StallClassifier,
) -> str | None:
    """Select the next provider to try for a task.

    Logic:
    1. If the failure wasn't a provider error, return None (normal re-dispatch).
    2. Build a set of failed providers from agent_runs history + current failure.
    3. Return the first provider from the list that hasn't failed.
    4. If all providers have been tried, return None.

    Args:
        task_id: Task ID to check history for.
        provider_list: Ordered list of providers to try (from parse_provider_list).
        failed_provider: The provider that just failed (if any).
        is_provider_error: Whether the current failure is provider-side.
        agent_run_manager: For querying historical failures.
        classifier: Registry-backed classifier root.

    Returns:
        Next provider name to try, or None if all exhausted / not a provider error.
    """
    if not is_provider_error:
        return None

    if not provider_list:
        return None

    # Build set of providers that have already failed with provider errors
    already_failed: set[str] = set()
    if agent_run_manager:
        already_failed = set(
            get_failed_providers_for_task(task_id, agent_run_manager, classifier=classifier)
        )

    if failed_provider:
        already_failed.add(failed_provider.lower())

    # Find first untried provider
    for provider in provider_list:
        if provider not in already_failed:
            logger.info(
                "Provider rotation for task %s: skipping %s, trying %s",
                task_id,
                already_failed,
                provider,
            )
            return provider

    logger.warning(
        "Provider rotation for task %s: all providers exhausted (%s)", task_id, provider_list
    )
    return None
