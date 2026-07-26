"""CLI process-lifetime database ownership tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.runtime import CliRuntime, require_cli_database
from gobby.config.app import DaemonConfig


def _database_context(database: MagicMock) -> Iterator[MagicMock]:
    try:
        yield database
    finally:
        database.close()


def test_runtime_memoizes_database_and_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    database = MagicMock()

    def open_database(*args: object, **kwargs: object) -> object:
        return contextmanager(_database_context)(database)

    monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
    runtime = CliRuntime(config_file="custom.yaml")

    assert runtime.require_database() is database
    assert runtime.require_database() is database

    runtime.close()
    runtime.close()

    database.close.assert_called_once_with()


@pytest.mark.parametrize("mode", ["success", "failure", "abort", "early_exit"])
def test_click_teardown_closes_database_for_all_exit_paths(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    database = MagicMock()

    def open_database(*args: object, **kwargs: object) -> object:
        return contextmanager(_database_context)(database)

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


def test_config_and_tasks_list_share_runtime_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.cli.tasks import crud
    from gobby.cli.tasks import main as tasks_main
    from gobby.cli.tasks._utils import config as task_config

    database = MagicMock()
    manager = MagicMock(db=database)
    task = object()
    manager.list_tasks.return_value = [task]
    seen: list[object] = []

    @contextmanager
    def open_database(*args: object, **kwargs: object) -> Iterator[MagicMock]:
        try:
            yield database
        finally:
            database.close()

    def load_config(
        config_file: str | None = None,
        *,
        database: object | None = None,
    ) -> DaemonConfig:
        seen.append(database)
        return DaemonConfig()

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
    monkeypatch.setattr("gobby.cli.load_full_config_from_db", load_config)
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
    assert seen == [database, database, database, database]
    database.close.assert_called_once_with()
