"""Pipeline conditions see a host-local day and file checks confined to the project checkout."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from gobby.workflows.definitions import PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit

DIGEST_CONDITION = "${{ project_file_exists('.gobby/reports/gobby-digest-' + today + '.md') }}"


def _renderer() -> StepRenderer:
    return StepRenderer(TemplateEngine())


def _context(project_path: str | None) -> dict[str, Any]:
    return {"inputs": {}, "steps": {}, "project_path": project_path}


def _checkout(tmp_path: Path) -> Path:
    project_root = tmp_path / "checkout"
    (project_root / ".gobby" / "reports").mkdir(parents=True)
    return project_root


def _digest_path(project_root: Path, day: str) -> Path:
    return project_root / ".gobby" / "reports" / f"gobby-digest-{day}.md"


def _should_run(project_path: str | None) -> bool:
    step = PipelineStep(id="deliver", exec="true", condition=DIGEST_CONDITION)
    return _renderer().should_run_step(step, _context(project_path))


def test_render_context_exposes_host_local_today() -> None:
    context = _renderer().build_render_context({"inputs": {}, "steps": {}})

    assert context["today"] == date.today().isoformat()


def test_report_path_template_renders_todays_date(tmp_path: Path) -> None:
    renderer = _renderer()
    context = renderer.build_render_context(
        {"inputs": {}, "steps": {}, "project_path": str(tmp_path)}
    )

    rendered = renderer.render_mcp_arguments(
        {"file_path": "${{ project_path }}/.gobby/reports/gobby-digest-${{ today }}.md"},
        context,
        drop_none=True,
    )

    assert rendered["file_path"] == (
        f"{tmp_path}/.gobby/reports/gobby-digest-{date.today().isoformat()}.md"
    )


def test_condition_runs_when_todays_digest_exists(tmp_path: Path) -> None:
    project_root = _checkout(tmp_path)
    _digest_path(project_root, date.today().isoformat()).write_text("# Digest\n")

    assert _should_run(str(project_root)) is True


def test_condition_skips_when_todays_digest_is_missing(tmp_path: Path) -> None:
    project_root = _checkout(tmp_path)

    assert _should_run(str(project_root)) is False


def test_condition_ignores_other_days_digest(tmp_path: Path) -> None:
    project_root = _checkout(tmp_path)
    _digest_path(project_root, "1999-01-01").write_text("# Old\n")

    assert _should_run(str(project_root)) is False


def test_condition_skips_without_project_checkout(tmp_path: Path) -> None:
    project_root = _checkout(tmp_path)
    _digest_path(project_root, date.today().isoformat()).write_text("# Digest\n")

    assert _should_run(None) is False


def test_condition_refuses_symlinked_digest_escaping_checkout(tmp_path: Path) -> None:
    project_root = _checkout(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("not in the checkout\n")
    _digest_path(project_root, date.today().isoformat()).symlink_to(outside)

    assert _should_run(str(project_root)) is False


@pytest.mark.parametrize(
    "relative_path",
    ["../outside.md", "../../outside.md", "/etc/hosts", ""],
)
def test_file_check_never_resolves_outside_checkout(tmp_path: Path, relative_path: str) -> None:
    project_root = _checkout(tmp_path)
    (tmp_path / "outside.md").write_text("outside\n")
    step = PipelineStep(
        id="probe",
        exec="true",
        condition=f"${{{{ project_file_exists('{relative_path}') }}}}",
    )

    assert _renderer().should_run_step(step, _context(str(project_root))) is False


def test_operator_messaging_defaults_off_and_parses_from_definition() -> None:
    step = {"id": "deliver", "exec": "true"}
    plain = PipelineDefinition.model_validate({"name": "plain", "steps": [step]})
    granted = PipelineDefinition.model_validate(
        {"name": "digest", "steps": [step], "operator_messaging": True}
    )

    assert plain.operator_messaging is False
    assert granted.operator_messaging is True
