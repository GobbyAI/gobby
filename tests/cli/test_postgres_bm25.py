from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from gobby.cli import postgres


def _status(healthy: bool) -> dict[str, Any]:
    state = "healthy" if healthy else "damaged"
    return {
        "healthy": healthy,
        "repair_command": "gobby postgres repair-code-index",
        "indexes": [
            {
                "name": "public.code_symbols_search_bm25",
                "state": state,
                "repaired": healthy,
                "checks": [],
                "error": None if healthy else "invalid chunk style tag: 254",
            }
        ],
    }


def test_repair_code_index_json_success(monkeypatch: Any) -> None:
    monkeypatch.setattr(postgres, "_read_bootstrap_database_url", lambda _home: "postgres://db")
    monkeypatch.setattr(
        postgres,
        "get_cli_runtime",
        lambda: SimpleNamespace(
            require_config=lambda: SimpleNamespace(
                code_index=SimpleNamespace(maintenance_index_timeout_seconds=41)
            )
        ),
    )
    calls: list[tuple[str, float]] = []

    def repair(dsn: str, *, timeout_seconds: float) -> dict[str, Any]:
        calls.append((dsn, timeout_seconds))
        return _status(True)

    monkeypatch.setattr(postgres, "repair_bm25_indexes", repair)

    result = CliRunner().invoke(postgres.postgres_cli, ["repair-code-index", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["healthy"] is True
    assert calls == [("postgres://db", 41)]


def test_repair_code_index_failure_is_nonzero(monkeypatch: Any) -> None:
    monkeypatch.setattr(postgres, "_read_bootstrap_database_url", lambda _home: "postgres://db")
    monkeypatch.setattr(
        postgres,
        "get_cli_runtime",
        lambda: SimpleNamespace(
            require_config=lambda: SimpleNamespace(
                code_index=SimpleNamespace(maintenance_index_timeout_seconds=41)
            )
        ),
    )
    monkeypatch.setattr(postgres, "repair_bm25_indexes", lambda *_args, **_kwargs: _status(False))

    result = CliRunner().invoke(postgres.postgres_cli, ["repair-code-index"])

    assert result.exit_code == 1
    assert "invalid chunk style tag: 254" in result.output
    assert "gobby postgres repair-code-index" in result.output
