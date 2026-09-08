"""Regressions for the frozen cohort's question and domain inventory."""

import pytest
from validate_matrix import MATRIX_PATH, _validate_question_bindings


@pytest.mark.parametrize("identifier", ["Q01", "D01"])
@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unexpected"])
def test_question_bindings_reject_non_unique_or_incomplete_inventory(
    identifier: str, mutation: str
) -> None:
    matrix = MATRIX_PATH.read_text(encoding="utf-8")
    row = next(line for line in matrix.splitlines() if line.startswith(f"| {identifier} |"))
    if mutation == "duplicate":
        changed = matrix + "\n" + row
    elif mutation == "missing":
        changed = matrix.replace(row, "", 1)
    else:
        changed = matrix + "\n" + row.replace(identifier, f"{identifier[0]}99", 1)

    with pytest.raises(AssertionError, match="domain inventory|question key"):
        _validate_question_bindings(changed)
