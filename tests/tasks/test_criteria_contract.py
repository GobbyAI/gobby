from __future__ import annotations

import pytest

from gobby.tasks.criteria_contract import (
    missing_operational_evidence,
    operational_actions_from_command,
    required_operational_actions,
    split_validation_criteria,
)

pytestmark = pytest.mark.unit


def test_list_criteria_discard_introductory_prose() -> None:
    criteria = split_validation_criteria(
        """
        Completion should be observable:

        - Focused unit tests pass.
        - The stored value is normalized.
          Continuation text remains attached.
        """
    )

    assert criteria == (
        "Focused unit tests pass.",
        "The stored value is normalized. Continuation text remains attached.",
    )


def test_non_list_criteria_preserve_prose_paragraphs() -> None:
    criteria = split_validation_criteria("First paragraph.\n\nSecond paragraph.")

    assert criteria == ("First paragraph.", "Second paragraph.")


def test_operational_criteria_require_affirmative_completion_evidence() -> None:
    criteria = "Install the release, restart the daemon, and run a live smoke check."

    assert required_operational_actions(criteria) == ("install", "restart", "smoke")
    assert missing_operational_evidence(criteria, "Implementation and tests are complete.") == (
        "install",
        "restart",
        "smoke",
    )
    assert (
        missing_operational_evidence(
            criteria,
            "Release installed and daemon restart completed; live smoke check passed.",
        )
        == ()
    )


def test_operational_evidence_accepts_successful_transcript_actions() -> None:
    criteria = "Install the release, restart the daemon, and run a smoke test."

    assert (
        missing_operational_evidence(
            criteria,
            "Implementation complete.",
            transcript_actions=("install:release", "restart:daemon", "smoke"),
        )
        == ()
    )
    assert operational_actions_from_command("uv run gobby restart --wait") == (
        "restart:daemon,gobby",
    )


def test_pending_or_negated_operations_are_not_completion_evidence() -> None:
    criteria = "Install the release and restart the daemon."

    assert missing_operational_evidence(
        criteria,
        "Release was not installed; restart is pending.",
    ) == ("install", "restart")


def test_code_only_installer_criteria_do_not_require_operational_evidence() -> None:
    criteria = (
        "Install command parsing and installer unit tests pass. A close attempt whose criterion "
        "requires an install/restart/live smoke operation remains blocked without evidence."
    )

    assert required_operational_actions(criteria) == ()


def test_release_install_and_smoke_outcome_phrasing_is_operational() -> None:
    criteria = (
        "The release ghook is installed atomically and daemon smoke checks show provisional naming."
    )

    assert required_operational_actions(criteria) == ("install", "smoke")


def test_nominal_operational_requirements_are_detected() -> None:
    criteria = "Release ghook installation, daemon restart, and live smoke checks are required."

    assert required_operational_actions(criteria) == ("install", "restart", "smoke")


def test_target_specific_operations_reject_unrelated_evidence() -> None:
    criteria = "Release ghook installation must be complete."

    assert missing_operational_evidence(criteria, "Plugin installed successfully.") == ("install",)
    assert missing_operational_evidence(criteria, "Release ghook installed successfully.") == ()
    assert missing_operational_evidence(
        criteria,
        "Implementation complete.",
        transcript_actions=("install:plugin",),
    ) == ("install",)
    assert (
        missing_operational_evidence(
            criteria,
            "Implementation complete.",
            transcript_actions=("install:ghook",),
        )
        == ()
    )
