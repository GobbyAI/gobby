from __future__ import annotations

from collections.abc import Callable

import pytest

from gobby.storage.daemon_resume_keys import (
    daemon_resume_consumed_condition,
    daemon_resume_unconsumed_condition,
)


@pytest.mark.parametrize(
    "condition",
    [daemon_resume_consumed_condition, daemon_resume_unconsumed_condition],
)
def test_daemon_resume_condition_rejects_untrusted_column(
    condition: Callable[[object, str], str],
) -> None:
    with pytest.raises(ValueError, match="Unsupported daemon resume metadata column"):
        condition(object(), "ar.resume_metadata_json; DROP TABLE agent_runs")
