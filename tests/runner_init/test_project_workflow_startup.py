"""Startup activation and CLI provenance use isolated project files and hub rows."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import yaml
from click.testing import CliRunner

from gobby.cli.rules import rules
from gobby.runner import GobbyRunner
from gobby.runner_init.storage import run_startup_content_sync
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory, init_git_checkout

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir", lambda: tmp_path / "global"
    )
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: False)
    monkeypatch.setattr(
        "gobby.sync_registry.sync_bundled_content_to_db", lambda _db: {"total_synced": 0}
    )


def _write_rule(path: Path, description: str, *, name: str = "policy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "tags": ["seat"],
                "rules": {
                    name: {
                        "description": description,
                        "event": "turn_start",
                        "effects": [{"type": "inject_context", "template": description}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _startup(db: HubDatabase) -> None:
    run_startup_content_sync(cast(GobbyRunner, SimpleNamespace(database=db)))


@pytest.mark.parametrize("dev_mode", [False, True])
def test_startup_updates_registered_project_rule_in_both_install_modes(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dev_mode: bool,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "startup-policy")
    path = Path(checkout.root_path) / ".gobby/workflows/rules/policy.yaml"
    _write_rule(path, "before landing")
    _write_rule(tmp_path / "global/rules/policy.yaml", "global")
    monkeypatch.setattr("gobby.utils.dev.is_dev_mode", lambda _path: dev_mode)
    # Startup must discover registered roots independently of its working directory.
    monkeypatch.chdir(tmp_path)
    _startup(temp_db)
    manager = RuleDefinitionManager(temp_db)
    before = manager.get_by_name("policy", project_id=checkout.project.id)
    assert before is not None
    assert before.description == "before landing"

    _write_rule(path, "landed revision")
    _startup(temp_db)

    updated = manager.get(before.id)
    assert updated.description == "landed revision"
    assert updated.project_id == checkout.project.id
    assert updated.source == "installed"
    assert set(updated.tags or []) == {"user", "seat", "project-workflow-import"}
    global_row = manager.get_by_name("policy", project_id=None)
    assert global_row is not None
    assert global_row.description == "global"
    assert global_row.project_id is None


@pytest.mark.parametrize("symlink", [False, True])
def test_cli_and_startup_share_canonical_tags_but_external_import_survives_cleanup(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink: bool,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "cli-policy")
    root = Path(checkout.root_path)
    path = root / ".gobby/workflows/rules/policy.yaml"
    external = tmp_path / "external.yaml"
    _write_rule(path, "manual revision")
    if symlink:
        target = tmp_path / "canonical-target.yaml"
        path.rename(target)
        path.symlink_to(target)
    _write_rule(external, "external", name="external")
    monkeypatch.chdir(root)
    monkeypatch.setattr("gobby.cli.rules.require_cli_database", lambda: temp_db)
    cli = CliRunner()
    result = cli.invoke(rules, ["import", str(path)])
    assert result.exit_code == 0, result.output
    manager = RuleDefinitionManager(temp_db)
    manual = manager.get_by_name("policy", project_id=checkout.project.id)
    assert manual is not None
    assert set(manual.tags or []) == {"user", "seat", "project-workflow-import"}

    _write_rule(path, "startup revision")
    _startup(temp_db)
    automatic = manager.get(manual.id)
    assert automatic.description == "startup revision"
    assert automatic.tags == manual.tags
    _write_rule(path, "manual update")
    result = cli.invoke(rules, ["import", str(path)])
    assert result.exit_code == 0, result.output
    assert manager.get(manual.id).tags == automatic.tags
    assert manager.get(manual.id).description == "manual update"

    result = cli.invoke(rules, ["import", str(external)])
    assert result.exit_code == 0, result.output
    external_row = manager.get_by_name("external", project_id=checkout.project.id)
    assert external_row is not None
    assert "project-workflow-import" not in (external_row.tags or [])
    path.unlink()
    _startup(temp_db)
    assert manager.get_by_name("policy", project_id=checkout.project.id) is None
    assert manager.get(external_row.id).description == "external"


@pytest.mark.parametrize("register_linked", [False, True])
def test_startup_imports_main_checkout_even_from_linked_cwd_or_registration(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    register_linked: bool,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "main-policy")
    main = Path(checkout.root_path)
    relative = Path(".gobby/workflows/rules/policy.yaml")
    _write_rule(main / relative, "main revision")
    init_git_checkout(main)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked)],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )
    _write_rule(linked / relative, "unlanded branch revision")
    _write_rule(linked / relative.with_name("branch-only.yaml"), "branch", name="branch-only")
    if register_linked:
        LocalProjectCheckoutManager(temp_db).rebind(
            checkout.machine_id, checkout.project.id, str(linked.resolve())
        )
    monkeypatch.chdir(linked)

    _startup(temp_db)

    manager = RuleDefinitionManager(temp_db)
    row = manager.get_by_name("policy", project_id=checkout.project.id)
    assert row is not None
    assert row.description == "main revision"
    assert manager.get_by_name("branch-only", project_id=checkout.project.id) is None

    # Explicit branch imports remain ad hoc; main files retain canonical
    # provenance even when the command runs from the linked checkout.
    monkeypatch.setattr("gobby.cli.rules.require_cli_database", lambda: temp_db)
    cli = CliRunner()
    branch_file = linked / relative.with_name("branch-only.yaml")
    result = cli.invoke(rules, ["import", str(branch_file)])
    assert result.exit_code == 0, result.output
    branch_row = manager.get_by_name("branch-only", project_id=checkout.project.id)
    assert branch_row is not None
    assert "project-workflow-import" not in (branch_row.tags or [])
    result = cli.invoke(rules, ["import", str(main / relative)])
    assert result.exit_code == 0, result.output
    assert "project-workflow-import" in (manager.get(row.id).tags or [])


@pytest.mark.parametrize("invalid", ["rules: [", "rules:\n  broken:\n    event: invalid\n"])
def test_malformed_rule_is_logged_and_skipped_without_startup_failure_or_retirement(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    caplog: pytest.LogCaptureFixture,
    invalid: str,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "malformed-policy")
    root = Path(checkout.root_path) / ".gobby/workflows/rules"
    path = root / "broken.yaml"
    _write_rule(path, "last good revision")
    _startup(temp_db)
    path.write_text(invalid, encoding="utf-8")
    _write_rule(root / "sibling.yaml", "valid sibling", name="sibling")

    _startup(temp_db)

    manager = RuleDefinitionManager(temp_db)
    preserved = manager.get_by_name("policy", project_id=checkout.project.id)
    assert preserved is not None
    assert preserved.description == "last good revision"
    sibling = manager.get_by_name("sibling", project_id=checkout.project.id)
    assert sibling is not None
    assert sibling.description == "valid sibling"
    assert f"Failed to sync imported workflow {path}" in caplog.text


def test_cli_reports_malformed_canonical_file(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "malformed-cli")
    root = Path(checkout.root_path)
    path = root / ".gobby/workflows/rules/broken.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("rules: [", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr("gobby.cli.rules.require_cli_database", lambda: temp_db)

    result = CliRunner().invoke(rules, ["import", str(path)])

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "broken.yaml" in result.output


def test_cli_uppercase_extension_is_not_owned_by_lowercase_startup_scan(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "uppercase-cli")
    root = Path(checkout.root_path)
    path = root / ".gobby/workflows/rules/policy.YAML"
    _write_rule(path, "explicit import")
    monkeypatch.chdir(root)
    monkeypatch.setattr("gobby.cli.rules.require_cli_database", lambda: temp_db)

    result = CliRunner().invoke(rules, ["import", str(path)])

    assert result.exit_code == 0, result.output
    manager = RuleDefinitionManager(temp_db)
    row = manager.get_by_name("policy", project_id=checkout.project.id)
    assert row is not None
    assert "project-workflow-import" not in (row.tags or [])
    _startup(temp_db)
    assert manager.get(row.id).description == "explicit import"


@pytest.mark.parametrize("kind", ["rule", "pipeline"])
def test_cli_rejects_named_definitions_before_mutating_canonical_rows(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    checkout = isolated_checkout_factory(temp_db, "named-cli")
    root = Path(checkout.root_path)
    path = root / ".gobby/workflows/rules/named.yaml"
    path.parent.mkdir(parents=True)
    data: dict[str, object] = {"type": kind, "name": "named"}
    if kind == "rule":
        data.update(event="turn_start", effects=[{"type": "inject_context", "template": "x"}])
    else:
        data["steps"] = [{"id": "check", "exec": "true"}]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr("gobby.cli.rules.require_cli_database", lambda: temp_db)

    result = CliRunner().invoke(rules, ["import", str(path)])

    assert result.exit_code == 1
    assert "Rule file must contain a rules mapping" in result.output
    assert (
        RuleDefinitionManager(temp_db).get_by_name("named", project_id=checkout.project.id) is None
    )
    assert (
        PipelineDefinitionManager(temp_db).get_by_name("named", project_id=checkout.project.id)
        is None
    )
