"""Option parsing helpers for the build CLI."""

from __future__ import annotations

import click

from gobby.build import BuildOptions
from gobby.config.build import StageCapOverride


def _parse_skip_stages(raw_values: tuple[str, ...]) -> list[str]:
    stages: list[str] = []
    for raw in raw_values:
        stages.extend(stage.strip() for stage in raw.split(",") if stage.strip())
    return stages


def _parse_stage_cap(raw_values: tuple[str, ...]) -> list[StageCapOverride]:
    by_stage: dict[str, dict[str, int | None]] = {}
    for raw in raw_values:
        stage_name, separator, cap_text = raw.partition(":")
        stage_name = stage_name.strip()
        if not stage_name:
            raise click.ClickException("--stage requires a stage name")
        stage_caps = by_stage.setdefault(stage_name, {})
        if not separator:
            continue
        for cap_item in (item.strip() for item in cap_text.split(",") if item.strip()):
            cap_name, cap_separator, value_text = cap_item.partition("=")
            if not cap_separator:
                raise click.ClickException("--stage setting must use <name>=<value>")
            cap_name = cap_name.strip()
            if cap_name not in {"max_work_attempts", "max_review_rounds"}:
                raise click.ClickException(
                    "--stage setting must be max_work_attempts or max_review_rounds"
                )
            try:
                value = int(value_text)
            except ValueError as exc:
                raise click.ClickException("--stage setting value must be an integer") from exc
            stage_caps[cap_name] = value
    return [
        StageCapOverride(
            stage_name=stage_name,
            max_work_attempts=values.get("max_work_attempts"),
            max_review_rounds=values.get("max_review_rounds"),
        )
        for stage_name, values in by_stage.items()
    ]


def _stage_cap_options(stage_caps: list[StageCapOverride]) -> list[str]:
    options: list[str] = []
    for cap in stage_caps:
        settings = []
        if cap.max_work_attempts is not None:
            settings.append(f"max_work_attempts={cap.max_work_attempts}")
        if cap.max_review_rounds is not None:
            settings.append(f"max_review_rounds={cap.max_review_rounds}")
        suffix = f":{','.join(settings)}" if settings else ""
        options.append(f"{cap.stage_name}{suffix}")
    return options


def _restart_options_were_supplied(opts: BuildOptions) -> bool:
    return bool(
        opts.skip_stages
        or opts.isolation_explicit
        or opts.no_merge
        or opts.pr is not None
        or opts.stage_caps
        or opts.target_branch is not None
        or opts.assigned_agent is not None
        or opts.max_retries is not None
        or opts.planning_seed_state != "drafted"
        or opts.completed_plan_review_rounds != 0
        or opts.coordinator_session_ref is not None
    )
