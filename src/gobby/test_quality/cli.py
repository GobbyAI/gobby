"""Click commands for static test-quality audits."""

from __future__ import annotations

from pathlib import Path

import click

from gobby.test_quality.analyzer import audit_paths
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
from gobby.test_quality.render import render_json, render_text


@click.group(name="test-quality")
def test_quality() -> None:
    """Audit test quality."""


@test_quality.command("audit")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--write-baseline", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--baseline", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--fail-on-new", is_flag=True)
@click.option(
    "--min-severity",
    type=click.Choice(["low", "medium", "high"]),
    default="high",
    show_default=True,
)
def audit(
    paths: tuple[Path, ...],
    output_format: str,
    output: Path | None,
    write_baseline: Path | None,
    baseline: Path | None,
    fail_on_new: bool,
    min_severity: Severity,
) -> None:
    """Run the static test-quality audit."""

    if fail_on_new and baseline is None:
        raise click.ClickException("--fail-on-new requires --baseline")

    audit_paths_input = paths or (Path("tests"),)
    report = audit_paths(audit_paths_input, root=Path.cwd())

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
                AuditBaseline(path=str(baseline), fingerprints=frozenset()),
                min_severity=min_severity,
                baseline_status="missing",
                baseline_mode="current-issues-as-new",
                warning_message=BASELINE_MISSING_MESSAGE,
            )

    if write_baseline is not None:
        write_baseline_file(report, write_baseline)

    rendered = render_json(report, diff) if output_format == "json" else render_text(report, diff)
    if output is None:
        click.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")

    if fail_on_new and diff is not None and diff.failing_issues:
        raise click.exceptions.Exit(1)
