from __future__ import annotations

import pytest

from gobby.tasks.criteria_contract import (
    is_external_criterion,
    missing_operational_evidence,
    operational_actions_from_command,
    required_operational_actions,
    split_validation_criteria,
)

pytestmark = pytest.mark.unit


def test_live_criterion_detection_preserves_split_output() -> None:
    source = "- Live: restart the daemon\n2) lIvE: run a smoke check\n- Unit tests pass."

    criteria = split_validation_criteria(source)

    assert criteria == (
        "Live: restart the daemon",
        "lIvE: run a smoke check",
        "Unit tests pass.",
    )
    assert is_external_criterion("- Live: restart the daemon") is True
    assert is_external_criterion("2) lIvE: run a smoke check") is True
    assert [is_external_criterion(criterion) for criterion in criteria] == [True, True, False]


def test_inline_numbered_criteria_split_only_when_sequential() -> None:
    assert split_validation_criteria(
        "1. First criterion. 2) Second criterion. 3. Third criterion."
    ) == (
        "First criterion.",
        "Second criterion.",
        "Third criterion.",
    )

    decimal_and_version = "Version 0.5.0 stays compatible with 1.2 clients."
    assert split_validation_criteria(decimal_and_version) == (decimal_and_version,)

    non_sequential = "1. First criterion. 3) Third criterion."
    assert split_validation_criteria(non_sequential) == ("First criterion. 3) Third criterion.",)

    embedded_number = "Version v1. stays compatible with 2. clients."
    assert split_validation_criteria(embedded_number) == (embedded_number,)


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


def test_operational_evidence_can_skip_external_criteria() -> None:
    criteria = "- Live: restart the daemon\n- Install the release."

    assert missing_operational_evidence(criteria, "", skip_external=True) == ("install",)
    assert missing_operational_evidence(criteria, "") == ("restart", "install")


@pytest.mark.parametrize(
    "changes_summary",
    [
        "`uv sync` succeeded: Resolved 68 packages in 1ms; Installed 68 packages in 2ms.",
        "`uv sync --frozen` succeeded: Audited 68 packages in 1ms.",
        "`uv pip install -e .` succeeded: Successfully installed game-goblins-0.1.0.",
        (
            "`uv run --with pip python -m pip install --editable .` succeeded: "
            "Successfully installed game-goblins-0.1.0."
        ),
    ],
)
def test_tool_native_install_output_is_affirmative_evidence(changes_summary: str) -> None:
    criteria = "Package installation completed successfully."

    assert missing_operational_evidence(criteria, changes_summary) == ()


@pytest.mark.parametrize(
    ("criteria", "changes_summary"),
    [
        ("Install the package.", "Packages installed successfully."),
        ("Install the artifact.", "Artifacts installed successfully."),
        ("Install the build.", "Builds installed successfully."),
    ],
)
def test_plural_install_subjects_match_singular_requirements(
    criteria: str,
    changes_summary: str,
) -> None:
    assert missing_operational_evidence(criteria, changes_summary) == ()


def test_smoke_completion_uses_the_containing_sentence() -> None:
    criteria = "The smoke test passes."
    changes_summary = (
        "The smoke test exercised installation, startup, authenticated requests, and shutdown "
        "across all supported providers and passed cleanly."
    )

    assert missing_operational_evidence(criteria, changes_summary) == ()


def test_install_and_smoke_mentions_without_success_remain_rejected() -> None:
    criteria = "Install the package and run a smoke test."
    changes_summary = "`uv sync` was scheduled, and the smoke test remains pending."

    assert missing_operational_evidence(criteria, changes_summary) == ("install", "smoke")


def test_uv_project_commands_emit_install_markers() -> None:
    assert operational_actions_from_command("uv sync --locked") == ("install",)
    assert operational_actions_from_command("uv add httpx") == ("install",)


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


def test_state_describing_participles_are_not_operational_requirements() -> None:
    criteria = (
        "The installed binary remains unchanged.",
        "Installed package metadata is preserved.",
        "Existing installed skill rows are unchanged.",
        "The test asserts the daemon restart path is unaffected.",
    )

    for criterion in criteria:
        assert required_operational_actions(criterion) == ()


def test_nominal_operational_requirements_are_detected() -> None:
    nominal = "Release ghook installation, daemon restart, and live smoke checks are required."

    assert required_operational_actions(nominal) == ("install", "restart", "smoke")
    assert required_operational_actions("Install the release.") == ("install",)
    assert required_operational_actions("The daemon must be restarted.") == ("restart",)


def test_operational_requirement_detection_uses_criterion_units() -> None:
    criteria = (
        "Background prose says to restart the daemon during rollout.\n"
        "- Existing installed skill rows are unchanged."
    )

    assert required_operational_actions(criteria) == ()


def test_criteria_that_rule_an_operation_out_do_not_require_it() -> None:
    """#21233's criterion promised no restart, and the gate demanded restart evidence."""
    criteria = (
        "A wedged child is reaped by the supervisor and does not require a daemon restart to clear."
    )

    assert required_operational_actions(criteria) == ()
    assert missing_operational_evidence(criteria, "Implementation and tests are complete.") == ()


@pytest.mark.parametrize(
    "criteria",
    [
        "The fix clears without a daemon restart.",
        "Recovery involves no daemon restart.",
        "The change avoids restarting the daemon.",
        "Clearing the wedge should never require another daemon restart.",
        "A daemon restart is not required to pick the change up.",
        "A daemon restart is no longer needed.",
        "The daemon restart must never be part of recovery.",
    ],
)
def test_both_negation_forms_leave_an_operation_unrequired(criteria: str) -> None:
    """Criteria negate before the phrase and after it, and both mean the same thing."""
    assert required_operational_actions(criteria) == ()


def test_ruling_one_operation_out_still_requires_the_others() -> None:
    """Polarity is read per occurrence, so a mixed criteria block keeps its demands."""
    criteria = (
        "Install the release and run a live smoke check. Recovery does not require a "
        "daemon restart."
    )

    assert required_operational_actions(criteria) == ("install", "smoke")


def test_an_unrelated_negation_nearby_still_requires_the_operation() -> None:
    """Only an unbroken negator-to-phrase run negates; punctuation ends the run."""
    criteria = "The change must not regress startup; restart the daemon and confirm health."

    assert required_operational_actions(criteria) == ("restart",)


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
