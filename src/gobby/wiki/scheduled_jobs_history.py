from __future__ import annotations

import json
from typing import Any

_FAILED_RUN_STATUSES = frozenset({"failed", "failure", "error", "timeout", "degraded"})
WIKI_HEALTH_HISTORY_SAMPLE_SIZE = 10
WIKI_REFRESH_HISTORY_SAMPLE_SIZE = 5
WIKI_VERBOSE_HISTORY_SAMPLE_SIZE = 3
WIKI_HISTORY_SAMPLE_TEXT_MAX_CHARS = 300

# Refresh payloads enumerate every catalog source per run; stored history keeps
# counts (and per-code samples) instead so cron output stays inside executor
# limits. `refreshed` stays verbatim — it holds only what actually changed.
_REFRESH_GROUPED_LIST_FIELDS = ("failed", "skipped")
_REFRESH_COUNTED_LIST_FIELDS = ("planned", "unchanged")
_AUDIT_COUNTED_LIST_FIELDS = ("claims", "unsupported_claims", "source_context")
_SYNC_SESSIONS_COUNTED_LIST_FIELDS = ("accepted", "skipped", "failed", "reconciled")

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
    if command == "refresh":
        visible_payload = _compact_refresh_payload(payload)
    elif command == "audit":
        visible_payload = _compact_counted_list_payload(
            payload,
            fields=_AUDIT_COUNTED_LIST_FIELDS,
        )
    elif command == "sync-sessions":
        visible_payload = _compact_counted_list_payload(
            payload,
            fields=_SYNC_SESSIONS_COUNTED_LIST_FIELDS,
        )
    else:
        visible_payload = payload
    return _history_output_json(
        purpose=purpose,
        scope=scope,
        command=command,
        status=status,
        error=error,
        result=_visible_result(gwiki_result, visible_payload),
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
    result = _visible_result(gwiki_result, _compact_librarian_payload(payload))
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
        result=_visible_result(gwiki_result, _compact_health_payload(payload)),
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
        "stderr": result.get("stderr", ""),
    }
    for passthrough in ("index_handoff", "task_filing", "presync"):
        if passthrough in result:
            visible[passthrough] = result[passthrough]
    return visible


def _grouped_by_code(entries: list[Any], sample_size: int) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for entry in entries:
        code = entry.get("code") if isinstance(entry, dict) else None
        key = code if isinstance(code, str) and code else "unknown"
        group = groups.setdefault(key, {"code": key, "count": 0, "sample": []})
        group["count"] += 1
        if len(group["sample"]) < sample_size:
            group["sample"].append(entry)
    return list(groups.values())


def _compact_refresh_payload(
    payload: dict[str, Any],
    *,
    sample_size: int = WIKI_REFRESH_HISTORY_SAMPLE_SIZE,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _REFRESH_GROUPED_LIST_FIELDS and isinstance(value, list):
            compact[f"{key}_count"] = len(value)
            compact[key] = _grouped_by_code(value, sample_size)
        elif key in _REFRESH_COUNTED_LIST_FIELDS and isinstance(value, list):
            compact[f"{key}_count"] = len(value)
        else:
            compact[key] = value
    return compact


def _compact_counted_list_payload(
    payload: dict[str, Any],
    *,
    fields: tuple[str, ...],
    sample_size: int = WIKI_VERBOSE_HISTORY_SAMPLE_SIZE,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in fields and isinstance(value, list):
            compact[f"{key}_count"] = len(value)
            compact[f"{key}_sample"] = [
                _compact_history_sample(item) for item in value[:sample_size]
            ]
        else:
            compact[key] = value
    return compact


def _compact_history_sample(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= WIKI_HISTORY_SAMPLE_TEXT_MAX_CHARS:
            return value
        return value[: WIKI_HISTORY_SAMPLE_TEXT_MAX_CHARS - 3] + "..."
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, nested in value.items():
            if isinstance(nested, list):
                compact[f"{key}_count"] = len(nested)
            else:
                compact[key] = _compact_history_sample(nested)
        return compact
    if isinstance(value, list):
        return {"count": len(value)}
    return value


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
    if command == "upkeep" and (upkeep_error := _upkeep_failures_error(payload)):
        return upkeep_error
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


def _upkeep_failures_error(payload: dict[str, Any]) -> str | None:
    failures = payload.get("failures")
    if not isinstance(failures, int) or isinstance(failures, bool) or failures <= 0:
        return None
    representative_errors: list[str] = []
    clusters = payload.get("clusters")
    if isinstance(clusters, list):
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            error = cluster.get("error")
            if isinstance(error, str) and error and error not in representative_errors:
                representative_errors.append(error)
            if len(representative_errors) == 3:
                break
    label = "failure" if failures == 1 else "failures"
    summary = f"gwiki upkeep reported {failures} synthesis {label}"
    if representative_errors:
        summary = f"{summary}: {'; '.join(representative_errors)}"
    return summary


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
