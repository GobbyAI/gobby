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
from gobby.test_types.suppressions import (
    SuppressionSite,
    diff_suppressions,
    load_suppression_baseline,
    scan_suppressions,
    write_suppression_baseline,
)


@click.group(name="test-types")
def test_types() -> None:
    """Audit Python test types."""


@test_types.command("suppressions")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path))
@click.option(
    "--baseline",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
@click.option(
    "--write-baseline",
    is_flag=True,
    help="Rewrite the baseline only when the current sites are a strict subset.",
)
def suppressions(paths: tuple[Path, ...], baseline: Path, write_baseline: bool) -> None:
    """Reject new, changed, or stale Python suppression sites."""
    if not baseline.exists():
        raise click.ClickException(f"suppression baseline does not exist: {baseline}")
    try:
        scan = scan_suppressions(paths or (Path("."),), root=Path.cwd())
        loaded_baseline = load_suppression_baseline(baseline)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    diff = diff_suppressions(scan.sites, loaded_baseline)

    click.echo("Python suppression ratchet")
    click.echo(f"Files scanned: {scan.files_scanned}")
    click.echo(f"Suppressions: {len(scan.sites)}")
    click.echo(f"Baseline: {len(loaded_baseline.entries)}")
    click.echo(f"New: {len(diff.new_sites)}")
    click.echo(f"Stale: {len(diff.stale_entries)}")
    _render_suppression_sites("New suppression sites", diff.new_sites)
    if diff.stale_entries:
        click.echo("Stale baseline sites:")
        for entry in diff.stale_entries:
            codes_value = entry["codes"]
            if not isinstance(codes_value, list):
                raise click.ClickException("suppression baseline contains invalid codes")
            codes = ",".join(str(code) for code in codes_value)
            directive = str(entry["directive"])
            suffix = f"[{codes}]" if codes and directive == "type: ignore" else ""
            suffix = f": {codes}" if codes and directive == "noqa" else suffix
            click.echo(
                f"  {entry['path']}::{entry['symbol']}: {directive}{suffix} on {entry['statement']}"
            )

    if write_baseline:
        if diff.new_sites:
            raise click.ClickException(
                "refusing to expand the suppression baseline; fix every new or changed site"
            )
        if not diff.stale_entries:
            raise click.ClickException(
                "refusing to rewrite the suppression baseline without a strict debt reduction"
            )
        write_suppression_baseline(baseline, scan.sites)
        click.echo(f"Baseline reduced to {len(scan.sites)} suppression sites.")
        return
    if diff.new_sites or diff.stale_entries:
        raise click.exceptions.Exit(1)


def _render_suppression_sites(title: str, sites: tuple[SuppressionSite, ...]) -> None:
    if not sites:
        return
    click.echo(f"{title}:")
    for site in sites:
        codes = ",".join(site.codes)
        suffix = f"[{codes}]" if codes and site.directive == "type: ignore" else ""
        suffix = f": {codes}" if codes and site.directive == "noqa" else suffix
        click.echo(
            f"  {site.path}:{site.line}::{site.symbol}: {site.directive}{suffix} "
            f"on {site.statement}"
        )


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
    if output is not None:
        resolved_output = output.resolve()
        for option_name, option_path in (
            ("--write-baseline", write_baseline),
            ("--baseline", baseline),
        ):
            if option_path is not None and option_path.resolve() == resolved_output:
                raise click.ClickException(f"--output must differ from {option_name}")

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
