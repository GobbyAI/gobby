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
            ("POST", "/api/feedback/review"): {
                "success": True,
                "status": "completed",
                "run_id": "run-1",
                "rows_considered": 5,
                "tasks_filed": 2,
                "deduplicated": 1,
            },
            ("GET", "/api/feedback/review/run-1"): {
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
            ("POST", "/api/feedback/review"): {
                "success": True,
                "status": "completed",
                "run_id": "run-2",
                "rows_considered": 3,
                "tasks_filed": 0,
                "deduplicated": 0,
            },
            ("GET", "/api/feedback/review/run-2"): {"success": True, "run": {"id": "run-2"}},
        }
    )

    result = _invoke(requests, ["review", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert requests.calls[0]["json_data"] == {"dry_run": True}
    assert "Dry run" in result.output
    assert "(no digest recorded)" in result.output


def test_review_reports_empty_backlog_without_digest_fetch() -> None:
    requests = _FakeRequests(
        {("POST", "/api/feedback/review"): {"success": True, "status": "no_rows", "run_id": None}}
    )

    result = _invoke(requests, ["review"])

    assert result.exit_code == 0, result.output
    assert "No unreviewed feedback rows." in result.output
    assert len(requests.calls) == 1


def test_digest_defaults_to_latest_run() -> None:
    requests = _FakeRequests(
        {
            ("GET", "/api/feedback/review/latest"): {
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


@pytest.mark.parametrize("command", ["observations", "results"])
def test_evidence_commands_forward_page_bounds(command: str) -> None:
    endpoint = f"/api/feedback/review/run-3/{command}?offset=2&limit=1"
    requests = _FakeRequests({("GET", endpoint): {"run_id": "run-3", "next_offset": 3}})
    result = _invoke(requests, [command, "run-3", "--offset", "2", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert '"next_offset": 3' in result.output
    assert requests.calls[0]["endpoint"] == endpoint


def test_digest_by_run_id_surfaces_error_field() -> None:
    requests = _FakeRequests(
        {
            ("GET", "/api/feedback/review/run-4"): {
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


_ENTRY = {
    "id": "fb-1",
    "session_id": "session-1",
    "source": "survey",
    "kind": "bug",
    "kind_other_label": None,
    "evidence": "hook blocked the read\nsecond line",
    "impact": "impact",
    "frequency": "always",
    "suggestion": None,
    "disposition": "fixed",
    "created_at": "2026-08-01T12:00:00+00:00",
    "reviewed": False,
    "review_run_id": None,
}


def test_list_forwards_filters_and_prints_one_line_per_entry() -> None:
    endpoint = (
        "/api/feedback/entries?limit=5&unreviewed=true&kind=bug&frequency=always&disposition=fixed"
    )
    requests = _FakeRequests({("GET", endpoint): {"success": True, "entries": [_ENTRY]}})

    result = _invoke(
        requests,
        [
            "list",
            "--limit",
            "5",
            "--unreviewed",
            "--kind",
            "bug",
            "--frequency",
            "always",
            "--disposition",
            "fixed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert requests.calls[0]["endpoint"] == endpoint
    assert result.output.splitlines() == [
        "fb-1  2026-08-01T12:00:00+00:00  bug/always  fixed  unreviewed  hook blocked the read"
    ]


def test_list_defaults_and_json_output() -> None:
    requests = _FakeRequests(
        {("GET", "/api/feedback/entries?limit=50"): {"success": True, "entries": [_ENTRY]}}
    )

    result = _invoke(requests, ["list", "--json"])

    assert result.exit_code == 0, result.output
    assert '"id": "fb-1"' in result.output
    empty = _FakeRequests({("GET", "/api/feedback/entries?limit=50"): {"entries": []}})
    assert _invoke(empty, ["list"]).output == "No feedback entries.\n"


def test_list_rejects_unknown_kind_before_calling_daemon() -> None:
    requests = _FakeRequests({})

    result = _invoke(requests, ["list", "--kind", "unknown"])

    assert result.exit_code == 2
    assert requests.calls == []


def test_status_prints_backlog_latest_run_and_schedule() -> None:
    requests = _FakeRequests(
        {
            ("GET", "/api/feedback/status"): {
                "success": True,
                "backlog": 7,
                "latest_run": {
                    "id": "run-3",
                    "status": "completed",
                    "dry_run": False,
                    "rows_considered": 4,
                    "created_at": "2026-08-01T03:00:00+00:00",
                },
                "schedule": {
                    "enabled": True,
                    "cron_expr": "0 3 * * *",
                    "timezone": "America/Chicago",
                    "next_run_at": "2026-08-02T08:00:00+00:00",
                    "last_status": "completed",
                },
            }
        }
    )

    result = _invoke(requests, ["status"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Backlog: 7 unreviewed",
        "Latest run: run-3  completed  rows=4  2026-08-01T03:00:00+00:00",
        "Schedule: 0 3 * * * (America/Chicago)  enabled  "
        "next=2026-08-02T08:00:00+00:00  last=completed",
    ]


def test_status_reports_missing_run_and_schedule() -> None:
    requests = _FakeRequests(
        {
            ("GET", "/api/feedback/status"): {
                "success": True,
                "backlog": 0,
                "latest_run": None,
                "schedule": None,
            }
        }
    )

    result = _invoke(requests, ["status"])
    as_json = _invoke(requests, ["status", "--json"])

    assert result.output.splitlines() == [
        "Backlog: 0 unreviewed",
        "Latest run: none",
        "Schedule: not registered",
    ]
    assert '"backlog": 0' in as_json.output
