"""Release CLI retirement and pipeline credential isolation."""

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.workflows.pipeline.renderer import StepRenderer

pytestmark = pytest.mark.unit


def test_unused_seat_batch_cli_is_removed() -> None:
    result = CliRunner().invoke(cli, ["release", "batch", "--help"])
    assert result.exit_code == 2, result.output
    assert "No such command" in result.output


def test_pipeline_rendering_excludes_database_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://seat-must-not-see-this:secret@example.invalid/daemon"
    monkeypatch.setenv("DATABASE_URL", secret)
    renderer = StepRenderer(allowed_env_keys=frozenset({"DATABASE_URL"}))

    context = renderer.build_render_context({})

    assert "DATABASE_URL" not in context["env"]
    assert secret not in renderer.render_string("{{ env }}", context)
