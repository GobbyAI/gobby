"""Click commands for mypy-based test type audits."""

from __future__ import annotations

from pathlib import Path

import click

from gobby.test_quality.baseline import (
    BASELINE_MISSING_MESSAGE,
    AuditBaseline,
    diff_report,
    load_baseline,
)
from gobby.test_quality.baseline import (
    write_baseline as write_baseline_file,
)
from gobby.test_quality.models import Severity
from gobby.test_types._mypy import MypyInvocationError
from gobby.test_types.audit import audit_types_paths
from gobby.test_types.render import render_json, render_text


@click.group(name="test-types")
def test_types() -> None:
    """Audit Python test types."""


@test_types.command("audit")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--write-baseline", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--allow-failing-baseline",
    is_flag=True,
    help="Write the baseline even when --fail-on-new fails.",
)
@click.option("--baseline", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--fail-on-new", is_flag=True)
@click.option(
    "--min-severity",
    type=click.Choice(["low", "medium", "high"]),
    default="high",
    show_default=True,
)
@click.option(
    "--mypy-command",
    help="Override mypy resolution with a shell-style command string.",
)
def audit(
    paths: tuple[Path, ...],
    output_format: str,
    output: Path | None,
    write_baseline: Path | None,
    allow_failing_baseline: bool,
    baseline: Path | None,
    fail_on_new: bool,
    min_severity: Severity,
    mypy_command: str | None,
) -> None:
    """Run the mypy ratchet over Python test files."""

    if fail_on_new and baseline is None:
        raise click.ClickException("--fail-on-new requires --baseline")
    if allow_failing_baseline and write_baseline is None:
        raise click.ClickException("--allow-failing-baseline requires --write-baseline")

    audit_paths_input = paths or (Path("tests"),)
    try:
        report = audit_types_paths(
            audit_paths_input,
            root=Path.cwd(),
            mypy_command=mypy_command,
        )
    except (MypyInvocationError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    diff = None
    if baseline is not None:
        if baseline.exists():
            try:
                diff = diff_report(
                    report,
                    load_baseline(baseline),
                    min_severity=min_severity,
                )
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
        else:
            diff = diff_report(
                report,
                AuditBaseline(path=str(baseline), issue_counts={}),
                min_severity=min_severity,
                baseline_status="missing",
                baseline_mode="current-issues-as-new",
                warning_message=BASELINE_MISSING_MESSAGE,
            )

    zero_file_audit = any(warning.code == "NO_ANALYZABLE_FILES" for warning in report.warnings)
    audit_failed = fail_on_new and (
        zero_file_audit or (diff is not None and bool(diff.failing_issues))
    )
    baseline_write_refused = (
        write_baseline is not None and audit_failed and not allow_failing_baseline
    )
    if write_baseline is not None and not baseline_write_refused:
        write_baseline_file(report, write_baseline)

    rendered = render_json(report, diff) if output_format == "json" else render_text(report, diff)
    if output is None:
        click.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")

    if baseline_write_refused:
        raise click.ClickException(
            "refusing to write a baseline from a failing audit; "
            "pass --allow-failing-baseline to explicitly accept the current failures"
        )
    if audit_failed:
        raise click.exceptions.Exit(1)
