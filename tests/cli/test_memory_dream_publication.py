"""Dream status keeps report publication distinct from maintenance status."""

from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.memory.dream import memory_dream_status

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status", ["pending", "failed", "completed"])
def test_dream_status_displays_publication_and_committed_references(status: str) -> None:
    publication = {"status": status}
    if status == "completed":
        publication.update(
            commit_sha="abc123",
            report_path="docs/reports/dream/run-1.md",
            branch_name="reports/dream/run-1",
        )
    request = Mock(
        return_value={
            "success": True,
            "run": {"id": "run-1", "status": "completed", "publication": publication},
        }
    )
    with patch("gobby.cli.memory.dream._request", request):
        result = CliRunner().invoke(memory_dream_status, ["run-1"])
    assert result.exit_code == 0, result.output
    assert "Status: completed" in result.output
    assert f"Report publication: {status}" in result.output
    if status == "completed":
        assert "docs/reports/dream/run-1.md @ abc123 (reports/dream/run-1)" in result.output
    request.assert_called_once()
    assert request.call_args.args[1] == "/memory/dream/run-1"
