"""Daemon request helpers for the build CLI."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import click

from gobby.build import BuildOptions, BuildResult, DispatcherTickSummary

from ._build_options import _stage_cap_options

logger = logging.getLogger("gobby.cli.build")

DAEMON_BUILD_REQUEST_TIMEOUT_SECONDS = 900.0
_PROFILE_ERROR_RE = re.compile(
    r"^(?:"
    r"build profile(?:s)?\b|"
    r"unknown build profile\b|"
    r"duplicate build profile\b|"
    r"malformed build profiles\b|"
    r"bundled build profiles\b|"
    r"build profile name\b|"
    r"delivery_target_repo\b|"
    r"source must be installed or project\b|"
    r"installed build profiles must be global\b"
    r")",
    re.IGNORECASE,
)


class BuildProfileClickException(click.ClickException):
    exit_code = 4


def _build_payload(
    opts: BuildOptions,
    input_ref: str,
    *,
    project_id: str | None = None,
    cwd: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "input_ref": input_ref,
        "project_id": project_id,
        "cwd": cwd,
        "project_explicit": opts.project_explicit,
        "quick": opts.quick,
        "skip_stages": opts.skip_stages,
        "no_merge": opts.no_merge,
        "pr": opts.pr,
        "stage": _stage_cap_options(opts.stage_caps),
        "target_branch": opts.target_branch,
        "agent": opts.assigned_agent,
        "reset_expansion_output": opts.reset_expansion_output,
        "max_active_agents": opts.max_active_agents,
        "max_retries": opts.max_retries,
        "planning_seed_state": opts.planning_seed_state,
        "completed_plan_review_rounds": opts.completed_plan_review_rounds,
        "dry_run": opts.dry_run,
    }
    if opts.coordinator_session_ref:
        payload["coordinator"] = opts.coordinator_session_ref
    if opts.isolation_explicit:
        payload["isolation"] = opts.isolation
    return payload


def _restart_options_payload(opts: BuildOptions) -> dict[str, object]:
    payload: dict[str, object] = {
        "skip_stages": opts.skip_stages,
        "no_merge": opts.no_merge,
        "stage": _stage_cap_options(opts.stage_caps),
        "planning_seed_state": opts.planning_seed_state,
        "completed_plan_review_rounds": opts.completed_plan_review_rounds,
        "project_explicit": opts.project_explicit,
    }
    if opts.pr is not None:
        payload["pr"] = opts.pr
    if opts.target_branch is not None:
        payload["target_branch"] = opts.target_branch
    if opts.assigned_agent is not None:
        payload["agent"] = opts.assigned_agent
    if opts.max_retries is not None:
        payload["max_retries"] = opts.max_retries
    if opts.coordinator_session_ref:
        payload["coordinator"] = opts.coordinator_session_ref
    if opts.isolation_explicit:
        payload["isolation"] = opts.isolation
    return payload


def _result_from_payload(payload: dict[str, object]) -> BuildResult:
    tick_payload = payload.get("dispatcher_tick")
    dispatcher_tick = (
        _dispatcher_tick_from_payload(tick_payload)
        if isinstance(tick_payload, dict)
        else DispatcherTickSummary(ticks=_payload_int(payload.get("tick_dispatched")))
    )
    manifest = payload.get("manifest")
    return BuildResult(
        task_id=str(payload["task_id"]),
        created=bool(payload["created"]),
        initial_lifecycle=str(payload["initial_lifecycle"]),
        applied_stages_skipped=_payload_string_list(payload.get("applied_stages_skipped")),
        tick_dispatched=_payload_int(payload.get("tick_dispatched"), dispatcher_tick.ticks),
        dispatcher_tick=dispatcher_tick,
        manifest=manifest if isinstance(manifest, list) else None,
        warnings=_payload_string_list(payload.get("warnings")),
        dry_run=bool(payload.get("dry_run")),
    )


def _payload_int(value: object, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _dispatcher_tick_from_payload(payload: dict[object, object]) -> DispatcherTickSummary:
    reason = payload.get("reason")
    cap_reached = payload.get("cap_reached")
    return DispatcherTickSummary(
        ticks=_payload_int(payload.get("ticks")),
        scanned=_payload_int(payload.get("scanned")),
        executed=_payload_int(payload.get("executed")),
        skipped=_payload_int(payload.get("skipped")),
        cap_reached=cap_reached if isinstance(cap_reached, bool) else False,
        reason=reason if isinstance(reason, str) else None,
    )


def _payload_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _try_daemon_build(
    input_ref: str,
    opts: BuildOptions,
    *,
    project_id: str | None = None,
    cwd: str | None = None,
) -> BuildResult | None:
    try:
        import httpx

        from gobby.config.app import load_config
        from gobby.utils.daemon_client import DaemonClient

        config = load_config()
        client = DaemonClient(port=config.daemon_port, timeout=5.0)
        is_healthy, _ = client.check_health()
        if not is_healthy:
            return None
        response = client.call_http_api(
            "/api/build",
            method="POST",
            json_data=_build_payload(opts, input_ref, project_id=project_id, cwd=cwd),
            timeout=DAEMON_BUILD_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return _result_from_payload(response.json())
        if response.status_code == 400:
            detail = _daemon_error_detail(response)
            message = _daemon_error_message(detail)
            if _is_profile_error(detail, response.headers):
                raise BuildProfileClickException(message)
            raise click.ClickException(message)
        return None
    except click.ClickException:
        raise
    except httpx.TimeoutException as exc:
        raise click.ClickException(
            "Daemon build request timed out before the initial dispatcher result returned. "
            "The daemon may still be running accepted build work; local fallback was skipped. "
            "Check progress with `gobby agents runs list --status running` or rerun "
            f"`gobby build {input_ref}` later."
        ) from exc
    except Exception:
        logger.debug("Daemon build request failed; falling back to local build", exc_info=True)
        return None


def _try_daemon_build_control(
    action: str,
    *,
    input_ref: str | None = None,
    project_id: str | None = None,
    cwd: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    yes: bool = False,
    no_resume: bool = False,
    opts: BuildOptions | None = None,
) -> dict[str, object] | None:
    try:
        import httpx

        from gobby.config.app import load_config
        from gobby.utils.daemon_client import DaemonClient

        config = load_config()
        client = DaemonClient(port=config.daemon_port, timeout=5.0)
        is_healthy, _ = client.check_health()
        if not is_healthy:
            return None
        payload: dict[str, object] = {
            "input_ref": input_ref,
            "project_id": project_id,
            "cwd": cwd,
            "dry_run": dry_run,
            "force": force,
            "yes": yes,
            "no_resume": no_resume,
        }
        if opts is not None:
            payload.update(_restart_options_payload(opts))
        response = client.call_http_api(
            f"/api/build/{action}",
            method="POST",
            json_data=payload,
            timeout=DAEMON_BUILD_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, dict):
                return _control_payload_from_daemon(payload)
            return None
        if response.status_code == 400:
            raise click.ClickException(_daemon_error_message(_daemon_error_detail(response)))
        return None
    except click.ClickException:
        raise
    except httpx.TimeoutException as exc:
        raise click.ClickException(
            "Daemon build control request timed out before the dispatcher result returned. "
            "The daemon may still be running accepted build work; local fallback was skipped. "
            "Check progress with `gobby agents runs list --status running` or rerun "
            f"`gobby build {action}` later."
        ) from exc
    except Exception:
        logger.debug(
            "Daemon build control request failed; falling back to local control",
            exc_info=True,
        )
        return None


def _control_payload_from_daemon(payload: dict[str, object]) -> dict[str, object] | None:
    if payload.get("success") is True:
        result = payload.get("result")
        if isinstance(result, dict):
            return {str(key): value for key, value in result.items()}
        return None
    if payload.get("success") is False:
        raise click.ClickException(_daemon_error_message(payload))
    return payload


def _daemon_error_detail(response: Any) -> Any:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        return payload.get("detail", payload)
    return payload


def _daemon_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or detail.get("error")
        if isinstance(message, Mapping):
            nested = message.get("message") or message.get("detail") or message.get("error")
            if nested is not None:
                return str(nested)
        return str(message) if message is not None else str(detail)
    return str(detail)


def _is_profile_error(detail: Any, headers: Mapping[str, str] | None = None) -> bool:
    if headers is not None and headers.get("X-Error-Type") == "build_profile":
        return True
    if isinstance(detail, dict):
        structured_seen = False
        for key in ("error_code", "type", "error_type", "code"):
            value = detail.get(key)
            if value is None:
                continue
            structured_seen = True
            return value in {
                "BUILD_PROFILE_ERROR",
                "BuildProfileError",
                "build_profile_error",
                "build_profile",
            }
        if structured_seen:
            return False
        message = detail.get("message") or detail.get("detail") or detail.get("error")
        return isinstance(message, str) and bool(_PROFILE_ERROR_RE.search(message))
    if isinstance(detail, str):
        return bool(_PROFILE_ERROR_RE.search(detail))
    return False
