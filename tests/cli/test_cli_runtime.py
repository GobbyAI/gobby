"""CLI process-lifetime database ownership tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.runtime import CliRuntime, require_cli_database, resolve_cli_project
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def test_subcommand_help_does_not_open_runtime_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_database = MagicMock()
    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)

    result = CliRunner().invoke(cli, ["skills", "--help"])

    assert result.exit_code == 0
    open_database.assert_not_called()


@contextmanager
def _database_context(database: MagicMock) -> Iterator[MagicMock]:
    try:
        yield database
    finally:
        database.close()


def test_resolve_cli_project_uses_explicit_reference() -> None:
    project_manager = MagicMock()
    project = MagicMock(id="project-id", deleted_at=None)
    project_manager.resolve_ref.return_value = project

    assert resolve_cli_project(project_manager, "project-ref") == "project-id"
    project_manager.resolve_ref.assert_called_once_with("project-ref")


def test_resolve_cli_project_rejects_missing_or_deleted_reference() -> None:
    project_manager = MagicMock()
    project_manager.resolve_ref.return_value = None

    with pytest.raises(click.ClickException, match="Project not found: missing"):
        resolve_cli_project(project_manager, "missing")

    project_manager.resolve_ref.return_value = MagicMock(deleted_at=object())
    with pytest.raises(click.ClickException, match="Project not found: deleted"):
        resolve_cli_project(project_manager, "deleted")


def test_resolve_cli_project_uses_context_for_empty_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_manager = MagicMock()

    def project_context(*, cwd: Path) -> dict[str, str]:
        assert cwd == Path.cwd()
        return {"id": "context-project"}

    monkeypatch.setattr("gobby.cli.runtime.get_project_context", project_context)

    assert resolve_cli_project(project_manager, "") == "context-project"
    project_manager.resolve_ref.assert_not_called()


def test_resolve_cli_project_allows_unscoped_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_manager = MagicMock()
    context_lookup = MagicMock()
    monkeypatch.setattr("gobby.cli.runtime.get_project_context", context_lookup)

    assert resolve_cli_project(project_manager, require_project=False) == ""
    context_lookup.assert_not_called()


def test_runtime_memoizes_database_and_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    database = MagicMock()

    def open_database(*args: object, **kwargs: object) -> object:
        return _database_context(database)

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    runtime = CliRuntime(config_file="custom.yaml")

    assert runtime.require_database() is database
    assert runtime.require_database() is database

    runtime.close()
    runtime.close()

    database.close.assert_called_once_with()


def test_operational_config_overlays_bootstrap_owned_fields(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()
    bootstrap_path.write_text(
        "daemon_port: 61999\n"
        "websocket_port: 62000\n"
        "bind_host: 127.0.0.2\n"
        f"files_home: {files_home}\n",
        encoding="utf-8",
    )
    bootstrap_path.chmod(0o600)
    runtime = CliRuntime(
        config_file=str(bootstrap_path),
        config=DaemonConfig(daemon_port=60887, bind_host="127.0.0.1"),
    )

    config = runtime.operational_config

    assert config.daemon_port == 61999
    assert config.websocket.port == 62000
    assert config.bind_host == "127.0.0.2"


@pytest.mark.parametrize("mode", ["success", "failure", "abort", "early_exit"])
def test_click_teardown_closes_database_for_all_exit_paths(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    database = MagicMock()

    def open_database(*args: object, **kwargs: object) -> object:
        return _database_context(database)

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)

    @click.command()
    @click.pass_context
    def command(ctx: click.Context) -> None:
        runtime = CliRuntime(config_file=None)
        ctx.obj = runtime
        ctx.call_on_close(runtime.close)
        assert require_cli_database(ctx) is database
        if mode == "failure":
            raise click.ClickException("failed")
        if mode == "abort":
            raise click.Abort
        if mode == "early_exit":
            ctx.exit(0)

    CliRunner().invoke(command)

    database.close.assert_called_once_with()


def test_failed_lazy_acquisition_can_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    database = MagicMock()
    attempts = 0

    @contextmanager
    def open_database(*args: object, **kwargs: object) -> Iterator[MagicMock]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database unavailable")
        yield database

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    runtime = CliRuntime(config_file=None)

    with pytest.raises(RuntimeError, match="database unavailable"):
        runtime.require_database()

    assert runtime.require_database() is database
    assert attempts == 2
    runtime.close()


def test_tasks_list_reuses_one_runtime_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.cli.tasks import crud
    from gobby.cli.tasks import main as tasks_main
    from gobby.cli.tasks._utils import config as task_config

    database = MagicMock()
    manager = MagicMock(db=database)
    task = object()
    manager.list_tasks.return_value = [task]
    seen: list[object] = []
    migration_policies: list[bool] = []

    @contextmanager
    def open_database(
        *args: object,
        apply_migrations: bool = True,
        **kwargs: object,
    ) -> Iterator[MagicMock]:
        migration_policies.append(apply_migrations)
        try:
            yield database
        finally:
            database.close()

    def task_manager(database_arg: object) -> MagicMock:
        seen.append(database_arg)
        return manager

    def claimed_owners(database_arg: object) -> dict[str, str]:
        seen.append(database_arg)
        return {}

    def render_tasks(tasks: list[object], **kwargs: object) -> str:
        seen.append(kwargs["db"])
        return "rendered"

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    monkeypatch.setattr(tasks_main, "check_tasks_enabled", lambda: None)
    monkeypatch.setattr(task_config, "LocalTaskManager", task_manager)
    monkeypatch.setattr(crud, "resolve_project_ref", lambda ref: None)
    monkeypatch.setattr(crud, "filter_tasks_by_stage", lambda manager, tasks, **kwargs: tasks)
    monkeypatch.setattr(crud, "sort_tasks_for_tree", lambda tasks: tasks)
    monkeypatch.setattr(crud, "compute_tree_prefixes", lambda tasks, primary_ids: {})
    monkeypatch.setattr(crud, "get_claimed_task_owners", claimed_owners)
    monkeypatch.setattr(crud, "format_task_list", render_tasks)

    result = CliRunner().invoke(cli, ["tasks", "list", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert seen == [database, database, database]
    assert migration_policies == [False]
    database.close.assert_called_once_with()
