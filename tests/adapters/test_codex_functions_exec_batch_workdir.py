"""A managed functions.exec batch is allowed only when every call has its own literal workdir."""

from __future__ import annotations

import pytest

from gobby.adapters.codex_impl.execution_chain import validate_functions_exec_wrapper

pytestmark = pytest.mark.unit

LITERAL_A = 'text(await tools.exec_command({cmd:"A",workdir:"/task"}));'
LITERAL_B = 'text(await tools.exec_command({cmd:"B",workdir:"/task"}));'
BATCH_MISSING_WORKDIR = 'text(await tools.exec_command({cmd:"A"}));' + LITERAL_B
BATCH_COMPUTED_WORKDIR = 'const w = "/task"; text(await tools.exec_command({cmd:"A",workdir:w}));'


def test_batch_of_literal_absolute_workdirs_is_allowed() -> None:
    assert validate_functions_exec_wrapper(LITERAL_A + LITERAL_B) is None


@pytest.mark.parametrize("batch", [BATCH_MISSING_WORKDIR, BATCH_COMPUTED_WORKDIR + LITERAL_B])
def test_batch_with_a_missing_or_computed_workdir_is_refused(batch: str) -> None:
    reason = validate_functions_exec_wrapper(batch)
    assert reason is not None
    assert "exactly one static exec_command" in reason
