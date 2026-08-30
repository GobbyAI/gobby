"""CLI contract for `gobby feedback review` and `gobby feedback digest`."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gobby.cli.feedback import feedback

pytestmark = pytest.mark.unit


class _FakeRequests:
    def __init__(self, responses: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        ctx: Any,
        endpoint: str,
        *,
        method: str,
        json_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {"endpoint": endpoint, "method": method, "json_data": json_data, "timeout": timeout}
        )
        return self.responses[(method, endpoint)]


def _invoke(requests: _FakeRequests, args: list[str]) -> Any:
    with patch("gobby.cli.feedback._request", requests):
        return CliRunner().invoke(feedback, args, obj=object())


def test_review_prints_summary_and_digest() -> None:
    requests = _FakeRequests(
        {
            ("POST", "/feedback/review"): {
                "success": True,
                "status": "completed",
                "run_id": "run-1",
                "rows_considered": 5,
                "tasks_filed": 2,
                "deduplicated": 1,
            },
            ("GET", "/feedback/review/run-1"): {
                "success": True,
                "run": {"id": "run-1", "digest_md": "# Session-feedback review digest"},
            },
        }
    )

    result = _invoke(requests, ["review"])

    assert result.exit_code == 0, result.output
    assert "Review run: run-1" in result.output
    assert "Rows considered: 5" in result.output
    assert "Tasks filed: 2" in result.output
    assert "Deduplicated: 1" in result.output
    assert "# Session-feedback review digest" in result.output
    assert requests.calls[0]["json_data"] == {"dry_run": False}


def test_review_dry_run_flag_is_forwarded_and_labeled() -> None:
    requests = _FakeRequests(
        {
            ("POST", "/feedback/review"): {
                "success": True,
                "status": "completed",
                "run_id": "run-2",
                "rows_considered": 3,
                "tasks_filed": 0,
                "deduplicated": 0,
            },
            ("GET", "/feedback/review/run-2"): {"success": True, "run": {"id": "run-2"}},
        }
    )

    result = _invoke(requests, ["review", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert requests.calls[0]["json_data"] == {"dry_run": True}
    assert "Dry run" in result.output
    assert "(no digest recorded)" in result.output


def test_review_reports_empty_backlog_without_digest_fetch() -> None:
    requests = _FakeRequests(
        {("POST", "/feedback/review"): {"success": True, "status": "no_rows", "run_id": None}}
    )

    result = _invoke(requests, ["review"])

    assert result.exit_code == 0, result.output
    assert "No unreviewed feedback rows." in result.output
    assert len(requests.calls) == 1


def test_digest_defaults_to_latest_run() -> None:
    requests = _FakeRequests(
        {
            ("GET", "/feedback/review/latest"): {
                "success": True,
                "run": {
                    "id": "run-3",
                    "status": "completed",
                    "dry_run": False,
                    "rows_considered": 4,
                    "digest_md": "# Digest body",
                },
            }
        }
    )

    result = _invoke(requests, ["digest"])

    assert result.exit_code == 0, result.output
    assert "Review run: run-3" in result.output
    assert "# Digest body" in result.output


def test_digest_by_run_id_surfaces_error_field() -> None:
    requests = _FakeRequests(
        {
            ("GET", "/feedback/review/run-4"): {
                "success": True,
                "run": {
                    "id": "run-4",
                    "status": "failed",
                    "dry_run": False,
                    "rows_considered": 4,
                    "error": "provider unavailable",
                    "digest_md": None,
                },
            }
        }
    )

    result = _invoke(requests, ["digest", "--run-id", "run-4"])

    assert result.exit_code == 0, result.output
    assert "Error: provider unavailable" in result.output
    assert "(no digest recorded)" in result.output
