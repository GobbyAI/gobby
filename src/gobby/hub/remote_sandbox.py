"""Redact and persist sandbox records before acknowledging a node report."""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue

from gobby.hub.node_commands import ReportBody
from gobby.hub.node_reports import is_uuid
from gobby.storage.agents import LocalAgentRunManager
from gobby.utils.terminal_output import redact_terminal_output


def redact_report_sandbox(report: ReportBody) -> ReportBody:
    """Redact every string in exit events before they enter storage or the inbox."""
    exits = []
    for exit_record in report.exits:
        sandbox = exit_record.sandbox
        if sandbox is not None and sandbox.recent_violations is not None:
            events = [
                {redact_terminal_output(key): _redact(value) for key, value in event.items()}
                for event in sandbox.recent_violations
            ]
            exit_record = exit_record.model_copy(
                update={"sandbox": sandbox.model_copy(update={"recent_violations": events})}
            )
        exits.append(exit_record)
    return report.model_copy(update={"exits": exits})


def _redact(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return redact_terminal_output(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {redact_terminal_output(key): _redact(item) for key, item in value.items()}
    return value


def apply_sandbox_records(
    run_manager: LocalAgentRunManager, machine_id: str, report: ReportBody
) -> None:
    """Merge only the reporting machine's runs; database failures must propagate."""
    for final, entries in ((False, report.agents), (True, report.exits)):
        for entry in entries:
            sandbox = entry.sandbox
            if sandbox is None or not is_uuid(entry.run_id):
                continue
            run = run_manager.get(entry.run_id)
            if run is None or run.machine_id != machine_id:
                continue
            patch: dict[str, Any] = {
                "backend": "srt",
                "enforced": True,
                "runtime_version": sandbox.runtime_version,
                "policy_schema_version": sandbox.policy_schema_version,
                "policy_hash": sandbox.policy_hash,
                "violation_count": sandbox.violation_count,
                "denial_operations": sandbox.denial_operations,
            }
            if sandbox.violation_count_truncated:
                patch["violation_count_truncated"] = True
            if final:
                patch["recent_violations"] = sandbox.recent_violations
            else:
                stored = (run.resume_metadata_json or {}).get("sandbox")
                if (
                    isinstance(stored, dict)
                    and {
                        key: value
                        for key, value in stored.items()
                        if key not in ("report_revision", "report_final")
                    }
                    == patch
                ):
                    continue
            run_manager.merge_sandbox_metadata(
                run.id, patch, report_revision=report.revision, final=final
            )
