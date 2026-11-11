"""Direct database access through CLI tools is held to the write_database_directly grant.

Bundled rules are enumerated generically by the capability their gate names (AGENTS.md
rule 16) and matched through the engine's own ``command_patterns_match`` with each
effect's flags, so the corpus pins what a seat's Bash call actually meets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import gobby
from gobby.workflows.engine.command_matching import command_patterns_match

pytestmark = pytest.mark.unit

RULES_ROOT = Path(gobby.__file__).parent / "install" / "shared" / "workflows" / "rules"
CAPABILITY = "write_database_directly"


def _gated_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for path in sorted(RULES_ROOT.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        rules.extend(
            rule
            for rule in (data.get("rules") or {}).values()
            if CAPABILITY in str(rule.get("when") or "")
        )
    return rules


def _gated_bash_effects() -> list[dict[str, Any]]:
    return [
        effect
        for rule in _gated_rules()
        for effect in rule.get("effects") or []
        if effect.get("type") == "block" and "Bash" in (effect.get("tools") or [])
    ]


def _blocked(command: str) -> bool:
    return any(
        command_patterns_match(
            command,
            pattern=effect.get("command_pattern"),
            not_pattern=effect.get("command_not_pattern"),
            mask_quoted=bool(effect.get("mask_quoted", False)),
            resolve_uv_run=bool(effect.get("resolve_uv_run", True)),
        )
        for effect in _gated_bash_effects()
    )


def test_gate_exempts_only_the_grant_and_the_operator_terminal() -> None:
    rules = _gated_rules()
    assert rules, "no bundled rule gates on the direct database grant"
    for rule in rules:
        gate = str(rule["when"])
        assert gate.lstrip().startswith("not (")
        assert "operator_session(" in gate
        assert "session_has_capability(" in gate
        assert rule.get("mandatory") is True
    assert _gated_bash_effects(), "the gated rule carries no Bash block effect"


@pytest.mark.parametrize(
    "command",
    [
        "psql \"$DATABASE_URL\" -c 'UPDATE agent_runs SET task_id = NULL'",
        "uv run psql -c 'select 1'",
        "PGPASSWORD=x psql -h 127.0.0.1 -d gobby",
        "env PGHOST=x /opt/homebrew/bin/psql -c 'DELETE FROM tasks'",
        "cd /tmp && psql gobby < fix.sql",
        "pg_restore -d gobby dump.bin",
        "dropdb gobby",
        "sudo -u postgres psql",
        "bash -c 'psql -c \"drop table x\"'",
        "python -c 'import psycopg; psycopg.connect(u).execute(\"delete from t\")'",
        "uv run python -c 'import asyncpg'",
        "python3 - <<'PY'\nimport psycopg\nprint(1)\nPY",
        "uv run python <<'PY'\nfrom psycopg import connect\nPY",
        "python3 -c 'import os, psycopg'",
        "python3 -c 'import os; import psycopg'",
        "python3 - <<'PY'\nimport os, psycopg\nPY",
        "python3 - <<'PY'\nimport os; import psycopg\nPY",
        "python3 -c \"__import__('psycopg').connect(u)\"",
        "python3 - <<'PY'\nimport importlib\nimportlib.import_module('psycopg')\nPY",
        "python3 - <<'PY'\nif True: import psycopg\nPY",
        "python3 -c 'if True: import psycopg'",
        "python3 -c \"\nimport os\nif 1: import psycopg\n\"",
        "uv run python - <<'PY'\nx = __import__('asyncpg')\nPY",
        "python3 -Ic 'import psycopg'",
        "python3 -c 'import psycopg as pg'",
        "python3 -c 'from psycopg.rows import dict_row'",
        "python3 -c 'import os.path, psycopg2.extras'",
        "python3 -c \"import os; os.system('psql gobby')\"",
        "python3 -c \"import subprocess; subprocess.run(['psql', '-c', 'delete from t'])\"",
        "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['/opt/homebrew/bin/psql', 'gobby'])\nPY",
        "python3 -c 'import psycopg_pool'",
        "python3 -c 'from psycopg_pool import ConnectionPool'",
        "docker run --rm -it postgres psql -h host.docker.internal gobby",
        "podman run postgres psql",
        "python3 - <<'PY'\nimport os, \\\n    psycopg\nPY",
        "python3 -c \"__import__(b'psycopg')\"",
        "python3 -c \"import importlib; importlib.import_module(b'psycopg')\"",
        "python3 - <<'PY'\n__import__('''psycopg''')\nPY",
        "python3 - <<'PY'\nimport importlib\nimportlib.import_module(\n    'psycopg'\n)\nPY",
        "python3 -c \"__import__(f'psycopg')\"",
        "python3 -c \"__import__ ('psycopg')\"",
        "python3 -c \"import importlib; importlib.import_module(f'psycopg')\"",
        "python3 - <<'PY'\nimport importlib\nimportlib.import_module (\n 'psycopg'\n)\nPY",
        "python3 -c \"__import__(rf'psycopg')\"",
        "docker container exec db psql",
        "podman container exec db psql",
        "docker compose -f compose.yml exec db psql",
        "docker compose --project-directory /tmp exec db psql",
        "docker -H unix:///var/run/docker.sock exec db psql",
        "docker run --rm --entrypoint=psql postgres",
        "docker exec db sh -c 'psql gobby'",
        "docker-compose exec db /usr/bin/psql -U gobby",
        "python3 -c \"__import__(name='psycopg')\"",
        "python3 -c \"import importlib; importlib.import_module(name='asyncpg')\"",
        "python3 -c \"__import__(('psycopg'))\"",
        "python3 - <<'PY'\n__import__(\\\n'psycopg')\nPY",
        "python3 -c \"__import__('x' if 0 else 'psycopg')\"",
        "pgbench -i gobby",
        "caffeinate -i psql gobby",
        "arch -arm64 psql gobby",
        "script -q /dev/null psql gobby",
        "docker exec -it -u postgres pg psql -U gobby",
        "docker compose exec db psql",
        "docker compose -f - exec db psql",
        "docker compose --file - exec db psql",
        "docker compose -f x.yml --env-file - exec db psql",
        'docker compose -f "my file.yml" exec db psql',
        'docker --config "/tmp/my config.json" exec db psql',
        "docker -Htcp://127.0.0.1:2375 exec db psql",
        "docker compose -f./x.yml exec db psql",
        "docker-compose -f./x.yml exec db psql",
        "docker --context prod exec db psql",
        "docker --log-level debug exec db psql",
    ],
)
def test_database_clients_and_inline_drivers_are_refused(command: str) -> None:
    assert _blocked(command)


@pytest.mark.parametrize(
    "command",
    [
        "gcode grep -F psycopg -m 20",
        "rg psql docs/",
        "grep -rn 'import psycopg' src",
        "git commit -m 'fix: psql and psycopg; psql -c drop'",
        "cat > notes.py <<'PY'\nimport psycopg\nPY",
        "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/storage -q",
        "uv run python -c 'import json'",
        "uv run python -m pytest tests/storage -k psycopg",
        "uv run pytest tests/storage -k 'psycopg or asyncpg'",
        "uv run python script.py --driver psycopg",
        "docker exec pg ls",
        "script -q /dev/null ls",
        "gcode grep -F \"__import__('psycopg')\" -m 5",
        "gcode grep -F 'import psycopg' -m 5",
        "python3 - <<'PY'\nprint('hello')\nPY",
        "uv run python -m pytest -c pytest.ini -k psycopg",
        "python3 -c 'print(1)'",
        "uv run python -c \"from pathlib import Path; "
        "print(Path('src/gobby/storage/psycopg.py').exists())\"",
        "python3 -c \"import json; print('psycopg')\"",
        "python3 - <<'PY'\nimport json\nprint('asyncpg', json)\nPY",
        "git commit -m 'docs: psql -c drop and os.system(\"psql\")'",
        "docker run --rm alpine ls",
        "python3 -c \"print('psycopg_pool is a package')\"",
        "git commit -m 'docker exec db psql'",
        "docker ps",
        "docker exec db ls /var/lib",
        "docker compose logs db",
        'docker ps | rg "run psql"',
        "docker inspect run psql",
        "docker compose logs run psql",
        "python3 -c \"import os; print(__import__('json').dumps(['psycopg']))\"",
        "echo psql",
        "which psql",
        "ls ~/.psqlrc",
    ],
)
def test_searches_messages_and_unrelated_python_stay_open(command: str) -> None:
    assert not _blocked(command)
