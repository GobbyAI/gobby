"""Receipt-backed Python shape edits must preserve genuine TDD chronology."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)

START = datetime(2026, 10, 3, tzinfo=UTC)


def _edit(path: str, order: int, source: str | None = None) -> TranscriptEdit:
    return TranscriptEdit(
        session_id="owner",
        source="codex",
        path=path,
        timestamp=START + timedelta(seconds=order * 10),
        order=order,
        tool_name="Write",
        source_after=source,
        source_confirmed=source is not None,
        source_confirmed_at=START + timedelta(seconds=order * 10 + 1),
    )


def _run(test: AcceptanceTest, order: int, *, red: bool) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id="owner",
        source="codex",
        command=f"pytest {test.reference} -q",
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
            "E       Failed: DID NOT RAISE <class 'feature.CronSessionError'>\n"
            "tests/test_feature.py:7: Failed\n"
            "FAILED tests/test_feature.py::test_feature\n"
            if red
            else "1 passed"
        ),
    )


def _exception_cycle(declaration: str) -> tuple[AcceptanceTest, TranscriptEvidence]:
    source = (
        "import pytest\n"
        "from feature import CronSessionError, run\n\n"
        "def test_feature():\n"
        "    with pytest.raises(CronSessionError):\n"
        "        run()\n"
    )
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=source,
    )
    stub = replace(
        _edit("src/feature.py", 2, declaration + "\ndef run():\n    return None\n"),
        tool_name="Edit",
        python_added_source=declaration,
    )
    return test, TranscriptEvidence(
        edits=(_edit(test.path, 1, source), stub, _edit("src/feature.py", 4)),
        validation_runs=(_run(test, 3, red=True), _run(test, 5, red=False)),
    )


@pytest.mark.parametrize("base", ["RuntimeError", "Exception", "ValueError"])
def test_exception_declaration_stub_red(base: str) -> None:
    test, evidence = _exception_cycle(f"class CronSessionError({base}):\n    pass\n")

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (f"pytest {test.reference} -q",)


@pytest.mark.parametrize(
    "declaration",
    [
        "class CronSessionError(CustomError):\n    pass\n",
        "class CronSessionError(make_base()):\n    pass\n",
        "class CronSessionError(RuntimeError, metaclass=Meta):\n    pass\n",
        "class CronSessionError(RuntimeError):\n    registry.append('side effect')\n",
        "class CronSessionError(RuntimeError):\n    def __str__(self):\n        return 'feature'\n",
        "RuntimeError = CustomError\nclass CronSessionError(RuntimeError):\n    pass\n",
    ],
)
def test_behavioral_exception_declaration_does_not_credit_red(declaration: str) -> None:
    test, evidence = _exception_cycle(declaration)

    result = evaluate_tdd_evidence((test,), evidence)

    assert not result.passed
    assert not result.red_runs


def test_unconfirmed_exception_declaration_does_not_credit_red() -> None:
    test, evidence = _exception_cycle("class CronSessionError(RuntimeError):\n    pass\n")
    stub = replace(evidence.edits[1], source_confirmed=False, source_confirmed_at=None)

    result = evaluate_tdd_evidence(
        (test,), replace(evidence, edits=(evidence.edits[0], stub, evidence.edits[2]))
    )

    assert not result.passed
    assert not result.red_runs


def test_exception_base_shadowed_outside_added_fragment() -> None:
    test, evidence = _exception_cycle("class CronSessionError(RuntimeError):\n    pass\n")
    stub = replace(
        evidence.edits[1],
        source_after="RuntimeError = CustomError\n" + (evidence.edits[1].source_after or ""),
    )

    result = evaluate_tdd_evidence(
        (test,), replace(evidence, edits=(evidence.edits[0], stub, evidence.edits[2]))
    )

    assert not result.passed
    assert not result.red_runs


@pytest.mark.parametrize(
    "behavior",
    [
        "raise CronSessionError",
        "raise CronSessionError('active')",
        "error = CronSessionError('active')\n    raise error",
        "raise feature.CronSessionError('active')",
        None,
    ],
)
def test_exception_already_raised_in_confirmed_module_is_not_stub(behavior: str | None) -> None:
    test, evidence = _exception_cycle("class CronSessionError(RuntimeError):\n    pass\n")
    stub = replace(
        evidence.edits[1],
        source_after=(
            f"class CronSessionError(RuntimeError):\n    pass\n\ndef run():\n    {behavior}\n"
        )
        if behavior is not None
        else None,
    )

    result = evaluate_tdd_evidence(
        (test,), replace(evidence, edits=(evidence.edits[0], stub, evidence.edits[2]))
    )

    assert not result.passed
    assert not result.red_runs


def _move_cycle() -> tuple[AcceptanceTest, TranscriptEvidence]:
    source = "from feature import payload\n\ndef test_feature():\n    assert payload()['seat']\n"
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=source,
    )
    moved = "def payload():\n    return {'kind': 'base'}\n"
    before_test = replace(_edit("src/feature.py", 0, moved), source_created=True)
    repeated_write = _edit("src/feature.py", 2, moved)
    cleanup = replace(
        _edit("src/old.py", 3),
        tool_name="Edit",
        source_fragment="from collections.abc import Callable\n",
        source_confirmed=True,
    )
    red = replace(
        _run(test, 4, red=True),
        output=(
            "_______________________ test_feature _______________________\n"
            "E       KeyError: 'seat'\n"
            "tests/test_feature.py:4: KeyError\n"
            "FAILED tests/test_feature.py::test_feature - KeyError: 'seat'\n"
        ),
    )
    return test, TranscriptEvidence(
        edits=(
            before_test,
            _edit(test.path, 1, source),
            repeated_write,
            cleanup,
            _edit("src/feature.py", 5),
        ),
        validation_runs=(red, _run(test, 6, red=False)),
    )


def test_unchanged_move_red() -> None:
    test, evidence = _move_cycle()

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (f"pytest {test.reference} -q",)


@pytest.mark.parametrize(
    "changed",
    [
        "body",
        "unconfirmed",
        "late_baseline",
        "dependency",
        "rebound",
        "import-rebound",
        "class-rebound",
        "test-rebound",
        "other-invoked-api",
        "reconstructed",
    ],
)
def test_move_needs_proven_unchanged_api(changed: str) -> None:
    test, evidence = _move_cycle()
    edits = list(evidence.edits)
    if changed == "body":
        edits[2] = replace(edits[2], source_after="def payload():\n    return {'other': 1}\n")
    elif changed == "unconfirmed":
        edits[2] = replace(edits[2], source_confirmed=False, source_confirmed_at=None)
    elif changed == "late_baseline":
        edits = edits[1:]
    elif changed == "dependency":
        dependent = "from dependency import value\ndef payload():\n    return value()\n"
        edits[0] = replace(edits[0], source_after=dependent)
        edits[2] = replace(edits[2], source_after=dependent)
        edits[3] = replace(edits[3], path="src/dependency.py", source_fragment=None)
    elif changed in {"rebound", "import-rebound", "class-rebound"}:
        binding = {
            "rebound": "payload = lambda: {}\n",
            "import-rebound": "from dependency import payload\n",
            "class-rebound": "class payload:\n    pass\n",
        }[changed]
        rebound = (edits[0].source_after or "") + binding
        edits[0] = replace(edits[0], source_after=rebound)
        edits[2] = replace(edits[2], source_after=rebound)
    elif changed == "test-rebound":
        original = edits[1].source_after or ""
        edits[1] = replace(edits[1], source_after=original + "payload = lambda: {}\n")
    elif changed == "other-invoked-api":
        original = edits[1].source_after or ""
        edits[1] = replace(
            edits[1],
            source_after="from other import implemented\n"
            + original.replace("    assert payload()", "    implemented()\n    assert payload()"),
        )
        edits[3] = replace(
            edits[3], path="src/other.py", source_after="def implemented():\n    return 42\n"
        )
    else:
        edits = [replace(edit, order=edit.order * 10) for edit in edits]
        evidence = replace(
            evidence,
            validation_runs=tuple(
                replace(run, order=run.order * 10) for run in evidence.validation_runs
            ),
        )
        implemented = replace(
            _edit("src/feature.py", 2, "def payload():\n    return {'seat': 'implemented'}\n"),
            order=15,
            timestamp=START + timedelta(seconds=15),
            source_confirmed_at=START + timedelta(seconds=16),
        )
        edits.insert(2, implemented)

    result = evaluate_tdd_evidence((test,), replace(evidence, edits=tuple(edits)))

    assert not result.passed
    assert not result.red_runs
