from __future__ import annotations

import json
from typing import Any

_FAILED_RUN_STATUSES = frozenset({"failed", "failure", "error", "timeout", "degraded"})
WIKI_HEALTH_HISTORY_SAMPLE_SIZE = 10

_HEALTH_HISTORY_LIST_FIELDS = (
    "broken_links",
    "stale_pages",
    "stale_citations",
    "uncited_sources",
    "uncompiled_sources",
    "duplicate_concepts",
)


def _history_output(
    *,
    purpose: str,
    scope: str,
    command: str,
    gwiki_result: dict[str, Any],
    changed_paths: list[str] | None = None,
    extra_error: str | None = None,
) -> str:
    payload = _payload(gwiki_result)
    status = _status(gwiki_result, payload)
    error = _run_error(gwiki_result, payload, command=command, status=status)
    if extra_error:
        error = f"{error}; {extra_error}" if error else extra_error
    return _history_output_json(
        purpose=purpose,
        scope=scope,
        command=command,
        status=status,
        error=error,
        result=_visible_result(gwiki_result, payload),
        changed_paths=changed_paths,
    )


def _librarian_history_output(
    *,
    purpose: str,
    scope: str,
    gwiki_result: dict[str, Any],
    task_filing: dict[str, Any],
) -> str:
    """Compact librarian history so cron output stays inside executor limits."""
    payload = _payload(gwiki_result)
    result = _visible_health_result(gwiki_result, _compact_librarian_payload(payload))
    result["task_filing"] = task_filing
    status = _status(gwiki_result, payload)
    return _history_output_json(
        purpose=purpose,
        scope=scope,
        command="librarian",
        status=status,
        error=_run_error(gwiki_result, payload, command="librarian", status=status),
        result=result,
    )


def _compact_librarian_payload(
    payload: dict[str, Any],
    *,
    sample_size: int = WIKI_HEALTH_HISTORY_SAMPLE_SIZE,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "checks" and isinstance(value, list):
            compact[key] = [_compact_librarian_check(check, sample_size) for check in value]
        elif key == "suggested_tasks" and isinstance(value, list):
            compact["suggested_tasks_count"] = len(value)
        elif key == "suggested_patch_diffs" and isinstance(value, list):
            compact["suggested_patch_diffs_count"] = len(value)
            compact["suggested_patch_diffs_sample"] = [
                {"path": diff.get("path"), "summary": diff.get("summary")}
                for diff in value[:sample_size]
                if isinstance(diff, dict)
            ]
        elif key in ("artifacts", "degradation", "dependency_classification", "error"):
            compact[key] = value
        elif _is_json_scalar(value):
            compact[key] = value
    return compact


def _compact_librarian_check(check: Any, sample_size: int) -> dict[str, Any]:
    if not isinstance(check, dict):
        return {"items_count": 0}
    compact = {key: value for key, value in check.items() if _is_json_scalar(value)}
    items = check.get("items")
    if isinstance(items, list):
        compact["items_count"] = len(items)
        compact["items_sample"] = items[:sample_size]
    return compact


def _health_history_output(
    *,
    purpose: str,
    scope: str,
    command: str,
    gwiki_result: dict[str, Any],
) -> str:
    payload = _payload(gwiki_result)
    status = _status(gwiki_result, payload)
    return _history_output_json(
        purpose=purpose,
        scope=scope,
        command=command,
        status=status,
        error=_run_error(gwiki_result, payload, command=command, status=status),
        result=_visible_health_result(gwiki_result, _compact_health_payload(payload)),
    )


def _history_output_json(
    *,
    purpose: str,
    scope: str,
    command: str,
    status: str,
    result: dict[str, Any],
    error: str | None = None,
    changed_paths: list[str] | None = None,
) -> str:
    # The cron executor parses JSON handler output and coerces top-level
    # ok/error into the run outcome, so failed and degraded gwiki results
    # record failed runs instead of silently completing.
    output: dict[str, Any] = {
        "purpose": purpose,
        "scope": scope,
        "command": command,
        "status": status,
        "ok": error is None,
        "result": result,
    }
    if error is not None:
        output["error"] = error
    if changed_paths is not None:
        output["changed_paths"] = changed_paths
    return json.dumps(output, sort_keys=True)


def _visible_result(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    visible = dict(payload)
    visible["gwiki"] = {
        "ok": result.get("ok"),
        "command": result.get("command"),
        "payload": payload,
        "stderr": result.get("stderr", ""),
    }
    for passthrough in ("index_handoff", "task_filing", "presync"):
        if passthrough in result:
            visible[passthrough] = result[passthrough]
    return visible


def _visible_health_result(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    visible = dict(payload)
    visible["gwiki"] = {
        "ok": result.get("ok"),
        "command": result.get("command"),
        "stderr": result.get("stderr", ""),
    }
    return visible


def _compact_health_payload(
    payload: dict[str, Any],
    *,
    sample_size: int = WIKI_HEALTH_HISTORY_SAMPLE_SIZE,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _HEALTH_HISTORY_LIST_FIELDS and isinstance(value, list):
            compact[f"{key}_count"] = len(value)
            compact[f"{key}_sample"] = value[:sample_size]
        elif _is_json_scalar(value):
            compact[key] = value
    return compact


def _is_json_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else result


def _status(result: dict[str, Any], payload: dict[str, Any]) -> str:
    status = payload.get("status") or result.get("status")
    if isinstance(status, str) and status:
        return status
    return "completed" if result.get("ok") else "failed"


def _run_error(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    command: str,
    status: str,
) -> str | None:
    """Error text for gwiki results that must record a failed cron run."""
    if index_handoff_error := _index_handoff_degradation(result):
        return index_handoff_error
    if result.get("ok") is not False and status.lower() not in _FAILED_RUN_STATUSES:
        return None
    for candidate in (result.get("error"), payload.get("error")):
        if isinstance(candidate, dict):
            message = candidate.get("message") or candidate.get("type")
            if isinstance(message, str) and message:
                return message
        if isinstance(candidate, str) and candidate:
            return candidate
    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip()
    return f"gwiki {command} reported status '{status}'"


def _index_handoff_degradation(result: dict[str, Any]) -> str | None:
    handoff = result.get("index_handoff")
    if not isinstance(handoff, dict) or handoff.get("status") != "degraded":
        return None
    degradation = handoff.get("degradation")
    if isinstance(degradation, dict):
        message = degradation.get("message") or degradation.get("type")
        if isinstance(message, str) and message:
            return f"index handoff degraded: {message}"
    if isinstance(degradation, str) and degradation:
        return f"index handoff degraded: {degradation}"
    return "index handoff reported status 'degraded'"
