"""Gate 12 credits a RED against a declaration-only API stub that fails in test bodies."""

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

pytestmark = pytest.mark.unit

START = datetime(2026, 10, 16, tzinfo=UTC)
CONFIG = "src/gobby/config/ai.py"
WIRING = "src/gobby/config/wiring.py"
TEST_PATH = "tests/config/test_decisions_config.py"
# The #24243 stub went in mid-file, between an existing list and ``AIConfig``.
ANCHOR_HEAD = "    ]\n\n\n"
ANCHOR_TAIL = 'class AIConfig(BaseModel):\n    """Daemon-owned AI configuration namespace."""\n'
CONSUMER = (
    "class ChoiceConsumerConfig(BaseModel):\n"
    '    mode: str = "off"\n'
    "    min_confidence: float = 0.5\n"
    "    evaluated_model: str | None = None\n"
)
DECISIONS = (
    "    api_base: str | None = None\n"
    "    model: str | None = None\n"
    "    timeout_seconds: float = 2.0\n"
    "    community_label: ChoiceConsumerConfig | None = None\n"
)
BEHAVIOR = (
    "class ChoiceConsumerConfig(BaseModel):\n"
    '    mode: Literal["off", "shadow", "on"] = "off"\n'
    "    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)\n"
    "    evaluated_model: str | None = None\n\n\n"
    "class DecisionsConfig(BaseModel):\n"
    f"{DECISIONS}\n"
    '    @model_validator(mode="after")\n'
    "    def _require_model(self) -> DecisionsConfig:\n"
    "        if self.api_base is not None and self.model is None:\n"
    '            raise ValueError("model is required with api_base")\n'
    "        return self\n"
)
TEST_SOURCE = (
    "import pytest\n"
    "from pydantic import ValidationError\n\n"
    "from gobby.config.ai import AIConfig, DecisionsConfig\n\n\n"
    "def test_decisions_config_invariants():\n"
    '    with pytest.raises(ValidationError, match="model"):\n'
    '        DecisionsConfig(api_base="http://127.0.0.1:1234/v1")\n\n\n'
    "def test_ai_config_exposes_decisions():\n"
    '    assert "decisions" in AIConfig.model_fields\n\n\n'
    "def test_decisions_scalar_rows_defaults_and_bounds():\n"
    "    assert DecisionsConfig(timeout_seconds=0).timeout_seconds == 0\n"
)


def _test(symbol: str) -> AcceptanceTest:
    return AcceptanceTest(
        reference=f"{TEST_PATH}::{symbol}", path=TEST_PATH, symbol=symbol, body=TEST_SOURCE
    )


INVARIANTS = _test("test_decisions_config_invariants")
# Reaches only ``AIConfig``, which the file declared before the stub.
EXPOSES = _test("test_ai_config_exposes_decisions")
SCALAR = _test("test_decisions_scalar_rows_defaults_and_bounds")
COMMAND = (
    "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
    f"GOBBY_TEST_PROTECT=1 uv run pytest {TEST_PATH} tests/ai/test_capability_registry.py -q"
)
# The RTK-compacted shape #24243's RED recorded, with sibling failures of other kinds.
RED_OUTPUT = (
    "Pytest: 39 passed, 3 failed in 9.24s\n\n"
    "Failures:\n"
    "1. [FAIL] test_decisions_config_invariants\n"
    f"     {TEST_PATH}:8: in test_decisions_config_invariants\n"
    '     with pytest.raises(ValidationError, match="model"):\n'
    "     E   Failed: DID NOT RAISE <class 'pydantic_core._pydantic_core.ValidationError'>\n\n"
    "2. [FAIL] test_ai_config_exposes_decisions\n"
    f"     {TEST_PATH}:12: in test_ai_config_exposes_decisions\n"
    '     assert "decisions" in AIConfig.model_fields\n'
    "     E   AssertionError: assert 'decisions' in {'models': FieldInfo(annotation=dict, r...\n\n"
    "3. [FAIL] test_decisions_scalar_rows_defaults_and_bounds\n"
    f"     {TEST_PATH}:16: in test_decisions_scalar_rows_defaults_and_bounds\n"
    "     E   pydantic_core._pydantic_core.ValidationError: 1 validation error for DecisionsConfig\n"
    "     E   timeout_seconds\n"
    "[full output: rtk recall 97a9b156be8f]\n"
)
COLLECTION_ERROR = (
    "==================================== ERRORS ====================================\n"
    f"____________ ERROR collecting {TEST_PATH} ____________\n"
    f"ImportError while importing test module '{TEST_PATH}'.\n"
    "E   ImportError: cannot import name 'DecisionsConfig' from 'gobby.config.ai'\n"
    "=========================== short test summary info ============================\n"
    f"ERROR {TEST_PATH}\n"
    "1 error in 0.31s\n"
)
BEHAVIOR_MODULE = "def wire():\n    return 1\n"


def _stub(decisions: str = DECISIONS) -> str:
    return f"{CONSUMER}\n\nclass DecisionsConfig(BaseModel):\n{decisions}"


def _edit(
    order: int, old: str, new: str, *, path: str = CONFIG, session_id: str = "owner"
) -> TranscriptEdit:
    """An Edit of a file only Read in-session, so no full ``source_after`` exists."""
    at = START + timedelta(seconds=order * 10)
    return TranscriptEdit(
        session_id=session_id,
        source="claude",
        path=path,
        timestamp=at,
        order=order,
        tool_name="Edit",
        source_confirmed=True,
        source_confirmed_at=at + timedelta(seconds=1),
        source_fragment=new,
        source_replaced=old,
    )


def _insertion(order: int, stub: str, *, session_id: str = "owner") -> TranscriptEdit:
    anchor = f"{ANCHOR_HEAD}{ANCHOR_TAIL}"
    return _edit(order, anchor, f"{ANCHOR_HEAD}{stub}\n\n{ANCHOR_TAIL}", session_id=session_id)


def _run(order: int, *, red: bool, output: str = RED_OUTPUT) -> TranscriptValidationRun:
    at = START + timedelta(seconds=order * 10)
    return TranscriptValidationRun(
        session_id="owner",
        source="claude",
        command=COMMAND,
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure" if red else "success",
        started_at=at,
        completed_at=at + timedelta(seconds=2),
        order=order,
        exit_code=1 if red else 0,
        output=output if red else "Pytest: 42 passed in 9.81s",
    )


def _declaration_cycle(
    *,
    stub: TranscriptEdit | None = None,
    closing: TranscriptEdit | None = None,
    red_output: str = RED_OUTPUT,
    extra_edit: TranscriptEdit | None = None,
) -> TranscriptEvidence:
    """Test at 1, stub at 2, RED at 4, behavior at 6, GREEN at 8; odd orders interleave."""
    return TranscriptEvidence(
        edits=(
            TranscriptEdit(
                session_id="owner",
                source="claude",
                path=TEST_PATH,
                timestamp=START + timedelta(seconds=10),
                order=1,
                tool_name="Write",
                source_after=TEST_SOURCE,
                source_confirmed=True,
                source_confirmed_at=START + timedelta(seconds=11),
                source_created=True,
            ),
            stub if stub is not None else _insertion(2, _stub()),
            closing if closing is not None else _edit(6, _stub(), BEHAVIOR),
            *((extra_edit,) if extra_edit is not None else ()),
        ),
        validation_runs=(_run(4, red=True, output=red_output), _run(8, red=False)),
    )


def _stub_cycle(decisions: str) -> TranscriptEvidence:
    """The default cycle with ``decisions`` as the stubbed body and the behavior's anchor."""
    return _declaration_cycle(
        stub=_insertion(2, _stub(decisions)), closing=_edit(6, _stub(decisions), BEHAVIOR)
    )


@pytest.mark.parametrize(
    "decisions",
    [
        pytest.param(DECISIONS, id="literal-defaults"),
        pytest.param(f"{DECISIONS}    retries: int = -1\n", id="negative-literal-default"),
        pytest.param(f"{DECISIONS}    def resolve(self) -> str:\n        ...\n", id="ellipsis"),
        pytest.param(
            f'{DECISIONS}    def resolve(self) -> str:\n        """Stub."""\n', id="docstring-only"
        ),
        pytest.param(
            f'    """Shared decide settings."""\n\n{DECISIONS}\n'
            "    def resolve(self) -> str:\n        raise NotImplementedError\n",
            id="docstring-and-not-implemented-method",
        ),
    ],
)
def test_declaration_stub_credits_a_red_on_test_body_assertions(decisions: str) -> None:
    result = evaluate_tdd_evidence((INVARIANTS,), _stub_cycle(decisions))

    assert result.passed, result.findings
    assert result.red_runs == (COMMAND,)
    assert result.green_runs == (COMMAND,)


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(
            '    @field_validator("model")\n    @classmethod\n'
            "    def _check(cls, value: str) -> str:\n        return value\n",
            id="decorated-validator",
        ),
        pytest.param(
            "    def resolved(self) -> str:\n"
            "        if self.model is None:\n            raise NotImplementedError\n",
            id="branch-in-a-method",
        ),
        pytest.param(
            "    def resolved(self) -> str | None:\n        return self.model\n",
            id="method-returns-a-value",
        ),
        pytest.param('    model_config = ConfigDict(extra="forbid")\n', id="class-level-call"),
        pytest.param("    retries: int = 4096 * 2\n", id="computed-default"),
        pytest.param('    mode: Literal["off", "on"] = "off"\n', id="literal-constraint"),
        pytest.param(
            '    wire_api: str = Field(default="systemone", description="Wire.")\n',
            id="field-call-with-literals",
        ),
        pytest.param(
            "    label: ChoiceConsumerConfig = Field(default_factory=ChoiceConsumerConfig)\n",
            id="field-factory-for-a-declared-class",
        ),
        pytest.param("    retries: int = Field(default=2, ge=0)\n", id="field-with-a-constraint"),
        pytest.param("    retries: int = Field(2)\n", id="positional-field-default"),
        pytest.param(
            "    retries: int = Field(default=2, description=RETRIES_HELP)\n",
            id="non-literal-description",
        ),
        pytest.param(
            "    retries: int = Field(default=len(DEFAULTS))\n", id="computed-field-default"
        ),
        pytest.param(
            "    ai: AIConfig = Field(default_factory=AIConfig)\n",
            id="factory-names-a-pre-existing-class",
        ),
        pytest.param(
            "    tags: list[str] = Field(default_factory=list)\n",
            id="factory-names-a-builtin",
        ),
        pytest.param(
            "    label: ChoiceConsumerConfig = ChoiceConsumerConfig()\n",
            id="default-constructs-a-class",
        ),
    ],
)
def test_declaration_stub_refuses_executable_behavior_in_the_stub(member: str) -> None:
    result = evaluate_tdd_evidence((INVARIANTS,), _stub_cycle(f"{DECISIONS}{member}"))

    assert not result.passed
    assert result.red_runs == ()


@pytest.mark.parametrize(
    "stub",
    [
        pytest.param(
            _stub().replace("class DecisionsConfig(BaseModel):", "@final\nclass DecisionsConfig:"),
            id="decorated-class",
        ),
        pytest.param(
            _stub().replace(
                "(BaseModel):\n    api_base", "(BaseModel, frozen=True):\n    api_base"
            ),
            id="class-keyword",
        ),
        pytest.param(f"{_stub()}\n\nDEFAULT_DECISIONS = DecisionsConfig()\n", id="module-call"),
    ],
)
def test_declaration_stub_refuses_behavior_around_the_classes(stub: str) -> None:
    evidence = _declaration_cycle(stub=_insertion(2, stub), closing=_edit(6, stub, BEHAVIOR))

    result = evaluate_tdd_evidence((INVARIANTS,), evidence)

    assert not result.passed
    assert result.red_runs == ()


@pytest.mark.parametrize(
    ("test", "evidence"),
    [
        pytest.param(EXPOSES, _declaration_cycle(), id="test-reaches-only-pre-existing-names"),
        pytest.param(SCALAR, _declaration_cycle(), id="named-test-fails-with-a-validation-error"),
        pytest.param(
            INVARIANTS,
            _declaration_cycle(red_output=COLLECTION_ERROR),
            id="red-is-a-collection-error",
        ),
        pytest.param(
            INVARIANTS,
            _declaration_cycle(
                closing=_edit(6, _stub(), _stub(f"{DECISIONS}    retries: int = 2\n"))
            ),
            id="closing-edit-only-declares",
        ),
        pytest.param(
            INVARIANTS,
            _declaration_cycle(closing=_edit(6, "", BEHAVIOR_MODULE, path=WIRING)),
            id="closing-edit-on-another-file",
        ),
        pytest.param(
            INVARIANTS,
            _declaration_cycle(extra_edit=_edit(3, "", BEHAVIOR_MODULE, path=WIRING)),
            id="behavior-edit-between-stub-and-red",
        ),
        pytest.param(
            INVARIANTS,
            _declaration_cycle(
                stub=replace(
                    _insertion(2, _stub()),
                    source_fragment=f"{ANCHOR_HEAD}{_stub()}\n\nclass AIConfig(BaseModel):\n"
                    "    pass\n",
                )
            ),
            id="stub-rewrites-its-anchor",
        ),
        pytest.param(
            INVARIANTS,
            _declaration_cycle(stub=_insertion(2, _stub(), session_id="peer")),
            id="stub-from-a-foreign-session",
        ),
        pytest.param(
            INVARIANTS,
            _declaration_cycle(extra_edit=_edit(3, "", "from elsewhere import DecisionsConfig\n")),
            id="import-rebinds-the-declared-class",
        ),
    ],
)
def test_declaration_stub_refuses_an_unattributable_red(
    test: AcceptanceTest, evidence: TranscriptEvidence
) -> None:
    result = evaluate_tdd_evidence((test,), evidence)

    assert not result.passed
    assert result.red_runs == ()


@pytest.mark.parametrize("snapshot", [False, True], ids=["member-edit", "full-write"])
def test_declaration_stub_credits_behavior_added_by_different_edit_shapes(snapshot: bool) -> None:
    old = "    timeout_seconds: float = 2.0\n"
    new = "    timeout_seconds: float = Field(default=2.0, gt=0)\n"
    closing = _edit(6, old, new)
    if snapshot:
        closing = replace(
            closing,
            tool_name="Write",
            source_replaced=None,
            source_fragment=None,
            source_after=_stub().replace(old, new),
        )
    evidence = _declaration_cycle(closing=closing)

    result = evaluate_tdd_evidence((INVARIANTS,), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (COMMAND,)
    assert result.green_runs == (COMMAND,)


def test_declaration_stub_allows_import_only_edits_before_red() -> None:
    evidence = _declaration_cycle(
        extra_edit=_edit(3, "", "from pydantic import BaseModel\n", path=WIRING)
    )

    result = evaluate_tdd_evidence((INVARIANTS,), evidence)

    assert result.passed, result.findings
    assert result.red_runs == (COMMAND,)


def test_declaration_stub_does_not_credit_an_uninvoked_lambda() -> None:
    evidence = _declaration_cycle()
    test_edit, *production_edits = evidence.edits
    evidence = replace(
        evidence,
        edits=(
            replace(
                test_edit,
                source_after=TEST_SOURCE.replace(
                    "        DecisionsConfig(api_base=", "        lambda: DecisionsConfig(api_base="
                ),
            ),
            *production_edits,
        ),
    )

    result = evaluate_tdd_evidence((INVARIANTS,), evidence)

    assert not result.passed
    assert result.red_runs == ()
