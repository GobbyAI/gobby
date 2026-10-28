"""The Release Manager builds binaries; seats never own daemon activation."""

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from gobby.cli import cli
from gobby.workflows.pipeline.renderer import StepRenderer

pytestmark = pytest.mark.unit

SHARED = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
PROCEDURES = SHARED / "skills/gobby/references/agents/procedures"


def _guidance() -> list[Path]:
    return sorted(
        [
            *(SHARED / "workflows/agents").glob("*.yaml"),
            *(SHARED / "workflows/pipelines").glob("*.yaml"),
            *PROCEDURES.glob("*.md"),
        ]
    )


@pytest.mark.parametrize("surface", ["definition", "procedure"])
def test_release_manager_only_rebuilds_and_promotes_binaries(surface: str) -> None:
    if surface == "definition":
        path = SHARED / "workflows/agents/release-manager.yaml"
        definition = yaml.safe_load(path.read_text())
        text = definition["prompts"]["agent"]
    else:
        path = PROCEDURES / "release-manager.md"
        assert path.is_file(), "Release Manager needs its rebuild-only procedure"
        text = path.read_text()

    lowered = text.lower()
    assert "rust" in lowered and "binar" in lowered, path
    assert "patch" in lowered and "Cargo.lock" in text, path
    assert "promote_workspace_binary_set" in text, path
    for duty in ("release batch", "activation", "cutover", "restart", "landing ledger"):
        assert duty not in lowered, f"{path}: obsolete duty {duty!r}"


def test_orchestrator_owns_activation_and_the_landing_ledger() -> None:
    text = (PROCEDURES / "orchestrator.md").read_text().lower()
    assert re.search(
        r"(?:you|(?:the )?orchestrator) (?:own|owns|handle|handles) every "
        r"(?:daemon )?restart[^.\n]*activation",
        text,
    ), "Orchestrator must own every restart and activation"
    assert "any time" in text, "Restart ownership must be independent of a release window"
    assert "04:45" in text and "06:45" in text and "quiet" in text
    assert "landing ledger" in text and "release batch" in text
    assert "release manager's batches" not in text


def test_seat_guidance_never_executes_daemon_commands_or_reads_bootstrap() -> None:
    violations: list[str] = []
    for path in _guidance():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for instruction in re.split(r"(?<=[.!?])\s+", line):
                # A prohibition in a later sentence cannot hide an executable command.
                if re.search(r"\b(never|do not|don't)\b", instruction, re.IGNORECASE):
                    continue
                command = any(
                    word in instruction for word in ("gobby restart", "gobby release batch")
                )
                bootstrap_read = "bootstrap.yaml" in instruction and any(
                    verb in instruction.lower() for verb in ("read", "load", "cat ", "open")
                )
                if command or bootstrap_read:
                    violations.append(f"{path.relative_to(SHARED)}:{number}: {instruction.strip()}")
    assert not violations, "\n".join(violations)


def test_unused_seat_batch_cli_is_removed() -> None:
    result = CliRunner().invoke(cli, ["release", "batch", "--help"])
    assert result.exit_code == 2, result.output
    assert "No such command" in result.output


def test_seat_guidance_arguments_and_environment_do_not_carry_database_url() -> None:
    exposed = [
        str(path.relative_to(SHARED))
        for path in _guidance()
        if "database_url" in path.read_text().lower()
    ]
    assert not exposed, f"Database settings exposed to seats: {exposed}"


def test_pipeline_rendering_excludes_database_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://seat-must-not-see-this:secret@example.invalid/daemon"
    monkeypatch.setenv("DATABASE_URL", secret)
    renderer = StepRenderer(allowed_env_keys=frozenset({"DATABASE_URL"}))

    context = renderer.build_render_context({})

    assert "DATABASE_URL" not in context["env"]
    assert secret not in renderer.render_string("{{ env }}", context)
