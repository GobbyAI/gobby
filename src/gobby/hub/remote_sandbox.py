"""Hub-side sandbox records for node-reported runs (behavior-free placeholder for #24393)."""

from __future__ import annotations

from gobby.hub.node_commands import ReportBody
from gobby.storage.agents import LocalAgentRunManager


def redact_report_sandbox(report: ReportBody) -> ReportBody:
    return report


def apply_sandbox_records(
    run_manager: LocalAgentRunManager, machine_id: str, report: ReportBody
) -> None:
    return None
