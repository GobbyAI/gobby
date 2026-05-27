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

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager

logger = logging.getLogger(__name__)


def parse_provider_list(provider_string: str | None) -> list[str]:
    """Parse a comma-separated provider string into a list.

    .. deprecated::
        Use ``fallback_agent`` on agent definitions instead of comma-separated
        provider strings. Kept for backward compatibility.

    Args:
        provider_string: e.g. "gemini,claude" or "claude"

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
    classifier: StallClassifier | None = None,
) -> list[str]:
    """Get providers that failed with provider-side errors for a task.

    Queries agent_runs for error/timeout runs on this task and checks
    whether the error matches provider error patterns.

    Args:
        task_id: Task ID to check.
        agent_run_manager: Agent run storage manager.
        classifier: Optional StallClassifier instance (created if not provided).

    Returns:
        List of provider names that failed due to provider errors.
    """
    if classifier is None:
        classifier = StallClassifier()

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
        if provider and classifier.is_provider_error(error):
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
    classifier: StallClassifier | None = None,
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
        classifier: Optional StallClassifier instance.

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
                f"Provider rotation for task {task_id}: skipping {already_failed}, trying {provider}",
            )
            return provider

    logger.warning(
        f"Provider rotation for task {task_id}: all providers exhausted ({provider_list})",
    )
    return None
