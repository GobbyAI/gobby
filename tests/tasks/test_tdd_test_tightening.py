"""A later test-only change can establish an attributable TDD red."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_evidence import _red_run_proof, evaluate_tdd_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)

pytestmark = pytest.mark.unit
START = datetime(2026, 10, 14, tzinfo=UTC)
TEST_PATH = "tests/test_feature.py"
BEFORE = "from feature import Engine\n\ndef test_feature():\n    assert Engine().run()\n"
AFTER = BEFORE.replace("assert Engine().run()", "assert Engine().run() == 2")


def edit(path: str, order: int, source: str) -> TranscriptEdit:
    return TranscriptEdit(
        session_id="owner",
        source="codex",
        path=path,
        timestamp=START + timedelta(seconds=order * 10),
        order=order,
        tool_name="Write",
        source_after=source,
        source_confirmed=True,
        source_confirmed_at=START + timedelta(seconds=order * 10 + 1),
    )


def run(order: int, *, red: bool = False) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id="owner",
        source="codex",
        command=f"pytest {TEST_PATH} -q",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure" if red else "success",
        started_at=START + timedelta(seconds=order * 10),
        completed_at=START + timedelta(seconds=order * 10 + 2),
        order=order,
        exit_code=1 if red else 0,
        output=(
            "_______________________ test_feature _______________________\n"
            "E       assert 1 == 2\n"
            "tests/test_feature.py:4: AssertionError\n"
            "FAILED tests/test_feature.py::test_feature - assert 1 == 2\n"
            if red
            else "1 passed"
        ),
    )


def cycle() -> tuple[AcceptanceTest, TranscriptEvidence]:
    test = AcceptanceTest(
        reference=f"{TEST_PATH}::test_feature", path=TEST_PATH, symbol="test_feature", body=AFTER
    )
    return test, TranscriptEvidence(
        edits=(
            edit(TEST_PATH, 1, BEFORE),
            edit("src/feature.py", 2, "class Engine:\n    def run(self):\n        return 1\n"),
            edit(TEST_PATH, 4, AFTER),
            edit("src/feature.py", 6, "class Engine:\n    def run(self):\n        return 2\n"),
        ),
        validation_runs=(run(3), run(5, red=True), run(7)),
    )


def test_post_production_proof_credits_test_only_tightening() -> None:
    test, evidence = cycle()
    red = evidence.validation_runs[1]

    matched, reason = _red_run_proof(test, evidence, red, red.command, True, {}, {}, None)

    assert matched, reason
    assert evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("cause", ["production_changed", "other_node_changed"])
def test_post_production_proof_rejects_unattributable_red(cause: str) -> None:
    test, evidence = cycle()
    if cause == "production_changed":
        evidence = replace(
            evidence,
            edits=(*evidence.edits, edit("src/other.py", 4, "value = 1\n")),
        )
    else:
        evidence = replace(
            evidence,
            edits=tuple(
                replace(item, source_after=BEFORE + "\ndef test_other():\n    assert True\n")
                if item.path == TEST_PATH and item.order == 4
                else item
                for item in evidence.edits
            ),
        )
    red = evidence.validation_runs[1]

    matched, _ = _red_run_proof(test, evidence, red, red.command, True, {}, {}, None)

    assert not matched
    assert not evaluate_tdd_evidence((test,), evidence).passed


def test_post_production_proof_rejects_unproven_added_node() -> None:
    test, evidence = cycle()
    baseline = BEFORE.replace("test_feature", "test_other")
    evidence = replace(
        evidence,
        edits=tuple(
            replace(
                item,
                source_after=baseline,
            )
            if item.path == TEST_PATH and item.order == 1
            else replace(item, source_after=baseline + "\n" + AFTER.split("\n\n", 1)[1])
            if item.path == TEST_PATH and item.order == 4
            else item
            for item in evidence.edits
        ),
    )

    assert not evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("cause", ["no_green", "unconfirmed", "fragment", "foreign_green"])
def test_post_production_proof_needs_confirmed_baseline(cause: str) -> None:
    test, evidence = cycle()
    if cause == "no_green":
        evidence = replace(evidence, validation_runs=evidence.validation_runs[1:])
    elif cause == "foreign_green":
        evidence = replace(
            evidence,
            validation_runs=(
                replace(evidence.validation_runs[0], session_id="other"),
                *evidence.validation_runs[1:],
            ),
        )
    else:
        baseline = evidence.edits[0]
        baseline = (
            replace(baseline, source_confirmed=False)
            if cause == "unconfirmed"
            else replace(baseline, source_after=None, source_fragment=BEFORE)
        )
        evidence = replace(evidence, edits=(baseline, *evidence.edits[1:]))

    if cause == "no_green":
        red = evidence.validation_runs[0]
        matched, reason = _red_run_proof(test, evidence, red, red.command, True, {}, {}, None)
        assert not matched
        assert (
            reason
            == "post-production red has no attributable NotImplementedError or proven API stub"
        )
        assert evaluate_tdd_evidence((test,), evidence).passed
    else:
        assert not evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("cause", ["no_fix", "no_pass", "wrong_node_pass", "newer_green"])
def test_post_production_cycle_requires_fix_and_same_node_pass(cause: str) -> None:
    test, evidence = cycle()
    if cause == "no_fix":
        evidence = replace(evidence, edits=evidence.edits[:-1])
    elif cause == "no_pass":
        evidence = replace(evidence, validation_runs=evidence.validation_runs[:-1])
    elif cause == "wrong_node_pass":
        evidence = replace(
            evidence,
            validation_runs=(
                *evidence.validation_runs[:-1],
                replace(evidence.validation_runs[-1], command=f"pytest {TEST_PATH}::test_other -q"),
            ),
        )
    else:
        # A newer pass already used the tightened test, so the older pass cannot
        # explain a subsequent failure against identical test source.
        evidence = replace(
            evidence,
            validation_runs=(
                *evidence.validation_runs,
                replace(
                    run(4),
                    started_at=START + timedelta(seconds=43),
                    completed_at=START + timedelta(seconds=44),
                ),
            ),
        )

    assert not evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("order", [1, 4])
def test_post_production_proof_rejects_unconfirmed_source(order: int) -> None:
    test, evidence = cycle()
    evidence = replace(
        evidence,
        edits=tuple(
            replace(item, source_confirmed=False) if item.order == order else item
            for item in evidence.edits
        ),
    )

    assert not evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize(
    "selection",
    [
        f"{TEST_PATH}::test_other",
        f"{TEST_PATH} -k test_other",
        f"{TEST_PATH} -k=test_other",
        f"{TEST_PATH} -m unit",
        f"{TEST_PATH} -k 'a and not b'",
    ],
)
def test_post_production_cycle_rejects_unproven_green_selection(selection: str) -> None:
    test, evidence = cycle()
    evidence = replace(
        evidence,
        validation_runs=(
            *evidence.validation_runs[:-1],
            replace(evidence.validation_runs[-1], command=f"pytest {selection} -q"),
        ),
    )

    assert not evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("selection", ["-m unit", "-k 'a and not b'"])
def test_post_production_proof_needs_confirmed_filtered_baseline(selection: str) -> None:
    test, evidence = cycle()
    evidence = replace(
        evidence,
        validation_runs=(
            replace(evidence.validation_runs[0], command=f"pytest {TEST_PATH} {selection} -q"),
            *evidence.validation_runs[1:],
        ),
    )

    assert not evaluate_tdd_evidence((test,), evidence).passed


def test_post_production_cycle_accepts_python_module_pytest() -> None:
    test, evidence = cycle()
    evidence = replace(
        evidence,
        validation_runs=tuple(
            replace(item, command=f"uv run python -m {item.command}")
            for item in evidence.validation_runs
        ),
    )

    assert evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("cause", ["setup", "module_binding", "helper", "conftest"])
def test_post_production_proof_rejects_behavior_changes(cause: str) -> None:
    test, evidence = cycle()
    if cause == "conftest":
        evidence = replace(
            evidence,
            edits=(
                *evidence.edits,
                edit(
                    "tests/conftest.py",
                    4,
                    "from feature import Engine\nEngine.run = lambda self: 0\n",
                ),
            ),
        )
    else:
        source = {
            "setup": AFTER.replace("    assert", "    Engine.run = lambda self: 0\n    assert"),
            "module_binding": AFTER + "\nEngine.run = lambda self: 0\n",
            "helper": AFTER + "\ndef helper():\n    Engine.run = lambda self: 0\n",
        }[cause]
        evidence = replace(
            evidence,
            edits=tuple(
                replace(item, source_after=source)
                if item.path == TEST_PATH and item.order == 4
                else item
                for item in evidence.edits
            ),
        )
    red = evidence.validation_runs[1]

    matched, _ = _red_run_proof(test, evidence, red, red.command, True, {}, {}, None)

    assert not matched
    assert not evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("source", [BEFORE, "def test_feature():\n    pass\n", AFTER])
def test_post_production_green_must_retain_red_expectation(source: str) -> None:
    test, evidence = cycle()
    evidence = replace(
        evidence,
        edits=(*evidence.edits, edit(TEST_PATH, 7, source)),
        validation_runs=(*evidence.validation_runs[:-1], run(8)),
    )

    assert evaluate_tdd_evidence((test,), evidence).passed is (source == AFTER)


@pytest.mark.parametrize("body", [BEFORE, "def test_feature():\n    assert True\n"])
def test_post_production_cycle_rejects_changed_committed_expectation(body: str) -> None:
    test, evidence = cycle()

    assert not evaluate_tdd_evidence((replace(test, body=body),), evidence).passed


def test_post_production_cycle_rejects_intermediate_expectation_reset() -> None:
    test, evidence = cycle()
    evidence = replace(
        evidence,
        edits=(*evidence.edits, edit(TEST_PATH, 7, BEFORE), edit(TEST_PATH, 8, AFTER)),
        validation_runs=(*evidence.validation_runs[:-1], run(9)),
    )

    assert not evaluate_tdd_evidence((test,), evidence).passed


@pytest.mark.parametrize("mutation", ["setattr(Engine, 'run', fake)", "install_fake()"])
def test_post_production_proof_rejects_added_assertion_mutation(mutation: str) -> None:
    test, _ = cycle()
    baseline = (
        "import feature\nfrom feature import Engine\n\n"
        "def fake(self):\n    return feature.flag\n\n"
        "def install_fake():\n    Engine.run = fake\n\n"
        "def test_other():\n    assert Engine().run() == 2\n"
    )
    added = (
        f"\ndef test_feature():\n    assert {mutation} is None\n    assert Engine().run() == 2\n"
    )
    production = "flag = 0\nclass Engine:\n    def run(self):\n        return 2\n"
    evidence = TranscriptEvidence(
        edits=(
            edit(TEST_PATH, 1, baseline),
            edit("src/feature.py", 2, production),
            edit(TEST_PATH, 4, baseline + added),
            edit("src/feature.py", 6, production.replace("flag = 0", "flag = 2")),
        ),
        validation_runs=(run(3), run(5, red=True), run(7)),
    )

    assert not evaluate_tdd_evidence((replace(test, body=added),), evidence).passed


def test_post_production_proof_credits_literal_expectation_change() -> None:
    test, evidence = cycle()
    evidence = replace(
        evidence,
        edits=(
            replace(
                evidence.edits[0],
                source_after=BEFORE.replace("Engine().run()", "Engine().run() == 1"),
            ),
            *evidence.edits[1:],
        ),
    )

    assert evaluate_tdd_evidence((test,), evidence).passed
