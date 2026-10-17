"""Collection credit is restricted to declared operator admission guards."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.tasks.close_checklist import CloseGateResult, evaluate_validation_commands
from gobby.tasks.close_pytest_collection import declares_operator_guard
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun

TEST = "tests/e2e/test_live.py"
GUARDED = """import os
import pytest
_GRANT = "OPERATOR_AUTHORIZED"
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(
    os.environ.get("GOBBY_LIVE_EXECUTION") != _GRANT,
    reason="operator execution grant required",
)]
def test_live():
    assert True
"""


def _run(command: str, output: str, *, exit_code: int = 0) -> TranscriptValidationRun:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    return TranscriptValidationRun(
        session_id="author",
        source="codex",
        command=command,
        categories=(),
        matcher_id="",
        label="command",
        outcome="success" if exit_code == 0 else "failure",
        started_at=now,
        completed_at=now,
        order=1,
        exit_code=exit_code,
        output=output,
    )


def _gate(
    root: Path,
    collection: TranscriptValidationRun,
    source: str = GUARDED,
    *,
    changed: tuple[str, ...] = (TEST,),
) -> CloseGateResult:
    path = root / TEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    real_test = replace(
        _run("uv run pytest tests/test_other.py -q", "1 passed in 0.01s"),
        categories=("test",),
        matcher_id="pytest",
        order=2,
    )
    audit = replace(
        _run(
            "uv run gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new",
            "Errors: 0",
        ),
        categories=("type_check",),
        matcher_id="gobby-test-types-audit",
        order=3,
    )
    return evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        close_root=str(root),
        changed_paths=changed,
        evidence=TranscriptEvidence(validation_runs=(real_test, audit), command_runs=(collection,)),
    )


def test_guarded_collection_credits_changed_file(root: Path) -> None:
    # The RTK proxy preserves this raw summary by declining the lossy rewrite.
    gate = _gate(root, _run(f"uv run pytest {TEST} --collect-only -q", "1 test collected in 0.01s"))
    assert gate.status == "passed", gate.message
    assert gate.details["pytest_uncovered_paths"] == []


def test_composer_live_proof_guard_earns_collection_credit(root: Path) -> None:
    # #24411's file reads _GRANT in composer_fixture and the child env.
    real = Path(__file__).resolve().parents[2] / "tests/e2e/test_composer_live_proof.py"
    gate = _gate(
        root,
        _run(f"uv run pytest {TEST} --collect-only -q", "1 test collected in 0.01s"),
        real.read_text(encoding="utf-8"),
    )
    assert gate.status == "passed", gate.message
    assert gate.details["pytest_collection_credited_paths"] == [TEST]


def test_collection_accepts_read_only_grant_uses(root: Path) -> None:
    source = GUARDED + (
        "@pytest.fixture\ndef admitted():\n"
        '    if os.environ.get("GOBBY_LIVE_EXECUTION") != _GRANT:\n'
        '        pytest.skip("no grant")\n'
        "    return dict(GOBBY_LIVE_EXECUTION=_GRANT)\n"
    )
    gate = _gate(
        root, _run(f"uv run pytest {TEST} --collect-only -q", "1 test collected in 0.01s"), source
    )
    assert gate.status == "passed", gate.message
    assert gate.details["pytest_uncovered_paths"] == []


@pytest.mark.parametrize(
    ("source", "output", "exit_code"),
    [
        ("def test_live():\n    assert True\n", "1 test collected in 0.01s", 0),
        (
            "pytestmark = pytest.mark.skipif(True, reason='operator')",
            "1 test collected in 0.01s",
            0,
        ),
        (GUARDED, "no tests collected in 0.01s", 0),
        (GUARDED, "collected 0 items", 0),
        (GUARDED, "1 test collected in 0.01s\n1 error", 1),
        (GUARDED, "1 skipped in 0.01s", 0),
        (GUARDED, "collected 1 item", 0),
    ],
)
def test_collection_without_guard_or_positive_completion_is_rejected(
    root: Path,
    source: str,
    output: str,
    exit_code: int,
) -> None:
    gate = _gate(
        root, _run(f"uv run pytest {TEST} --collect-only -q", output, exit_code=exit_code), source
    )
    assert gate.status == "failed"
    assert gate.details["pytest_uncovered_paths"] == [TEST]


@pytest.mark.parametrize("target", ["tests/", f"{TEST} tests/test_other.py", f"{TEST}::test_live"])
def test_collection_requires_one_complete_file(root: Path, target: str) -> None:
    gate = _gate(
        root, _run(f"uv run pytest {target} --collect-only -q", "1 test collected in 0.01s")
    )
    assert gate.details["pytest_uncovered_paths"] == [TEST]


def test_collection_never_credits_unguarded_sibling(root: Path) -> None:
    gate = _gate(
        root,
        _run(f"uv run pytest {TEST} --collect-only -q", "1 test collected in 0.01s"),
        changed=(TEST, "tests/test_ordinary.py"),
    )
    assert gate.details["pytest_uncovered_paths"] == ["tests/test_ordinary.py"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.parametrize(
    "source",
    [
        GUARDED + "pytestmark: list = []\n",
        GUARDED.replace(
            '_GRANT = "OPERATOR_AUTHORIZED"',
            '_GRANT = "OPERATOR_AUTHORIZED"\n_GRANT: str | None = None',
        ),
        GUARDED + "pytestmark *= 0\n",
        GUARDED + "if True:\n    pytestmark = []\n",
        GUARDED + "markers = pytestmark\nmarkers.clear()\n",
        GUARDED + "pytestmark.clear()\n",
        GUARDED + "pytestmark[1] = pytest.mark.e2e\n",
        GUARDED + "del _GRANT\n",
        GUARDED.replace(
            '_GRANT = "OPERATOR_AUTHORIZED"',
            '_GRANT = "OPERATOR_AUTHORIZED"\n_GRANT += "X"',
        ),
        GUARDED + "def reset():\n    global _GRANT\n    _GRANT = None\n",
        GUARDED.replace(
            '_GRANT = "OPERATOR_AUTHORIZED"',
            '_GRANT = "OPERATOR_AUTHORIZED"\nfrom os import sep as _GRANT',
        ),
        GUARDED + "if (_GRANT := None):\n    pass\n",
        GUARDED + "pytestmark, other = [], None\n",
        GUARDED.replace("pytestmark = [", "markers = pytestmark = ["),
        GUARDED.replace(
            '_GRANT = "OPERATOR_AUTHORIZED"',
            '_GRANT = "OPERATOR_AUTHORIZED"\nif True:\n    _GRANT = None',
        ),
    ],
    ids=[
        "annotated-marker",
        "annotated-grant",
        "augmented-marker",
        "conditional-marker",
        "alias-mutation",
        "marker-method-mutation",
        "marker-item-store",
        "deleted-grant",
        "augmented-grant",
        "nested-global-grant",
        "imported-grant",
        "walrus-grant",
        "unpacked-marker",
        "chained-alias",
        "conditional-grant",
    ],
)
def test_collection_rejects_ambiguous_guard_bindings(root: Path, source: str) -> None:
    gate = _gate(
        root, _run(f"uv run pytest {TEST} --collect-only -q", "1 test collected in 0.01s"), source
    )
    assert gate.status == "failed"
    assert gate.details["pytest_uncovered_paths"] == [TEST]


def test_non_test_category_never_credits_ordinary_file(root: Path) -> None:
    gate = _gate(
        root,
        _run(f"uv run pytest {TEST} -q", "1 passed in 0.01s"),
        "def test_live():\n    assert True\n",
    )
    assert gate.details["pytest_uncovered_paths"] == [TEST]


@pytest.mark.parametrize("version", ["0.51.0", "1.0.1"])
def test_lossy_rtk_collection_output_never_earns_credit(root: Path, version: str) -> None:
    output = (
        Path(__file__).parents[1] / f"fixtures/rtk/pytest-collection-{version}.txt"
    ).read_text()
    gate = _gate(root, _run(f"rtk uv run pytest {TEST} --collect-only -q", output))
    assert gate.details["pytest_uncovered_paths"] == [TEST]


@pytest.mark.parametrize(
    "write",
    [
        'globals()["pytestmark"] = []\n',
        'globals()["pytestmark"]: list = []\n',
        'globals()["pytestmark"] += []\n',
        'del globals()["pytestmark"]\n',
    ],
    ids=["assignment", "annotated", "augmented", "delete"],
)
def test_declares_operator_guard_rejects_globals_pytestmark_write(write: str) -> None:
    source = GUARDED.replace("!= _GRANT", '!= "OPERATOR_AUTHORIZED"') + write
    assert declares_operator_guard(source) is False


@pytest.mark.parametrize("protected_name", ["pytestmark", "_GRANT"])
def test_declares_operator_guard_rejects_mapping_rest_binding(protected_name: str) -> None:
    source = GUARDED + f"match {{}}:\n    case {{**{protected_name}}}:\n        pass\n"
    assert declares_operator_guard(source) is False
